"""
GhostDirector — YouTube Compliance Checker

Pre-upload gate that checks the video + metadata against YouTube's published
rules so uploads don't get rejected, demoted, or demonetized:

  1. Metadata spam policies      (title/description/tag limits, misleading text)
  2. Inauthentic-content policy  (the July 2025 "AI slop" mass-demonetization
                                  rules: repetitive/mass-produced content,
                                  template sameness, AI disclosure)
  3. Defamation / legal safety   (allegations vs. convictions, anchor report)
  4. Chapters format             (00:00 first, >=3 chapters, >=10s each)
  5. Advertiser-friendly scan    (profanity / shock words in title+description)
  6. Thumbnail checks            (exists, aspect, no clickbait-allcaps)
  7. Shorts validity             (duration <= 60s, 9:16)

Output: compliance_report.json + a pass/warn/fail verdict. `--upload` refuses
to run when the verdict is FAIL unless --force-upload.

This is static analysis, not moderation — it catches the objective rules that
get channels rejected (esp. the inauthentic-content criteria from the July
2025 monetization update) before an upload is wasted.
"""

import sys
import json
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from models import Script
import config
from utils.logger import get_logger

log = get_logger("compliance")

TITLE_MAX = 100
DESCRIPTION_MAX = 5000
TAGS_MAX_COUNT = 30
TAGS_MAX_CHARS = 500
SHORTS_MAX_SECONDS = 60
CHAPTERS_MIN = 3
CHAPTER_MIN_GAP_SECONDS = 10

# Advertiser-friendly: hard-profanity list (words YouTube flags immediately).
# Covers full words, vowel-censored forms (f*ck) and bleeped runs where the
# whole syllable is starred (f***, f***ing, motherf***ing, sh**, s***t).
_VOW = r"(?:u|i|\*|@)"  # one censored/real vowel slot
_AD_UNFRIENDLY = re.compile(
    r"\b(?:"
    r"motherf" + _VOW + r"+ck(?:ers?|in[g']?)?"        # motherfucker, motherf*cking
    r"|motherf[\*@]{2,}(?:in[g']?|ers?)?"              # motherf***, motherf***ing
    r"|f" + _VOW + r"+ck(?:ers?|in[g']?)?"             # fuck, f*ck, f*cking
    r"|f[\*@]{2,}(?:in[g']?|ers?)?"                    # f***, f***ing, f***er
    r"|sh" + _VOW + r"+t(?:ty|head|s)?"                # shit, sh*t, shitty
    r"|sh[\*@]{2,}(?:t(?:ty|head|s)?)?"                # sh**, sh**t
    r"|s[\*@]{2,}t?"                                   # s***, s***t
    r"|b" + _VOW + r"tch(?:es)?"                       # bitch, b*tch
    r"|d" + _VOW + r"ck(?:s|head)?"                    # dick, d*ck
    r"|c" + _VOW + r"cks?"                             # cock
    r"|asshole|bastard|puss(?:y|ies)|whores?|sluts?(?:ty)?"
    r")(?!\w)",  # (?!\w) not \b: starred forms end in non-word chars
    re.IGNORECASE,
)

# Mild profanity: advertiser-friendly grey zone in titles (YouTube's 2023
# guidelines tolerate moderate profanity in content, but titles are riskier).
_MILD_PROFANITY = re.compile(r"\b(damn|hell|crap|arse)\b", re.IGNORECASE)

# Shock/violence words that commonly trigger limited ads when in title/thumbnail text.
_SHOCK_TERMS = re.compile(
    r"\b(dead|death|kills?|killed|murder|suicide|torture|brutal(ly)?|"
    r"caught on (camera|tape)|graphic)\b",
    re.IGNORECASE,
)

# Overclaiming conviction language (defamation shield).
_CONVICTION_TERMS = re.compile(
    r"\b(is|was|is a|was a)?\s*(convicted|guilty|criminal|fraudster|"
    r"rapist|murderer|scammer|pedophile)\b",
    re.IGNORECASE,
)

# Sensational/engagement-bait patterns that read as slop to reviewers.
_BAIT_PATTERNS = [
    (re.compile(r"you won'?t believe", re.IGNORECASE), "clickbait cliché"),
    (re.compile(r"gone (wrong|sexual)", re.IGNORECASE), "clickbait cliché"),
    (re.compile(r"shocking (truth|reason|secret)", re.IGNORECASE), "clickbait cliché"),
    (re.compile(r"what happened (next|after)", re.IGNORECASE), "clickbait cliché"),
    (re.compile(r"\bexposed\b", re.IGNORECASE), "'exposed' framing"),
]


