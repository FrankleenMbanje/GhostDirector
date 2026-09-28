"""
GhostDirector — Quality Gate (the "human editor" pass)

Scores a project the way a professional short-form editor would, from the
artifacts actually on disk: the script's words, the assets' pixels, the
edit's pacing. Returns a 0-10 verdict with the concrete problems that cost
points — callers (trending_short / main) re-run the weak stage and re-score
until the gate passes or attempts run out.

Score model (weights tuned to Shorts survival):
  HOOK        30%  — words, numeric anchor, overlay text, visual-density order
  STORY       25%  — open loops, stakes cadence, loop ending, no sag
  HUMANITY    15%  — burstiness, AI tells (vocab + structural), contractions
  VISUALS     20%  — asset resolution, contrast, flat-frame rate, reuse
  EDIT        10%  — visual-change cadence, cut count, SFX coverage
"""

from __future__ import annotations

import json
import re
import statistics
from pathlib import Path

from utils.logger import get_logger

log = get_logger("quality_gate")

# ── The structural + vocabulary AI tells (Carnegie Mellon 2025, Wikipedia
#    "Signs of AI writing", Buffer 52M-post analysis). Beyond the word list
#    the scriptwriter already bans, these are the patterns that make viewers
#    *feel* the slop without knowing why.
AI_VOCAB_TELLS = [
    "delve", "tapestry", "testament to", "pivotal", "crucial",
    "it's important to note", "it's worth noting", "in conclusion",
    "furthermore", "moreover", "in the realm of", "let's dive",
    "navigate the landscape", "ever-evolving", "ever changing",
    "undeniable", "intricate", "myriad", "plethora", "beacon",
    "game changer", "deep dive", "buckle up", "welcome back",
]

AI_STRUCTURAL_TELLS = {
    "not_just_but": re.compile(r"\bnot just\b[^.!?]*\bbut\b", re.I),
    "isnt_x_its_y": re.compile(r"\bit'?s not (?:just )?about\b[^.!?]*\bit'?s about\b", re.I),
    "rule_of_three": re.compile(r"\b\w+,\s+\w+,?\s+and\s+\w+\b"),
    "corporate_pep": re.compile(r"\bunlock|unleash|elevate|empower(ing)?\b", re.I),
}

HOOK_ARCHETYPES = [
    # (name, regex) — the hook must BE one of these, not merely be short.
    ("curiosity_gap", re.compile(r"\b(the reason|why |what (?:they|nobody|no one)|the real|hidden|secret|no (?:one|body) (?:saw|noticed|expected)|in a way no)\b", re.I)),
    ("bold_claim", re.compile(r"\b(this|one) (?:single|one|10-second|hidden|secret)\b|\bbuilt an empire|\bchanged everything\b|\bjust (?:dethroned|broke|shattered|rewrote)\b", re.I)),
    ("shock_stat", re.compile(r"\b\d+(?:\.\d+)?\s?(%|percent|million|billion|trillion)\b", re.I)),
    ("transformation", re.compile(r"\bfrom .{2,40} to\b", re.I)),
    ("controversy", re.compile(r"\b(unpopular truth|nobody (?:talks|wants)|everyone (?:is )?wrong|the lie)\b", re.I)),
    ("in_medias_res", re.compile(r"\b(just|right now|at this moment|today)\b.*(walked|dropped|filed|quit|lost|won|arrested|collapsed|dethroned|confirmed|received)\b", re.I)),
]

# Words that signal stakes. A beat without stakes is exposition; exposition
# is where swipe-away happens.
STAKES_WORDS = re.compile(
    r"\b(lost|worth|risk|prison|jail|lawsuit|collapse|crumble|empire|fortune|"
    r"million|billion|trillion|deadline|bet|owes|debt|fraud|verdict|"
    r"arrest|fired|quit|walked away|everything|nothing left|bankrupt)\b", re.I)

CONTRACTION_RE = re.compile(r"\b\w+n't\b|\b\w+'(s|d|ll|re|ve)\b")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"[.!?]+", text) if s.strip()]


