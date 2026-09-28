"""Phase 3 export/compliance tests — run with: venv/Scripts/python.exe test_phase3_units.py

Covers (UPGRADE_PLAN §8 remaining items):
  FIX-031 assembler edit manifest (timeline.json: cuts, sfx, music, fps)
  FIX-032 FCPXML timeline export + SRT captions for DaVinci Resolve
  FIX-033 YouTube compliance gate rules
"""

import sys
import json
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import config  # noqa: E402
from models import Script, Scene  # noqa: E402

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond), extra))
    print(f"  {'PASS' if cond else 'FAIL'} — {name}" + (f" ({extra})" if extra and not cond else ""))


def make_script() -> Script:
    scenes = []
    for i in range(1, 4):
        scenes.append(Scene(
            scene_number=i,
            narration=f"This is the narration for scene number {i}. It has two sentences on purpose.",
            visual_prompt="a placeholder",
            visual_type="stock_video" if i % 2 else "web_photo",
            mood="dramatic",
            duration_target_seconds=8.0,
            audio_duration_seconds=8.0,
            audio_path=None,
        ))
    return Script(
        title="The Rise of Jane Doe",
        description="A documentary-style breakdown.",
        tags=["documentary", "jane doe"],
        hook="Jane Doe started with nothing.",
        scenes=scenes,
        total_scenes=3,
        estimated_duration_minutes=0.4,
    )


def stamp_scene(script: Script):
    """Attach simple word timestamps so SRT/caption paths run (t resets per scene)."""
    for scene in script.scenes:
        t = 0.0
        stamps = []
        for word in scene.narration.split():
            stamps.append({"word": word, "start": round(t, 3), "end": round(t + 0.3, 3)})
            t += 0.3
        scene.timestamps = stamps


# ─────────────────────────────────────────────
print("\n[1] Compliance checker rules")
from pipeline.compliance import check_compliance  # noqa: E402

tmp = Path("output/_phase3_units")
if tmp.exists():
    shutil.rmtree(tmp)
tmp.mkdir(parents=True)

script = make_script()
script.chapters = [
    {"timestamp": "00:00", "title": "The Secret / Hook"},
    {"timestamp": "00:12", "title": "Part 2"},
    {"timestamp": "00:24", "title": "Part 3"},
]
metadata = {
    "title": "The Rise of Jane Doe",
    "description": ("A documentary breakdown of the alleged events. " * 3).strip(),
    "tags": ["documentary", "jane doe"],
    "chapters": script.chapters,
    "checklist": {"synthetic_media_disclosed": False},
    "ai_generated": True,
}
report = check_compliance(script, metadata, tmp)
check("report written", (tmp / "compliance_report.json").exists())
check("verdict present", report["verdict"] in ("pass", "warn", "fail"))
ai_warn = any(f["check"] == "ai.disclosure" for f in report["findings"])
check("undisclosed AI flag warns", ai_warn)

# Conviction language without hedge must FAIL
metadata_bad = dict(metadata)
metadata_bad["description"] = "He is a convicted fraudster and everyone knows it."
rep_bad = check_compliance(script, metadata_bad, tmp)
check("unhedged conviction claim fails", rep_bad["verdict"] == "fail",
      extra=str([f["check"] for f in rep_bad["findings"] if f["severity"] == "fail"]))

# Hedged claim passes the defamation check
metadata_hedged = dict(metadata)
metadata_hedged["description"] = (
    "Events involving legal allegations are presented as reported by public sources; "
    "nothing here is a statement of guilt. " * 2
)
rep_hedged = check_compliance(script, metadata_hedged, tmp)
defam = [f for f in rep_hedged["findings"] if f["check"] == "legal.defamation"]
check("hedged claim passes defamation", not defam or defam[0]["severity"] != "fail")

# Title over-limit fails
metadata_long = dict(metadata)
metadata_long["title"] = "x" * 120
rep_long = check_compliance(script, metadata_long, tmp)
check("over-length title fails", any(
    f["check"] == "metadata.title" and f["severity"] == "fail"
    for f in rep_long["findings"]
))

# Chapter rules: missing 00:00 fails
metadata_ch = dict(metadata)
metadata_ch["chapters"] = [{"timestamp": "00:05", "title": "Late"}]
rep_ch = check_compliance(script, metadata_ch, tmp)
check("missing 00:00 chapter fails", any(
    f["check"] == "chapters.format" and f["severity"] == "fail"
    for f in rep_ch["findings"]
))