def check_compliance(
    script: Script,
    metadata: dict,
    output_dir: Path,
    short_duration: float | None = None,
) -> dict:
    """Run every check; write compliance_report.json; return the report.

    Verdicts: "pass" (upload freely), "warn" (upload allowed; fix recommended),
    "fail" (fix before uploading — `--upload` will refuse without --force).
    """
    findings: list[dict] = []

    def _add(check: str, severity: str, message: str, fix: str | None = None):
        findings.append({"check": check, "severity": severity, "message": message, "fix": fix})

    title = (metadata.get("title") or "").strip()
    description = (metadata.get("description") or "").strip()
    tags = metadata.get("tags") or []
    chapters = metadata.get("chapters") or []
    checklist = metadata.get("checklist") or {}

    # ── 1. Metadata spam policies ──────────────────────────────────────
    if not title:
        _add("metadata.title", "fail", "Title is empty", "Set a title in metadata.json")
    elif len(title) > TITLE_MAX:
        _add("metadata.title", "fail",
             f"Title is {len(title)} chars (YouTube hard limit {TITLE_MAX})",
             "Trim the title")
    elif len(title) < 5:
        _add("metadata.title", "warn", "Title is under 5 chars", "Make the title descriptive")

    if len(description) > DESCRIPTION_MAX:
        _add("metadata.description", "fail",
             f"Description is {len(description)} chars (limit {DESCRIPTION_MAX})",
             "Trim credits/boilerplate — credits list is the usual culprit")
    if len(description) < 50:
        _add("metadata.description", "warn",
             "Description under 50 chars — weak for search/discovery")

    if len(tags) > TAGS_MAX_COUNT:
        _add("metadata.tags", "fail",
             f"{len(tags)} tags (limit {TAGS_MAX_COUNT})", "Trim to 30 tags")
    if sum(len(t) + 1 for t in tags) > TAGS_MAX_CHARS:
        _add("metadata.tags", "fail", "Tag string exceeds 500 chars", "Remove low-value tags")

    # All-caps title reads as spam (allow small acronym runs)
    words = title.split()
    long_caps = [w for w in words if len(w) > 3 and w.isupper()]
    if len(long_caps) >= 3:
        _add("metadata.title_style", "warn",
             f"Title has {len(long_caps)} all-caps words — spam signal",
             "Capitalize normally; keep caps for acronyms only")

    # Repeated character spam e.g. "!!!!!"
    if re.search(r"(.)\1{4,}", title + " " + description[:500]):
        _add("metadata.spam_chars", "warn", "Repeated-character runs in title/description",
             "Remove '!!!!!' style emphasis")

    # ── 2. Inauthentic content / AI-slop policy ────────────────────────
    # (July 2025 monetization update: mass-produced/repetitive content is
    # ineligible; synthetic media must be disclosed.)
    ai_disclosed = bool(checklist.get("synthetic_media_disclosed"))
    if metadata.get("ai_generated", True) and not ai_disclosed:
        _add("ai.disclosure", "warn",
             "AI-generated content flag: 'synthetic_media_disclosed' is unchecked",
             "Toggle 'Altered content' in YouTube Studio before/after upload — "
             "required by the synthetic-media disclosure policy")

    # Repetition scan: near-duplicate narration across scenes (mass-produced feel)
    narrations = [(s.narration or "").strip().lower() for s in script.scenes]
    narrations = [n for n in narrations if n]
    dup_pairs = 0
    for i in range(len(narrations)):
        for j in range(i + 1, len(narrations)):
            if len(narrations[i]) > 40 and _rough_overlap(narrations[i], narrations[j]) > 0.85:
                dup_pairs += 1
    if narrations and dup_pairs:
        _add("slop.repetition", "warn" if dup_pairs <= 2 else "fail",
             f"{dup_pairs} near-duplicate narration pairs detected",
             "Re-run the scriptwriter or vary scenes — near-identical repeats are the "
             "core 'repetitive content' demonetization criterion")

    # Template sameness across videos (persona rotation already helps; report it)
    try:
        state = json.loads((config.DB_DIR / "channel_state.json").read_text(encoding="utf-8"))
        packaging = state.get("packaging_log", [])
        recent_titles = [p.get("title_used", "") for p in packaging[-10:]]
        title_stems = {_norm_stem(t) for t in recent_titles if t}
        if _norm_stem(title) in title_stems:
            _add("slop.title_sameness", "warn",
                 "A recent upload used an almost identical title",
                 "Rotate the title angle (title_variants exist in metadata.json)")
        voice_pool = state.get("voice_pool", [])
        if voice_pool and len(set(voice_pool)) == 1:
            _add("slop.voice_sameness", "warn",
                 "Only one voice in the persona rotation pool",
                 "Add voices to utils/channel_state.py VOICE_POOL")
    except Exception:
        pass  # channel_state absent on first runs — nothing to compare

    # Watchability: wall-of-text narration with no sentence variation
    if narrations:
        avg_words = sum(len(n.split()) for n in narrations) / len(narrations)
        if avg_words > 60:
            _add("slop.density", "warn",
                 f"Scenes average {avg_words:.0f} narration words — dense walls of text",
                 "Target 20-40 words per scene beat")

    # ── 3. Defamation / legal safety ───────────────────────────────────
    scan_text = f"{title}\n{description}\n" + " ".join(
        (s.narration or "") for s in script.scenes
    )
    conv_hits = [m.group(0) for m in _CONVICTION_TERMS.finditer(scan_text)]
    hedge_present = bool(re.search(
        r"alleged|reported|according to|accused|nothing here is a statement of guilt",
        description, re.IGNORECASE,
    ))
    if conv_hits and not hedge_present:
        _add("legal.defamation", "fail",
             f"Conviction-level language without 'alleged/reported' hedging: "
             f"{sorted(set(h.lower() for h in conv_hits))[:5]}",
             "Use 'alleged'/'reported' phrasing for unproven claims")
    elif conv_hits:
        _add("legal.defamation", "pass",
             "Strong claims present but hedged in description")

    anchor_report = getattr(script, "anchor_report", None)
    if isinstance(anchor_report, dict):
        stripped = anchor_report.get("stripped") or []
        unresolved = anchor_report.get("unresolved") or []
        if unresolved:
            _add("legal.fact_anchors", "warn",
                 f"{len(unresolved)} narration claims never matched the research payload",
                 "Review script.json 'anchor_report' — unanchored figures are "
                 "defamation/hallucination risk")
        else:
            _add("legal.fact_anchors", "pass", "All checkable claims anchored to research")

    # ── 4. Chapters format ─────────────────────────────────────────────
    if chapters:
        first = chapters[0].get("timestamp", "")
        if first != "00:00":
            _add("chapters.format", "fail",
                 f"First chapter timestamp is {first!r} (must be 00:00)",
                 "Regenerate metadata — generator always emits 00:00 first")
        if len(chapters) < CHAPTERS_MIN:
            _add("chapters.count", "warn",
                 f"Only {len(chapters)} chapters (min {CHAPTERS_MIN} for YouTube to show them)")
        prev = 0.0
        for ch in chapters[1:]:
            secs = _ts_to_seconds(ch.get("timestamp", ""))
            if secs is not None and secs - prev < CHAPTER_MIN_GAP_SECONDS:
                _add("chapters.spacing", "warn",
                     f"Chapters closer than {CHAPTER_MIN_GAP_SECONDS}s at {ch.get('timestamp')}")
                break
            prev = secs if secs is not None else prev

    # ── 5. Advertiser-friendly scan ────────────────────────────────────
    for field, label in ((title, "title"), (description[:1000], "description")):
        if _AD_UNFRIENDLY.search(field):
            _add("ads.profanity", "fail",
                 f"Profanity in {label} triggers age-restriction/limited-ads risk",
                 "Remove or bleep-spell profanity")
            break
    if _MILD_PROFANITY.search(title):
        _add("ads.profanity_mild", "warn",
             "Mild profanity ('damn'/'hell'/'crap') in the title — monetization grey zone",
             "Titles carry the risk; the same words in narration are fine")
    title_shock = [m.group(0) for m in _SHOCK_TERMS.finditer(title)]
    if title_shock:
        _add("ads.shock_title", "warn",
             f"Shock terms in title: {sorted(set(w.lower() for w in title_shock))} — "
             f"monetization review risk",
             "Consider neutral phrasing; shock terms in narration are fine")

    bait = next((label for pat, label in _BAIT_PATTERNS if pat.search(title)), None)
    if bait:
        _add("ads.clickbait", "warn",
             f"Clickbait pattern ({bait}) in title — CTR may spike then collapse "
             f"(hurts retention signals)",
             "Use curiosity without overclaiming")

    # ── 6. Thumbnail ───────────────────────────────────────────────────
    thumb_path = output_dir / "thumbnail.png"
    if not thumb_path.exists():
        _add("thumbnail.exists", "fail", "thumbnail.png missing from project",
             "Run the thumbnail generator before upload")
    else:
        try:
            from PIL import Image
            with Image.open(thumb_path) as im:
                w, h = im.size
            if abs(w / h - 16 / 9) > 0.02:
                _add("thumbnail.aspect", "warn",
                     f"Thumbnail is {w}x{h} (not 16:9) — Studio will letterbox it")
            if w < 640:
                _add("thumbnail.resolution", "warn",
                     f"Thumbnail under 640px wide ({w}px) — blurry on TV layouts")
        except Exception as e:
            _add("thumbnail.readable", "warn", f"Thumbnail unreadable: {e}")

    # ── 7. Shorts validity (when a short was rendered) ──────────────────
    shorts_path = output_dir / "final_9x16_short.mp4"
    if shorts_path.exists():
        if short_duration is not None:
            if short_duration > SHORTS_MAX_SECONDS:
                _add("shorts.duration", "fail",
                     f"Short is {short_duration:.1f}s (limit {SHORTS_MAX_SECONDS}s) — "
                     f"uploads as a normal video, not a Short",
                     "Regenerate with a shorter clip selection")
        else:
            try:
                from utils.ffmpeg_cmd import get_duration
                d = get_duration(shorts_path)
                if d and d > SHORTS_MAX_SECONDS + 1:
                    _add("shorts.duration", "fail",
                         f"Short file is {d:.1f}s (limit {SHORTS_MAX_SECONDS}s)")
            except Exception:
                pass

    # ── Verdict + report ───────────────────────────────────────────────
    fails = [f for f in findings if f["severity"] == "fail"]
    warns = [f for f in findings if f["severity"] == "warn"]
    passes = [f for f in findings if f["severity"] == "pass"]
    verdict = "fail" if fails else ("warn" if warns else "pass")

    report = {
        "verdict": verdict,
        "summary": {
            "fail": len(fails), "warn": len(warns), "pass": len(passes),
        },
        "findings": findings,
        "policy_basis": [
            "YouTube metadata spam policies",
            "YouTube inauthentic content policy (July 2025 monetization update)",
            "YouTube synthetic/altered content disclosure requirement",
            "YouTube chapters formatting rules",
            "YouTube advertiser-friendly content guidelines",
        ],
    }

    report_path = output_dir / "compliance_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    log.info(f"[bold]Compliance verdict:[/bold] {verdict.upper()} "
             f"({len(fails)} fail / {len(warns)} warn / {len(passes)} pass)")
    for f in findings:
        icon = {"fail": "✗", "warn": "⚠", "pass": "✓"}[f["severity"]]
        color = {"fail": "red", "warn": "yellow", "pass": "green"}[f["severity"]]
        log.info(f"  [{color}]{icon} {f['check']}[/{color}] — {f['message']}")
        if f.get("fix"):
            log.info(f"      fix: {f['fix']}")
    log.info(f"Report: {report_path.name}")

    return report


def _rough_overlap(a: str, b: str) -> float:
    """Word-level Jaccard similarity — cheap near-duplicate detector."""
    wa, wb = set(a.split()), set(b.split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _norm_stem(title: str) -> str:
    """Normalize a title to a comparable stem (function words dropped)."""
    stop = {"the", "a", "an", "of", "in", "on", "to", "and", "is", "was",
            "why", "how", "what", "untold", "real", "story", "truth", "secret"}
    words = [w for w in re.findall(r"\w+", title.lower()) if w not in stop]
    return " ".join(sorted(words))[:60]


def _ts_to_seconds(ts: str) -> float | None:
    parts = str(ts or "").split(":")
    try:
        secs = 0.0
        for p in parts:
            secs = secs * 60 + int(p)
        return secs
    except (ValueError, TypeError):
        return None


if __name__ == "__main__":
    print("GhostDirector compliance module loaded. Run via main.py.")
