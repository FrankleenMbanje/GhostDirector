"""
GhostDirector — Caption Renderer

Generates ASS (Advanced SubStation Alpha) subtitle files with
word-by-word highlighting for animated captions.
"""

import math
import re
from pathlib import Path
from typing import Optional

from utils.logger import get_logger

log = get_logger("captions")


# ASS color format: &HBBGGRR& (reversed from RGB, hex)
def _rgb_to_ass(hex_color: str) -> str:
    """Convert #RRGGBB to ASS &HBBGGRR& format."""
    hex_color = hex_color.lstrip("#")
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    return f"&H00{b:02X}{g:02X}{r:02X}&"


def _seconds_to_ass_time(seconds: float) -> str:
    """Convert seconds to ASS timestamp format: H:MM:SS.CC"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int((seconds % 1) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_ts(seconds: float) -> str:
    """ASS timestamp H:MM:SS.cc (FIX-097 callout events)."""
    t = max(0.0, float(seconds))
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _with_alignment(line: str, an: int) -> str:
    """FIX-062: inject an ASS alignment override into an event's text field.

    Style functions emit `Dialogue: layer,start,end,Style,Name,ML,MR,MV,Effect,Text`
    with no alignment tag of their own, so the band is chosen per scene here
    (a scene whose subject sits low gets captions on the top band).
    """
    parts = line.split(",")
    if len(parts) < 10:
        return line
    text = ",".join(parts[9:])
    tag = f"{{\\an{an}"
    if text.startswith("{"):
        text = tag + text[1:]
    else:
        text = tag + "}" + text
    return ",".join(parts[:9] + [text])


def generate_ass_subtitles(
    all_timestamps: list[list[dict]],
    scene_offsets: list[float],
    output_path: Path,
    caption_config: dict,
    resolution: tuple[int, int] = (1920, 1080),
    hook_overlay_text: Optional[str] = None,
    band_overrides: Optional[dict[int, str]] = None,
    callouts: Optional[list[dict]] = None,
) -> Path:
    """
    Generate an ASS subtitle file with word-by-word highlighting and optional hook card.

    Args:
        all_timestamps: List of timestamp lists, one per scene.
        scene_offsets: Cumulative start time of each scene in the final video.
        output_path: Where to save the .ass file.
        caption_config: Template caption settings.
        resolution: Video resolution (width, height).
        hook_overlay_text: 5-7 word headline to show during the first 3 seconds for silent autoplay.
        band_overrides: FIX-062 per-scene caption band, {scene_number: "top"|"bottom"}.
            Decided from the rendered frame (utils.frame_occupancy) so captions
            never sit on the subject's face; the QC repair pass can rewrite it.
        callouts: FIX-097 kinetic fact callouts, each {"start", "end", "text",
            optionally "align"} — big number/date/money text that pops on the
            beat (the "55g", ">14K", "$30 MILLION" layer converting shorts use).
            Alignment defaults to top-center; the assembler flips it to bottom
            when the scene's captions were lifted to the top band.
    """
    width, height = resolution
    font = caption_config.get("font", "Montserrat-Bold")
    font_size = caption_config.get("font_size", 54)
    color = caption_config.get("color", "#FFFFFF")
    highlight_color = caption_config.get("highlight_color", "#FFE600")
    stroke_color = caption_config.get("stroke_color", "#000000")
    stroke_width = caption_config.get("stroke_width", 3)
    words_per_group = caption_config.get("words_per_group", 3)
    position = caption_config.get("position", "center")
    style = caption_config.get("style", "hormozi")

    is_vertical = height > width

    ass_color = _rgb_to_ass(color)
    ass_highlight = _rgb_to_ass(highlight_color if highlight_color else "#FFE600")
    ass_stroke = _rgb_to_ass(stroke_color)

    # Vertical safe-zone alignment
    if is_vertical:
        # FIX-062: BOTTOM band, not middle-centre. The old centre-anchored
        # block sat across the middle of the frame — exactly where the subject
        # sits in a portrait crop — and the post-render frame review caught it
        # live ("caption obscures subject face" / "covers mouth and chin").
        # A bottom band anchored ~20% up clears the subject AND YouTube's
        # bottom UI (title/channel), while the asymmetric side margins keep
        # the like/comment/share stack clear on the right.
        alignment = 2  # Bottom center
        margin_v = int(height * 0.20)
        margin_l = int(width * 0.08)   # 8% left margin
        margin_r = int(width * 0.16)   # 16% right margin (clearing like/share buttons)
        stroke_width = max(4, stroke_width)
        font_size = max(64, font_size)
    else:
        # 16:9 Landscape settings
        if position == "bottom":
            alignment = 2  # Bottom center
            margin_v = 70
        elif position == "top":
            alignment = 8  # Top center
            margin_v = 70
        else:
            alignment = 5  # Middle center
            margin_v = 50
        margin_l = 60
        margin_r = 60

    # ASS Header with Default and HookCard styles
    hook_font_size = int(font_size * 1.18)
    hook_margin_v = int(height * 0.20) if is_vertical else int(height * 0.14)
    # FIX-097: kinetic callouts are the biggest type on screen — they read at
    # a glance on a phone and carry no more than a few characters.
    callout_font_size = int(font_size * 1.5)
    callout_margin_v = int(height * 0.10) if is_vertical else int(height * 0.07)
    header = f"""[Script Info]
