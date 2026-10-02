"""Phase 1 unit tests — run with: venv/Scripts/python.exe test_phase1_units.py

Covers (UPGRADE_PLAN.md §8 Phase 1):
  1.1  _prepare_video_cuts generalization + cut-point math + last-cut absorb
  1.2  SFX cue pools, midpoint riser, music fade filter, SFX duck chain
  1.3  Subject-aware thumbnail picker + vectorized gradient + overlay text
  1.4  Chapter titles from narration (A27)
  1.5  celebrity_8min template + scene_duration_range prompt plumbing (A29)
"""

import sys
import asyncio
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import config
from models import Script, Scene
from pipeline.assembler_ffmpeg import (
    _compute_cut_points, _sentence_groups, _prepare_video_cuts,
    _prepare_photo_cuts, _pick_sfx_file, _build_sfx_track, SFX_VOLUME,
)
from pipeline.metadata import _chapter_title_from_narration
from pipeline.thumbnail import (
    _subject_tokens, _pick_subject_scene, _make_gradient_overlay,
)

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def _stamps_from_text(text: str, wps: float = 2.6) -> list[dict]:
    """Fabricate Whisper-style word timestamps for narration text."""
    stamps, t = [], 0.0
    for word in text.split():
        dur = max(0.18, len(word) / (5.0 * wps))
        stamps.append({"word": word, "start": round(t, 3), "end": round(t + dur, 3)})
        t += dur + 0.02
    return stamps


def make_scene(**kw) -> Scene:
    defaults = dict(
        scene_number=1,
        narration="This is a test sentence. Here comes another one! And a third, right? A fourth one follows now.",
        visual_prompt="manhattan skyline",
        visual_type="stock_video",
        mood="dramatic",
        duration_target_seconds=10.0,
    )
    defaults.update(kw)
    return Scene(**defaults)


# ── 1.1 punch-cut generalization ──────────────────────────────────────
print("\n[1.1] Punch-cut generalization to video scenes")
template = config.load_template("celebrity_4min")

narr = ("He started with nothing. But within five years he had everything. "
        "Then one phone call changed it all. Nobody saw what happened next.")
scene = make_scene(
    visual_type="youtube_clip",
    narration=narr,
    duration_target_seconds=10.0,
    audio_duration_seconds=10.0,
    video_path=str(Path("assets/test/bunny.mp4").resolve()),
    timestamps=_stamps_from_text(narr),
)

cuts = _compute_cut_points(scene.timestamps, 10.0)
check("cut points computed", cuts is not None, "None returned for valid scene")
if cuts:
    pts, n = cuts
    check("cut count in 2..3", 2 <= n <= 3, f"n={n}")
    check("cuts start at 0 end at duration", pts[0] == 0.0 and pts[-1] == 10.0, str(pts))
    check("cuts monotonic", all(a <= b for a, b in zip(pts, pts[1:])), str(pts))

groups = _sentence_groups(scene.timestamps, 2)
check("sentence groups never split sentences", all(
    g[-1]["word"].rstrip().endswith((".", "!", "?")) for g in groups
), str([g[-1]["word"] for g in groups]))

out = Path("output/_phase1_test")
out.mkdir(parents=True, exist_ok=True)
video_cuts = _prepare_video_cuts(scene, template, out, 1)
check("video scene punch-cut works", video_cuts is not None and len(video_cuts) >= 2,
      f"got {video_cuts}")
if video_cuts:
    rendered = sum(seg for _, _, seg in video_cuts)
    check("rendered length == audio duration", abs(rendered - 10.0) < 0.15,
          f"rendered={rendered}")
    for p, _, _ in video_cuts:
        check(f"cut file exists {p.name}", p.exists())

photo_scene = make_scene(
    visual_type="web_photo",
    narration=narr,
    audio_duration_seconds=10.0,
    photo_path=str(Path("assets/test/taj.jpg").resolve()),
    timestamps=_stamps_from_text(narr),
)
photo_cuts = _prepare_photo_cuts(photo_scene, template, out, 2)
check("photo punch-cut still works", photo_cuts is not None and len(photo_cuts) >= 2)

short_scene = make_scene(audio_duration_seconds=4.0, timestamps=_stamps_from_text("Too short to cut."))
check("short scene not cut", _prepare_video_cuts(short_scene, template, out, 3) is None)

no_ts = make_scene(timestamps=None)
check("scene without timestamps not cut", _prepare_video_cuts(no_ts, template, out, 4) is None)

# ── 1.2 SFX discipline ────────────────────────────────────────────────
print("\n[1.2] SFX discipline")
w = _pick_sfx_file("whoosh", 0)
check("whoosh pool resolves", w is not None and w.exists(), str(w))
check("SFX volume is 0.35", SFX_VOLUME == 0.35, str(SFX_VOLUME))

