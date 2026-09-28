"""
GhostDirector — Scriptwriter Module

Takes research results and a template, then uses Gemini to generate
a structured scene-by-scene video script with visual directions.
"""

import sys
import re
import json
import asyncio
import statistics
import time
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from google import genai
from google.genai import types

from models import ResearchResult, Scene, Script
import config
from pipeline.anchors import validate_script_anchors
from utils.logger import get_logger
from utils.retry import retry, pace_llm_call

log = get_logger("scriptwriter")


# ──────────────────────────────────────────────
# JSON Schema for structured output
# ──────────────────────────────────────────────
SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "A compelling, clickbait-but-accurate YouTube title. Max 70 chars.",
        },
        "description": {
            "type": "string",
            "description": "YouTube video description. 2-3 sentences summarizing the story.",
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "10-15 relevant YouTube tags for SEO.",
        },
        "hook": {
            "type": "string",
            "description": "The opening hook line — the very first thing the narrator says.",
        },
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scene_number": {"type": "integer"},
                    "narration": {
                        "type": "string",
                        "description": "The narrator's voiceover text for this scene. 2-4 sentences. Conversational, engaging tone.",
                    },
                    "visual_prompt": {
                        "type": "string",
                        "description": "A search query to find relevant stock video or photo. Be specific: 'aerial shot of Manhattan skyline at night', 'close up of courtroom gavel', etc.",
                    },
                    "alternative_visual_prompts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "2-3 alternative search queries in case the primary one yields no results. Vary the keywords but keep the same concept.",
                    },
                    "visual_type": {
                        "type": "string",
                        "enum": ["stock_video", "web_photo", "stock_photo", "youtube_clip"],
                        "description": "Default to web_photo (real archival images look premium). youtube_clip for specific interview/event footage of a named person. stock_video is a LAST RESORT for truly generic scenery — never for people, emotions, crowds, or concepts.",
                    },
                    "mood": {
                        "type": "string",
                        "enum": ["dramatic", "suspenseful", "upbeat", "dark", "neutral", "emotional", "triumphant"],
                        "description": "The emotional tone of this scene. Affects music and color grading.",
                    },
                    "people_to_show": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Names of people to show photos of during this scene. Leave empty if none.",
                    },
                    "duration_target_seconds": {
                        "type": "number",
                        "description": "Estimated duration of this scene in seconds (1.8-3.2 for Shorts, 3.5-6.0 for long-form).",
                    },
                    "sfx_cue": {
                        "type": "string",
                        "enum": ["whoosh", "sub_impact", "camera_shutter", "tension_riser", "none"],
                        "description": "Sound effect trigger for this scene cut (e.g., whoosh for transitions, sub_impact for shocking revelations, camera_shutter for photos/evidence).",
                    },
                    "broll_keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "2-4 tangible physical object/action queries to find authentic B-roll (e.g. ['courtroom gavel', 'police sirens night', 'stacks of cash']).",
                    },
                },
                "required": [
                    "scene_number",
                    "narration",
                    "visual_prompt",
                    "alternative_visual_prompts",
                    "visual_type",
                    "mood",
                    "people_to_show",
                    "duration_target_seconds",
                    "sfx_cue",
                    "broll_keywords",
                ],
            },
        },
        "loop_phrase": {
            "type": "string",
            "description": "The seamless transition phrase at the end of the script that loops back into the opening hook line.",
        },
        "pinned_comment": {
            "type": "string",
            "description": "A high-retention YouTube pinned comment (under 200 chars) with a timestamp and a debate question to drive viewer replies.",
        },
        "hook_overlay_text": {
            "type": "string",
            "description": "5-7 bold words complementary visual headline for silent autoplay on mobile (e.g., 'THE $200B SECRET', 'NEVER DO THIS FIRST'). Must NOT just repeat the spoken line.",
        },
        "title_variants": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["curiosity_gap", "direct_benefit", "contrarian"]},
                    "title": {"type": "string"},
                },
                "required": ["type", "title"],
            },
            "description": "3 distinct A/B/C YouTube title variants: one curiosity gap, one direct benefit, one contrarian.",
        },
        "midpoint_trigger": {
            "type": "string",
            "description": "A deliberate stakes-raise or shocking revelation at 50% runtime to reset attention and kill the mid-video sag.",
        },
        "bonus_payoff": {
            "type": "string",
            "description": "A bonus insight delivered in the conclusion beyond what the hook promised, converting viewers into subscribers.",
        },
        "midroll_markers": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Suggested commercial ad break timestamps for 8+ min videos (e.g. ['03:30', '07:00']).",
        },
        "total_scenes": {"type": "integer"},
        "estimated_duration_minutes": {"type": "number"},
    },
    "required": [
        "title",
        "description",
        "tags",
        "hook",
        "scenes",
        "total_scenes",
        "estimated_duration_minutes",
    ],
}