def _score_hook(script, is_short: bool) -> tuple[float, list[str]]:
    issues: list[str] = []
    score = 10.0
    scenes = script.scenes
    if not scenes:
        return 0.0, ["No scenes at all"]
    hook = (scenes[0].narration or "").strip()
    words = len(hook.split())

    # Length discipline
    limit = 12 if is_short else 15
    if words > limit:
        score -= 2.5
        issues.append(f"Hook is {words} words (limit {limit}) — trim to a punch")
    if words < 4:
        score -= 1.0
        issues.append("Hook is too thin (<4 words) — add one specific")

    # Must be an actual archetype, not generic reporting
    if not any(rx.search(hook) for _, rx in HOOK_ARCHETYPES):
        score -= 3.0
        issues.append(
            "Hook is not a recognized archetype (curiosity gap / bold claim / "
            "shock stat / transformation / controversy / mid-action) — rewrite "
            "scene 1 to open a gap the viewer must close")

    # Numeric anchor within the first 3 scenes
    if not any(re.search(r"\d", s.narration or "") for s in scenes[:3]):
        score -= 2.0
        issues.append("No number/date/dollar in scenes 1-3 — specifics anchor attention")

    # Hook overlay text for muted autoplay
    if not (script.hook_overlay_text or "").strip():
        score -= 1.5
        issues.append("Missing hook_overlay_text — muted viewers see nothing in second 1")

    # Scene 1 visual must be the densest, not an establishing shot
    vp1 = (scenes[0].visual_prompt or "").lower()
    if any(w in vp1 for w in ("wide shot", "establishing", "city skyline", "landscape of")):
        score -= 1.0
        issues.append("Scene 1 visual is an establishing shot — open on the densest frame instead")

    return max(0.0, score), issues