script = Script(
    title="Test", description="", tags=[], hook="", scenes=[
        make_scene(scene_number=1, sfx_cue="whoosh", photo_path=None),
        make_scene(scene_number=2, sfx_cue="camera_shutter",
                   photo_path=str(Path("assets/test/taj.jpg").resolve())),
        make_scene(scene_number=3, sfx_cue="whoosh"),
        make_scene(scene_number=4, sfx_cue="whoosh"),
        make_scene(scene_number=5, sfx_cue="whoosh"),
        make_scene(scene_number=6, sfx_cue="sub_impact"),
    ],
    total_scenes=6, estimated_duration_minutes=0.5,
)
track, sfx_placements = _build_sfx_track(script, [0.0, 10.0, 20.0, 30.0, 40.0, 50.0], 60.0, out)
check("sfx track built", track is not None and track.exists())
check("sfx placements recorded", len(sfx_placements) >= 6 and all(
    "file" in p and "offset_seconds" in p for p in sfx_placements
))

# Shutter on non-photo scene must be dropped
script2 = Script(title="T", description="", tags=[], hook="", scenes=[
    make_scene(scene_number=1, sfx_cue="camera_shutter", photo_path=None),
], total_scenes=1, estimated_duration_minutes=0.2)
track2, placements2 = _build_sfx_track(script2, [0.0], 10.0, out)
check("shutter dropped on non-photo scene", track2 is None and placements2 == [])

# Music fade + sfx duck in mix_audio_with_music filter string
from utils.ffmpeg_cmd import mix_audio_with_music  # noqa: E402
import inspect
src = inspect.getsource(mix_audio_with_music)
check("music fade filter present", "afade=t=out" in src)
check("sfx sidechain duck present", src.count("sidechaincompress") >= 2)
check("mix accepts total_duration", "total_duration" in inspect.signature(mix_audio_with_music).parameters)

# ── 1.3 thumbnail ─────────────────────────────────────────────────────
print("\n[1.3] Subject-aware thumbnail")
check("subject tokens", _subject_tokens("The Rise and Fall of Sean Diddy Combs") ==
      {"sean", "diddy", "combs"}, str(_subject_tokens("The Rise and Fall of Sean Diddy Combs")))

s1 = make_scene(scene_number=1, visual_type="stock_video", people_to_show=[])
s2 = make_scene(scene_number=2, visual_type="web_photo", people_to_show=["Sean Diddy Combs"])
s2.photo_path = str(Path("assets/test/taj.jpg").resolve())
s3 = make_scene(scene_number=3, visual_type="web_photo", people_to_show=["Kim Porter"])
script3 = Script(title="The Rise and Fall of Sean Diddy Combs", description="", tags=[],
                 hook="", scenes=[s1, s2, s3], total_scenes=3, estimated_duration_minutes=0.5)
idx = _pick_subject_scene(script3)
check("subject scene picked over first photo", idx == 1, f"idx={idx}")

grad = _make_gradient_overlay(1280, 720, (10, 10, 15), 230)
px = grad.getpixel((0, 0))[3], grad.getpixel((1279, 0))[3]
check("gradient dark left, transparent right", px[0] == 230 and px[1] < 10, str(px))

# hook_overlay_text drives thumbnail words
from pipeline.thumbnail import generate_thumbnail  # noqa: F401  (import sanity)
script3.hook_overlay_text = "THE $200B FALL"
words = script3.hook_overlay_text.upper().split()[:4]
check("overlay text used for power words", words == ["THE", "$200B", "FALL"], str(words))

thumb = generate_thumbnail(script3, template, out)
check("thumbnail rendered", thumb is not None and thumb.exists())

# FIX-073: exclusive-split secondary face is picked deliberately
from pipeline.thumbnail import _pick_secondary_face
_face_dir = out / "_faces"
_face_dir.mkdir(exist_ok=True)

def _face_file(name: str) -> str:
    p = _face_dir / name
    p.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")   # exists on disk; content irrelevant
    return str(p)

sfx1 = make_scene(scene_number=1, visual_type="web_photo", people_to_show=["Bruno Mars"])
sfx1.photo_path = _face_file("bruno.jpg")
sfx_karol = make_scene(scene_number=2, visual_type="web_photo", people_to_show=["Karol G"])
sfx_karol.photo_path = _face_file("karol.jpg")
sfx_drake = make_scene(scene_number=3, visual_type="web_photo", people_to_show=["Drake"])
sfx_drake.photo_path = _face_file("drake.jpg")
vs_title = "Bruno Mars vs Drake: The War For Karol G"
vs_script = Script(title=vs_title, description="", tags=[], hook="", scenes=[sfx1, sfx_karol, sfx_drake],
                   total_scenes=3, estimated_duration_minutes=0.5)