Title: GhostDirector Captions
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{font_size},{ass_color},{ass_highlight},{ass_stroke},&H90000000&,-1,0,0,0,100,100,0,0,1,{stroke_width},3,{alignment},{margin_l},{margin_r},{margin_v},1
Style: HookCard,{font},{hook_font_size},&H0000FFFF&,&H00FFFFFF&,&H00000000&,&H80000000&,-1,0,0,0,100,100,0,0,3,5,2,8,40,40,{hook_margin_v},1
Style: Callout,{font},{callout_font_size},{ass_color},{ass_highlight},{ass_stroke},&H80000000&,-1,0,0,0,100,100,0,0,1,5,2,8,60,60,{callout_margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    events = []

    # Insert silent autoplay Hook Card banner for seconds 0.0 to 3.0
    if hook_overlay_text:
        clean_hook = hook_overlay_text.strip().upper().replace("\n", " ")
        events.append(f"Dialogue: 1,0:00:00.00,0:00:03.00,HookCard,,0,0,0,,{{\\fad(120,300)}}{clean_hook}")

    # FIX-097: callout overlays sit ABOVE the caption events (layer 2) so a
    # dense caption block can never hide the number the viewer scans for.
    for c in (callouts or []):
        try:
            txt = str(c.get("text", "")).strip().upper()
            st = float(c.get("start", 0.0))
            en = float(c.get("end", st + 1.8))
            if not txt or en <= st:
                continue
            an = int(c.get("align", 8) or 8)
            events.append(
                f"Dialogue: 2,{_ass_ts(st)},{_ass_ts(en)},Callout,,0,0,0,,"
                f"{{\\an{an}\\fad(140,160)}}{txt}")
        except Exception:
            continue

    for scene_idx, (timestamps, offset) in enumerate(zip(all_timestamps, scene_offsets)):
        if not timestamps:
            continue

        if style == "editorial":
            # FIX-058: news-editorial restraint — selective emphasis, no
            # per-word pop, no all-caps. Available for vertical too: the
            # style is about typography discipline, not orientation.
            scene_events = _editorial_news_style(
                timestamps, offset, words_per_group, ass_highlight, ass_color)
        elif style in ["hormozi", "shorts"] or is_vertical:
            # Group words into 2-3 words per beat and apply kinetic pop
            words_per_beat = min(words_per_group, 3) if is_vertical else words_per_group
            scene_events = _hormozi_style(timestamps, offset, words_per_beat, ass_highlight, ass_color)
        elif style == "documentary":
            scene_events = _simple_style(timestamps, offset)
        else:
            scene_events = _simple_style(timestamps, offset)

        # FIX-062: per-scene band, written EXPLICITLY into every vertical
        # event so the .ass records the placement decision (a style edit can
        # never silently move a scene's captions back over a face). Landscape
        # templates keep their template-authored position untouched.
        if is_vertical:
            override = (band_overrides or {}).get(scene_idx + 1)
            an = 8 if override == "top" else 2
            scene_events = [_with_alignment(e, an) for e in scene_events]
        events += scene_events

    # Write ASS file
    ass_content = header + "\n".join(events) + "\n"
    output_path.write_text(ass_content, encoding="utf-8")
    log.info(f"[bold green]Captions generated:[/bold green] {output_path.name} ({len(events)} events)")
    return output_path


def _is_keyword(raw_word: str) -> bool:
    """Detect emphasis-worthy words from ORIGINAL casing (before .upper()).

    Emphasize: numbers (dates, dollar figures, counts), percentages/money,
    and initialisms (FBI, NASA, CEO) — the hard specifics viewers scan for.
    """
    w = (raw_word or "").strip().strip(",.!?;:\"'()“”")
    if not w:
        return False
    if any(ch.isdigit() for ch in w):
        return True
    if re.fullmatch(r"[A-Z][A-Z&-]{1,}", w):  # FBI, NASA, CEO, UFC
        return True
    return False


# FIX-058: celebrity names get quiet emphasis in news style (bold white,
# never oversized, never a different hue unless the template demands).
_TITLES = {
    "mr", "mrs", "ms", "dr", "prof", "sir", "dame", "lord", "lady",
    "president", "sen", "senator", "gov", "judge",
}


def _person_name_word(raw_word: str, prev_raw: str | None) -> bool:
    """A capitalized non-sentence-start word — very likely a proper name."""
    w = (raw_word or "").strip().strip(",.!?;:\"'()“”")
    if not w or len(w) < 3:
        return False
    if w.lower() in _TITLES:
        return True
    if not w[0].isupper() or not w[1:].islower():
        return False
    # Internal capitals (McDonald, O'Brien, Depp) are always names
    if any(c.isupper() for c in w[1:]):
        return True
    if prev_raw:
        p = prev_raw.strip().strip(",.!?;:\"'()“”")
        if p and p[0].isupper():
            return True  # follows another capitalized word
    return False


def _editorial_news_style(
    timestamps: list[dict],
    offset: float,
    words_per_group: int,
    highlight_color: str,
    normal_color: str,
) -> list[str]:
    """FIX-058: NEWS-EDITORY captions — the anti-kids-template style.

    Restraint rules:
      - ALL-CAPS is dropped (sentence case reads editorial, not shouty);
      - emphasis is SELECTIVE: numbers/dates (color+bold), names (bold
        only), one key phrase per scene at most (color);
      - NO per-word scale-pop animation — the active word brightens, the
        rest hold a calm secondary gray;
      - group size 4-6 words so the eye reads phrases, not confetti.
    The viewer follows the story and the celebrity — not the captions.
    """
    events = []
    ass_dim = _rgb_to_ass("#B8B8B8")
    group_size = max(4, min(6, words_per_group + 2))

    clamped = []
    prev_end = 0.0
    for w in timestamps:
        start = max(float(w["start"]), prev_end)
        end = max(float(w["end"]), start + 0.08)
        clamped.append({"word": w["word"], "start": start, "end": end})
        prev_end = end

    groups = []
    for i in range(0, len(clamped), group_size):
        groups.append(clamped[i : i + group_size])

    phrase_used = False  # one color-emphasis phrase per scene max
    for group in groups:
        prev_raw = group[0]["word"] if group else None
        kinds = []
        for j, wd in enumerate(group):
            raw = wd["word"]
            if _is_keyword(raw):
                kinds.append("number")          # date/amount/figure: color+bold
            elif _person_name_word(raw, prev_raw if j else None):
                kinds.append("name")            # person: bold only
            elif (not phrase_used and j >= len(group) - 2
                  and raw.strip(".,!?;:\"'").isalpha()
                  and len(raw) >= 5
                  and raw[0].isupper()
                  and j > 0):
                kinds.append("phrase")          # one closing key phrase
                phrase_used = True
            else:
                kinds.append("plain")
            prev_raw = raw

        # One event per GROUP (reading rhythm: phrases, not strobe words)
        text_parts = []
        for j, wd in enumerate(group):
            word = wd["word"]
            k = kinds[j]
            if k == "number":
                text_parts.append(f"{{\\c{highlight_color}\\b1}}{word}{{\\r}}")
            elif k == "name":
                text_parts.append(f"{{\\b1}}{word}{{\\r}}")
            elif k == "phrase":
                text_parts.append(f"{{\\c{highlight_color}}}{word}{{\\r}}")
            else:
                text_parts.append(word)
        text = " ".join(text_parts)

        start = _seconds_to_ass_time(group[0]["start"] + offset)
        end = _seconds_to_ass_time(group[-1]["end"] + offset)
        events.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{{\\fad(80,80)}}{text}")

    return events


def _hormozi_style(
    timestamps: list[dict],
    offset: float,
    words_per_group: int,
    highlight_color: str,
    normal_color: str,
    dim_color: str = "#8A8A8A",
) -> list[str]:
    """
    Hormozi-style kinetic captions (FIX-009 overhaul, FIX-045 VidRush pass):
      - every event carries \\fad(60,60) so consecutive word states
        cross-dissolve instead of hard-blinking at word/group boundaries;
      - INACTIVE words render DIMMED (gray, FIX-045) — the active word owns
        the eye, exactly the VidRush/signature short-form look, instead of
        every word fighting at full white;
      - the active word POPS with overshoot: scales to 118% in 90ms then
        settles to 104% by 180ms (VidRush punch), against the old single
        105→112 ramp;
      - keywords (numbers, %, initialisms) stay GOLD even when not active,
        so hard specifics keep visual weight across the whole group.

    Word timing comes from Whisper, so highlight transitions track the
    narration exactly; the fade bridges any micro-gaps between words.
    """
    events = []
    ass_dim = _rgb_to_ass(dim_color)

    # Group words into chunks
    groups = []
    for i in range(0, len(timestamps), words_per_group):
        group = timestamps[i : i + words_per_group]
        groups.append(group)

    # FIX: enforce a strictly monotonic, non-overlapping word timeline.
    # Whisper word timings can round/overlap across segment boundaries;
    # without clamping, two caption groups render simultaneously and
    # stack on top of each other at the center anchor.
    clamped = []
    prev_end = 0.0
    for w in timestamps:
        start = max(float(w["start"]), prev_end)
        end = max(float(w["end"]), start + 0.08)  # keep every word legible
        clamped.append({"word": w["word"], "start": start, "end": end})
        prev_end = end

    # Regroup from the clamped stream so group boundaries respect the clamp
    groups = []
    for i in range(0, len(clamped), words_per_group):
        groups.append(clamped[i : i + words_per_group])

    for group in groups:
        is_keyword = [_is_keyword(w["word"]) for w in group]

        # For each word in the group, create a dialogue event that shows
        # all words but pops the current one.
        for word_idx, word_data in enumerate(group):
            word_start = word_data["start"] + offset
            word_end = word_data["end"] + offset
            # Word must end at or before the next word starts — already
            # guaranteed by the global clamp, but a local group check is
            # cheap insurance against any future regression.
            if word_idx + 1 < len(group):
                word_end = min(word_end, group[word_idx + 1]["start"] + offset)

            parts = []
            for j, w in enumerate(group):
                clean_word = w["word"].upper()
                if j == word_idx:
                    # Active: highlight + overshoot pop (100→118→104)
                    parts.append(
                        f"{{\\c{highlight_color}\\b1\\fscx100\\fscy100"
                        f"\\t(0,90,\\fscx118\\fscy118)"
                        f"\\t(90,180,\\fscx104\\fscy104)}}{clean_word}{{\\r}}"
                    )
                elif is_keyword[j]:
                    # Inactive but emphasized: persistent gold, no motion
                    parts.append(f"{{\\c{highlight_color}}}{clean_word}{{\\r}}")
                else:
                    # Inactive filler: dimmed so the active word owns the eye
                    parts.append(f"{{\\c{ass_dim}}}{clean_word}{{\\r}}")

            text = " ".join(parts)
            start_ts = _seconds_to_ass_time(word_start)
            end_ts = _seconds_to_ass_time(word_end)
            events.append(f"Dialogue: 0,{start_ts},{end_ts},Default,,0,0,0,,{{\\fad(60,60)}}{text}")

    return events


def _simple_style(timestamps: list[dict], offset: float) -> list[str]:
    """Simple captions: show each word as it's spoken."""
    events = []
    # Clamp overlapping word timings first (same guarantee as hormozi style):
    # two visible events at once on the same anchor reads as stacked garbage.
    clamped = []
    prev_end = 0.0
    for w in timestamps:
        start = max(float(w["start"]), prev_end)
        end = max(float(w["end"]), start + 0.08)
        clamped.append({"word": w["word"], "start": start, "end": end})
        prev_end = end
    timestamps = clamped
    # Group into sentences (by looking for punctuation or every ~8 words)
    sentence = []
    for word_data in timestamps:
        sentence.append(word_data)
        if len(sentence) >= 8 or word_data["word"].rstrip().endswith((".", "!", "?")):
            text = " ".join(w["word"] for w in sentence)
            start = _seconds_to_ass_time(sentence[0]["start"] + offset)
            end = _seconds_to_ass_time(sentence[-1]["end"] + offset)
            events.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")
            sentence = []

    # Remaining words
    if sentence:
        text = " ".join(w["word"] for w in sentence)
        start = _seconds_to_ass_time(sentence[0]["start"] + offset)
        end = _seconds_to_ass_time(sentence[-1]["end"] + offset)
        events.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")

    return events