def _score_story(script, is_short: bool) -> tuple[float, list[str]]:
    issues: list[str] = []
    score = 10.0
    scenes = script.scenes
    n = len(scenes)
    if n < 4:
        return 3.0, ["Too few scenes to build a story arc"]

    # Open loops: questions/teases that aren't resolved in the same scene
    loop_words = re.compile(r"\b(but (?:here'?s|here is|that'?s|that is|wait)|what (?:they|he|she) (?:didn'?t|does|happened)|until|then (?:it|everything) (?:got|changed|fell)|the (?:problem|twist|catch|stakes)|which is why|yet the|but wait)\b", re.I)
    question_re = re.compile(r"\?")
    loop_scenes = 0
    for s in scenes[:-1]:
        t = s.narration or ""
        if loop_words.search(t) or question_re.search(t):
            loop_scenes += 1
    loop_ratio = loop_scenes / max(1, n - 1)
    if loop_ratio < 0.25:
        score -= 3.0
        issues.append(
            f"Open-loop density too low ({loop_scenes}/{n - 1} scenes) — every "
            "2-3 scenes should tease forward ('but here's where it turns…')")
    elif loop_ratio < 0.4:
        score -= 1.0

    # Stakes cadence: after the hook, stakes every ~3 scenes
    gaps_without_stakes = 0
    run = 0
    for s in scenes[1:]:
        if STAKES_WORDS.search(s.narration or ""):
            run = 0
        else:
            run += 1
            gaps_without_stakes = max(gaps_without_stakes, run)
    if gaps_without_stakes > 3:
        score -= 2.0
        issues.append(
            f"{gaps_without_stakes} consecutive scenes without stakes — insert a "
            "cost/risk number or consequence beat")

    # Loop ending: last scene must bridge back, not sign off
    last = (scenes[-1].narration or "").lower()
    if re.search(r"\b(subscribe|follow for|like and|comment below|see you (?:next|in the))\b", last):
        score -= 2.5
        issues.append("Ending signs off instead of looping — end on the open question")
    hook_first4 = " ".join((scenes[0].narration or "").lower().split()[:4])
    last_last8 = " ".join(last.split()[-8:])
    overlap = len(set(hook_first4.split()) & set(last_last8.split()))
    if overlap == 0 and not question_re.search(last):
        score -= 1.5
        issues.append("Final scene doesn't echo the hook or ask a question — weak loop seam")

    # Midpoint re-engagement (long-form sag killer; shorts get it free via pacing)
    if not is_short and n >= 8:
        mid = scenes[n // 2]
        if not (STAKES_WORDS.search(mid.narration or "") or question_re.search(mid.narration or "")):
            score -= 1.5
            issues.append("Midpoint scene is flat exposition — put the twist/reveal here")

    return max(0.0, score), issues


def _score_humanity(script) -> tuple[float, list[str]]:
    issues: list[str] = []
    score = 10.0
    all_text = " ".join(s.narration or "" for s in script.scenes)

    # Vocabulary tells
    low = all_text.lower()
    vocab_hits = [w for w in AI_VOCAB_TELLS if w in low]
    if vocab_hits:
        score -= min(4.0, 1.5 * len(vocab_hits))
        issues.append(f"AI vocabulary in narration: {vocab_hits[:5]} — rewrite those lines")

    # Structural tells
    for name, rx in AI_STRUCTURAL_TELLS.items():
        hits = len(rx.findall(all_text))
        if hits:
            score -= min(2.5, 0.8 * hits)
            issues.append(f"Structural AI tell '{name}' ×{hits} — break the construction")

    # Burstiness (sentence-length variance) — the #1 human signal
    lengths = [len(s.split()) for text in [all_text] for s in _sentences(text)]
    if len(lengths) >= 6:
        stdev = statistics.pstdev(lengths)
        if stdev < 3.0:
            score -= 2.5
            issues.append(
                f"Low burstiness (stdev {stdev:.1f} < 3.0) — mix 3-word punches "
                "with long flowing sentences")
        if statistics.mean(lengths) > 24:
            score -= 1.0
            issues.append("Average sentence too long — spoken narration needs air")

    # Contractions — real speech contracts
    if all_text and not CONTRACTION_RE.search(all_text):
        score -= 1.5
        issues.append("No contractions anywhere — narration reads stiff")

    # Second-person pull
    if not re.search(r"\b(you|your|imagine|look at|think about)\b", low):
        score -= 1.0
        issues.append("No direct viewer address — add a 'you' beat to pull them in")

    return max(0.0, score), issues


def _image_flatness(path: Path) -> float | None:
    """RMS contrast proxy 0..1 via luminance stddev (low = flat/dull)."""
    try:
        from PIL import Image, ImageStat
        with Image.open(path) as im:
            im = im.convert("L").resize((160, 90))
            stat = ImageStat.Stat(im)
            return stat.stddev[0] / 64.0  # ~0..1 typical range
    except Exception:
        return None


def _score_visuals(project_dir: Path, script, is_short: bool) -> tuple[float, list[str]]:
    issues: list[str] = []
    score = 10.0
    scenes = script.scenes
    checked = 0
    flat = 0
    small = 0
    for s in scenes:
        p = s.photo_path or ""
        if not p:
            continue
        path = Path(p)
        if not path.exists():
            continue
        checked += 1
        try:
            from PIL import Image
            with Image.open(path) as im:
                w, h = im.size
        except Exception:
            continue
        if max(w, h) < 1280:
            small += 1
        rms = _image_flatness(path)
        if rms is not None and rms < 0.16:
            flat += 1
    if checked == 0:
        return 2.0, ["No usable images on disk for any scene"]
    flat_ratio = flat / checked
    small_ratio = small / checked
    if flat_ratio > 0.3:
        score -= 3.0
        issues.append(
            f"{flat}/{checked} images are visually flat (low contrast) — re-search "
            "with more specific/dramatic visual_prompts, or apply the grade")
    elif flat_ratio > 0.1:
        score -= 1.5
        issues.append(f"{flat}/{checked} flat images — prefer high-contrast frames")
    if small_ratio > 0.2:
        score -= 2.0
        issues.append(f"{small}/{checked} images below 1280px long edge — hold for higher-res")

    # Distinctness: repeated prompts = repeated frames
    prompts = [(s.visual_prompt or "").lower() for s in scenes if s.visual_prompt]
    if prompts:
        dup_ratio = 1 - len(set(prompts)) / len(prompts)
        if dup_ratio > 0.25:
            score -= 2.0
            issues.append("Visual prompts repeat across scenes — every scene needs a distinct frame")

    return max(0.0, score), issues


def _score_edit(project_dir: Path, script, is_short: bool) -> tuple[float, list[str]]:
    issues: list[str] = []
    score = 10.0
    scenes = script.scenes
    durations = [s.audio_duration_seconds or s.duration_target_seconds or 0 for s in scenes]
    durations = [d for d in durations if d > 0]
    if not durations:
        return 4.0, ["No timed scenes — edit cadence unknown"]

    # Visual-change cadence: the 2026 target is a change every 1.5-2s.
    # Proxy: per-scene narration length; >4s scenes need punch-cuts (the
    # assembler provides them via cut_sec — count them as covered).
    long_scenes = [d for d in durations if d > 4.0]
    total = sum(durations)
    if total / len(durations) > (3.4 if is_short else 6.5):
        score -= 2.0
        issues.append("Average shot length too slow for Shorts pacing (target 1.5-2s visual change)")

    # Scene count sanity for a 40-60s short
    if is_short:
        n = len(scenes)
        if n < 8:
            score -= 2.0
            issues.append(f"Only {n} scenes for a short — target 10-16 fast cuts")
        if len(long_scenes) > len(durations) * 0.4:
            score -= 1.5
            issues.append("Too many >4s scenes — punch-cut the long ones (wide → tight → pan)")

    # SFX coverage
    with_sfx = sum(1 for s in scenes if (s.sfx_cue or "none") != "none")
    if with_sfx / len(scenes) < 0.5:
        score -= 1.5
        issues.append("Sparse SFX cues — hits and transitions sell the cuts")

    # Captions exist (rendered by the ASS pass) — check a caption file artifact
    if not list(project_dir.glob("**/*.ass")) and not list(project_dir.glob("scenes/*timestamps*")):
        score -= 1.5
        issues.append("No caption track found — captions must appear by second 1")

    # ── Footage relevance & era audit ───────────────
    # A cutaway earns its seconds only if a viewer instantly connects it to
    # the story. The archive.org fetcher once fulltext-matched "Tom Cruise"
    # to a 1950s fruit commercial and intercut it into five scenes — random
    # footage reads as AI slop. Audit every b-roll source title: (1) at
    # least one distinctive story token in the title, and (2) never a
    # silent-film title under a modern story (era mismatch is an instant
    # non-sequitur even when a token coincidentally matches).
    story_text = " ".join(
        filter(None, [script.title, *(s.visual_prompt or "" for s in scenes)])
    ).lower()
    story_tokens = {t for t in re.split(r"\W+", story_text) if len(t) >= 5}
    modern_story = not any(
        f"{c}" in story_text for c in ("19", "190", "191", "192")
    ) and not any(
        w in story_text for w in (
            "rockefeller", "edison", "ford", "tesla", "napoleon", "lincoln",
            "empire", "dynasty", "silent", "history", "historic", "century",
        )
    )
    bad_broll: list[str] = []
    for src in project_dir.glob("scenes/*_broll.source.json"):
        try:
            data = json.loads(src.read_text(encoding="utf-8"))
        except Exception:
            continue
        title = str(data.get("title") or "").lower()
        if not title or title.startswith("stock b-roll"):
            continue  # stock clips were queried FROM the scene — relevant by construction
        if "archive" not in str(data.get("channel", "")).lower():
            continue
        if not ({t for t in re.split(r"\W+", title) if len(t) >= 5} & story_tokens):
            bad_broll.append(str(data.get("title") or src.stem))
        elif modern_story and any(y in title for y in ("19", "film", "silent")) and not any(
            w in title for w in story_tokens
        ):
            bad_broll.append(str(data.get("title") or src.stem))
    if bad_broll:
        score -= 2.5
        uniq = list(dict.fromkeys(bad_broll))[:3]
        issues.append(
            "Unrelated b-roll cutaway(s) — " + "; ".join(f"'{t}'" for t in uniq)
            + " share nothing with the story. Re-query the archive with the person's exact name or drop to photo cuts"
        )

    return max(0.0, score), issues


def evaluate_script_only(script, is_short: bool = True) -> dict:
    """Mid-pipeline check: score just the writing (hook/story/humanity),
    normalized to the same 0-10 scale so callers can iterate on the script
    BEFORE paying for voice/render/upload."""
    hook_s, hook_i = _score_hook(script, is_short)
    story_s, story_i = _score_story(script, is_short)
    human_s, human_i = _score_humanity(script)
    total = (hook_s * 0.40 + story_s * 0.35 + human_s * 0.25)
    return {
        "score": round(total, 1),
        "verdict": "ship" if total >= 8.0 else ("fix" if total >= 6.0 else "redo"),
        "breakdown": {"hook": round(hook_s, 1), "story": round(story_s, 1),
                      "humanity": round(human_s, 1)},
        "issues": hook_i + story_i + human_i,
    }


def evaluate_project(project_dir: Path, script_path: Path | None = None,
                     is_short: bool = True) -> dict:
    """Score the project like a human editor. Returns:

    {"score": 0-10, "verdict": "ship|fix|redo", "breakdown": {...},
     "issues": [str, ...], "fix_targets": {"script": bool, "assets": bool, "edit": bool}}
    """
    project_dir = Path(project_dir)
    script_path = Path(script_path or project_dir / "script.json")
    try:
        from models import Script
        script = Script.load(script_path)
    except Exception as e:
        return {"score": 0.0, "verdict": "redo", "breakdown": {},
                "issues": [f"script unreadable: {e}"],
                "fix_targets": {"script": True, "assets": False, "edit": False}}

    # FIX-068: Script has no total_duration_seconds attribute — compute the
    # narration total the same way every other call site does. The old line
    # only ever worked because `is_short or ...` short-circuited on shorts;
    # long-forms hit the RHS and crashed with AttributeError AFTER the render.
    total_dur = sum(
        s.audio_duration_seconds or s.duration_target_seconds or 0.0
        for s in script.scenes
    )
    is_short = is_short or total_dur <= 90
    hook_s, hook_i = _score_hook(script, is_short)
    story_s, story_i = _score_story(script, is_short)
    human_s, human_i = _score_humanity(script)
    vis_s, vis_i = _score_visuals(project_dir, script, is_short)
    edit_s, edit_i = _score_edit(project_dir, script, is_short)

    breakdown = {
        "hook": round(hook_s, 1),
        "story": round(story_s, 1),
        "humanity": round(human_s, 1),
        "visuals": round(vis_s, 1),
        "edit": round(edit_s, 1),
    }
    total = (hook_s * 0.30 + story_s * 0.25 + human_s * 0.15
             + vis_s * 0.20 + edit_s * 0.10)
    issues = hook_i + story_i + human_i + vis_i + edit_i

    verdict = "ship" if total >= 8.0 else ("fix" if total >= 6.0 else "redo")
    fix_targets = {
        "script": bool(hook_i or story_i or human_i),
        "assets": bool(vis_i),
        "edit": bool(edit_i),
    }
    result = {
        "score": round(total, 1),
        "verdict": verdict,
        "breakdown": breakdown,
        "issues": issues,
        "fix_targets": fix_targets,
    }
    (project_dir / "quality_gate.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(f"Quality gate: {result['score']}/10 {verdict.upper()} "
             f"({json.dumps(breakdown)})")
    return result