# Profanity in title fails
metadata_prof = dict(metadata)
metadata_prof["title"] = "This Damn Title Works"
rep_prof = check_compliance(script, metadata_prof, tmp)
check("mild profanity title warns", any(
    f["check"] == "ads.profanity_mild" for f in rep_prof["findings"]
))
metadata_hard = dict(metadata)
metadata_hard["description"] = "This video is about straight up motherf***ing nonsense."
rep_hard = check_compliance(script, metadata_hard, tmp)
check("hard profanity fails", any(
    f["check"] == "ads.profanity" and f["severity"] == "fail"
    for f in rep_hard["findings"]
))

# Duplicate narration -> slop repetition
dup = make_script()
for s in dup.scenes:
    s.narration = "Exactly the same sentence repeated over and over in every single scene here."
rep_dup = check_compliance(dup, metadata, tmp)
check("repetitive narration flags slop", any(
    f["check"] == "slop.repetition" for f in rep_dup["findings"]
))

# ─────────────────────────────────────────────
print("\n[2] FCPXML + SRT export")
from pipeline.assembler_resolve import generate_srt, _x, _timestamp_to_seconds  # noqa: E402
try:
    from pipeline.assembler_resolve import generate_fcpxml
    HAS_FCPXML = True
except Exception:
    HAS_FCPXML = False
    check("fcpxml import", False, "generate_fcpxml missing")

srt_path = tmp / "captions.srt"
stamp_scene(script)
scene_starts = [0.0, 8.5, 17.0]  # transition-aware offsets
generate_srt(script, scene_starts, srt_path)
srt_text = srt_path.read_text(encoding="utf-8")
check("srt cue count", srt_text.count("-->") >= 6)
check("srt has timestamps", "00:00:00,000" in srt_text)
check("srt timestamps offset", "00:00:08,500" in srt_text)

check("rational time format", _x(1.0) == "3000/3000s")
check("timestamp parser", _timestamp_to_seconds("1:02:03") == 3723.0)

if HAS_FCPXML:
    # Create the media files the FCPXML references (it must skip nothing)
    em = tmp / "edit_media"
    em.mkdir(exist_ok=True)
    for name, body in (
        ("scene_01_cut0.mp4", b"a"), ("scene_01_cut1.mp4", b"b"),
        ("scene_02_prepared.mp4", b"c"), ("scene_03_prepared.mp4", b"d"),
    ):
        (em / name).write_bytes(body)
    for i in range(1, 4):
        (tmp / "scenes").mkdir(exist_ok=True)
        (tmp / "scenes" / f"scene_{i}_audio.mp3").write_bytes(b"x")
        script.scenes[i - 1].audio_path = str(tmp / "scenes" / f"scene_{i}_audio.mp3")

    # Fake the edit manifest the assembler writes
    tl = {
        "hook_rewind_seconds": 3.0,
        "fps": 30,
        "resolution": "1920x1080",
        "edit_media_dir": str(tmp / "edit_media"),
        "music_track": None,
        "sfx_placements": [
            {"cue": "whoosh", "file": "nonexistent_whoosh.wav", "offset_seconds": 8.38},
        ],
        "scenes": [
            {"scene_number": 1, "start_seconds": 3.0, "duration_seconds": 8.0,
             "mood": "dramatic",
             "cuts": [
                 {"file": "scene_01_cut0.mp4", "start_seconds": 3.0, "duration_seconds": 4.0},
                 {"file": "scene_01_cut1.mp4", "start_seconds": 7.0, "duration_seconds": 4.0},
             ]},
            {"scene_number": 2, "start_seconds": 11.0, "duration_seconds": 8.0, "mood": "dark", "cuts": []},
            {"scene_number": 3, "start_seconds": 19.0, "duration_seconds": 8.0, "mood": "emotional", "cuts": []},
        ],
    }
    fx = tmp / "timeline.fcpxml"
    generate_fcpxml(script, {"visuals": {"fps": 30, "resolution": "1920x1080"}}, tmp, tl)
    check("fcpxml written", fx.exists())
    xml = fx.read_text(encoding="utf-8")
    check("fcpxml declares 1.9", 'version="1.9"' in xml)
    check("hook on spine", 'name="Hook Rewind"' in xml)
    check("punch-cut clips on spine", xml.count("punch-cut") >= 2)
    check("fallback prepared clip used", 'name="Scene 2"' in xml)
    check("narration lane clips", xml.count('audioRole="dialogue"') >= 3)
    check("sfx missing file skipped cleanly", "nonexistent_whoosh" not in xml)
    check("fcpxml references file:// URIs", 'src="file:///' in xml)
    check("chapters become markers", xml.count("Chapter:") >= 1)
    check("midpoint marker", "Midpoint" in xml)

    # XML well-formedness
    import xml.etree.ElementTree as ET
    try:
        ET.fromstring(xml)
        check("fcpxml is well-formed XML", True)
    except Exception as e:
        check("fcpxml is well-formed XML", False, str(e)[:80])

    # SRT beats match burned caption groups (<=3 words)
    cues = [l for l in srt_text.splitlines() if l and "-->" not in l and not l.strip().isdigit()]
    if cues:
        max_words = max(len(c.split()) for c in cues)
        check("srt beats <=3 words", max_words <= 3, f"max={max_words}")

