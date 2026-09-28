"""
GhostDirector — Fact-Anchor Validation (Phase 2.6, full A26)

The defamation shield for celebrity content: every specific figure the script
utters (dates, money, percentages, ages, large counts) must be traceable to
the research payload. Anything unanchored gets rewritten to remove or soften
the unverifiable specific — we never ship a hard claim the research doesn't
support.

Pure functions here (no LLM calls); scriptwriter.py owns the LLM rewrite pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ──────────────────────────────────────────────
# Extraction patterns
# ──────────────────────────────────────────────

_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
# "1990s": capture the first 3 digits ("199"), the literal "0" closes the
# decade's first year. Value stored as the decade's base year (1990).
_DECADE_RE = re.compile(r"\b((?:19|20)\d)0s\b")

# "$1.2 billion", "$900,000", "$3.5B", "48 million dollars"
_MONEY_RE = re.compile(
    r"\$\s?(?P<num>[\d,]+(?:\.\d+)?)\s*(?P<mult>billion|b\b|million|m\b|thousand|k\b|trillion)?",
    re.IGNORECASE,
)
_MONEY_WORDS_RE = re.compile(
    r"(?P<words>(?:\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|"
    r"fifty|sixty|seventy|eighty|ninety|hundred|point)\b[\s\-]?){1,7})\s*"
    r"(?P<mult>billion|million|thousand|trillion)?\s*(?:dollars|bucks)\b",
    re.IGNORECASE,
)

_PERCENT_RE = re.compile(r"\b(?P<num>\d+(?:\.\d+)?)\s*(?:%|percent)", re.IGNORECASE)

_AGE_RE = re.compile(
    r"\b(?:(?P<a>\d{1,3})\s*years?\s*old|age\s*of\s*(?P<b>\d{1,3})|aged\s*(?P<c>\d{1,3}))\b",
    re.IGNORECASE,
)

# Large standalone counts: "50,000 pages", "100000 documents" (≥5 digits or comma-grouped)
_FIGURE_RE = re.compile(r"\b(?:\d{1,3}(?:,\d{3})+|\d{5,})\b")

_WORD_VALUES = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_MULTILPIERS = {"thousand": 1e3, "million": 1e6, "billion": 1e9, "trillion": 1e12}
_SUFFIX_MULT = {"b": 1e9, "m": 1e6, "k": 1e3, "trillion": 1e12,
                "billion": 1e9, "million": 1e6, "thousand": 1e3}


def _parse_word_number(raw: str) -> float | None:
    """'two hundred' → 200, 'one point two' → 1.2, 'forty eight' → 48."""
    num = 0.0
    current = 0.0
    saw_any = False
    for tok in raw.lower().replace("-", " ").split():
        if tok == "hundred":
            current = (current or 1) * 100
            saw_any = True
        elif tok == "point":
            # decimal part handled loosely: "one point two" ≈ 1.2 (first decimal only)
            num += current
            current = -1.0  # mark decimal mode
            saw_any = True
        elif tok in _WORD_VALUES:
            v = _WORD_VALUES[tok]
            if current < 0:  # decimal digit
                current = v / 10.0
            elif v >= 10 and current % 10 == 0 and current >= 20:
                num += current + v  # "twenty two" style continuation
                current = 0.0
            elif current and v < 10 and current >= 20:
                current += v
            else:
                current += v
            saw_any = True
    if not saw_any:
        return None
    if current > 0:
        num += current
    return num if num > 0 else (current if current > 0 else None)


@dataclass
class Anchor:
    claim_type: str    # "year" | "decade" | "money" | "percent" | "age" | "figure"
    text: str          # how it appears in the narration
    value: float       # canonical numeric value (decade: base year)
    scene_number: int = 0
    anchored: bool = False


@dataclass
class AnchorReport:
    checked_scenes: int = 0
    anchors_checked: int = 0
    unanchored: list[Anchor] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "checked_scenes": self.checked_scenes,
            "anchors_checked": self.anchors_checked,
            "unanchored": [
                {"scene_number": a.scene_number, "type": a.claim_type, "claim": a.text}
                for a in self.unanchored
            ],
        }


def extract_anchors(narration: str, scene_number: int = 0) -> list[Anchor]:
    """Pull every checkable specific out of one scene's narration."""
    anchors: list[Anchor] = []
    if not narration:
        return anchors

    text = narration.replace(",", "")  # "1,994" typo-guard; money handled below

    for m in _DECADE_RE.finditer(narration):
        base = int(m.group(1)) * 10  # "199" + literal "0" → 1990
        anchors.append(Anchor("decade", m.group(0), float(base), scene_number))
    for m in _YEAR_RE.finditer(narration):
        y = float(m.group(1))
        # Skip plain years already covered by a decade match in the same text
        if any(a.claim_type == "decade" and a.value <= y <= a.value + 9 for a in anchors):
            continue
        anchors.append(Anchor("year", m.group(0), y, scene_number))

    for m in _MONEY_RE.finditer(narration):
        raw_num = m.group("num").replace(",", "")
        try:
            value = float(raw_num)
        except ValueError:
            continue
        mult = (m.group("mult") or "").lower()
        value *= _SUFFIX_MULT.get(mult, 1.0)
        anchors.append(Anchor("money", m.group(0).strip(), value, scene_number))

    # Figures: skip spans already captured as money ("$900,000" is one claim,
    # not a money claim + a standalone figure claim).
    money_spans = [(m.start(), m.end()) for m in _MONEY_RE.finditer(narration)]
    for m in _FIGURE_RE.finditer(narration):
        if any(s <= m.start() < e for s, e in money_spans):
            continue
        raw = m.group(0).replace(",", "")
        anchors.append(Anchor("figure", m.group(0), float(raw), scene_number))

    for m in _MONEY_WORDS_RE.finditer(narration):
        words = m.group("words")
        num = _parse_word_number(words)
        if num is None:
            continue
        mult = (m.group("mult") or "").lower()
        value = num * _MULTILPIERS.get(mult, 1.0)
        anchors.append(Anchor("money", m.group(0).strip(), value, scene_number))

    for m in _PERCENT_RE.finditer(narration):
        anchors.append(Anchor("percent", m.group(0).strip(), float(m.group("num")), scene_number))

    for m in _AGE_RE.finditer(narration):
        raw = m.group("a") or m.group("b") or m.group("c")
        anchors.append(Anchor("age", m.group(0).strip(), float(raw), scene_number))

    return anchors