def _build_prompt(research: ResearchResult, template: dict) -> str:
    """Build the scriptwriting prompt with deep humanization for YouTube monetization."""
    script_cfg = template.get("script", {})
    tone = script_cfg.get("tone", "engaging, informative")
    persona = script_cfg.get("persona", "You are a professional documentary narrator.")
    hook_style = script_cfg.get("hook_style", "Start with a provocative question.")
    target_scenes = script_cfg.get("target_scenes", 15)
    target_duration = script_cfg.get("target_duration_minutes", 7)
    structure = script_cfg.get("structure", ["hook", "body", "conclusion"])
    guidelines = script_cfg.get("content_guidelines", [])

    guidelines_text = ""
    if guidelines:
        guidelines_text = "\n\nCONTENT GUIDELINES (you MUST follow these):\n"
        for g in guidelines:
            guidelines_text += f"- {g}\n"

    research_text = f"""
TOPIC: {research.topic}

SUMMARY: {research.summary}

NARRATIVE ARC: {research.narrative_arc}

TIMELINE OF EVENTS:
{json.dumps(research.timeline, indent=2)}

KEY PEOPLE:
{json.dumps(research.key_people, indent=2)}

KEY FACTS:
{json.dumps(research.key_facts, indent=2)}
"""

    is_shorts = (
        target_duration <= 1.5
        or template.get("aspect_ratio") == "9:16"
        or "short" in template.get("name", "").lower()
    )

    # Per-template scene length (Phase 1.5): templates may demand longer
    # narration units (8-12s) that the assembler punch-cuts into multiple
    # visual shots. The generic prompt's 3.5-6.0s range is kept as fallback.
    scene_range = script_cfg.get("scene_duration_range", [3.5, 6.0] if not is_shorts else [1.8, 3.2])
    lo, hi = float(scene_range[0]), float(scene_range[1])

    if is_shorts:
        pacing_instruction = """
═══════════════════════════════════════════════════
SHORTS VIRAL RETENTION & PACING RULES (MANDATORY):
═══════════════════════════════════════════════════
0. STORY ENGINE (OPEN LOOPS + STAKES — THIS IS WHAT SEPARATES VIRAL FROM DEAD):
   - OPEN-LOOP LADDER: every second scene should END by teasing the next beat
     before it resolves: "But here's where it turns...", "What he did next
     cost him everything...", "That was only the first crack." Never resolve
     a tease in the same scene that opens it.
   - STAKES CADENCE: never more than 2 consecutive scenes without a cost,
     risk, or consequence (dollar figure, deadline, loss, prison, ruin).
     Stakes are the reason to keep watching; exposition alone is death.
   - ONE DIRECT-ADDRESS BEAT: at least one scene pulls the viewer in with
     "you" — "imagine being the lawyer who...", "look at what happened when..."
   - VISUAL DENSITY ORDER: Scene 1's visual_prompt must be the single most
     dramatic, specific frame of the whole video (a face mid-emotion, a
     document, a confrontation) — NEVER a wide establishing shot or skyline.
   - TARGET 10-16 scenes for a 35-55 second short. Six slow scenes is a
     dead short; twelve fast ones is a plateau retention curve.
1. FIRST 3 SECONDS AUDITION (CRITICAL FOR SWIPE PREVENTION):
   - The SPOKEN HOOK (Scene 1) MUST be UNDER 12 WORDS! Short, punchy, immediate signal.
   - Choose one of 6 proven hook archetypes:
     * Curiosity Gap: "The reason Elon never talks about his first company..."
     * Controversy: "Unpopular truth: Tesla was not founded by Elon Musk."
     * Shocking Stat: "99% of people get this completely backward."
     * Bold Claim: "This 10-second habit created a $200B empire."
     * Transformation: "He went from sleeping on office floors to trillionaire."
     * List Tease: "Three rules that turned Elon into an anomaly."
   - HOOK OVERLAY TEXT (hook_overlay_text): Provide a 5 to 7 word bold, complementary text hook
     for muted mobile autoplay (e.g., "THE $200B SECRET", "WHAT THEY HID"). It must complement, NOT just repeat, the spoken line!

2. ULTRA-FAST SCENE PACING:
   - Each scene MUST be between {lo} and {hi} seconds (approx {words_lo} to {words_hi} words per scene).
   - Cut visuals constantly. Viewers swipe away if a visual lingers more than 3 seconds!
   - Target 12 to 18 fast scenes for a 35-45 second video.

3. SEAMLESS LOOP (CRITICAL FOR >100% RETENTION):
   - The final sentence of the script MUST grammatically and sonically bridge right back into the opening hook line!
   - Best loop seam: REPEAT one key phrase from the hook verbatim, or end on
     the exact open question the hook asked. Viewers whose brain doesn't
     register the ending rewatch without deciding to.
   - DO NOT say "subscribe", "like for more", or "comment down below" — this tells viewers the video ended and they will swipe.
   - Example:
     Hook (Scene 1): "This one hidden secret made Elon Musk billions..."
     Final Scene: "Which is why everyone in Silicon Valley is still obsessed with that one hidden secret..."
     (Loop repeats seamlessly into Scene 1!)
   - Set loop_phrase to this bridge phrase.

4. SOUND DESIGN CUES (sfx_cue):
   - Assign a specific SFX to every scene cut:
     - 'whoosh': for rapid topic shifts and swift transitions
     - 'sub_impact': for shocking statistics, arrests, or turning points
     - 'camera_shutter': when showing mugshots, documents, or photos
     - 'tension_riser': for suspenseful pauses and rising tension
     - 'none': only when narration is continuous across cuts

5. TANGIBLE B-ROLL KEYWORDS (broll_keywords):
   - Provide 2-4 tangible physical search queries (e.g., ['police sirens night', 'gavel hitting sound block', 'stock market ticker red']).

6. PACKAGING & TITLE VARIANTS:
   - Provide 3 title_variants: one curiosity_gap, one direct_benefit, one contrarian.
"""
    else:
        pacing_instruction = """
═══════════════════════════════════════════════════
LONG-FORM RETENTION & MONETIZATION RULES:
═══════════════════════════════════════════════════
1. FIRST 30 SECONDS CLIFF SURVIVAL:
   - The opening hook (Scene 1) must be under 15 words and immediately open a dramatic knowledge gap.
   - HOOK OVERLAY TEXT (hook_overlay_text): Provide a 5 to 7 word bold on-screen title card hook for muted viewers.
   - Never say "welcome back" or "in this video we will discuss" — open mid-action!

2. BEAT ARCHITECTURE & MIDPOINT SAG KILLER (CRITICAL):
   - Structure scenes into 3-5 distinct beats.
   - MIDPOINT RE-ENGAGEMENT TRIGGER (midpoint_trigger): At exactly 50% runtime, place a dramatic stakes-raise
     or twist ("And that is when everything unraveled...") to prevent the classic mid-video view sag!
   - SURPLUS PAYOFF (bonus_payoff): In the conclusion, deliver a bonus insight NOT promised in the hook
     to reward viewers and trigger high subscriber conversion.
   - MID-ROLL AD ANCHORS (midroll_markers): Note natural cliffhangers (~03:30, ~07:00) suitable for mid-roll ads.

3. DYNAMIC DOCUMENTARY PACING:
   - Scene duration target: {lo} to {hi} seconds per scene ({words_lo}-{words_hi} words of narration).
   - Alternate between rapid exposition and deliberate dramatic pauses.

4. SOUND DESIGN CUES (sfx_cue):
   - Use 'sub_impact' for chapter beginnings and climaxes.
   - Use 'whoosh' on scene transitions.
   - Use 'camera_shutter' on historical photos and documents.
   - Use 'tension_riser' before big revelations.

5. TANGIBLE B-ROLL KEYWORDS (broll_keywords):
   - Provide 2-4 concrete, physical B-roll search terms for each scene.

6. PACKAGING & TITLE VARIANTS:
   - Provide 3 title_variants: one curiosity_gap, one direct_benefit, one contrarian.
"""

    anchor_rule = """

═══════════════════════════════════════════════
7. FACT ANCHORING (MANDATORY — defamation shield):
   - EVERY date, dollar figure, percentage, age and large number in the narration
     MUST come from the RESEARCH DATA above. If the research doesn't contain it,
     do NOT write it. Unanchored specifics are rewritten out automatically and
     will weaken the scene — anchor everything up front.
   - Soften speculation with 'allegedly' / 'according to reports'.
"""

    # ~2.6 spoken words per second of narration
    words_lo, words_hi = int(lo * 2.6), int(hi * 2.6)
    pacing_instruction = pacing_instruction.format(
        lo=f"{lo:g}", hi=f"{hi:g}", words_lo=words_lo, words_hi=words_hi
    ) + anchor_rule

    return f"""{persona}

Your job is to write a compelling, broadcast-grade video script engineered for high retention and monetization on YouTube.

TONE: {tone}
HOOK STYLE: {hook_style}
TARGET NUMBER OF SCENES: {target_scenes}
TARGET TOTAL DURATION: {target_duration} minutes
STORY STRUCTURE: {" -> ".join(structure)}
{guidelines_text}
{pacing_instruction}

RESEARCH DATA (use this as your source material — do NOT invent facts):
{research_text}

═══════════════════════════════════════════════════
CRITICAL: HUMANIZATION RULES (Your #1 priority)
═══════════════════════════════════════════════════

This script MUST sound like a real human YouTuber wrote it — NOT like AI.
YouTube will reject and demonetize content that sounds robotic or AI-generated.
Follow these rules religiously:

1. NATURAL SPEECH PATTERNS:
   - Use contractions: "don't", "wasn't", "couldn't", "it's", NOT "do not", "was not"
   - Start some sentences with "And", "But", "So", "Look", "Now", "Honestly"
   - Use rhetorical questions: "Can you even imagine?", "Right?", "Wild, huh?"
   - Vary sentence length dramatically. Short punchy ones. Then longer ones that build up the story with more detail and emotion.

2. PERSONALITY INJECTION:
   - React to the story: "Now this is where it gets CRAZY", "I couldn't believe this part"
   - Express genuine opinions: "Honestly, I think...", "Here's what gets me about this..."
   - Use casual transitions: "Okay so fast forward to...", "But here's the thing..."
   - Sound like you're telling a friend: "Bro, listen to this", "You're not gonna believe this"

3. ENGAGEMENT HOOKS (every 2-3 scenes):
   - "But wait, it gets worse."
   - "And this next part? Nobody saw it coming."
   - "Stay with me here because this changes everything."
   - "Now pay attention to this detail — it matters later."

4. FORBIDDEN PATTERNS (these scream "AI"):
   - NEVER use: "delve", "tapestry", "It's important to note that", "In conclusion"
   - NEVER use: "Let's explore", "In the realm of", "It is worth noting"
   - NEVER use: "Furthermore", "Moreover", "Subsequently", "Consequently"
   - NEVER start with: "In today's video" or "Welcome back to"
   - NEVER use numbered lists in narration ("First... Second... Third...")
   - NEVER use the phrase "a testament to"

5. RHYTHM AND PACING:
   - The hook scene must be SHORT and PUNCHY (under 5 seconds)
   - After 2 information-heavy scenes, add a reaction/transition scene
   - Build to a climax around scene 70% through
   - End with an open question or loop that drives comments, NOT a neat summary

6. VISUAL TYPE SELECTION (PHOTOS WIN — this decides if the video looks cheap):
   - Real people/events: 60-70% web_photo, 15-25% youtube_clip. stock_video ONLY if the scene is genuinely filmable B-roll (a city skyline, a red carpet swarm) — target 0-10% of scenes, ideally zero.
   - NEVER use stock_video for: emotions, crowds watching screens, people at counters/queues, abstract concepts (AI, money, fame), or anything where a photo of the actual person/event exists.
   - When mentioning a specific person → ALWAYS use "web_photo" and put full name in people_to_show.
   - For visual_prompt: write specific search terms for real archival moments. A great prompt names a PERSON + a MOMENT ('Tom Cruise 2023 Oscars stage', 'Manchester United fans in stadium 1999'), never a generic place ('modern supermarket checkout').
   - If no real footage of the moment exists, use a photo of the PERSON reacting (web_photo, people_to_show=[name]) — a face beats generic stock every time.

7. PINNED COMMENT:
   - Include a 1-sentence thought-provoking debate question in pinned_comment to spark arguments in the comment section.
"""


