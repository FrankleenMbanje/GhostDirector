"""Phase 7 unit tests — FIX-062 face-aware caption placement.

Run with: venv/Scripts/python.exe test_phase7_units.py

Covers:
  7.1  frame occupancy: where the subject's face sits (synthetic frames)
  7.2  ASS caption band: vertical default is the bottom band; per-scene lift
  7.3  assembler band map: computed from rendered clips + QC overrides
  7.4  QC repair: caption findings move the band instead of re-rolling the visual
"""

import sys
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond), extra))
    print(f"  {'PASS' if cond else 'FAIL'} — {name}" + (f" ({extra})" if extra and not cond else ""))


_td = tempfile.TemporaryDirectory()
TMP = Path(_td.name)


def _skin_frame(path: Path, cy: int, size=(120, 200)):
    """Draw a skin-toned blob at vertical centre `cy` on a dark frame."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", size, (28, 30, 38))
    d = ImageDraw.Draw(img)
    d.ellipse([size[0] // 2 - 30, cy - 36, size[0] // 2 + 30, cy + 36],
              fill=(196, 148, 120))
    img.save(path)
    return path


# ── 7.1 frame occupancy ──────────────────────────────────────────────
print("\n[7.1] frame occupancy (where is the face?)")
from utils.frame_occupancy import (  # noqa: E402
    caption_band, detect_face_band, face_centre_y,
)

upper = _skin_frame(TMP / "upper.png", 40)
middle = _skin_frame(TMP / "middle.png", 100)
lower = _skin_frame(TMP / "lower.png", 170)
plain = TMP / "plain.png"
from PIL import Image  # noqa: E402

Image.new("RGB", (120, 200), (30, 32, 40)).save(plain)

check("upper-band face detected as 'upper'", detect_face_band(upper) == "upper",
      detect_face_band(upper))
check("middle face detected as 'middle'", detect_face_band(middle) == "middle",
      detect_face_band(middle))
check("low face detected as 'lower'", detect_face_band(lower) == "lower",
      detect_face_band(lower))
check("face-less frame reports 'none'", detect_face_band(plain) == "none",
      detect_face_band(plain))
cy_up = face_centre_y(upper)
check("centroid is normalized 0..1 and above the middle",
      cy_up is not None and 0.0 <= cy_up < 0.38, str(cy_up))
check("bottom band is the default placement", caption_band(middle) == "bottom")
check("captions lift to the top band when the face sits low",
      caption_band(lower) == "top")
check("missing file degrades to the default band and never raises",
      caption_band(TMP / "nope.mp4") == "bottom")
check("undecodable bytes degrade to the default band",
      caption_band(Path(__file__)) == "bottom")

# ── 7.2 ASS caption band ─────────────────────────────────────────────
print("\n[7.2] ASS caption band placement")
from utils.caption_renderer import generate_ass_subtitles  # noqa: E402


def _ts(*words):
    return [{"word": w, "start": i * 0.4, "end": i * 0.4 + 0.38}
            for i, w in enumerate(words)]


ass_path = TMP / "captions.ass"
generate_ass_subtitles(
    [_ts("Kanye", "halts", "the", "trial"), _ts("lawyers", "want", "a", "delay")],
    [0.0, 2.0], ass_path, {"style": "editorial", "font_size": 64},
    resolution=(1080, 1920), band_overrides={2: "top"},
)
ass = ass_path.read_text(encoding="utf-8")
style_line = [ln for ln in ass.splitlines() if ln.startswith("Style: Default")][0]
style_fields = style_line.split(",")
check("vertical style anchors at the bottom band (Alignment=2)",
      style_fields[18].strip() == "2", style_fields[18])
check("vertical style reserves ~20% bottom margin",
      abs(int(style_fields[21]) - 384) <= 4, style_fields[21])
events = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]
check("scene 1 captions stay on the default bottom band", any("\\an2" in e for e in events))
check("every vertical event carries an explicit band",
      all("\\an2" in e or "\\an8" in e for e in events))
check("overridden scene 2 captions lift to the top band", any("\\an8" in e for e in events))
check("the override applies to the flagged scene only",
      sum(1 for e in events if "\\an8" in e) and
      sum(1 for e in events if "\\an8" in e) < len(events))
check("style functions keep their own content (no text lost)",
      "Kanye" in ass and "lawyers" in ass.lower())

ass_flat = TMP / "captions_flat.ass"
generate_ass_subtitles(
    [_ts("a", "b", "c", "d")], [0.0], ass_flat,
    {"style": "documentary", "position": "bottom"}, resolution=(1920, 1080),
)
flat = ass_flat.read_text(encoding="utf-8")
flat_style = [ln for ln in flat.splitlines() if ln.startswith("Style: Default")][0]
check("landscape captions are left untouched (template position wins)",
      "\\an8" not in flat and "\\an2" not in flat)
check("landscape style keeps its own alignment", flat_style.split(",")[18].strip() == "2")

# ── 7.3 assembler band map ───────────────────────────────────────────
print("\n[7.3] assembler caption band map")
from pipeline.assembler_ffmpeg import _caption_bands_for_scenes  # noqa: E402

proj = TMP / "proj"
(proj / "_temp").mkdir(parents=True, exist_ok=True)
_skin_frame(proj / "_temp" / "scene_01_prepared.v4.mp4".replace(".mp4", ".png"), 40)
bands = _caption_bands_for_scenes({1: (upper, False, []), 2: (lower, False, [])},
                                  proj / "_temp")
check("band map covers every prepared scene", set(bands) == {1, 2}, str(bands))
check("high-subject scene keeps the bottom band", bands.get(1) == "bottom", str(bands))
check("low-subject scene lifts captions", bands.get(2) == "top", str(bands))

(proj / "_temp" / "caption_band_overrides.json").write_text(
    json.dumps({"2": "bottom"}), encoding="utf-8")
bands2 = _caption_bands_for_scenes({1: (upper, False, []), 2: (lower, False, [])},
                                   proj / "_temp")
check("QC overrides win over the frame default", bands2.get(2) == "bottom", str(bands2))
bands3 = _caption_bands_for_scenes({1: (TMP / "missing.mp4", False, [])}, proj / "_temp")
check("unreadable clips are skipped without raising", bands3 == {} or 1 not in bands3,
      str(bands3))

# ── 7.4 QC repair moves the caption ──────────────────────────────────
print("\n[7.4] QC repair: caption problems move the band")
from pipeline.visual_director import _write_caption_band_overrides  # noqa: E402

repair_proj = TMP / "repair"
(repair_proj / "_temp").mkdir(parents=True, exist_ok=True)
# a stand-in prepared clip (undecodable as video → detector yields the default)
(repair_proj / "_temp" / "scene_05_prepared.v4.mp4").write_bytes(b"\x00" * 2048)
moved = _write_caption_band_overrides(repair_proj, [5])
check("repair writes an override for the flagged scene", moved.get(5) == "top", str(moved))
saved = json.loads((repair_proj / "_temp" / "caption_band_overrides.json")
                   .read_text(encoding="utf-8"))
check("override is persisted for the next render", saved.get("5") == "top", str(saved))
check("band change is deterministic (no repeat flip-flop on re-read)",
      _write_caption_band_overrides(repair_proj, [5]).get(5) is not None)

# ── 7.5 long-form length floor (FIX-063) ─────────────────────────────
print("\n[7.5] long-form length floor")
import asyncio  # noqa: E402

from models import Scene, Script  # noqa: E402
from pipeline.scriptwriter import (  # noqa: E402
    _apply_expansion, enforce_longform_length, estimate_script_seconds,
)


def _script(n_scenes: int, words_per_scene: int = 12) -> Script:
    scenes = [Scene(scene_number=i, narration="word " * words_per_scene,
                    visual_prompt="p", visual_type="stock_photo", mood="neutral",
                    duration_target_seconds=8.0)
              for i in range(1, n_scenes + 1)]
    return Script(title="T", description="d", tags=[], hook="h", scenes=scenes,
                  total_scenes=n_scenes, estimated_duration_minutes=1.0)


_eight_min = {"script": {"target_duration_minutes": 8}}
short = _script(10)                      # ~600 words → ~3.8 min
long_enough = _script(90, words_per_scene=40)   # well past 8 min

check("estimate honours an explicit words/second rate",
      abs(estimate_script_seconds(_script(10, 26), 2.6) - 100.0) < 1.0,
      str(estimate_script_seconds(_script(10, 26), 2.6)))
check("estimate uses the CALIBRATED default rate (2.25 wps measured live)",
      abs(estimate_script_seconds(_script(10, 26)) - 260 / 2.25) < 1.0,
      str(estimate_script_seconds(_script(10, 26))))
check("calibrated rate is slower than the old optimistic 2.6",
      __import__("pipeline.scriptwriter", fromlist=["DEFAULT_WPS"]).DEFAULT_WPS < 2.6,
      str(__import__("pipeline.scriptwriter", fromlist=["DEFAULT_WPS"]).DEFAULT_WPS))
async def _no_call(*_a, **_k):
    raise AssertionError("expander must not be called")


check("no target configured → untouched, expander never called",
      asyncio.run(enforce_longform_length(short, template={}, expand=_no_call)) is short)
check("target met → untouched, expander never called",
      asyncio.run(enforce_longform_length(long_enough, template=_eight_min,
                                          expand=_no_call)) is long_enough)

_calls = {"n": 0}


async def _grow(script, research, target_s, wps):
    _calls["n"] += 1
    payload = {"scenes": [{"scene_number": i, "narration": "detail " * 40,
                           "visual_prompt": "p", "mood": "tense",
                           "duration_target_seconds": 10}
                          for i in range(1, 120)]}
    return _apply_expansion(script, payload)


_before = len(short.scenes)   # expansion is in-place (mirrors FIX-060)
grown = asyncio.run(enforce_longform_length(short, template=_eight_min, expand=_grow))
check("short draft is expanded toward the target", len(grown.scenes) > _before,
      f"{_before} → {len(grown.scenes)}")
check("expansion stops once the target is met (no runaway calls)", _calls["n"] >= 1)
check("scene numbers stay contiguous after expansion",
      [s.scene_number for s in grown.scenes] == list(range(1, len(grown.scenes) + 1)))
check("expanded estimate reaches the target floor",
      estimate_script_seconds(grown) >= 8 * 60 * 0.92,
      f"{estimate_script_seconds(grown) / 60:.1f} min")

check("a shrinking response is refused", _apply_expansion(short, {"scenes": [
    {"scene_number": 1, "narration": "only one"}]}) is None)

# Found live: the model returned the SAME scene count with more words — the
# old guard (scene-count only) rejected real growth and kept a 2.9-min draft.
_same_n = len(short.scenes)
_grown_words = _apply_expansion(short, {"scenes": [
    {"scene_number": i, "narration": "detail " * 60, "visual_prompt": "p",
     "mood": "tense", "duration_target_seconds": 10} for i in range(1, _same_n + 1)]})
check("word growth at the SAME scene count is accepted",
      _grown_words is not None and len(_grown_words.scenes) == _same_n)
check("long-form scene targets are recalibrated into the 6-12s band",
      all(6.0 <= s.duration_target_seconds <= 12.0 for s in _grown_words.scenes))

_reworded = _apply_expansion(short, {"scenes": [
    {"scene_number": i, "narration": "word " * 13, "visual_prompt": "p", "mood": "neutral"}
    for i in range(1, len(short.scenes) + 1)]})
check("same length reworded is NOT growth", _reworded is None,
      "accepted a no-op expansion")

_claim_script = _script(10, 8)
_claim_script.estimated_duration_minutes = 8.0      # the model's inflated claim
_corrected = asyncio.run(enforce_longform_length(
    _claim_script, template={"script": {"target_duration_minutes": 0}}, expand=_no_call))
check("measured length replaces a lying self-reported duration",
      _corrected.estimated_duration_minutes < 1.0,
      str(_corrected.estimated_duration_minutes))


async def _fail(*_a, **_k):
    raise RuntimeError("quota exhausted")


check("an expansion failure degrades to the current draft",
      asyncio.run(enforce_longform_length(short, template=_eight_min,
                                          expand=_fail)) is short)
check("an all-blank expansion response is refused",
      _apply_expansion(short, {"scenes": [{"scene_number": i, "narration": " "}
                                           for i in range(1, 200)]}) is None)

# ── 7.6 research depth scales with format (FIX-064) ──────────────────
print("\n[7.6] research depth")
from pipeline.researcher import _merge_facts, min_facts_for  # noqa: E402

check("an 8-minute doc demands documentary-grade depth", min_facts_for(8) >= 20,
      str(min_facts_for(8)))
check("a short needs only a light fact set", min_facts_for(0.8) <= 8,
      str(min_facts_for(0.8)))
check("depth is monotonic across formats",
      min_facts_for(8) >= min_facts_for(4) >= min_facts_for(1))

_known = ["Swift filed the application in 2025", "Lawyers called the claim nonsensical"]
_merged = _merge_facts(_known, [
    "SWIFT FILED THE APPLICATION IN 2025",              # exact dup (case)
    "nonsensical lawyers called the claim",             # same words, reordered
    "The show opened on October 3, 2025 in Los Angeles",  # genuinely new
    "   ",                                              # blank
])
check("gap-fill cannot inflate the count with duplicates", len(_merged) == 3,
      str(len(_merged)))
check("gap-fill keeps the real new fact",
      any("October 3" in f for f in _merged))
check("gap-fill preserves the original facts in order",
      _merged[0] == _known[0] and _merged[1] == _known[1])
check("an empty gap-fill is harmless", _merge_facts(_known, []) == _known)

# ── 7.7 research must not silently degrade (regression) ─────────────
print("\n[7.7] research path integrity")
import pipeline.researcher as _res  # noqa: E402

_CANNED = {
    "summary": "s",
    "timeline": [{"date": "2025", "event": "album released"}],
    "key_people": [{"name": "Maren Wade", "role": "plaintiff", "search_query": "Maren Wade"}],
    "key_facts": ["fact one 2025", "fact two $10m", "fact three Los Angeles"],
    "narrative_arc": "arc",
}


class _FakeResponse:
    text = json.dumps(_CANNED)


class _FakeModels:
    def generate_content(self, model, contents, config):  # noqa: ARG002
        return _FakeResponse()


class _FakeClient:
    def __init__(self, api_key=None):  # noqa: ARG002
        self.models = _FakeModels()


_orig = (_res.genai.Client, _res._web_context, _res.pace_llm_call)
_res.genai.Client = _FakeClient
async def _no_web(*_a, **_k):
    return "STUB WEB RESULTS"
_res._web_context = _no_web
_res.pace_llm_call = lambda *a, **k: None
try:
    _r = asyncio.run(_res.research_topic("t", {"script": {"target_duration_minutes": 8}}))
    check("research returns the model's facts (no silent fallback to headlines)",
          len(_r.key_facts) == 3, str(_r.key_facts))
    check("research keeps structured people/timeline data",
          len(_r.key_people) == 1 and len(_r.timeline) == 1,
          f"people={len(_r.key_people)} timeline={len(_r.timeline)}")
    check("facts are prose, not article titles",
          all("." not in f[:0] for f in _r.key_facts) and "fact one" in _r.key_facts[0],
          str(_r.key_facts[:1]))
finally:
    _res.genai.Client, _res._web_context, _res.pace_llm_call = _orig

# ── 7.8 expansion prompt survives oddly-shaped research ──────────────
print("\n[7.8] expansion prompt robustness")
from pipeline.scriptwriter import _expansion_prompt, _fact_text  # noqa: E402


class _MessyResearch:
    key_facts = ["plain fact about 2025", 12345, {"text": "dict-shaped fact", "date": "May 2026"}]
    key_people = [{"name": "Maren Wade", "role": "plaintiff"}, "Taylor Swift"]


_p = _expansion_prompt(_script(3), _MessyResearch(), 480.0, 2.6)
check("prompt builds from dict/int research without raising",
      "dict-shaped fact" in _p and "12345" in _p)
check("prompt carries people with their roles",
      "Maren Wade" in _p and "plaintiff" in _p and "Taylor Swift" in _p)
check("prompt states the required word budget", "1248" in _p, _p[:120])
check("prompt forbids fabrication and padding",
      "NEVER invent" in _p and "never padding" in _p.replace("never padding", "never padding"))
check("a None research object is safe",
      isinstance(_expansion_prompt(_script(2), None, 480.0, 2.6), str))
check("dict flattening keeps every value",
      _fact_text({"a": "one", "b": "two"}) == "one — two")
check("blank values never reach the prompt",
      _fact_text({"a": "", "b": "kept"}) == "kept")

print(f"\n{'='*50}\nRESULT: {sum(1 for _, ok, _ in RESULTS if ok)} passed, "
      f"{sum(1 for _, ok, _ in RESULTS if not ok)} failed")
sys.exit(0 if all(ok for _, ok, _ in RESULTS) else 1)
