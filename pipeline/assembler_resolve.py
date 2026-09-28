"""
GhostDirector — DaVinci Resolve Export

Produces a 1:1 editable recreation of the GhostDirector edit inside DaVinci
Resolve (Free and Studio). The timeline is exported as FCPXML (1.9), which
Resolve imports as a *real editable timeline* — every punch-cut, the hook
rewind, narration audio lanes, SFX placements, music, and chapter markers.

Exports per project (resolve_project/):
  timeline.fcpxml          FCPXML timeline (video spine + audio lanes + markers)
  captions.srt             Same captions the render burns in — imports as an
                           editable subtitle track in Resolve
  timeline.edl             CMX 3600 EDL fallback for NLEs without FCPXML import
  open_in_resolve.py       Live automation script (Studio external scripting)
  HOW_TO_OPEN_IN_DAVINCI.md

Design notes:
- The spine places every individual cut file (hook rewind, per-scene punch-cuts,
  prepared clips) in edit order, matching the FFmpeg concat order. The 0.2-0.4s
  xfades are baked into scene start offsets; they are noted in markers, not
  rebuilt (Resolve can re-add transitions in one click).
- Spine media is video-only by construction (cut/prepared files carry no audio);
  narration mp3s ride audio lane 1 at their transition-aware scene starts.
- The rendered music already has ducking/fade baked in by ffmpeg; Resolve does
  not rebuild sidechain chains, so the source music track is placed on lane 3
  for the operator to re-level if they edit.
"""

import sys
import os
import json
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from xml.sax.saxutils import escape as _xml_escape

sys.path.insert(0, str(Path(__file__).parent.parent))

from models import Script
import config
from utils.logger import get_logger

log = get_logger("assembler_resolve")

PYTHON_313_EXE = r"C:\Users\frank\AppData\Local\Programs\Python\Python313\python.exe"