# ──────────────────────────────────────────────
# Corpus matching
# ──────────────────────────────────────────────

def _corpus_values(corpus: str) -> dict:
    """Pre-parse the research payload into fast lookup sets."""
    money = set()
    money_spans = [(m.start(), m.end()) for m in _MONEY_RE.finditer(corpus)]
    for m in _MONEY_RE.finditer(corpus):
        try:
            value = float(m.group("num").replace(",", "")) * _SUFFIX_MULT.get((m.group("mult") or "").lower(), 1.0)
            money.add(value)
        except ValueError:
            continue
    for m in _MONEY_WORDS_RE.finditer(corpus):
        num = _parse_word_number(m.group("words"))
        if num is not None:
            money.add(num * _MULTILPIERS.get((m.group("mult") or "").lower(), 1.0))

    years = {float(y) for y in _YEAR_RE.findall(corpus)}
    percents = {float(m.group("num")) for m in _PERCENT_RE.finditer(corpus)}
    figures = {
        float(m.group(0).replace(",", ""))
        for m in _FIGURE_RE.finditer(corpus)
        if not any(s <= m.start() < e for s, e in money_spans)
    }
    return {"money": money, "years": years, "percents": percents, "figures": figures}


def _corpus_text(research) -> str:
    """Flatten the ResearchResult payload into one lowercase text blob."""
    parts: list[str] = [research.summary or "", research.narrative_arc or ""]
    parts.extend(research.key_facts or [])
    parts.extend(research.source_queries or [])
    for e in research.timeline or []:
        parts.append(f"{e.get('date', '')} {e.get('event', '')}")
    for p in research.key_people or []:
        parts.append(f"{p.get('name', '')} {p.get('role', '')}")
    return " ".join(parts).lower()


def _is_anchored(anchor: Anchor, corpus: str, vals: dict) -> bool:
    t = anchor.claim_type
    if t == "year":
        return anchor.value in vals["years"]
    if t == "decade":
        return any(anchor.value <= y <= anchor.value + 9 for y in vals["years"])
    if t == "money":
        # Tolerate both "$1.2 billion" and bare "1,200,000,000" in the corpus
        for v in vals["money"] | vals["figures"]:
            if v and abs(v - anchor.value) / max(v, anchor.value) <= 0.02:
                return True
        return False
    if t == "percent":
        return anchor.value in vals["percents"]
    if t == "age":
        return re.search(rf"\b{int(anchor.value)}\b", corpus) is not None
    if t == "figure":
        return anchor.value in vals["figures"] or str(int(anchor.value)) in corpus
    return True  # unknown types pass


def validate_script_anchors(script, research) -> AnchorReport:
    """Check every scene's narration against the research payload. Pure."""
    corpus = _corpus_text(research)
    vals = _corpus_values(corpus)
    report = AnchorReport(checked_scenes=len(script.scenes))
    for scene in script.scenes:
        for anchor in extract_anchors(scene.narration or "", scene.scene_number):
            report.anchors_checked += 1
            if not _is_anchored(anchor, corpus, vals):
                report.unanchored.append(anchor)
    return report