# ─────────────────────────────────────────────
print("\n[3] Assembler manifest plumbing (static checks)")
asm = Path("pipeline/assembler_ffmpeg.py").read_text(encoding="utf-8")
check("sfx placements returned", '"offset_seconds": round(offset, 3)' in asm)
check("cuts meta propagated", "cuts_meta" in asm)
check("edit_media preserved", '"edit_media"' in asm and "edit_media_dir" in asm)
check("music track in manifest", '"music_track":' in asm)
main_src = Path("main.py").read_text(encoding="utf-8")
check("compliance gate wired", "check_compliance" in main_src and "compliance_report.json" in main_src)
check("force-upload flag", "--force-upload" in main_src)
check("resolve bundle wired", "export_resolve_bundle" in main_src)

# ─────────────────────────────────────────────
print("\n[4] Resolve Studio push (FIX-034)")
from pipeline.assembler_resolve import (
    generate_resolve_script, resolve_studio_installed, _collect_push_media,
)  # noqa: E402

push_script, manifest_path = generate_resolve_script(
    script, {"visuals": {"fps": 30, "resolution": "1920x1080"}}, tmp, tl, render=False
)
check("push script written", push_script.exists())
check("manifest written", manifest_path.exists())
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
check("manifest has fcpxml path", manifest["fcpxml"].endswith("timeline.fcpxml"))
check("manifest srt path", (manifest.get("srt") or "").endswith("captions.srt"))
check("manifest media resolved", isinstance(manifest["media"], list))
push_code = push_script.read_text(encoding="utf-8")
check("push imports FCPXML via API", "ImportTimelineFromFile" in push_code)
check("push has probe mode", "--probe" in push_code and "RESOLVE_OK" in push_code)
check("push has render queue", "AddRenderJob" in push_code and "StartRendering" in push_code)

# The generated script must itself be valid Python
import py_compile  # noqa: E402
try:
    py_compile.compile(str(push_script), doraise=True)
    check("generated push script compiles", True)
except Exception as e:
    check("generated push script compiles", False, str(e)[:100])

# Probe mode must run cleanly on this machine (no Resolve -> RESOLVE_FAIL, not a crash)
import subprocess  # noqa: E402
probe = subprocess.run(
    [sys.executable, str(push_script), "--manifest", str(manifest_path), "--probe"],
    capture_output=True, text=True, timeout=60,
)
probe_out = (probe.stdout or "") + (probe.stderr or "")
check("probe runs cleanly without Resolve", "RESOLVE_" in probe_out, probe_out[:120])
check("probe reports no connection", "RESOLVE_FAIL" in probe_out)

# Studio-install detection must not crash (False expected on this box)
check("studio install detection runs", resolve_studio_installed() in (True, False))
check("push media collector runs", isinstance(
    _collect_push_media(script, tl, tmp), list
))

# ─────────────────────────────────────────────
fails = [n for n, ok, _ in RESULTS if not ok]
print(f"\nRESULT: {len(RESULTS) - len(fails)}/{len(RESULTS)} passed" + (f" — FAILED: {fails}" if fails else " — ALL CLEAR"))
sys.exit(1 if fails else 0)