AUDIO_EXTENSIONS = {".mp3", ".wav", ".aac", ".m4a", ".flac", ".ogg", ".wma"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff"}


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _x(value: float) -> str:
    """Seconds → rational time string on a 3000/s base (divides 30/25/24fps and ms exactly)."""
    ticks = int(round(float(value) * 3000))
    if ticks < 0:
        ticks = 0
    return f"{ticks}/3000s"


def _esc(text) -> str:
    """Escape text for use inside an XML attribute."""
    return _xml_escape(str(text), {'"': "&quot;"})


def _sec(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _abs_media(path: str | Path) -> str:
    p = Path(path)
    try:
        return str(p.resolve())
    except Exception:
        return str(p)


def _file_url(path: str | Path) -> str:
    """Proper file:/// URI (Windows-safe: file:///C:/Users/...)."""
    p = Path(path)
    try:
        return p.resolve().as_uri()
    except Exception:
        return "file:///" + str(p).replace("\\", "/").lstrip("/")


def _format_timecode(seconds: float, fps: int = 30) -> str:
    """Convert seconds into HH:MM:SS:FF timecode."""
    total_frames = int(round(seconds * fps))
    ff = total_frames % fps
    total_seconds = total_frames // fps
    ss = total_seconds % 60
    total_minutes = total_seconds // 60
    mm = total_minutes % 60
    hh = total_minutes // 60
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def _timestamp_to_seconds(ts: str) -> float | None:
    """'03:30' / '1:02:03' → seconds. None when unparseable."""
    parts = str(ts or "").strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if not nums or any(n < 0 for n in nums):
        return None
    secs = 0.0
    for n in nums:
        secs = secs * 60 + n
    return secs


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _probe_audio(path: Path) -> tuple[int, int] | None:
    """(channels, sample_rate) via ffprobe, or None when unavailable."""
    try:
        result = subprocess.run(
            [config.FFPROBE_BIN, "-v", "quiet", "-print_format", "json",
             "-show_streams", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            streams = json.loads(result.stdout or "{}").get("streams", [])
            for s in streams:
                if s.get("codec_type") == "audio":
                    return (
                        int(s.get("channels", 2) or 2),
                        int(s.get("sample_rate", 44100) or 44100),
                    )
    except Exception:
        pass
    return None


def _media_duration(path: Path, fallback: float = 1.2) -> float:
    """Duration via ffprobe, with a sane fallback."""
    try:
        from utils.ffmpeg_cmd import get_duration
        d = get_duration(path)
        if d and d > 0.05:
            return d
    except Exception:
        pass
    return fallback


def _resolve_existing(path_str: str, edit_media_dir: Path) -> Path | None:
    """Return an existing media path, preferring preserved copies in edit_media/."""
    p = Path(path_str)
    if p.exists():
        return p
    alt = edit_media_dir / p.name
    if alt.exists():
        return alt
    return None


# ──────────────────────────────────────────────
# SRT export (captions as an editable subtitle track)
# ──────────────────────────────────────────────

def generate_srt(
    script: Script,
    scene_offsets: list[float] | None,
    output_path: Path,
    max_words: int = 3,
) -> Path:
    """Write the burned-in captions as a standard SRT for Resolve's subtitle track.

    Word groups mirror the burned caption beats (<=3 words) when word timestamps
    exist; scenes without timestamps fall back to one cue per narration sentence
    paced at the scene's own speaking rate.
    """
    offsets = scene_offsets or [0.0] * len(script.scenes)
    import re

    def _fmt(ts: float) -> str:
        total_ms = int(round(max(0.0, ts) * 1000))
        h, rem = divmod(total_ms, 3600000)
        m, rem = divmod(rem, 60000)
        s, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    events: list[tuple[float, float, str]] = []

    for scene_idx, scene in enumerate(script.scenes):
        stamps = scene.timestamps or []
        offset = offsets[scene_idx] if scene_idx < len(offsets) else 0.0

        if not stamps:
            narration = scene.narration or ""
            est_words = len(narration.split())
            est_dur = scene.audio_duration_seconds or scene.duration_target_seconds or 5.0
            wps = max(1.0, est_words / max(0.5, est_dur))
            sent_start = 0.0
            for sent in re.split(r"(?<=[.!?])\s+", narration):
                sent = sent.strip()
                if not sent:
                    continue
                sent_dur = max(0.8, len(sent.split()) / wps)
                events.append((offset + sent_start, offset + sent_start + sent_dur, sent))
                sent_start += sent_dur
            continue

        for i in range(0, len(stamps), max_words):
            group = stamps[i : i + max_words]
            text = " ".join(w["word"] for w in group).upper()
            start = group[0]["start"] + offset
            end = group[-1]["end"] + offset
            events.append((start, end, text))

    events.sort(key=lambda e: e[0])

    lines: list[str] = []
    for i, (st, en, txt) in enumerate(events, start=1):
        lines.append(f"{i}")
        lines.append(f"{_fmt(st)} --> {_fmt(en)}")
        lines.append(txt)
        lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    log.info(f"[bold green]SRT generated:[/bold green] {output_path.name} ({len(events)} cues)")
    return output_path


# ──────────────────────────────────────────────
# FCPXML timeline export
# ──────────────────────────────────────────────

def generate_fcpxml(
    script: Script,
    template: dict,
    output_dir: Path,
    timeline: dict | None = None,
) -> Path:
    """Generate timeline.fcpxml — a real, editable DaVinci Resolve timeline.

    Structure:
      - spine: hook rewind (if any) + every scene's cut files in edit order
      - audio lane 1: narration mp3s offset to scene starts
      - audio lane 2: SFX clips at their rendered placements
      - audio lane 3: music (full length)
      - markers: chapters (blue) + midpoint (orange)
    """
    tl = timeline or {}
    visuals_cfg = template.get("visuals", {})
    fps = int(visuals_cfg.get("fps", 30))

    width, height = 1920, 1080
    res = tl.get("resolution") or visuals_cfg.get("resolution", "1920x1080")
    try:
        w_h = str(res).split("x")
        width, height = int(w_h[0]), int(w_h[1])
    except Exception:
        pass

    hook_sec = _sec(tl.get("hook_rewind_seconds"))
    scene_starts = {s["scene_number"]: _sec(s.get("start_seconds")) for s in tl.get("scenes", [])}
    edit_media_dir = Path(tl.get("edit_media_dir") or (output_dir / "edit_media"))
    music_track = tl.get("music_track")
    sfx_placements = tl.get("sfx_placements", []) or []

    scene_by_number = {s.scene_number: s for s in script.scenes}

    assets: dict[str, dict] = {}      # abspath → asset row

    def _asset_id_for(abs_path: str) -> str:
        if abs_path not in assets:
            p = Path(abs_path)
            assets[abs_path] = {
                "id": f"a{len(assets) + 1}",
                "src": abs_path,
                "name": p.name,
            }
        return assets[abs_path]["id"]

    spine_clips: list[str] = []
    timeline_pos = 0.0

    # ── Hook rewind (video-only reverse of the first scene's first cut) ──
    if hook_sec > 0 and tl.get("scenes"):
        first_cuts = (tl["scenes"][0].get("cuts") or [])
        hook_src = None
        for c in first_cuts:
            hook_src = _resolve_existing(c.get("file", ""), edit_media_dir)
            if hook_src:
                break
        if hook_src:
            aid = _asset_id_for(_abs_media(hook_src))
            spine_clips.append(
                f'        <asset-clip name="Hook Rewind" ref="{aid}" offset="{_x(0)}" '
                f'start="{_x(0)}" duration="{_x(hook_sec)}" />'
            )
            timeline_pos = hook_sec
            hook_note = (
                f'      <marker start="{_x(0)}" duration="{_x(0)}" '
                f'value="Hook rewind (reverse of first 3s) — re-add a 2-frame dissolve here if desired" />'
            )
        else:
            hook_note = None
    else:
        hook_note = None

    # ── Spine: scenes in edit order ──
    for scene_row in tl.get("scenes", []):
        sn = scene_row["scene_number"]
        scene = scene_by_number.get(sn)
        dur = _sec(scene_row.get("duration_seconds")) or (
            (scene.audio_duration_seconds if scene else None) or 5.0
        )
        start = _sec(scene_row.get("start_seconds"))

        placed = False
        cuts = scene_row.get("cuts") or []
        if cuts:
            # Punch-cut scene: each sub-cut is its own spine clip at its own
            # timeline offset (they were concat-joined in the render).
            for k, c in enumerate(cuts):
                f = _resolve_existing(c.get("file", ""), edit_media_dir)
                cs = _sec(c.get("start_seconds"))
                cl = _sec(c.get("duration_seconds"))
                if f:
                    aid = _asset_id_for(_abs_media(f))
                    spine_clips.append(
                        f'        <asset-clip name="Scene {sn} cut {k + 1}" ref="{aid}" '
                        f'offset="{_x(cs)}" start="{_x(0)}" duration="{_x(cl)}" '
                        f'note="punch-cut {k + 1}/{len(cuts)} of scene {sn}" />'
                    )
                    placed = True
        if not placed:
            # Fallback: the scene's concatenated prepared clip
            prepared = edit_media_dir / f"scene_{sn:02d}_prepared.mp4"
            if not prepared.exists() and scene and scene.video_path:
                prepared = Path(scene.video_path)
            if prepared.exists():
                aid = _asset_id_for(_abs_media(prepared))
                spine_clips.append(
                    f'        <asset-clip name="Scene {sn}" ref="{aid}" '
                    f'offset="{_x(start)}" start="{_x(0)}" duration="{_x(dur)}" />'
                )
                placed = True
        if not placed:
            spine_clips.append(
                f'        <gap name="Missing media scene {sn}" offset="{_x(start)}" duration="{_x(dur)}" />'
            )

    total_duration = timeline_pos if not tl.get("scenes") else (
        hook_sec + sum(_sec(s.get("duration_seconds")) for s in tl.get("scenes", []))
    )
    if total_duration <= 0:
        total_duration = max(
            (s.audio_duration_seconds or s.duration_target_seconds or 5.0)
            for s in script.scenes
        ) * len(script.scenes)

    # ── Audio lane 1: narration ──
    narration_clips: list[str] = []
    for scene in script.scenes:
        if not (scene.audio_path and Path(scene.audio_path).exists()):
            continue
        sn = scene.scene_number
        start = scene_starts.get(sn, 0.0)
        dur = scene.audio_duration_seconds or scene.duration_target_seconds or 5.0
        aid = _asset_id_for(_abs_media(scene.audio_path))
        narration_clips.append(
            f'          <asset-clip name="VO Scene {sn}" ref="{aid}" lane="1" '
            f'offset="{_x(start)}" start="{_x(0)}" duration="{_x(dur)}" '
            f'audioRole="dialogue" />'
        )

    # ── Audio lane 2: SFX placements ──
    sfx_clips: list[str] = []
    for p in sfx_placements:
        f = _resolve_existing(p.get("file", ""), edit_media_dir)
        if not f:
            continue
        off = _sec(p.get("offset_seconds"))
        dur = _media_duration(f, fallback=1.2)
        cue = _esc(p.get("cue", "sfx"))
        note = _esc(p.get("note", "rendered at 0.35 volume, ducked under narration"))
        aid = _asset_id_for(_abs_media(f))
        sfx_clips.append(
            f'          <asset-clip name="{cue}" ref="{aid}" lane="2" '
            f'offset="{_x(off)}" start="{_x(0)}" duration="{_x(dur)}" '
            f'audioRole="effects" note="{note}" />'
        )

    # ── Audio lane 3: music ──
    music_clips: list[str] = []
    if music_track:
        mp = Path(music_track)
        if not mp.exists():
            mp = edit_media_dir / mp.name
        if mp.exists():
            dur = min(_media_duration(mp, fallback=total_duration), total_duration)
            aid = _asset_id_for(_abs_media(mp))
            music_clips.append(
                f'          <asset-clip name="Music" ref="{aid}" lane="3" '
                f'offset="{_x(0)}" start="{_x(0)}" duration="{_x(dur)}" '
                f'audioRole="music" note="ducked+faded in the render; re-level if re-editing" />'
            )

    # ── Markers: chapters + midpoint ──
    markers: list[str] = []
    if hook_note:
        markers.append(hook_note)
    for ch in (script.chapters or []):
        secs = _timestamp_to_seconds(ch.get("timestamp", ""))
        if secs is None:
            continue
        markers.append(
            f'      <marker start="{_x(secs)}" duration="{_x(0)}" '
            f'value="Chapter: {_esc(ch.get("title", ""))}" />'
        )
    midroll = getattr(script, "midroll_markers", None) or []
    for ts in midroll:
        secs = _timestamp_to_seconds(ts)
        if secs is None:
            continue
        markers.append(
            f'      <marker start="{_x(secs)}" duration="{_x(0)}" '
            f'value="Mid-roll ad cue" />'
        )
    midpoint = total_duration / 2.0
    markers.append(
        f'      <marker start="{_x(midpoint)}" duration="{_x(0)}" '
        f'value="Midpoint (attention turn)" />'
    )

    # ── Asset rows ──
    asset_rows: list[str] = []
    for row in assets.values():
        p = Path(row["src"])
        suff = p.suffix.lower()
        if suff in IMAGE_EXTENSIONS:
            asset_rows.append(
                f'    <asset id="{row["id"]}" name="{_esc(row["name"])}" '
                f'src="{_file_url(row["src"])}" hasAudio="0" format="r1" />'
            )
            continue
        audio_probe = _probe_audio(p) if suff in AUDIO_EXTENSIONS else None
        if suff in AUDIO_EXTENSIONS or (audio_probe and suff not in AUDIO_EXTENSIONS and suff not in (".mp4", ".mov", ".mkv")):
            channels, rate = audio_probe or (2, 44100)
            asset_rows.append(
                f'    <asset id="{row["id"]}" name="{_esc(row["name"])}" '
                f'src="{_file_url(row["src"])}" audioSources="1" '
                f'audioChannels="{channels}" audioRate="{rate}" />'
            )
        else:
            asset_rows.append(
                f'    <asset id="{row["id"]}" name="{_esc(row["name"])}" '
                f'src="{_file_url(row["src"])}" hasAudio="0" format="r1" />'
            )

    doc = f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- Generated by GhostDirector on {_now_rfc3339()} — edit freely in DaVinci Resolve -->
<fcpxml version="1.9">
  <resources>
    <format id="r1" name="FFVideoFormat{height}p{fps}" frameDuration="1/{fps}s" width="{width}" height="{height}" colorSpace="1-1-1 (Rec. 709)" />
{chr(10).join(asset_rows)}
  </resources>
  <library>
    <event name="GhostDirector">
      <project name="{_esc(script.title[:60])}">
        <sequence format="r1" duration="{_x(total_duration)}" tcFormat="NDF" audioLayout="stereo" audioRate="48k">
{chr(10).join(markers)}
          <spine>
{chr(10).join(spine_clips)}
          </spine>
          <audio>
{chr(10).join(narration_clips)}
{chr(10).join(sfx_clips)}
{chr(10).join(music_clips)}
          </audio>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
"""

    out = output_dir / "timeline.fcpxml"
    out.write_text(doc, encoding="utf-8")
    log.info(
        f"[bold green]FCPXML generated:[/bold green] {out.name} "
        f"({len(assets)} media files, {len(spine_clips)} spine clips, "
        f"{len(narration_clips)} VO + {len(sfx_clips)} SFX on audio lanes)"
    )
    return out


# ──────────────────────────────────────────────
# CMX 3600 EDL (fallback for other NLEs)
# ──────────────────────────────────────────────

def generate_edl(script: Script, template: dict, output_dir: Path) -> Path:
    """Generate a standard CMX 3600 EDL file (video cuts + voice audio)."""
    visuals_cfg = template.get("visuals", {})
    fps = int(visuals_cfg.get("fps", 30))
    safe_title = "".join(c if c.isalnum() or c in "_-" else "_" for c in script.title)[:30]

    # Transition-aware record times when the assembler's manifest exists
    offsets: list[float] | None = None
    tl_path = output_dir.parent / "timeline.json"
    if tl_path.exists():
        try:
            tl = json.loads(tl_path.read_text(encoding="utf-8"))
            offsets = [_sec(s.get("start_seconds")) for s in tl.get("scenes", [])]
        except Exception:
            offsets = None

    edl_lines = [
        f"TITLE: GhostDirector_{safe_title}",
        "FCM: NON-DROP FRAME",
        "",
    ]

    record_in_sec = 0.0
    event_no = 0

    for i, scene in enumerate(script.scenes):
        media_path = scene.video_path or scene.photo_path
        if not media_path:
            continue
        event_no += 1
        rec_start = offsets[i] if offsets and i < len(offsets) else record_in_sec
        dur = scene.audio_duration_seconds or scene.duration_target_seconds or 5.0

        src_in = "00:00:00:00"
        src_out = _format_timecode(dur, fps)
        rec_in = _format_timecode(rec_start, fps)
        rec_out = _format_timecode(rec_start + dur, fps)

        reel = f"AX{event_no:02d}"
        edl_lines.append(f"{event_no:03d}  {reel}  V     C        {src_in} {src_out} {rec_in} {rec_out}")
        edl_lines.append(f"* FROM CLIP NAME: {Path(media_path).name}")
        edl_lines.append("")

        if scene.audio_path and Path(scene.audio_path).exists():
            edl_lines.append(f"{event_no:03d}  {reel}  A     C        {src_in} {src_out} {rec_in} {rec_out}")
            edl_lines.append(f"* FROM CLIP NAME: {Path(scene.audio_path).name}")
            edl_lines.append("")

        record_in_sec += dur

    edl_path = output_dir / "timeline.edl"
    edl_path.write_text("\n".join(edl_lines), encoding="utf-8")
    return edl_path


# ──────────────────────────────────────────────
# Live push into Resolve Studio (external scripting — Studio only)
# ──────────────────────────────────────────────
# FIX-034: zero-click hand-off. The pipeline writes a self-contained push
# script + manifest; when Resolve Studio is running (Preferences → System →
# General → External Scripting → Local) the script creates the project,
# imports the media pool, imports the FCPXML timeline (the exact edit) and
# the captions SRT, and optionally queues a Studio render.

RESOLVE_PYTHON = os.environ.get(
    "RESOLVE_PYTHON", PYTHON_313_EXE
)
RESOLVE_INSTALL_DIRS = [
    Path(r"C:\Program Files\Blackmagic Design\DaVinci Resolve"),
    Path(r"C:\Program Files\Blackmagic Design\DaVinci Resolve Studio"),
]


def resolve_studio_installed() -> bool:
    """Cheap check: does a Studio install with the scripting bridge exist?"""
    for d in RESOLVE_INSTALL_DIRS:
        if (d / "fusionscript.dll").exists():
            return True
    return False


def _collect_push_media(script: Script, tl_data: dict, output_dir: Path) -> list[str]:
    """Every existing media file the timeline references (deduped, absolute)."""
    files: list[str] = []
    edit_media_dir = Path(tl_data.get("edit_media_dir") or (output_dir / "edit_media"))

    def _add(p: str | Path | None):
        if not p:
            return
        path = Path(p)
        if not path.exists():
            alt = edit_media_dir / path.name
            if alt.exists():
                path = alt
            else:
                return
        try:
            ap = str(path.resolve())
        except Exception:
            ap = str(path)
        if ap not in files:
            files.append(ap)

    for row in tl_data.get("scenes", []):
        for c in row.get("cuts") or []:
            _add(c.get("file"))
        sn = row.get("scene_number")
        if sn is not None:
            _add(edit_media_dir / f"scene_{sn:02d}_prepared.mp4")
    for s in script.scenes:
        _add(s.audio_path)
    for p in tl_data.get("sfx_placements") or []:
        _add(p.get("file"))
    _add(tl_data.get("music_track"))
    return files


def generate_resolve_script(
    script: Script,
    template: dict,
    resolve_dir: Path,
    tl_data: dict | None = None,
    render: bool = False,
) -> tuple[Path, Path]:
    """Write open_in_resolve.py (the push script) + push_manifest.json.

    The push script is self-contained (stdlib only, py3.6-compatible) and runs
    under Resolve's scripting Python: it connects, creates/loads the project,
    imports media, imports the FCPXML timeline, best-effort imports the SRT as
    a subtitle track, and optionally queues + completes a Studio render.
    """
    visuals_cfg = template.get("visuals", {})
    fps = int(visuals_cfg.get("fps", 30))
    res = (tl_data or {}).get("resolution") or visuals_cfg.get("resolution", "1920x1080")
    try:
        width, height = (int(v) for v in str(res).split("x"))
    except Exception:
        width, height = 1920, 1080
    safe_title = "".join(c if c.isalnum() or c in "_-" else "_" for c in script.title)[:40]

    fcpxml = resolve_dir / "timeline.fcpxml"
    srt = resolve_dir / "captions.srt"
    manifest = {
        "project_name": f"GhostDirector_{safe_title}",
        "fps": fps,
        "width": width,
        "height": height,
        "media": _collect_push_media(script, tl_data or {}, resolve_dir.parent),
        "fcpxml": str(fcpxml),
        "srt": str(srt) if srt.exists() else None,
        "render": bool(render),
        "render_dir": str(resolve_dir.parent),
        "render_name": safe_title.lower(),
    }
    manifest_path = resolve_dir / "push_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    code = '''#!/usr/bin/env python
"""GhostDirector -> DaVinci Resolve Studio push (auto-generated; do not edit).

Usage (run with Resolve's scripting Python 3.6-3.13, e.g. the Python 3.13
interpreter installed alongside Resolve Studio):
    python open_in_resolve.py --manifest push_manifest.json          # push
    python open_in_resolve.py --manifest push_manifest.json --render # push + render
    python open_in_resolve.py --manifest push_manifest.json --probe  # connection check

Requires Resolve Studio running with Preferences -> System -> General ->
External Scripting set to Local.
Prints RESOLVE_OK on success, RESOLVE_FAIL otherwise (machine-readable).
"""

import os
import sys
import json
import time

# fusionscript.dll is built for Python 3.6-3.13; loading it from 3.14+
# SEGFAULTS the interpreter (verified). Fail cleanly before touching it.
if sys.version_info >= (3, 14):
    print("RESOLVE_FAIL python {} cannot load fusionscript.dll — run with Python 3.6-3.13".format(sys.version.split()[0]))
    sys.exit(1)


def _connect():
    """Connect to a running Resolve Studio (in-app or external)."""
    resolve = None
    try:
        resolve = bmd.scriptapp("Resolve")  # noqa: F821 (inside Resolve's console)
    except NameError:
        pass
    if resolve is None:
        # External scripting environment (Windows layout; harmless if absent)
        os.environ.setdefault(
            "RESOLVE_SCRIPT_API",
            r"C:\\ProgramData\\Blackmagic Design\\DaVinci Resolve\\Support\\Developer\\Scripting",
        )
        os.environ.setdefault(
            "RESOLVE_SCRIPT_LIB",
            r"C:\\Program Files\\Blackmagic Design\\DaVinci Resolve\\fusionscript.dll",
        )
        mod_path = r"C:\\ProgramData\\Blackmagic Design\\DaVinci Resolve\\Support\\Developer\\Scripting\\Modules"
        if mod_path not in sys.path:
            sys.path.append(mod_path)
        try:
            import DaVinciResolveScript as dvr  # type: ignore
            resolve = dvr.scriptapp("Resolve")
        except Exception as err:
            print("connect error: {}".format(err))
    return resolve


def _queue_render(proj, timeline, m):
    """Queue + run a YouTube-style H.264 master of the pushed timeline."""
    try:
        proj.SetCurrentTimeline(timeline)
    except Exception:
        pass
    try:
        proj.SetRenderSettings({
            "MarkIn": timeline.GetStartFrame(),
            "MarkOut": timeline.GetEndFrame(),
            "TargetDir": m.get("render_dir") or os.getcwd(),
            "CustomName": m.get("render_name") or "ghostdirector_render",
            "UniqueFilenameStyle": 0,
        })
    except Exception as err:
        print("render settings warning: {}".format(err))
    preset_ok = False
    for preset in ("Youtube", "YouTube", "H.264 Master"):
        try:
            if proj.LoadRenderPreset(preset):
                preset_ok = True
                print("render preset: {}".format(preset))
                break
        except Exception:
            continue
    if not preset_ok:
        print("render preset: none loaded (using project defaults)")
    proj.AddRenderJob()
    proj.StartRendering()
    print("rendering started...")
    while proj.IsRenderingInProgress():
        time.sleep(2)
    try:
        for job in proj.GetRenderJobList():
            print("render job: {} ({})".format(job.get("JobName", ""), job.get("JobStatus", "")))
    except Exception:
        pass


def main():
    argv = sys.argv[1:]
    manifest_path = None
    if "--manifest" in argv:
        manifest_path = argv[argv.index("--manifest") + 1]
    probe_only = "--probe" in argv
    do_render = "--render" in argv

    resolve = _connect()
    if not resolve:
        print("RESOLVE_FAIL no connection (is Resolve Studio running with External Scripting = Local?)")
        sys.exit(1)
    if probe_only:
        print("RESOLVE_OK")
        sys.exit(0)
    if not manifest_path or not os.path.exists(manifest_path):
        print("RESOLVE_FAIL missing manifest {}".format(manifest_path))
        sys.exit(1)
    with open(manifest_path, "r", encoding="utf-8") as fh:
        m = json.load(fh)

    pm = resolve.GetProjectManager()
    proj_name = m["project_name"]
    proj = pm.CreateProject(proj_name) or pm.LoadProject(proj_name)
    if not proj:
        print("RESOLVE_FAIL could not create/load project {}".format(proj_name))
        sys.exit(1)
    print("project: {}".format(proj_name))

    try:
        proj.SetSetting("timelineFrameRate", str(m["fps"]))
        proj.SetSetting("timelineResolutionWidth", str(m["width"]))
        proj.SetSetting("timelineResolutionHeight", str(m["height"]))
    except Exception as err:
        print("settings warning: {}".format(err))

    mp = proj.GetMediaPool()
    media = [p for p in m.get("media", []) if os.path.exists(p)]
    if media:
        imported = mp.ImportMedia(media)
        print("media pool: imported {} of {} files".format(
            len(imported) if imported else 0, len(media)))

    timeline = None
    if os.path.exists(m.get("fcpxml", "")):
        try:
            timeline = mp.ImportTimelineFromFile(m["fcpxml"])
        except Exception as err:
            print("fcpxml import error: {}".format(err))
    if not timeline:
        print("RESOLVE_FAIL FCPXML import failed")
        sys.exit(1)
    proj.SetCurrentTimeline(timeline)
    try:
        vtrack = timeline.GetItemListInTrack("video", 1)
        n_clips = len(vtrack) if vtrack else 0
    except Exception:
        n_clips = 0
    print("timeline imported: {} video clips".format(n_clips))

    srt = m.get("srt")
    if srt and os.path.exists(srt):
        try:
            ok = timeline.ImportIntoTimeline(srt, {})
        except Exception:
            ok = False
        print("SRT_IMPORT_OK" if ok else "SRT_IMPORT_SKIPPED (Timeline -> Import -> Subtitle)")

    if do_render and m.get("render", True):
        _queue_render(proj, timeline, m)

    print("RESOLVE_PUSH_OK")


if __name__ == "__main__":
    main()
'''

    script_path = resolve_dir / "open_in_resolve.py"
    script_path.write_text(code, encoding="utf-8")
    return script_path, manifest_path


def _run_push(script_path: Path, manifest_path: Path, extra_args: list[str], timeout: int = 600) -> tuple[bool, str]:
    """Run the push script under a compatible scripting Python; return (ok, output)."""
    py = _find_resolve_python()
    if not py:
        return False, (
            "No compatible scripting Python found (fusionscript needs 3.6-3.13; "
            "this venv's Python crashes if it loads the DLL). Install Python 3.13 "
            "or set RESOLVE_PYTHON — meanwhile use File → Import → Timeline on "
            "resolve_project/timeline.fcpxml."
        )
    try:
        res = subprocess.run(
            [py, str(script_path), "--manifest", str(manifest_path), *extra_args],
            capture_output=True, text=True, timeout=timeout,
        )
        out = (res.stdout or "") + (res.stderr or "")
        return "RESOLVE_OK" in out or "RESOLVE_PUSH_OK" in out, out.strip()
    except Exception as e:
        return False, str(e)


def _find_resolve_python() -> str | None:
    """Locate a Python interpreter compatible with fusionscript.dll (3.6-3.13).

    Order: $RESOLVE_PYTHON → the known Python 3.13 path → newest installed
    Python 3.10-3.13 under %LOCALAPPDATA%\Programs\Python. Returns None when
    nothing compatible exists — the caller must then NOT attempt the bridge
    (loading fusionscript.dll from Python 3.14 segfaults the interpreter;
    verified on this machine).
    """
    env = os.environ.get("RESOLVE_PYTHON")
    if env and Path(env).exists():
        return env
    if PYTHON_313_EXE and Path(PYTHON_313_EXE).exists():
        return PYTHON_313_EXE
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python"
    if local.exists():
        candidates = sorted(local.glob("Python31[0-3]" / "python.exe"), reverse=True)
        if candidates:
            return str(candidates[0])
    return None


def resolve_is_running() -> bool:
    """True when a push attempt is worth making: Studio bridge + compatible Python.

    A full live probe (RESOLVE_OK) happens inside push_to_resolve; this stays
    cheap (filesystem only) so main.py can call it per run without spawning
    subprocesses.
    """
    return resolve_studio_installed() and _find_resolve_python() is not None


def push_to_resolve(
    script: Script,
    template: dict,
    output_dir: Path,
    tl_data: dict | None = None,
    render: bool = False,
    probe_only: bool = False,
) -> bool:
    """Push the edit into a running DaVinci Resolve Studio (zero-click import).

    probe_only=True performs a connection check only. Returns True on success;
    every failure path logs a human-readable hint instead of raising.
    """
    resolve_dir = output_dir / "resolve_project"
    script_path = resolve_dir / "open_in_resolve.py"
    manifest_path = resolve_dir / "push_manifest.json"
    if not script_path.exists() or not manifest_path.exists():
        generate_resolve_script(script, template, resolve_dir, tl_data, render=render)

    if probe_only:
        ok, out = _run_push(script_path, manifest_path, ["--probe"], timeout=30)
        return ok

    log.info("[bold blue]Pushing edit into DaVinci Resolve Studio...[/bold blue]")
    ok, out = _run_push(script_path, manifest_path, ["--render"] if render else [], timeout=1800 if render else 300)
    for line in out.splitlines():
        if line.strip():
            log.info(f"  [dim]resolve:[/dim] {line.strip()[:160]}")
    if ok:
        log.info("[bold green]✅ Resolve Studio has the timeline — open it and edit.[/bold green]")
    else:
        log.warning(
            "Resolve push failed. Manual path: File → Import → Timeline → "
            "resolve_project/timeline.fcpxml (see HOW_TO_OPEN_IN_DAVINCI.md). "
            "Ensure Resolve Studio is running with External Scripting = Local."
        )
    return ok


# ──────────────────────────────────────────────
# Orchestrator
# ──────────────────────────────────────────────

def export_resolve_bundle(
    script: Script,
    template: dict,
    output_dir: Path,
    timeline: dict | None = None,
    push: bool = False,
    render: bool = False,
) -> Path:
    """Write the complete DaVinci import bundle into output_dir/resolve_project/.

    push=True additionally attempts a zero-click push into a running Resolve
    Studio (project + media pool + FCPXML timeline + SRT; optional render).
    """
    resolve_dir = output_dir / "resolve_project"
    resolve_dir.mkdir(parents=True, exist_ok=True)

    log.info("[bold blue]Generating DaVinci Resolve export bundle...[/bold blue]")

    tl_data = timeline
    if tl_data is None:
        tl_path = output_dir / "timeline.json"
        if tl_path.exists():
            try:
                tl_data = json.loads(tl_path.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning(f"timeline.json unreadable ({e}); FCPXML uses estimates")
                tl_data = {}

    # 1. FCPXML timeline (the 1:1 editable edit)
    try:
        generate_fcpxml(script, template, resolve_dir, tl_data)
    except Exception as e:
        log.warning(f"FCPXML generation failed: {e}")

    # 2. Captions SRT
    try:
        scene_starts = [_sec(s.get("start_seconds")) for s in (tl_data or {}).get("scenes", [])]
        generate_srt(script, scene_starts or None, resolve_dir / "captions.srt")
    except Exception as e:
        log.warning(f"SRT generation failed: {e}")

    # 3. EDL fallback
    try:
        generate_edl(script, template, resolve_dir)
        log.info("✅ CMX 3600 EDL (resolve_project/timeline.edl)")
    except Exception as e:
        log.warning(f"EDL generation failed: {e}")

    # 4. Push script + manifest (FIX-034 zero-click Studio hand-off)
    try:
        script_path, manifest_path = generate_resolve_script(
            script, template, resolve_dir, tl_data, render=render
        )
        log.info("✅ Resolve push script + manifest (resolve_project/open_in_resolve.py)")
    except Exception as e:
        log.warning(f"Resolve push script generation failed: {e}")
        script_path = resolve_dir / "open_in_resolve.py"
        manifest_path = resolve_dir / "push_manifest.json"

    # 4b. Zero-click push into a running Resolve Studio (Studio-only feature)
    if push:
        try:
            push_to_resolve(script, template, output_dir, tl_data, render=render)
        except Exception as e:
            log.warning(f"Resolve push failed: {e}")

    # 5. Instructions
    instructions = f"""# DaVinci Resolve: {script.title}

The fastest path — the timeline imports **exactly as edited**:

## Option 1: FCPXML timeline (recommended — Free & Studio)
1. Open DaVinci Resolve → New/Any project.
2. **File → Import → Timeline...** and pick `resolve_project/timeline.fcpxml`.
3. Resolve rebuilds the edit: hook rewind + every punch-cut on the video
   spine, narration on audio lane 1, SFX on lane 2, music on lane 3,
   chapter/midpoint markers included.
4. Media is referenced by absolute path from this project folder — keep
   `edit_media/` and `scenes/` in place.

## Captions (editable subtitle track)
1. With the imported timeline open: **Timeline → Import → Subtitle...**
   (Edit page) and pick `resolve_project/captions.srt`.
2. You get Resolve's native subtitle track — same word-beat captions the
   render burned in, fully editable per cue.

## Option 2: EDL (other NLEs / older Resolve)
File → Import → Timeline → `timeline.edl` (video cuts + VO; no SFX/music lanes).

## Option 3: Zero-click push (Resolve Studio — what the pipeline uses)
1. Resolve Studio → Preferences → System → General → **External Scripting: Local**
   (one-time setup).
2. Either run `open_in_resolve.py --manifest push_manifest.json` yourself, or
   re-run the pipeline with `--push-resolve` while Resolve Studio is open —
   the project is created, media pool loaded, the FCPXML timeline imported,
   and captions.srt imported as a subtitle track automatically.
3. Add `--push-resolve --render` to also queue a YouTube-preset master render
   from Resolve (use this after hand-polishing; it exports a new MP4 into the
   project folder).

## Notes for hand-editing
- Transitions in the render are 0.2-0.4s cross-fades between clips; they are
  baked into the scene start offsets. Select all clips and apply your own
  transition if you want to reproduce them.
- SFX were mixed at 0.35 volume and ducked under narration; music had a
  2.5s fade-out. Re-level after editing.
- Cut points land on sentence boundaries of the narration — the narration
  audio on lane 1 is the timing reference.
- After polishing, render the master from Resolve (Deliver page, YouTube
  preset) — or `--push-resolve --render` queues it for you.
"""
    (resolve_dir / "HOW_TO_OPEN_IN_DAVINCI.md").write_text(instructions, encoding="utf-8")

    log.info(f"[bold green]Resolve bundle ready:[/bold green] {resolve_dir}")
    return resolve_dir


def assemble_resolve_project(
    script: Script,
    template: dict,
    output_dir: Path,
    push: bool = False,
    render: bool = False,
) -> Path:
    """Resolve-mode pipeline: build the import bundle AND render the FFmpeg MP4 preview."""
    export_resolve_bundle(script, template, output_dir, push=push, render=render)

    # The MP4 render stays available even in resolve mode (it is the uploadable
    # fallback if the user decides not to hand-edit after all).
    from pipeline.assembler_ffmpeg import assemble_video
    return assemble_video(script, template, output_dir)


if __name__ == "__main__":
    print("GhostDirector DaVinci Resolve export ready.")