def _load_llm_quarantine() -> dict:
    """Per-model daily-quota ledger: models that 429 with a DAILY exhaustion
    are skipped for the rest of the day instead of burning wall-clock in the
    chain (free tier: ~20 req/day/model — one script run can burn that)."""
    p = Path("output/_llm_quota_quarantine.json")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _quarantine_model(model_id: str) -> None:
    p = Path("output/_llm_quota_quarantine.json")
    data = _load_llm_quarantine()
    data[model_id] = time.time()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=1), encoding="utf-8")


def _is_quarantined_today(model_id: str) -> bool:
    ts = _load_llm_quarantine().get(model_id)
    if not ts:
        return False
    return datetime.fromtimestamp(float(ts)).date() == date.today()


@retry(max_attempts=8, base_delay=20.0, exceptions=(Exception,))
async def _call_gemini(prompt: str) -> dict:
    """Call Gemini with structured output to generate the script."""
    client = genai.Client(api_key=config.GEMINI_API_KEY)

    # Free-tier RPM pacing (found by first live run: 5 req/min on this key).
    await asyncio.to_thread(pace_llm_call)
    # Walk the full verified chain: a pinned id can 404, exhaust its own
    # daily quota bucket, or 503 under load independently of the others
    # (all three observed live on 2026-09-18). Models that hit a DAILY 429
    # are quarantined until midnight so later attempts skip them instantly.
    response = None
    last_err: Exception | None = None
    for model_id in config.gemini_candidates():
        if _is_quarantined_today(model_id):
            log.info(f"Model {model_id}: skipping (daily quota quarantined)")
            continue
        try:
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=model_id,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=config.GEMINI_TEMPERATURE,
                    max_output_tokens=config.GEMINI_MAX_OUTPUT_TOKENS,
                    response_mime_type="application/json",
                    response_schema=SCRIPT_SCHEMA,
                ),
            )
            break
        except Exception as gemini_err:
            last_err = gemini_err
            err_str = str(gemini_err)
            # Only a DAILY exhaustion quarantines (quotaId contains
            # PerDayPerProjectPerModel); a per-MINUTE 429 just needs a 60s wait.
            if "429" in err_str and "PerDayPerProjectPerModel" in err_str:
                _quarantine_model(model_id)
                log.warning(f"Model {model_id}: DAILY quota exhausted — quarantined until midnight")
            log.warning(
                f"Model {model_id} failed: {err_str[:140]}. "
                f"Trying next candidate in chain..."
            )
    if response is None:
        raise last_err if last_err else RuntimeError("No Gemini candidates available")

    # Parse the JSON response
    text = response.text
    if not text:
        raise ValueError("Gemini returned empty response")

    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    data = json.loads(text)
    log.info(
        f"Script generated: {data['total_scenes']} scenes, "
        f"~{data['estimated_duration_minutes']} min"
    )
    return data