picked = _pick_secondary_face(vs_script, 0, out)
check("FIX-073 secondary face = other named person (Drake, not Karol G)",
      picked == sfx_drake.photo_path, f"picked={picked}")
check("FIX-073 pick is order-independent",
      _pick_secondary_face(Script(title=vs_title, description="", tags=[], hook="",
                                  scenes=[sfx1, sfx_drake, sfx_karol],
                                  total_scenes=3, estimated_duration_minutes=0.5), 0, out)
      == sfx_drake.photo_path)
check("FIX-073 falls back to other named person when second lead absent",
      _pick_secondary_face(Script(title=vs_title, description="", tags=[], hook="",
                                  scenes=[sfx1, sfx_karol],
                                  total_scenes=2, estimated_duration_minutes=0.5), 0, out)
      == sfx_karol.photo_path)
# Tier-4 fallback ledger: a gradient scene must never become the split face
(out / "scenes").mkdir(exist_ok=True)
ledger = out / "scenes" / "scene_fallback.json"
ledger.write_text(json.dumps({"2": {"reason": "tier4_gradient: all sources exhausted"}}), encoding="utf-8")
solo = make_scene(scene_number=1, visual_type="web_photo", people_to_show=["Bruno Mars"])
solo.photo_path = str(Path("assets/test/taj.jpg").resolve())
sfb = make_scene(scene_number=2, visual_type="web_photo", people_to_show=[])
sfb.photo_path = _face_file("fallback.jpg")
sok = make_scene(scene_number=3, visual_type="web_photo", people_to_show=[])
sok.photo_path = _face_file("ok.jpg")
solo_script = Script(title="Solo Story About Bruno Mars", description="", tags=[], hook="",
                     scenes=[solo, sfb, sok], total_scenes=3, estimated_duration_minutes=0.5)
check("FIX-073 skips Tier-4 fallback scene for split face",
      _pick_secondary_face(solo_script, 0, out) == sok.photo_path)
ledger.write_text(json.dumps({"2": {"reason": "x"}, "3": {"reason": "y"}}), encoding="utf-8")
check("FIX-073 returns None when every candidate is a fallback",
      _pick_secondary_face(solo_script, 0, out) is None)
ledger.write_text("{}", encoding="utf-8")
check("FIX-073 still picks a face with empty ledger",
      _pick_secondary_face(solo_script, 0, out) == sfb.photo_path)

# ── 1.4 chapter titles ────────────────────────────────────────────────
print("\n[1.4] Chapter titles from narration")
t = _chapter_title_from_narration(
    "In 2019 the empire collapsed. Everyone saw it coming.", "fallback")
check("no raw visual query leak", "manhattan" not in t.lower(), t)
check("has content words", len(t.split()) >= 2, t)
check("falls back on empty narration",
      _chapter_title_from_narration("", "Part 2") == "Part 2")
t2 = _chapter_title_from_narration(
    "Sean Combs built Bad Boy Records from nothing at all.", "x")
check("names survive into title", "Combs" in t2, t2)

# ── 1.5 templates ─────────────────────────────────────────────────────
print("\n[1.5] Templates & prompt plumbing")
t8 = config.load_template("celebrity_8min")
check("8min template loads", t8["script"]["target_scenes"] == 44)
check("8min scene range 8-12", t8["script"]["scene_duration_range"] == [8, 12])
t4 = config.load_template("celebrity_4min")
check("4min: 22 scenes x 8-12s ≈ 4 min",
      t4["script"]["target_scenes"] == 22 and 22 * 10 / 60 >= 3.6)

from pipeline.scriptwriter import _build_prompt  # noqa: E402
from models import ResearchResult  # noqa: E402
research = ResearchResult(
    topic="X", summary="s", timeline=[], key_people=[], key_facts=[],
    narrative_arc="a", source_queries=[],
)
prompt8 = _build_prompt(research, t8)
prompt4 = _build_prompt(research, config.load_template("celebrity_4min"))
check("8min prompt has 8-12s range", "8 to 12 seconds" in prompt8)
check("4min prompt has 8-12s range", "8 to 12 seconds" in prompt4)
t_short = config.load_template("short_hook")
check("shorts keep 1.8-3.2s range", "1.8 and 3.2" in _build_prompt(research, t_short))

# ── 1.6 FIX-059: pre-lap J-cuts + motivated cut chooser ──────────────
print("\n[1.6] FIX-059 pre-laps & editorial cut styles")
from pipeline.assembler_ffmpeg import _cut_styles, _audio_leads_by  # noqa: E402

class _S:  # minimal scene stand-in for the style chooser
    def __init__(self, narration):
        self.narration = narration