def _validate_script(data: dict, template: dict) -> list[str]:
    """Validate the script meets template requirements. Returns list of issues."""
    issues = []
    target_scenes = template.get("script", {}).get("target_scenes", 15)
    target_duration = template.get("script", {}).get("target_duration_minutes", 7)

    actual_scenes = len(data.get("scenes", []))
    if actual_scenes < target_scenes * 0.4:
        issues.append(f"Too few scenes: {actual_scenes} (target: {target_scenes})")
    if actual_scenes > target_scenes * 2.0:
        issues.append(f"Too many scenes: {actual_scenes} (target: {target_scenes})")

    # FIX-047: word-budget check — total narration must actually reach the
    # duration target (~2.6 spoken words/second). Gemini routinely delivers
    # ~75% of the requested length on long templates; an 8-min video needs
    # ~1250 words, and a 6-min video from an 8-min template breaks the
    # mid-roll-ad monetization the template exists for.
    total_words = sum(len(s.get("narration", "").split())
                      for s in data.get("scenes", []))
    min_words = int(target_duration * 60 * 2.6 * 0.85)
    if total_words < min_words:
        issues.append(
            f"Script too short: {total_words} words (need ~{min_words}+ for "
            f"{target_duration} minutes). Expand scenes with more specific "
            "facts, dates, numbers and quotes — do NOT pad with filler."
        )
    # Max-budget check (shorts especially): an over-long script renders past
    # 59s and YouTube crops the audio mid-word. Compliance catches it too
    # late — block it here where the fix is a cheap rewrite.
    max_words = int(target_duration * 60 * 2.6 * 1.25)
    if total_words > max_words:
        issues.append(
            f"Script too long: {total_words} words (max ~{max_words} for "
            f"{target_duration} minutes). Tighten narration — cut filler words, "
            "keep every fact and the loop structure."
        )

    if not data.get("title"):
        issues.append("Missing title")
    if not data.get("hook"):
        issues.append("Missing hook")

    # Check each scene has required fields
    for i, scene in enumerate(data.get("scenes", [])):
        if not scene.get("narration"):
            issues.append(f"Scene {i + 1}: missing narration")
        if not scene.get("visual_prompt"):
            issues.append(f"Scene {i + 1}: missing visual_prompt")

    # ── Retention checks (30-second cliff defense) ──
    scenes = data.get("scenes", [])
    if scenes and scenes[0].get("narration"):
        hook_words = len(scenes[0]["narration"].split())
        if hook_words > 15:
            issues.append(f"Hook too long: {hook_words} words (keep under 15)")

    # Numeric-anchor rule: the first 3 scenes must contain hard specifics
    # (dates, dollar figures, measurements) — vagueness is why viewers click
    # away inside the first 30 seconds.
    if scenes and not any(re.search(r"\d", s.get("narration", "")) for s in scenes[:3]):
        issues.append("No numeric anchor in scenes 1-3 (add a date, dollar figure, or stat)")

    # ── Humanization checks (FIX-005) ──
    sentence_lengths = []
    for scene in scenes:
        narration = scene.get("narration", "")
        for sent in re.split(r"[.!?]+", narration):
            words = sent.split()
            if words:
                sentence_lengths.append(len(words))
        # Deterministic AI-phrase / legal-ism scan per scene
        text = narration.lower()
        for phrase in AI_TELL_PHRASES:
            if phrase in text:
                issues.append(f"Scene {scene.get('scene_number', '?')}: AI-tell phrase '{phrase}'")
                break
    if len(sentence_lengths) >= 6:
        stdev = statistics.pstdev(sentence_lengths)
        if stdev < 3.0:
            issues.append(
                f"Low burstiness: sentence-length stdev {stdev:.1f} (< 3.0) — "
                "narration may read as AI-generated"
            )

    return issues


def enforce_shorts_word_budget(
    script: Script, max_seconds: float = 55.0, wps: float = 2.6,
) -> Script:
    """FIX-060: deterministic Shorts length enforcement.

    The rewrite loop asks for a shorter draft but cannot FORCE one — a
    stubborn over-budget draft ships, renders past 59s, and compliance
    blocks the upload at the very end (found live 2026-09-25: a 12-scene
    Taylor Swift short at 206 words rendered 79.2s). This pass is the
    guarantee: at ~2.6 spoken words/second, scenes are dropped from the
    middle tail (never the hook, never the loop-CTA finale) until the
    narration fits max_seconds. Scene numbers are renumbered so the
    downstream stages (voices → assets → assembly) stay filename-consistent.
    """
    scenes = sorted(script.scenes, key=lambda s: s.scene_number)

    def _total(scns: list) -> int:
        return sum(len((s.narration or "").split()) for s in scns)

    max_words = int(max_seconds * wps)
    if _total(scenes) <= max_words or len(scenes) <= 2:
        return script

    # Start from the FULL scene set; drop tail-biased middle scenes (late
    # context goes first — the hook and the loop-CTA finale are untouchable)
    # until the budget fits.
    keep = {s.scene_number for s in scenes}
    middle_order = [s.scene_number for s in reversed(scenes[1:-1])]
    dropped: list[int] = []
    for gone in middle_order:
        if _total([s for s in scenes if s.scene_number in keep]) <= max_words:
            break
        keep.discard(gone)
        dropped.append(gone)

    kept = [s for s in scenes if s.scene_number in keep]
    if _total(kept) > max_words:
        # Even hook + finale bust the budget — trim their sentences rather
        # than ship an over-length short.
        for s in kept:
            sents = re.split(r"(?<=[.!?])\s+", s.narration or "")
            while len(sents) > 1 and _total(kept) > max_words:
                sents.pop()
                s.narration = " ".join(sents)

    for i, s in enumerate(kept, 1):
        s.scene_number = i
    script.scenes = kept
    script.total_scenes = len(kept)
    if dropped:
        log.warning(
            f"FIX-060: dropped scenes {dropped} to fit the Shorts word "
            f"budget ({max_words} words @ {max_seconds}s) — compliance "
            f"demands ≤60s and a rewrite could not get there")
    return script


# Spoken words per second, MEASURED from a live long-form run rather than
# assumed: 1350 words of narration rendered 606s of voice = 2.23 wps
# (edge-tts GuyNeural at -2Hz, sentence-join pauses included). The old 2.6
# assumption under-budgeted duration by ~16% — a "8 min" target produced a
# 10.1-minute render. Length math uses this number until it is re-measured.
DEFAULT_WPS = 2.25


def estimate_script_seconds(script: Script, wps: float = DEFAULT_WPS) -> float:
    """Spoken length of a script, in seconds.

    Rendering length IS voice length (the assembler builds video from the
    narration audio), so narration words / narrator rate is the truth. Scene
    duration targets are only a fallback for scenes with no narration text —
    the model's own targets were how a 450-word draft passed as "8.0 min".
    """
    words = sum(len((s.narration or "").split()) for s in script.scenes)
    if words > 0:
        return words / max(1.0, wps)
    return sum(float(s.duration_target_seconds or 0) for s in script.scenes)


def _fact_text(item) -> str:
    """Flatten a research fact/person of ANY shape into prompt text.

    Found live: key_people entries are dicts (name/role/search_query) and facts
    can arrive non-string — joining them raised TypeError and the expansion
    pass silently failed, so a 6-minute "8-minute" draft shipped unchanged.
    """
    if isinstance(item, dict):
        return " — ".join(str(v).strip() for v in item.values() if str(v or "").strip())
    if isinstance(item, (list, tuple)):
        return " — ".join(_fact_text(x) for x in item)
    return str(item).strip()


def _expansion_prompt(script: Script, research, target_s: float, wps: float) -> str:
    """Build the expansion prompt (pure function — unit-tested)."""
    raw_facts = list(getattr(research, "key_facts", None) or []) if research is not None else []
    raw_people = list(getattr(research, "key_people", None) or []) if research is not None else []
    facts = [f for f in (_fact_text(x) for x in raw_facts) if f]
    people = [p for p in (_fact_text(x) for x in raw_people) if p]
    current = [{"scene_number": s.scene_number, "narration": s.narration,
                "visual_prompt": s.visual_prompt, "mood": s.mood,
                "sfx_cue": s.sfx_cue, "broll_keywords": s.broll_keywords}
               for s in script.scenes]
    target_words = int(target_s * wps)
    current_words = sum(len((s.narration or "").split()) for s in script.scenes)
    per_scene_words = max(8, min(31, round(10.0 * wps)))   # ~10s of speech a beat
    prompt = (
        "You are expanding an existing documentary script that came in TOO SHORT.\n"
        f"Current draft: {len(script.scenes)} scenes, {current_words} words "
        f"(only ~{current_words / wps / 60:.1f} minutes of narration).\n"
        f"Required: {target_words} words of narration MINIMUM "
        f"(~{target_s/60:.0f} minutes at {wps} words/second).\n"
        f"Write roughly {per_scene_words} words of narration per scene "
        "(~10 seconds of speech). If the current scene count cannot hold that "
        "much detail, ADD MORE SCENES — do not leave scenes at 2-3 sentences.\n\n"
        "HOW TO EXPAND (substance, never padding):\n"
        "- Deepen existing beats with concrete detail already implied by the "
        "facts: dates, numbers, names, quotes that are on the record, "
        "chronology, cause and consequence, public reaction, what happened next.\n"
        "- Add NEW scenes for story beats that are missing, keeping the same "
        "running order and escalating stakes at the midpoint.\n"
        "- NEVER invent events, quotes, dates, relationships or people. "
        "If a fact is not given to you, write around it.\n"
        "- Never re-narrate the same sentence in different words; every added "
        "scene must add information.\n"
        "- Keep every existing fact exactly as stated.\n\n"
        "FACTS (the only source of truth):\n"
        + ("\n".join(f"- {f}" for f in facts[:80]) or "- (none supplied; stay strictly general)")
        + ("\nPEOPLE: " + "; ".join(people[:40]) if people else "")
        + "\n\nCURRENT SCENES (JSON):\n" + json.dumps(current, indent=1, ensure_ascii=False)
        + "\n\nReturn the COMPLETE expanded script as JSON with a 'scenes' array "
        "using the same fields per scene (scene_number, narration, "
        "visual_prompt, visual_type, mood, duration_target_seconds "
        "(8-12 for long-form), sfx_cue, broll_keywords). Longer narration per "
        "scene is fine; do not exceed 12 seconds of narration per beat."
    )
    return prompt


async def _expand_script(script: Script, research, target_s: float,
                         wps: float) -> "Script | None":
    """One LLM expansion pass: deepen and extend the SAME story.

    The facts on hand are the only truth given to the model — the prompt
    forbids invented events, quotes, dates or people and forbids padding.
    Returns None when the model fails to grow the draft.
    """
    prompt = _expansion_prompt(script, research, target_s, wps)
    data = await _call_gemini(prompt)
    return _apply_expansion(script, data, wps)


def _apply_expansion(script: Script, data: dict | None, wps: float = DEFAULT_WPS) -> "Script | None":
    """Rebuild the script from an expansion response; refuse to shrink it.

    Found live: a model can return the SAME scene count with reworded text —
    requiring scene-count growth rejected a genuine improvement and kept a
    2.9-minute "8-minute" script. Growth is therefore measured in WORDS,
    with scene count as the secondary signal (and a shrink is never accepted).
    """
    scenes_json = (data or {}).get("scenes") or []
    before_words = sum(len((s.narration or "").split()) for s in script.scenes)
    before_n = len(script.scenes)
    from models import Scene

    new_scenes = []
    for i, sj in enumerate(scenes_json, 1):
        narration = (sj.get("narration") or "").strip()
        if not narration:
            continue
        try:
            dur = float(sj.get("duration_target_seconds") or 8.0)
        except (TypeError, ValueError):
            dur = 8.0
        new_scenes.append(Scene(
            scene_number=i,
            narration=narration,
            visual_prompt=sj.get("visual_prompt") or "",
            visual_type=sj.get("visual_type") or "stock_video",
            mood=sj.get("mood") or "neutral",
            people_to_show=sj.get("people_to_show") or [],
            duration_target_seconds=dur,
            sfx_cue=sj.get("sfx_cue") or "none",
            broll_keywords=sj.get("broll_keywords") or [],
            alternative_visual_prompts=sj.get("alternative_visual_prompts") or [],
        ))
    if len(new_scenes) < before_n:
        return None                      # never accept a shrink
    after_words = sum(len((s.narration or "").split()) for s in new_scenes)
    grew_scenes = len(new_scenes) > before_n
    if not grew_scenes and after_words <= before_words * 1.15:
        return None                      # same length reworded is not growth
    script.scenes = new_scenes
    for sc in script.scenes:
        # Keep the declared per-scene length honest: the model's own targets
        # were part of the problem (44 "scenes" at ~4s each is not a doc);
        # long-form beats should read 6-12s of narration.
        est_s = len((sc.narration or "").split()) / max(1.0, wps)
        sc.duration_target_seconds = max(6.0, min(12.0, est_s))
    script.total_scenes = len(new_scenes)
    script.estimated_duration_minutes = estimate_script_seconds(script, wps) / 60.0
    return script


async def enforce_longform_length(script: Script, research=None, template=None,
                                  wps: float = DEFAULT_WPS, min_ratio: float = 0.92,
                                  max_passes: int = 2, expand=None) -> Script:
    """FIX-063: long-form needed a FLOOR, not just a ceiling.

    FIX-060 guarantees a Short cannot ship past 59 seconds. Long-form had no
    equivalent guarantee: when the model under-delivers (quota-truncated
    drafts were observed live — a "long-form" of 444 words), the render
    silently shipped at a fraction of the template's target duration and
    nobody noticed until playback. This measures the draft against the
    template target and expands IN PLACE using the research already on hand:
    more story, same facts, never padding.

    `expand` is injectable for tests (defaults to the LLM pass). Failures
    degrade to the current draft — the pipeline never breaks here.
    """
    if script is None or not script.scenes:
        return script
    cfg = (template or {}).get("script", {}) if isinstance(template, dict) else {}
    try:
        target_min = float(cfg.get("target_duration_minutes") or 0)
    except (TypeError, ValueError):
        target_min = 0.0

    # The model self-reports `estimated_duration_minutes` and it lies upward:
    # a 450-word draft claimed 8.0 minutes and every downstream stage believed
    # it (quality gate, packaging, the operator). Trust the measurement, never
    # the claim — this runs for every script, target or not.
    claimed = getattr(script, "estimated_duration_minutes", None)
    measured = estimate_script_seconds(script, wps) / 60.0
    if claimed and claimed > measured * 1.25:
        log.warning(f"FIX-063: script claimed {claimed:.1f} min but measures "
                    f"{measured:.1f} min ({len(script.scenes)} scenes) — "
                    f"trusting the measurement")
        script.estimated_duration_minutes = measured

    if target_min <= 0:
        return script
    target_s = target_min * 60.0
    expander = expand or _expand_script

    for attempt in range(max_passes + 1):
        est = estimate_script_seconds(script, wps)
        if est >= target_s * min_ratio:
            log.info(f"FIX-063: long-form length OK — {est / 60.0:.1f} min "
                     f"vs {target_min:.0f} min target ({len(script.scenes)} scenes)")
            return script
        if attempt >= max_passes:
            break
        log.warning(f"FIX-063: draft is {est / 60.0:.1f} min against a "
                    f"{target_min:.0f} min target — expanding "
                    f"(pass {attempt + 1}/{max_passes})")
        try:
            expanded = await expander(script, research, target_s, wps)
        except Exception as e:
            log.warning(f"FIX-063: expansion pass failed ({str(e)[:140]}) — keeping draft")
            break
        if expanded is None:
            log.warning("FIX-063: expansion returned no growth — keeping draft")
            break
        script = expanded

    # Final truth: whatever shipped, the recorded duration is the measurement.
    script.estimated_duration_minutes = estimate_script_seconds(script, wps) / 60.0
    return script