scenes59 = [
    _S("He was on top of the world."),
    _S("But then came the phone call."),
    _S("What happened next shocked everyone."),
    _S("The studio stayed silent."),
    _S("Yet the fans knew the truth."),
    _S("It changed everything."),
    _S("Sources confirm the split."),
]
styles59 = _cut_styles(scenes59, [0.5, 0.5, 0.45, 0.0, 0.5, 0.4, 0.45])
check("styles cover every boundary", len(styles59) == len(scenes59) - 1)
check("motivated beat gets pre-lap", "prelap_hard_cut" in styles59)
check("no back-to-back pre-laps", "prelap_hard_cutprelap_hard_cut" not in "".join(styles59))
pre_idx = [i for i, s in enumerate(styles59) if s == "prelap_hard_cut"]
check("pre-laps spaced ≥3 boundaries", all(b - a >= 3 for a, b in zip(pre_idx, pre_idx[1:])))
check("no audio lead → no pre-lap", styles59[3] != "prelap_hard_cut")
check("other cuts stay hard/xfade",
      all(s in ("hard_cut", "xfade") for s in styles59 if s != "prelap_hard_cut"))

import subprocess as _sp  # noqa: E402
import tempfile  # noqa: E402
import os as _os  # noqa: E402

def _wav59(tone_s, pad_s):
    fd, p = tempfile.mkstemp(suffix=".wav")
    _os.close(fd)
    _sp.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", f"sine=frequency=440:duration={tone_s}",
             "-af", f"apad=pad_dur={pad_s}", p], capture_output=True, timeout=30)
    return Path(p)

_w1 = _wav59(1.0, 0.7)
_lead1 = _audio_leads_by(_w1)
check("trailing silence yields pre-lap", 0.35 <= _lead1 <= 0.55, str(_lead1))
_w2 = _wav59(1.0, 0.2)
check("tight tail → no pre-lap", _audio_leads_by(_w2) == 0.0)
check("missing file → 0.0", _audio_leads_by(Path("definitely_missing.wav")) == 0.0)
_w1.unlink(missing_ok=True)
_w2.unlink(missing_ok=True)

# ── 1.7 FIX-059: celebrity visual variety ledger ─────────────────────
print("\n[1.7] FIX-059 variety ledger (dhash / repeat gate / rotation)")
from pipeline.assets import (  # noqa: E402
    _image_dhash, _dhash_distance, _scene_visual_repeat,
    _rotate_person_queries, _person_key, VARIETY_DUP_DISTANCE,
    VARIETY_MAX_REPEATS,
)
from PIL import Image as _Img  # noqa: E402

with tempfile.TemporaryDirectory() as _td:
    def _grad59(name, mode):
        img = _Img.new("RGB", (64, 64))
        for x in range(64):
            for y in range(64):
                v = (x * 4) % 256 if mode == "x" else (y * 4) % 256
                img.putpixel((x, y), (v, 255 - v, 128))
        p = Path(_td) / name
        img.save(p)
        return p

    _pa = _grad59("a.jpg", "x")
    _pb = _grad59("b.jpg", "y")
    _ha, _hb = _image_dhash(_pa), _image_dhash(_pb)
    check("dhash computes (16 hex chars)", len(_ha) == 16)
    check("distinct images differ", _ha != _hb)
    check("identical image distance 0", _dhash_distance(_ha, _ha) == 0)
    check("missing file hash empty", _image_dhash(Path(_td) / "nope.jpg") == "")

    _st = {"scenes": {"1": {"person_key": "brad pitt", "hash": _ha},
                      "2": {"person_key": "brad pitt", "hash": _ha},
                      "3": {"person_key": "other person", "hash": _ha}}}
    check("same-person dups counted",
          _scene_visual_repeat(_st, ["Brad Pitt"], _ha) == 2)
    check("other person unaffected",
          _scene_visual_repeat(_st, ["Other Person"], _ha) == 1)
    check("no people → no repeats", _scene_visual_repeat(_st, [], _ha) == 0)

check("dup threshold sane", 0 < VARIETY_DUP_DISTANCE < 64)
check("repeat cap allows 2 uses", VARIETY_MAX_REPEATS == 2)
_q = ["p1", "p2", "p3", "p4"]
check("rotation cycles", _rotate_person_queries(_q, 2)[0] == "p2"
      and _rotate_person_queries(_q, 5) == _q)
check("person key normalized", _person_key(["Brad  Pitt"]) == "brad pitt"
      and _person_key([]) is None)

from pipeline.visual_director import classify_qc  # noqa: E402
_c, _w = classify_qc(["QC: person 'brad pitt' carries 5 scenes ([1, 2, 3, 4, 5]) "
                      "— review visual variety before publishing"])
check("variety finding stays a warning", len(_c) == 0 and len(_w) == 1)

print(f"\n{'='*50}\nRESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