def _dedupe_rewrites(issues: list[str]) -> dict[int, str]:
    """Extract {scene_number: flagged_phrase} from validation issues.

    Used for recovery when the targeted-rewrite LLM pass fails: identical
    phrases never repeat verbatim across scenes, so we deterministically
    reword them ourselves instead of shipping an AI-tell in the final cut.
    """
    scene_map: dict[int, str] = {}
    for issue in issues:
        m = re.match(r"Scene (\d+): AI-tell phrase '(.+?)'", issue)
        if m:
            scene_map[int(m.group(1))] = m.group(2)
    return scene_map


_HUMAN_REWORDINGS = {
    "delve": "dig into",
    "delved": "dug into",
    "delves": "digs into",
    "tapestry": "mix",
    "in conclusion": "so where does that leave us",
    "in summary": "long story short",
    "to summarize": "long story short",
    "furthermore": "and on top of that",
    "moreover": "plus",
    "subsequently": "after that",
    "consequently": "so",
    "in the realm of": "in the world of",
    "let's explore": "let's look at",
    "let's dive in": "let's get into it",
    "let's unpack": "let's break down",
    "navigate the landscape": "find their way through",
    "in today's video": "today",
    "welcome back to the channel": "welcome back",
    "without further ado": "right, let's go",
    "buckle up": "hang on",
    "as an ai": "honestly",
    "rich tapestry": "messy mix",
    "vibrant tapestry": "messy mix",
    "fascinating journey": "wild ride",
    "deep dive into": "hard look at",
    "game changer": "turning point",
    "the question on everyone's mind": "what everyone was asking",
    "it's important to note": "worth saying",
    "it is important to note": "worth saying",
    "it's worth noting": "here's the thing",
    "it is worth noting": "here's the thing",
    "a testament to": "proof of",
    "testament to": "proof of",
}


# ──────────────────────────────────────────────
# Humanization v2 (FIX-005)
# ──────────────────────────────────────────────

# Phrases that pattern-match to LLM prose or AI-detection heuristics.
AI_TELL_PHRASES = [
    "delve", "tapestry", "a testament to", "testament to",
    "it's important to note", "it is important to note",
    "it's worth noting", "it is worth noting",
    "in conclusion", "in summary", "to summarize",
    "furthermore", "moreover", "subsequently", "consequently",
    "in the realm of", "let's explore", "let's dive in", "let's unpack",
    "navigate the landscape", "in today's video", "welcome back to the channel",
    "without further ado", "buckle up", "as an ai",
    "rich tapestry", "vibrant tapestry", "fascinating journey",
    "deep dive into", "game changer", "the question on everyone's mind",
    # 2025-26 detection-study additions (CMU, Wikipedia slop signs)
    "pivotal", "ever-evolving", "ever-changing", "undeniably",
    "intricate", "myriad", "plethora", "beacon of", "unlock", "unleash",
    "elevate", "empower", "not just", "it's not about", "it is not about",
    # Legal-conclusion isms (YPP reviewer + defamation risk on celebrity content)
    "he is guilty", "she is guilty", "they are guilty",
    "proves he", "proves she", "proof that he", "proof that she",
    "he murdered", "she murdered", "he killed her", "she killed him",
    "convicted of murdering", "he committed", "she committed",
]


def _scan_ai_phrases(script) -> list[tuple[int, str]]:
    """Deterministic check for AI-tell phrases in final narration.

    Returns [(scene_number, matched_phrase), ...], one hit per scene max.
    """
    hits: list[tuple[int, str]] = []
    for scene in script.scenes:
        text = scene.narration.lower()
        for phrase in AI_TELL_PHRASES:
            if phrase in text:
                hits.append((scene.scene_number, phrase))
                break
    return hits


HUMANIZE_PROMPT = """You are a ruthless script doctor for a YouTube documentary channel.
Rewrite the NARRATION of this script so it sounds like a real, specific human
wrote it for their channel — while keeping every fact, name, date, number and
claim EXACTLY as given.

Rules:
1. Keep scene count, scene order and scene_number values. ONLY change narration text.
2. Delivery tactics (apply them, never mention them):
   - Contractions everywhere ("didn't", "he'd", "that's").
   - Vary sentence length HARD: some 3-word punches ("He paid nothing."), some
     long flowing sentences. Adjacent sentences must NOT be similar lengths.
   - Occasional sentence starting with And / But / So / Look / Here's the thing.
   - One rhetorical question or direct address every few scenes ("you", "imagine").
   - Small verbal color: "wildly", "quietly", "for years", "and then — nothing."
3. BANNED vocabulary (never write any of these): {banned}.
4. BANNED constructions (never write any of these):
   - "Not just X, but Y" — say the Y directly.
   - "It's not about X. It's about Y." — pick one.
   - Lists of exactly three ("X, Y, and Z") — two or four instead.
   - Corporate verbs: unlock, unleash, elevate, empower, transform.
   - Two consecutive sentences of similar length — vary the rhythm.
5. Do NOT add facts, opinions presented as facts, or legal conclusions. Rephrase only.
6. Numbers and names must survive verbatim.

SCENES TO HUMANIZE:
{scenes_json}
"""


@retry(max_attempts=2, base_delay=2.0, exceptions=(Exception,))
async def _humanize_pass(script, template: dict):
    """Second Gemini pass: rewrite narration for human delivery.

    Trust boundary: structure, visual directions and facts stay from the draft;
    only narration text is replaced. Raises on failure — caller decides.
    """
    banned = ", ".join(f'"{p}"' for p in AI_TELL_PHRASES[:20])
    prompt = HUMANIZE_PROMPT.format(
        banned=banned,
        scenes_json=json.dumps(
            [{"scene_number": s.scene_number, "narration": s.narration}
             for s in script.scenes],
            indent=1, ensure_ascii=False,
        ),
    )

    client = genai.Client(api_key=config.GEMINI_API_KEY)
    await asyncio.to_thread(pace_llm_call)
    response = await asyncio.to_thread(
        client.models.generate_content,
        model=config.GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=1.0,  # hotter than the draft — facts are locked, we want variance
            max_output_tokens=config.GEMINI_MAX_OUTPUT_TOKENS,
            response_mime_type="application/json",
            response_schema={
                "type": "object",
                "properties": {
                    "scenes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "scene_number": {"type": "integer"},
                                "narration": {"type": "string"},
                            },
                            "required": ["scene_number", "narration"],
                        },
                    }
                },
                "required": ["scenes"],
            },
        ),
    )
    data = json.loads(response.text)

    by_number = {s["scene_number"]: s for s in data.get("scenes", [])}
    changed = 0
    for scene in script.scenes:
        new_text = by_number.get(scene.scene_number, {}).get("narration")
        if new_text and new_text.strip() and new_text.strip() != scene.narration:
            scene.narration = new_text.strip()
            changed += 1
    log.info(f"[bold green]Humanizer pass:[/bold green] rewrote {changed}/{len(script.scenes)} narrations")
    return script


ANCHOR_REWRITE_PROMPT = """You are the fact-checking editor of a documentary channel.
The following narration claims were NOT found in the verified research payload.
Rewrite each scene so the unanchored specific is REMOVED or reworded as an
explicitly attributed report ("reportedly", "according to several outlets").

Hard rules:
1. NEVER introduce new dates, dollar figures, percentages, ages or statistics.
2. Keep the scene's meaning, energy and length roughly the same.
3. Keep specifics that were NOT flagged — only fix the listed claims.
4. Return JSON.

RESEARCH PAYLOAD (your only source of truth):
{research_json}

SCENES TO FIX:
{scenes_json}
"""


@retry(max_attempts=2, base_delay=2.0, exceptions=(Exception,))
async def _rewrite_unanchored(script, research: ResearchResult, unanchored: list):
    """LLM pass to strip/soften claims the research payload can't support."""
    scene_map = {s.scene_number: s for s in script.scenes}
    targets = [
        {"scene_number": a.scene_number, "claim": a.text, "claim_type": a.claim_type,
         "narration": scene_map[a.scene_number].narration}
        for a in unanchored if a.scene_number in scene_map
    ]
    if not targets:
        return script

    prompt = ANCHOR_REWRITE_PROMPT.format(
        research_json=json.dumps({
            "summary": research.summary,
            "timeline": research.timeline,
            "key_facts": research.key_facts,
            "key_people": research.key_people,
        }, indent=1, ensure_ascii=False),
        scenes_json=json.dumps(targets, indent=1, ensure_ascii=False),
    )

    client = genai.Client(api_key=config.GEMINI_API_KEY)
    await asyncio.to_thread(pace_llm_call)
    response = await asyncio.to_thread(
        client.models.generate_content,
        model=config.GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.4,   # precision task — keep it conservative
            response_mime_type="application/json",
            response_schema={
                "type": "object",
                "properties": {
                    "scenes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "scene_number": {"type": "integer"},
                                "narration": {"type": "string"},
                            },
                            "required": ["scene_number", "narration"],
                        },
                    }
                },
                "required": ["scenes"],
            },
        ),
    )
    data = json.loads(response.text)
    fixed = 0
    for s in data.get("scenes", []):
        n = s.get("scene_number")
        if n in scene_map and s.get("narration", "").strip():
            scene_map[n].narration = s["narration"].strip()
            fixed += 1
    log.info(f"[bold green]Anchor rewrite:[/bold green] rewrote {fixed} scenes with unanchored claims")
    return script


@retry(max_attempts=2, base_delay=2.0, exceptions=(Exception,))
async def _targeted_rewrite(script, hits: list[tuple[int, str]]):
    """Rewrite only the scenes that still contain AI-tell phrases after the main pass."""
    scene_map = {s.scene_number: s for s in script.scenes}
    targets = [(n, p, scene_map[n].narration) for n, p in hits if n in scene_map]
    if not targets:
        return script

    prompt = (
        "Rewrite each narration to remove the flagged AI-sounding phrase while keeping "
        "all facts, numbers and names identical. Keep the delivery casual and human "
        "(contractions, varied sentence length). Return JSON.\n\n"
        "BANNED PHRASES: " + ", ".join(f'"{p}"' for p in AI_TELL_PHRASES) + "\n\n"
        "SCENES TO FIX:\n" + json.dumps(
            [{"scene_number": n, "flagged_phrase": p, "narration": t}
             for n, p, t in targets],
            indent=1, ensure_ascii=False,
        )
    )

    client = genai.Client(api_key=config.GEMINI_API_KEY)
    await asyncio.to_thread(pace_llm_call)
    response = await asyncio.to_thread(
        client.models.generate_content,
        model=config.GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.9,
            response_mime_type="application/json",
            response_schema={
                "type": "object",
                "properties": {
                    "scenes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "scene_number": {"type": "integer"},
                                "narration": {"type": "string"},
                            },
                            "required": ["scene_number", "narration"],
                        },
                    }
                },
                "required": ["scenes"],
            },
        ),
    )
    data = json.loads(response.text)
    fixed = 0
    for s in data.get("scenes", []):
        n = s.get("scene_number")
        if n in scene_map and s.get("narration", "").strip():
            scene_map[n].narration = s["narration"].strip()
            fixed += 1
    log.info(f"[bold green]Targeted rewrite:[/bold green] fixed {fixed} flagged scenes")
    return script


async def generate_script(
    research: ResearchResult,
    template: dict,
    max_retries: int = 2,
) -> Script:
    """
    Generate a structured video script from research results.

    Args:
        research: The research data gathered about the topic.
        template: The loaded template (e.g., celebrity.json merged with _base.json).
        max_retries: Max attempts to fix validation issues.

    Returns:
        A Script object with all scenes populated.
    """
    log.info(f"[bold blue]Generating script for:[/bold blue] {research.topic}")

    prompt = _build_prompt(research, template)
    data = await _call_gemini(prompt)

    # FIX-047: validation issues now drive a real rewrite loop — a script
    # under the word budget gets one shot at a fuller rewrite per retry,
    # with the specific problems fed back into the prompt.
    issues = _validate_script(data, template)
    for attempt in range(1, max_retries + 1):
        if not issues:
            break
        log.warning(f"Script validation issues (attempt {attempt}/{max_retries}): {issues}")
        data = await _call_gemini(
            prompt + "\n\nYour previous draft had these problems. Rewrite the "
            "COMPLETE script fixing all of them:\n- " + "\n- ".join(issues)
        )
        issues = _validate_script(data, template)
    if issues:
        # FIX-055: length problems are HARD blockers, quality problems are soft.
        # A too-short draft that slips through (e.g. every retry 503s into the
        # fallback chain, as on the SBF render) ships a 6-min video from an
        # 8-min template. On length failure, do ONE last targeted expansion
        # pass — send the existing draft back and demand longer scenes — and
        # only accept it if the budget is actually met; otherwise re-raise the
        # original draft choice to the caller via a loud warning + short flag.
        length_issues = [i for i in issues if "too short" in i.lower()]
        other_issues = [i for i in issues if "too short" not in i.lower()]
        if length_issues:
            log.warning("Length budget missed — running targeted expansion pass")
            expanded = await _call_gemini(
                prompt + "\n\nYour draft was TOO SHORT. Rewrite the COMPLETE "
                "script at full length. Keep the same structure and facts but "
                "expand every scene's narration with specific facts, dates, "
                "numbers and quotes until the total reaches the target word "
                "count. Fix:\n- " + "\n- ".join(length_issues)
            )
            exp_issues = _validate_script(expanded, template)
            if not [i for i in exp_issues if "too short" in i.lower()]:
                data = expanded
                issues = [i for i in exp_issues if "too short" not in i.lower()]
                log.info("Expansion pass met the word budget")
            else:
                log.error(
                    "Expansion pass STILL short — shipping short draft. "
                    "Consider re-running with --resume after model recovers."
                )
        issues = other_issues
    if issues:
        log.warning(f"Proceeding with best draft despite issues: {issues}")

    # Convert to Script model
    scenes = []
    running_time = 0.0
    chapters = [{"time": "00:00", "title": "The Hook"}]

    for s in data["scenes"]:
        dur = float(s.get("duration_target_seconds", 5.0))
        scene = Scene(
            scene_number=s["scene_number"],
            narration=s["narration"],
            visual_prompt=s["visual_prompt"],
            alternative_visual_prompts=s.get("alternative_visual_prompts", []),
            visual_type=s["visual_type"],
            mood=s.get("mood", "dramatic"),
            people_to_show=s.get("people_to_show", []),
            duration_target_seconds=dur,
            sfx_cue=s.get("sfx_cue", "whoosh"),
            broll_keywords=s.get("broll_keywords", []),
        )
        scenes.append(scene)

        # Milestone chapters every ~45s
        if running_time > 0 and int(running_time) % 45 < int(dur):
            mins = int(running_time) // 60
            secs = int(running_time) % 60
            chapters.append({
                "time": f"{mins:02d}:{secs:02d}",
                "title": f"Part {len(chapters)}: {scene.visual_prompt[:25]}..."
            })
        running_time += dur

    script = Script(
        title=data["title"],
        description=data["description"],
        tags=data.get("tags", []),
        hook=data.get("hook", ""),
        scenes=scenes,
        total_scenes=len(scenes),
        estimated_duration_minutes=data.get("estimated_duration_minutes", running_time / 60.0),
        loop_phrase=data.get("loop_phrase", None),
        chapters=chapters,
        pinned_comment=data.get("pinned_comment", None),
        hook_overlay_text=data.get("hook_overlay_text") or (
            " ".join(data.get("hook", data["title"]).split()[:5]).upper()
        ),
        title_variants=data.get("title_variants", []),
        midpoint_trigger=data.get("midpoint_trigger", None),
        bonus_payoff=data.get("bonus_payoff", None),
        midroll_markers=data.get("midroll_markers", []),
    )

    # ── Humanization v2 (FIX-005): second pass + deterministic enforcement ──
    if template.get("script", {}).get("humanize_pass", True):
        try:
            script = await _humanize_pass(script, template)
        except Exception as e:
            log.warning(f"Humanizer pass failed (keeping draft): {e}")

        remaining = _scan_ai_phrases(script)
        if remaining:
            log.warning(f"AI-tell phrases still present in {len(remaining)} scenes: "
                        f"{[p for _, p in remaining][:5]}")
            try:
                script = await _targeted_rewrite(script, remaining)
            except Exception as e:
                log.warning(f"Targeted rewrite failed: {e}; applying deterministic rewordings")
                for sn, phrase in _dedupe_rewrites(issues).items():
                    scene = next((s for s in script.scenes if s.scene_number == sn), None)
                    if scene:
                        replacement = _HUMAN_REWORDINGS.get(
                            phrase.lower(), "honestly",
                        )
                        scene.narration = re.sub(
                            re.escape(phrase), replacement,
                            scene.narration, flags=re.IGNORECASE,
                        )

        final_hits = _scan_ai_phrases(script)
        if final_hits:
            log.warning(
                f"[yellow]AI-tell phrases survived all passes in scenes "
                f"{[n for n, _ in final_hits]} — consider manual review[/yellow]"
            )
        else:
            log.info("[bold green]AI-tell scan clean:[/bold green] 0 flagged phrases")

    # ── Fact-anchor gate (Phase 2.6 / full A26) ──
    # Every hard number must trace to the research payload. Unanchored claims
    # get an LLM rewrite; if that fails, deterministic fallback strips them;
    # anything still failing is reported and saved for the operator. The video
    # itself still renders — no silent garbage, but also no hard stop.
    anchor_report = await _enforce_fact_anchors(script, research)
    script.anchor_report = anchor_report

    log.info(
        f"[bold green]Script ready:[/bold green] \"{script.title}\" — "
        f"{script.total_scenes} scenes, ~{script.estimated_duration_minutes:.1f} min"
    )
    return script


async def _enforce_fact_anchors(script: Script, research: ResearchResult) -> dict:
    """Validate → LLM rewrite → deterministic strip → report. Returns report dict."""
    report = validate_script_anchors(script, research)
    if report.anchors_checked == 0:
        log.info("[bold green]Anchor scan:[/bold green] no checkable claims in script")
        return report.as_dict()

    if not report.unanchored:
        log.info(
            f"[bold green]Anchor scan clean:[/bold green] "
            f"{report.anchors_checked}/{report.anchors_checked} claims anchored"
        )
        return report.as_dict()

    flagged = [(a.scene_number, a.text) for a in report.unanchored]
    log.warning(
        f"[yellow]Unanchored claims: {len(report.unanchored)}/{report.anchors_checked} "
        f"— {flagged[:6]}[/yellow]"
    )

    try:
        script = await _rewrite_unanchored(script, research, report.unanchored)
    except Exception as e:
        log.warning(f"Anchor rewrite pass failed: {e}; applying deterministic strip")

    # Deterministic fallback: replace the specific claim text with attributed
    # hedge wording so a hard unverifiable number never ships.
    recheck = validate_script_anchors(script, research)
    if recheck.unanchored:
        for a in recheck.unanchored:
            scene = next((s for s in script.scenes if s.scene_number == a.scene_number), None)
            if scene and a.text in scene.narration:
                hedge = {
                    "year": "in the years that followed" if False else "around that time",
                    "decade": "in that era",
                    "money": "a fortune reported in the press",
                    "percent": "a striking share",
                    "age": "at a young age" if False else "at that age",
                    "figure": "a striking number",
                }[a.claim_type]
                scene.narration = scene.narration.replace(a.text, hedge, 1)
        recheck = validate_script_anchors(script, research)

    remaining = len(recheck.unanchored)
    if remaining:
        log.warning(
            f"[yellow]{remaining} unanchored claim(s) survived all passes — "
            f"flagged in anchor_report for manual review[/yellow]"
        )
    else:
        log.info("[bold green]Anchor gate passed:[/bold green] all claims anchored after rewrites")
    return recheck.as_dict()


# ──────────────────────────────────────────────
# CLI test mode
# ──────────────────────────────────────────────
if __name__ == "__main__":
    import asyncio

    # Quick test with a mock ResearchResult
    test_research = ResearchResult(
        topic="The Rise and Fall of Enron",
        summary="Enron was an American energy company that became one of the largest corporate fraud scandals in history.",
        timeline=[
            {"date": "1985", "event": "Enron founded by Kenneth Lay"},
            {"date": "2001", "event": "Enron files for bankruptcy"},
        ],
        key_people=[
            {"name": "Kenneth Lay", "role": "Founder and CEO", "search_query": "Kenneth Lay Enron CEO"},
            {"name": "Jeffrey Skilling", "role": "CEO", "search_query": "Jeffrey Skilling Enron"},
        ],
        key_facts=[
            "Enron was the 7th largest company in America at its peak",
            "The scandal led to the dissolution of Arthur Andersen",
        ],
        narrative_arc="Rise from small energy company to corporate giant, followed by spectacular collapse due to fraud",
        source_queries=["Enron scandal timeline", "Enron bankruptcy"],
    )

    template = config.load_template("celebrity")
    script = asyncio.run(generate_script(test_research, template))
    print(f"\nTitle: {script.title}")
    print(f"Scenes: {script.total_scenes}")
    for s in script.scenes[:3]:
        print(f"  Scene {s.scene_number}: {s.narration[:80]}...")
