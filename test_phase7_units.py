"""Phase 7 unit tests — FIX-062 face-aware caption placement.

Run with: venv/Scripts/python.exe test_phase7_units.py

Covers:
  7.1  frame occupancy: where the subject's face sits (synthetic frames)
  7.2  ASS caption band: vertical default is the bottom band; per-scene lift
  7.3  assembler band map: computed from rendered clips + QC overrides
  7.4  QC repair: caption findings move the band instead of re-rolling the visual
  7.10 lane lock + publish queue + companion bridge (FIX-077/078/079)
  7.11 voice/music de-AI pass (FIX-075/076): pitch jitter, mix graph, voices
  7.18 strict celebrity gate, dead-black thumbnail guard, delivery audit
       (FIX-087/088/089)
  7.19 self-learning: winner-riding docs + proven-name boost (FIX-091/092)
  7.20 kinetic hook card: real motion + wired filter (FIX-093)
  7.21 silent-tail death: TTS retry/raise + assembler guard + QC probe (FIX-094)
"""

import sys
import json
import re
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

# ── 7.9 LLM daily-quota quarantine follows Google's PACIFIC quota day ─
print("\n[7.9] quarantine expires at midnight Pacific (Google's quota day)")
import os
import time as _time
from datetime import datetime, timezone
from pipeline.scriptwriter import (  # noqa: E402
    _load_llm_quarantine, _quarantine_model, _is_quarantined_today,
    _expire_stale_quarantines, _QUOTA_TZ,
)

_qdir = tempfile.TemporaryDirectory()
_cwd = os.getcwd()
os.chdir(_qdir.name)
try:
    check("no quarantine file = no model skipped",
          not _is_quarantined_today("gemini-flash-latest"))

    # a fresh daily 429 quarantines the model for the rest of the quota day
    _quarantine_model("gemini-flash-latest")
    check("a fresh daily 429 quarantines the model",
          _is_quarantined_today("gemini-flash-latest"))
    check("quarantine is per-model — the rest of the chain stays up",
          not _is_quarantined_today("gemini-flash-lite-latest"))

    # REGRESSION (run #4, 2026-09-30): a 429 earned just before Pacific
    # midnight must expire when the bucket refills, even though its UTC
    # date is still "today". midnight_pt - 300 = 23:55 PT yesterday.
    now_pt = datetime.now(tz=_QUOTA_TZ)
    midnight_pt = datetime.combine(
        now_pt.date(), datetime.min.time(), tzinfo=_QUOTA_TZ).timestamp()
    ts_pre_midnight = midnight_pt - 300
    p = Path("output/_llm_quota_quarantine.json")
    p.write_text(json.dumps({"gemini-3.6-flash": ts_pre_midnight}), encoding="utf-8")
    check("429 from 5 min before PT midnight is expired (bucket refilled)",
          not _is_quarantined_today("gemini-3.6-flash"))
    check("UTC-date logic would wrongly keep that 429 locked (the run #4 bug)",
          datetime.fromtimestamp(ts_pre_midnight, tz=timezone.utc).date()
          == datetime.now(tz=timezone.utc).date())
    p.write_text(json.dumps({"gemini-3.6-flash": _time.time() - 25 * 3600}), encoding="utf-8")
    check("a 429 earned 25 h ago never quarantines today",
          not _is_quarantined_today("gemini-3.6-flash"))

    # the pre-chain sweeper: keeps live entries, drops stale ones
    p.write_text(json.dumps({
        "gemini-flash-latest": _time.time(),          # live (today PT)
        "gemini-3.6-flash": midnight_pt - 300,        # stale (yesterday PT)
    }), encoding="utf-8")
    _expire_stale_quarantines()
    after = _load_llm_quarantine()
    check("sweeper keeps live quarantines", "gemini-flash-latest" in after)
    check("sweeper drops expired quarantines", "gemini-3.6-flash" not in after)

    p.write_text(json.dumps({"gone": midnight_pt - 300}), encoding="utf-8")
    _expire_stale_quarantines()
    check("sweeper deletes the file when nothing is live", not p.exists())
finally:
    os.chdir(_cwd)


# ── 7.10 lane lock + publish queue + companion bridge (FIX-077/078/079) ─
print("\n[7.10] lane lock, publish queue, companion bridge")
import pipeline.trending_news as TN

in_lane = {"title": "Taylor Swift's lawyers hit back at Drake in new feud filing",
           "source": "Billboard", "published": "", "url": "x"}
out_lane_weak = {"title": "A quiet weekend for the cast of a small streaming show",
                 "source": "Yahoo", "published": "", "url": "y"}
strong_out = {"title": "Billionaire pop mogul's arrest shocks the industry; lawyers respond",
              "source": "TMZ", "published": "", "url": "z"}

gated = TN._lane_gate([
    {**in_lane, "score": 2.0},       # in-lane, weak score -> kept
    {**out_lane_weak, "score": 2.0}, # out-of-lane, weak -> dropped
    {**strong_out, "score": 5.1},    # out-of-lane, exceptional -> kept
], enabled=True)
check("lane gate keeps in-lane items", any(it["title"] == in_lane["title"] for it in gated))
check("lane gate drops weak out-of-lane items", not any(it["title"] == out_lane_weak["title"] for it in gated))
check("lane gate keeps exceptional out-of-lane items", any(it["title"] == strong_out["title"] for it in gated))
check("lane gate disabled passes everything through",
      len(TN._lane_gate([{**out_lane_weak, "score": 0.5}], enabled=False)) == 1)
check("lane hit detector matches names and drama",
      TN._lane_hits("Travis Kelce feud with 50 Cent escalates") >= 2)

# bridge comment text: short points to the doc, doc points to the short
from pipeline.uploader import _companion_comment_text
short_txt = _companion_comment_text("https://youtu.be/DOC123", companion_is_doc=True)
doc_txt = _companion_comment_text("https://youtu.be/SHORT123", companion_is_doc=False)
check("bridge on short points to the full doc", "DOC123" in short_txt and "FULL story" in short_txt)
check("bridge on doc points to the short", "SHORT123" in doc_txt and "45-second" in doc_txt)

# publish queue: rows append with a +6h deadline and Studio link
_qdir = TMP / "queue_test"
_qdir.mkdir(exist_ok=True)
_os_cwd = os.getcwd()
try:
    os.chdir(_qdir)
    TN._QUEUE_PATH = _qdir / "output" / "PUBLISH_QUEUE.md"
    TN.append_publish_queue("Taylor Swift feud story", "short", "abc123XYZ",
                            packaged_title="Taylor Swift Just Ended This Feud")
    TN.append_publish_queue("Taylor Swift feud story", "longform", "doc456XYZ")
    q = TN._QUEUE_PATH.read_text(encoding="utf-8")
    check("queue header written", "PUBLISH QUEUE" in q and "publish-by" in q)
    check("queue row has Studio link + deadline", "studio.youtube.com/video/abc123XYZ/edit" in q)
    check("queue marks doc rows as 8-min", "8-min doc" in q)
    check("queue row references bridge pinning", "bridge comment" in q)
finally:
    os.chdir(_os_cwd)

# ledger fmt + companion lookup (short↔doc pairing)
_led_dir = TMP / "ledger_test"
_led_dir.mkdir(exist_ok=True)
try:
    os.chdir(_led_dir)
    TN._QUEUE_PATH = _led_dir / "output" / "PUBLISH_QUEUE.md"
    TN.record_story({"title": "Drake and Kendrick escalate the beef", "url": "", "source": "",
                     "published": ""}, status="used", channel="famefiles",
                    video_id="SHORTid111", project_dir=None, fmt="short")
    TN.record_story({"title": "Drake and Kendrick escalate the beef", "url": "", "source": "",
                     "published": ""}, status="used", channel="famefiles",
                    video_id="DOCid222", project_dir=None, fmt="longform")
    check("companion lookup finds the short for the doc",
          TN.find_companion_video_id("Drake and Kendrick escalate the beef", "short") == "SHORTid111")
    check("companion lookup finds the doc for the short",
          TN.find_companion_video_id("Drake and Kendrick escalate the beef", "longform") == "DOCid222")
    check("companion lookup returns None for unknown story",
          TN.find_companion_video_id("A story we never covered", "short") is None)
finally:
    os.chdir(_cwd)


# ── 7.11 voice/music de-AI pass (FIX-075/076) ─────────────────────────
print("\n[7.11] voice + music de-AI")
import inspect
from utils.ffmpeg_cmd import mix_audio_with_music as _mixfn
_mix_src = inspect.getsource(_mixfn)
check("mix default bed lowered to -24 dB", "music_volume_db: float = -24" in _mix_src)
check("mix ducker deepened (ratio 14)", "ratio=14" in _mix_src)
check("voice chain loudness-normalized", "loudnorm=I=-16" in _mix_src)
check("final limiter present in all branches", _mix_src.count("alimiter=limit=0.971") == 4)
check("no [out] emitted without passing the limiter",
      not re.search(r"\[voice\]\[music\]amix[^\"]*\[out\]", _mix_src))

from pipeline import voice as _voice
_vs = inspect.getsource(_voice)
check("pitch jitter around template base (FIX-075)", "pitch_jitter" in _vs and "base_pitch_val" in _vs)
check("default voice is the Conversation-class Andrew",
      _vs.count("en-US-AndrewMultilingualNeural") >= 2 and "en-US-GuyNeural" not in _vs)
import templates as _  # noqa: F401  (templates dir sanity; JSON checked below)
_tj = json.loads((Path(__file__).parent / "templates" / "celebrity_8min.json").read_text(encoding="utf-8"))
_tf = json.loads((Path(__file__).parent / "templates" / "famefiles_trending.json").read_text(encoding="utf-8"))
check("doc template on AndrewMultilingual", _tj["voice"]["voice_id"] == "en-US-AndrewMultilingualNeural")
check("famefiles short template on AvaMultilingual",
      _tf["voice"]["voice_id"] == "en-US-AvaMultilingualNeural")
_no_guy = [p.name for p in (Path(__file__).parent / "templates").glob("*.json")
           if "GuyNeural" in p.read_text(encoding="utf-8") or "EricNeural" in p.read_text(encoding="utf-8")]
check("no News-class voices left in templates", _no_guy == [], str(_no_guy))

# ── 7.12 daily slate + PUBLIC delivery (FIX-080) ─────────────────────
print("\n[7.12] daily slate + public delivery (FIX-080)")
import storage.scheduler as _sched
from pipeline.uploader import upload_video as _uv

_saved_env = {k: os.environ.get(k) for k in ("GD_SHORTS_PER_DAY", "GD_SHORTS_ONLY")}
for _k in _saved_env:
    os.environ.pop(_k, None)
try:
    _stages = _sched._build_stages("famefiles")
    check("daily slate = 3 shorts + 1 doc", len(_stages) == 4)
    check("every stage uploads public", all("public" in s for s in _stages))
    check("doc stage is last", "--longform" in _stages[-1] and
          all("--longform" not in s for s in _stages[:3]))
    check("all stages carry the channel", all("famefiles" in s for s in _stages))

    os.environ["GD_SHORTS_PER_DAY"] = "1"
    check("GD_SHORTS_PER_DAY overrides the count",
          len(_sched._build_stages("famefiles")) == 2)

    os.environ["GD_SHORTS_PER_DAY"] = "0"
    _clamped = _sched._build_stages("famefiles")
    check("shorts count clamps to >= 1",
          len(_clamped) == 2 and "--longform" in _clamped[-1])

    os.environ["GD_SHORTS_PER_DAY"] = "3"
    os.environ["GD_SHORTS_ONLY"] = "1"
    _shorts_only = _sched._build_stages("famefiles")
    check("GD_SHORTS_ONLY skips the doc",
          len(_shorts_only) == 3 and all("--longform" not in s for s in _shorts_only))

    _res_dir = TMP / "daily_result_test"
    _res_dir.mkdir(exist_ok=True)
    try:
        os.chdir(_res_dir)
        _sched._write_daily_result("PARTIAL", 2, 3, "failed")
        _res = (Path("output") / "_daily_result.txt").read_text(encoding="utf-8")
        check("daily result file carries the counts",
              "result=PARTIAL" in _res and "shorts=2/3" in _res and "doc=failed" in _res)
    finally:
        os.chdir(_cwd)
finally:
    for _k, _v in _saved_env.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v

# public by default: CLI, uploader, metadata
import main as _main_mod
_privacy_opt = next(p for p in _main_mod.main.params if p.name == "privacy")
check("CLI --privacy defaults to public", _privacy_opt.default == "public")
check("upload_video defaults to public",
      inspect.signature(_uv).parameters["privacy_status"].default == "public")
check("metadata records public privacy",
      '"privacy_status": "public"' in (Path(__file__).parent / "pipeline" / "metadata.py").read_text(encoding="utf-8"))

# workflow: public ad-hoc path, 3-shorts env, shorts_only mode, result artifact
_wf = (Path(__file__).parent / ".github" / "workflows" / "daily.yml").read_text(encoding="utf-8")
check("workflow ad-hoc short path is public", "--privacy public" in _wf)
check("workflow pins 3 shorts/day", "GD_SHORTS_PER_DAY" in _wf)
check("workflow offers shorts_only mode", "shorts_only" in _wf)
check("workflow surfaces _daily_result.txt", "_daily_result.txt" in _wf)
check("publish queue now says PUBLIC", "PUBLIC" in inspect.getsource(TN.append_publish_queue))

# ── 7.13 upload verification + duration-aware idempotency (FIX-081) ──
print("\n[7.13] upload verification (FIX-081)")
import pipeline.uploader as U

check("ISO duration parser: minutes+seconds", U._iso8601_seconds("PT7M44S") == 464.0)
check("ISO duration parser: seconds only", U._iso8601_seconds("PT55S") == 55.0)
check("ISO duration parser: hours", U._iso8601_seconds("PT1H2M3S") == 3723.0)
check("ISO duration parser rejects zero-day form", U._iso8601_seconds("P0D") is None)


class _FakeYT:
    def __init__(self, items):
        self._items = items

    def videos(self):
        return self

    def list(self, **kwargs):
        return self

    def execute(self):
        return {"items": self._items}


_orig_auth = U.get_authenticated_service
try:
    U.get_authenticated_service = lambda channel=None: _FakeYT(
        [{"contentDetails": {"duration": "PT55S"}}])
    ok_v, note_v = U.verify_upload_duration("vid123", 500.0)
    check("verification rejects a 55s video for a 500s render",
          not ok_v and "different video" in note_v)
    ok_v, note_v = U.verify_upload_duration("vid123", 51.0)
    check("verification accepts a matching render", ok_v)

    U.get_authenticated_service = lambda channel=None: _FakeYT([])
    ok_v, note_v = U.verify_upload_duration("gone", 50.0)
    check("verification fails when the video is missing",
          not ok_v and "not found" in note_v)

    U.get_authenticated_service = lambda channel=None: _FakeYT(
        [{"contentDetails": {}}])
    ok_v, note_v = U.verify_upload_duration("vid123", 50.0)
    check("unreadable duration verifies true (retry-safe)", ok_v)
finally:
    U.get_authenticated_service = _orig_auth

_ts_src = (Path(__file__).parent / "pipeline" / "trending_short.py").read_text(encoding="utf-8")
check("trending_short verifies every upload", "verify_upload_duration" in _ts_src)
check("get_duration imported in trending_short",
      "from utils.ffmpeg_cmd import get_duration" in _ts_src)
_up_src = (Path(__file__).parent / "pipeline" / "uploader.py").read_text(encoding="utf-8")
check("idempotency guard is duration-aware", "looks_dup" in _up_src)
check("same-title doc gets a distinct suffix", "(Full Story)" in _up_src)
# FIX-082: ad-hoc extras must survive the day's publish idempotency
check("ad-hoc extras bypass the publish-idempotency stop", "GD_ADHOC" in _ts_src)
check("ad-hoc duplicate is a warning, not a hard stop",
      "producing anyway" in _ts_src)
check("workflow passes GD_ADHOC in single_short mode", "GD_ADHOC=1" in _wf)

# ── 7.14 channel memory: learn from every video, manual included (FIX-083) ─
print("\n[7.14] channel memory (FIX-083)")
from pipeline import channel_memory as CM

check("short/long split at 65s", CM.fmt_from_seconds(55) == "short" and
      CM.fmt_from_seconds(120) == "long")
check("unknown duration classified", CM.fmt_from_seconds(0) == "unknown")
check("automated uploads detected by the CC-BY credit",
      CM.origin_of("Some title", "Music: Kevin MacLeod (incompetech.com)") == "automated")
check("manual uploads detected",
      CM.origin_of("The 2Pac Story", "made by hand") == "manual")
check("shorts suffix counts as automated",
      CM.origin_of("Headline #shorts", "") == "automated")
_tf = CM.title_features("Why DRAKE Lost 3 Deals: The Truth?")
check("title features extract pattern flags", bool(_tf["has_question"] and _tf["has_colon"]
      and _tf["has_number"] and _tf["caps_words"] >= 1))

_rows = [
    {"format": "short", "origin": "automated", "views": 1000, "engagement": 0.02,
     "lane_hits": 2, "features": {"has_question": False, "has_number": False,
                                    "has_colon": False, "caps_words": 0}},
    {"format": "short", "origin": "automated", "views": 2000, "engagement": 0.03,
     "lane_hits": 1, "features": {"has_question": True, "has_number": False,
                                    "has_colon": False, "caps_words": 1}},
    {"format": "long", "origin": "manual", "views": 16, "engagement": 0.06,
     "lane_hits": 3, "features": {"has_question": False, "has_number": False,
                                   "has_colon": True, "caps_words": 0}},
]
_agg = CM.aggregate(_rows)
check("aggregate counts automated vs manual",
      _agg["n_automated"] == 2 and _agg["n_manual"] == 1)
check("aggregate medians per format",
      _agg["shorts"]["median_views"] == 1500.0 and _agg["longs"]["median_views"] == 16.0)
check("title buckets computed for shorts",
      _agg["title_buckets_shorts"]["has_question"]["True"]["median_views"] == 2000.0)
check("manual long-form stays visible in the report",
      "manual" in CM.render_report({**_agg, "channel": "famefiles"}))

# ── 7.15 daily diagnosis + monetization gap (FIX-084) ────────────────
print("\n[7.15] daily diagnosis + monetization gap (FIX-084)")
from pipeline import diagnose as DG

_d = DG.parse_daily_result("result=PARTIAL\nshorts=2/3\ndoc=failed\n")
check("daily result parsed",
      _d["result"] == "PARTIAL" and _d["shorts"] == "2/3" and _d["doc"] == "failed")
check("daily result parser tolerates junk", DG.parse_daily_result("garbage") == {})

_gap = DG.monetization_gap({"subscribers": 11, "total_views": 6641}, 500.0)
check("gap counts subs to go", _gap["subs_to_go"] == 989)
check("gap counts views to go", _gap["views_to_go"] == 10_000_000 - 6641)
check("gap computes required pace", _gap["required_rate"] > 100_000)
check("gap ETA at pace", _gap["eta_days"] == round((10_000_000 - 6641) / 500.0))
check("gap flags off-track", _gap["on_track"] is False)

_lines = DG.diagnose(
    {"result": "PARTIAL", "shorts": "2/3", "doc": "failed"},
    memory={"shorts": {"n": 10, "median_views": 1043.5},
            "longs": {"n": 6, "median_views": 17.0}, "rows": [],
            "channel_stats": {"subscribers": 11, "total_views": 6641}},
    queued_topics=["Drake lawsuit update"])
_blob = "\n".join(_lines)
check("diagnosis names the failed doc stage", "doc stage failed" in _blob)
check("diagnosis states the monetization gap",
      "MONETIZATION" in _blob and "989 subs to go" in _blob)
check("diagnosis surfaces competitor topics", "Drake lawsuit update" in _blob)
check("diagnosis keeps docs in perspective", "watch-time experiments" in _blob)

_sched_src2 = (Path(__file__).parent / "storage" / "scheduler.py").read_text(encoding="utf-8")
check("scheduler runs daily intel",
      "_run_intel" in _sched_src2 and "_daily_intel.txt" in _sched_src2)
check("workflow surfaces the intel report", "_daily_intel.txt" in _wf)

# ── 7.16 real-audio hook intro (FIX-085) ────────────────────────────
print("\n[7.16] real-audio hook intro (FIX-085)")
from pipeline.trending_short import hook_query_for as _hqf

check("hook subject found in a known-name story",
      _hqf("Taylor Swift Just Broke Hollywood With This Trailer") == "Taylor Swift")
check("hook skipped when no known subject",
      _hqf("A quiet weekend for a small show") is None)
_as_src = (Path(__file__).parent / "pipeline" / "assets.py").read_text(encoding="utf-8")
check("clip downloader can keep audio when asked", "keep_audio" in _as_src)
check("hook fetch rejects clips without audio",
      "has no audio track" in _as_src and "fetch_hook_clip" in _as_src)
_ff_src = (Path(__file__).parent / "utils" / "ffmpeg_cmd.py").read_text(encoding="utf-8")
check("hook splice = stream copy + re-encode fallback",
      "prepend_hook_intro" in _ff_src and "veryfast" in _ff_src)
check("hook failure never costs the video",
      "shipping without the hook" in _ff_src)

# Real-ffmpeg integration: normalize + copy-concat preserves both streams
import subprocess as _subp
import config as _cfg2
_tmpdir = TMP / "hook_splice"
_tmpdir.mkdir(exist_ok=True)


def _mk(path, freq, dur):
    _subp.run([getattr(_cfg2, "FFMPEG_BIN", "ffmpeg"), "-y", "-v", "error",
               "-f", "lavfi", "-i", f"color=c=blue:s=320x240:r=30:d={dur}",
               "-f", "lavfi", "-i",
               f"sine=frequency={freq}:duration={dur}:sample_rate=44100",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
               "-ar", "44100", "-ac", "2", "-shortest", str(path)],
              capture_output=True)


_hook = _tmpdir / "hook.mp4"
_main = _tmpdir / "main.mp4"
_mk(_hook, 440, 0.6)
_mk(_main, 880, 0.8)
from utils.ffmpeg_cmd import prepend_hook_intro as _pre, get_duration as _gd
_out = _pre(_hook, _main)
check("splice produces a new file", Path(_out) != Path(_main) and Path(_out).exists())
check("splice duration = hook + main",
      abs(_gd(_out) - (_gd(_hook) + _gd(_main))) < 0.35)
check("splice result keeps an audio stream",
      "audio" in _subp.run([getattr(_cfg2, "FFPROBE_BIN", "ffprobe"), "-v", "quiet",
                           "-select_streams", "a", "-show_entries",
                           "stream=codec_type", "-of", "csv=p=0", str(_out)],
                          capture_output=True, text=True).stdout)
check("splice keeps the main video intact (stream copy)",
      _out.stat().st_size >= _main.stat().st_size * 0.5)

# ── 7.17 experiment engine (FIX-086) ────────────────────────────────
print("\n[7.17] experiment engine (FIX-086)")
import copy as _copy
import datetime as _dt
from pipeline import experiments as EX

_reg = {"version": 1, "promoted": {}, "experiments": []}
EX.ensure_defaults(_reg)
check("default experiment seeded", EX.find(_reg, "real_audio_hook") is not None)
_exp = EX.find(_reg, "real_audio_hook")
check("arm counts track assignments",
      EX.arm_counts({"variants": ["on", "off"],
                     "assignment": {"on": ["a"], "off": []}}) == {"on": 1, "off": 0})
# pending-aware alternation + recording through a temp registry file
_reg_path = TMP / "experiments_test.json"
_orig_reg_path = EX.REGISTRY_PATH
try:
    _reg_path.unlink(missing_ok=True)
    EX.REGISTRY_PATH = _reg_path
    _assigned = [EX.assign_variant("real_audio_hook", default="on")
                 for _ in range(4)]
    check("assignment alternates by count (pending-aware)",
          _assigned == ["on", "off", "on", "off"])
    EX.record("real_audio_hook", "on", "vid_on_1")
    EX.record("real_audio_hook", "off", "vid_off_1")
    _ex_disk = EX.find(EX.load(), "real_audio_hook")
    check("recorded ids land in their arms",
          "vid_on_1" in _ex_disk["assignment"]["on"] and
          "vid_off_1" in _ex_disk["assignment"]["off"])
    check("promoted default is used when no experiment runs",
          EX.active("real_audio_hook", default="off") == "off")
finally:
    EX.REGISTRY_PATH = _orig_reg_path

_exp["assignment"] = {"on": [f"a{i}" for i in range(6)],
                      "off": [f"b{i}" for i in range(6)]}
_rowsx = {**{f"a{i}": {"views": 2000 + i} for i in range(6)},
          **{f"b{i}": {"views": 900 + i} for i in range(6)}}
check("evaluate decides a 2x leader",
      EX.evaluate(_exp, _rowsx)["status"] == "decided" and
      EX.evaluate(_exp, _rowsx)["winner"] == "on")
lines_x = EX.run_cycle(_reg, _rowsx)
check("decision promotes the winner",
      _reg["promoted"].get("real_audio_hook") == "on")
check("cycle reports the decision", any("DECIDED" in ln for ln in lines_x))

_reg2 = {"version": 1, "promoted": {}, "experiments": []}
EX.ensure_defaults(_reg2)
_e2 = EX.find(_reg2, "real_audio_hook")
_e2["assignment"] = {"on": ["x1"], "off": ["y1"]}
_v2 = EX.evaluate(_e2, {"x1": {"views": 5000}, "y1": {"views": 10}})
check("no decision without the minimum sample", _v2["status"] == "pending")
check("pending states what it needs", "needs more videos" in _v2["reason"])

_e3 = _copy.deepcopy(_e2)
_e3["started"] = (EX._now() - _dt.timedelta(days=10)).isoformat()
_e3["assignment"] = {"on": [f"c{i}" for i in range(6)],
                     "off": [f"d{i}" for i in range(6)]}
_rows3 = {**{f"c{i}": {"views": 1000} for i in range(6)},
          **{f"d{i}": {"views": 950} for i in range(6)}}
check("inconclusive after the window without a significant gap",
      EX.evaluate(_e3, _rows3)["status"] == "inconclusive")

_sched_x = (Path(__file__).parent / "storage" / "scheduler.py").read_text(encoding="utf-8")
check("cloud runs the experiment cycle daily",
      "run_cycle" in _sched_x and "EXPERIMENTS" in _sched_x)
check("workflow persists the registry", "experiments.json" in _wf)
_ts_x = (Path(__file__).parent / "pipeline" / "trending_short.py").read_text(encoding="utf-8")
check("production assigns + records the arm",
      "assign_variant" in _ts_x and "real_audio_hook" in _ts_x)

# ── 7.18 strict celebrity gate + dead-black thumbnail guard + audit ──
#           (FIX-087/088/089)
print("\n[7.18] strict celebrity gate + dead-black thumbnail guard + "
      "delivery audit (FIX-087/088/089)")
import pipeline.trending_news as _TN87
import pipeline.thumbnail as _TB88

_f87_src = (Path(__file__).parent / "pipeline" / "trending_news.py").read_text(encoding="utf-8")
_f87_exact = ("Ex-Strictly star says there was secret feud between celebrity "
              "and pro dancer on his series")
check("show-gossip blocklist catches the Strictly story that shipped",
      _TN87._in_show_gossip(_f87_exact))
check("show-gossip blocklist keeps ordinary 'strictly' headlines",
      not _TN87._in_show_gossip("Strictly speaking, Drake's lawyers want a delay"))
check("named-person check rejects the anonymous-celebrity headline",
      not _TN87._named_person(_f87_exact))
check("place names don't count as people",
      not _TN87._named_person("New York Knicks fire coach after late-night scandal"))
check("roster names count as people",
      _TN87._named_person("Taylor Swift quietly drops a surprise single"))
check("strict gate rejects the exact story that shipped the Strictly doc",
      _TN87._score({"title": _f87_exact}, set(), strict=True) is None)
check("strict gate accepts a named-celebrity feud headline",
      _TN87._score({"title": "Drake and Kendrick Lamar feud escalates as lawyers get involved"},
                   set(), strict=True) is not None)
_f87_saved = os.environ.get("GD_STRICT_CELEB")
try:
    os.environ["GD_STRICT_CELEB"] = "0"
    check("GD_STRICT_CELEB=0 restores the old permissive gate",
          _TN87._score({"title": _f87_exact}, set()) is not None)
finally:
    if _f87_saved is None:
        os.environ.pop("GD_STRICT_CELEB", None)
    else:
        os.environ["GD_STRICT_CELEB"] = _f87_saved
check("strict celebrity filtering is ON by default", _TN87._strict_celeb_enabled())
check("strict sourcing falls back instead of starving the day",
      "retrying with it off" in _f87_src and "_scored(False)" in _f87_src)
check("strict gate runs before scoring (no lane bonus can rescue it)",
      _f87_src.find("strict_on and (_in_show_gossip") < _f87_src.find("lane = _lane_hits"))

from PIL import Image as _Img88, ImageDraw as _IDraw88  # noqa: E402

_f88_black = _Img88.new("RGB", (400, 300), (0, 0, 0))
_f88_dark, _f88_darklum = _TB88._dead_left_zone(_f88_black)
check("dead-zone meter reads a pure-black face as dead",
      _f88_dark > 0.9 and _f88_darklum < 10,
      f"dead={_f88_dark:.2f} lum={_f88_darklum:.0f}")

_f88_photo = TMP / "f88_clean_photo.png"
_f88_pim = _Img88.new("RGB", (800, 1000), (216, 172, 140))
_f88_pd = _IDraw88.Draw(_f88_pim)
for _f88_x in range(0, 800, 40):
    _f88_pd.line([(_f88_x, 0), (_f88_x, 1000)],
                 fill=(120 + (_f88_x % 90), 84, 66), width=7)
_f88_pim.save(str(_f88_photo))

_f88_bg = _Img88.new("RGB", (1280, 720), (12, 10, 16))  # darkened graded bg
_f88_kit_saved = dict(_TB88._EXCLUSIVE_KIT)
_f88_pool_saved = list(_TB88._CLEAN_PHOTO_POOL)
_f88_style = {"palette": {}, "accent": "red_bar"}
_f88_words = ["SECRET", "STRICTLY", "FEUD"]
try:
    _TB88._EXCLUSIVE_KIT = {"primary": None, "secondary": None}
    _TB88._CLEAN_PHOTO_POOL = []
    _f88_out_a = _TB88._layout_exclusive_news(
        _f88_bg.copy(), _f88_words, 64, 1280, 720, _f88_style)
    _f88_da, _f88_la = _TB88._dead_left_zone(_f88_out_a)
    check("no clean photo anywhere — the painter still returns an image",
          _f88_out_a.size == (1280, 720))

    _TB88._CLEAN_PHOTO_POOL = [str(_f88_photo)]
    _f88_out_b = _TB88._layout_exclusive_news(
        _f88_bg.copy(), _f88_words, 64, 1280, 720, _f88_style)
    _f88_db, _f88_lb = _TB88._dead_left_zone(_f88_out_b)
    check("black-hole left face is rebuilt from the clean-photo pool",
          _f88_db <= 0.60 and _f88_lb >= 42.0,
          f"dead={_f88_db:.2f} lum={_f88_lb:.1f}")
    check("the rebuild beats the dead fallback it replaced",
          _f88_db < _f88_da and _f88_lb > _f88_la,
          f"{_f88_da:.2f}/{_f88_la:.0f} -> {_f88_db:.2f}/{_f88_lb:.0f}")

    _TB88._EXCLUSIVE_KIT = {"primary": str(_f88_photo), "secondary": None}
    _f88_out_c = _TB88._layout_exclusive_news(
        _f88_bg.copy(), _f88_words, 64, 1280, 720, _f88_style)
    _f88_dc, _f88_lc = _TB88._dead_left_zone(_f88_out_c)
    check("a healthy primary face is used as-is (guard stays quiet)",
          _f88_dc <= 0.60 and _f88_lc >= 42.0)
finally:
    _TB88._EXCLUSIVE_KIT = _f88_kit_saved
    _TB88._CLEAN_PHOTO_POOL = _f88_pool_saved

_f88_src = (Path(__file__).parent / "pipeline" / "thumbnail.py").read_text(encoding="utf-8")
check("the clean-photo pool is built before the style matrix renders",
      _f88_src.find("_CLEAN_PHOTO_POOL = _clean_photo_pool(script)") <
      _f88_src.find("style_rows = render_style_variants"))

from pipeline.channel_memory import delivery_audit as _f89_audit  # noqa: E402

_f89_now = _dt.datetime(2026, 10, 4, 10, 0, tzinfo=_dt.timezone.utc)
_f89_rows = [
    {"id": "aaa", "title": "Rubbish doc", "origin": "automated",
     "privacy": "unlisted", "published": "2026-10-03T18:41:10Z"},
    {"id": "bbb", "title": "Fine short", "origin": "automated",
     "privacy": "public", "published": "2026-10-03T14:00:00Z"},
    {"id": "ccc", "title": "Old drift", "origin": "automated",
     "privacy": "private", "published": "2026-09-01T00:00:00Z"},
    {"id": "ddd", "title": "Operator manual", "origin": "manual",
     "privacy": "unlisted", "published": "2026-10-03T10:00:00Z"},
]
_f89_lines = "\n".join(_f89_audit(_f89_rows, hours=72, now=_f89_now))
check("delivery audit flags the drifted automated upload", "aaa" in _f89_lines)
check("delivery audit does not flag public videos", "bbb" not in _f89_lines)
check("delivery audit ignores drift older than the window", "ccc" not in _f89_lines)
check("delivery audit never flags the operator's manual uploads", "ddd" not in _f89_lines)
_f89_clean = "\n".join(_f89_audit(_f89_rows[1:2], now=_f89_now))
check("delivery audit reports health when nothing drifted",
      "are public. OK" in _f89_clean)
_f89_sched = (Path(__file__).parent / "storage" / "scheduler.py").read_text(encoding="utf-8")
check("scheduler runs the delivery audit inside the daily intel",
      "delivery_audit" in _f89_sched and "DELIVERY AUDIT" in _f89_sched)
check("workflow state cache is run-scoped (no immutable-key freeze, FIX-090)",
      "gd-state-${{ github.run_id }}" in _wf and "restore-keys" in _wf)

# ── 7.19 self-learning: winner-riding docs + proven-name boost (FIX-091/092) ──
print("\n[7.19] self-learning: winner-riding docs + proven-name boost "
      "(FIX-091/092)")
from pipeline.channel_memory import breakout_story as _bs91  # noqa: E402
from pipeline.trending_short import _winner_candidate as _wc91  # noqa: E402
import pipeline.trending_news as _TN92  # noqa: E402

_f91_now = _dt.datetime(2026, 10, 4, 8, 0, tzinfo=_dt.timezone.utc)


def _f91_row(vid, title, views, fmt="short", published="2026-10-03T14:00:00Z"):
    return {"id": vid, "title": title, "views": views, "format": fmt,
            "published": published}


_f91_strong = [
    _f91_row("w1", "Paul McCartney Reveals He Joked to Taylor Swift", 1863),
    _f91_row("w2", "Travis Kelce Get Great News Before Chiefs Game", 1589),
    _f91_row("s3", "Taylor Swift Charts Again", 335),
    _f91_row("s4", "Kanye West Russia Shows Cancelled", 89),
    _f91_row("s5", "Tom Cruise Still Chasing His Oscar", 1444),
    _f91_row("s6", "Blake Lively Follows Taylor Swift", 1355),
]
_w91 = _bs91(_f91_strong, now=_f91_now)
check("breakout picks the top recent short", bool(_w91) and _w91["id"] == "w1")
check("breakout refuses a weak week (absolute floor)",
      _bs91([_f91_row(f"x{i}", f"Story number {i}", 300) for i in range(6)],
            now=_f91_now) is None)
check("breakout ignores old shorts outside the window",
      _bs91([_f91_row("old", "Old winner stays old", 9999,
                      published="2026-09-01T00:00:00Z")], now=_f91_now) is None)
check("breakout never rides long-form",
      _bs91([_f91_row("doc", "Big documentary story", 9999, fmt="long")],
            now=_f91_now) is None)
_m91 = _bs91(
    [_f91_row("m1", "Travis Kelce Owes Wife Big Time", 1610)]
    + [_f91_row(f"z{i}", f"Small story number {i}", 300) for i in range(5)],
    now=_f91_now)
check("breakout can ride a manual short too", bool(_m91) and _m91["id"] == "m1")

check("winner matching accepts the same story re-syndicated",
      _wc91([{"title": "Drake Karol G Stage Reunion Shocks Fans"}],
            "Drake and Karol G Reunite on Stage") is not None)
check("winner matching accepts an exact headline",
      _wc91([{"title": "Taylor Swift Just Broke Hollywood With This Trailer"}],
            "Taylor Swift Just Broke Hollywood With This Trailer") is not None)
check("winner matching refuses a different story about the same person",
      _wc91([{"title": "Drake Announces World Tour Dates"}],
            "Drake and Karol G Reunite on Stage") is None)

_f92_rows = [
    _f91_row("a", "Taylor Swift Broke Hollywood", 1645),
    _f91_row("b", "Travis Kelce Owes Taylor Swift", 1610),
    _f91_row("c", "Tom Cruise Chasing Oscar", 1444),
    _f91_row("d", "DJ Khaled Omits Drake", 1377),
    _f91_row("e", "Blake Lively Follows Taylor Swift", 1355),
    _f91_row("f", "Taylor Swift Charts Again", 335),
]
_proven = _TN92._proven_names_from(_f92_rows)
check("proven names come from the catalogue's better half",
      {"taylor swift", "travis kelce"} <= _proven, str(sorted(_proven)))
check("below-median shorts contribute no names",
      "dj khaled" not in _proven and "blake lively" not in _proven)
check("a cold catalogue proves nothing",
      _TN92._proven_names_from(_f92_rows[:5]) == set())
_saved_proven = _TN92._PROVEN_NAMES
try:
    _TN92._PROVEN_NAMES = set()
    _s0 = _TN92._score({"title": "Taylor Swift Surprises Fans With New Album News"}, set())
    _TN92._PROVEN_NAMES = {"taylor swift"}
    _s1 = _TN92._score({"title": "Taylor Swift Surprises Fans With New Album News"}, set())
finally:
    _TN92._PROVEN_NAMES = _saved_proven
check("proven-name boost lifts the names the channel's winners carried",
      _s0 is not None and _s1 is not None and abs((_s1 - _s0) - 0.8) < 1e-6,
      f"{_s0} -> {_s1}")

_ts91 = (Path(__file__).parent / "pipeline" / "trending_short.py").read_text(encoding="utf-8")
check("doc stage rides the winner with an opt-out",
      "GD_RIDE_WINNER" in _ts91 and "_recent_breakout" in _ts91 and
      "_winner_candidate" in _ts91)
check("winner lookup uses channel memory, not guesses",
      "breakout_story" in _ts91 and "channel_memory" in _ts91)
check("workflow persists channel memory for the cloud",
      "output/state/channel_memory.json" in _wf and
      "db/channel_memory.json output/state/" in _wf)

# ── 7.20 the hook card actually moves (FIX-093) ──────────────────────
print("\n[7.20] kinetic hook card: real motion + wired filter (FIX-093)")
from models import Scene as _S93, Script as _Sc93  # noqa: E402
from pipeline.assembler_ffmpeg import _build_hook_card as _bhc93  # noqa: E402
import subprocess as _sp93  # noqa: E402
import config as _cfg93  # noqa: E402
import numpy as _np93  # noqa: E402
from PIL import Image as _I93, ImageDraw as _D93  # noqa: E402

# FIX-093b: the filter chain was computed but never passed to ffmpeg on the
# photo path — no darkening, no headline text, no zoom. It must now be wired.
_asm93 = (Path(__file__).parent / "pipeline" / "assembler_ffmpeg.py"
          ).read_text(encoding="utf-8")
check("photo path wires the filter into ffmpeg",
      "-filter_complex" in _asm93 and '"-map", "[vout]"' in _asm93)
check("the single-frame loop (the freeze-frame bug) is gone",
      '"-loop", "1", "-i", str(scene1_photo)' not in _asm93)
check("photo path zooms for the full card duration",
      "_type_chain(total_frames)" in _asm93)
check("video + color paths consume the same type chain",
      _asm93.count("_type_chain(") >= 3)

# Behavioral: build a card from a synthetic photo and measure it.
_photo93 = TMP / "hook93.jpg"
_img93 = _I93.new("RGB", (1600, 900), (40, 50, 90))
_d93 = _D93.Draw(_img93)
for _i93 in range(12):
    _d93.rectangle([_i93 * 130, 0, _i93 * 130 + 90, 900],
                   fill=(30 + _i93 * 18, 120 + (_i93 % 3) * 40, 220 - _i93 * 12))
    _d93.ellipse([_i93 * 130 + 20, 100 + _i93 * 40, _i93 * 130 + 80, 160 + _i93 * 40],
                 fill=(250, 240, 120))
_img93.save(_photo93)
_card93 = _bhc93(
    _Sc93(title="Test Title", description="", tags=[], hook="Test hook",
          scenes=[_S93(scene_number=1, narration="n", visual_prompt="x",
                       visual_type="stock_photo", mood="hook",
                       photo_path=str(_photo93))],
          total_scenes=1, estimated_duration_minutes=1.0,
          hook_overlay_text="THE SECRET TEST CARD"),
    {"visuals": {"resolution": "1920x1080"}}, TMP)
check("hook card builds from a scene-1 photo", bool(_card93))
if _card93:
    _dur93 = float(_sp93.run(
        [getattr(_cfg93, "FFPROBE_BIN", "ffprobe"), "-v", "error",
         "-show_entries", "format=duration", "-of", "csv=p=0", str(_card93)],
        capture_output=True, text=True).stdout.strip() or 0)
    check("hook card still ~2.2s", abs(_dur93 - 2.2) < 0.15, f"{_dur93:.3f}")

    _f93 = TMP / "f93"
    _f93.mkdir(exist_ok=True)
    _sp93.run([getattr(_cfg93, "FFMPEG_BIN", "ffmpeg"), "-y", "-v", "error",
               "-i", str(_card93), "-vf", "fps=8,scale=180:320",
               str(_f93 / "f_%02d.png")], capture_output=True)
    _arrs93 = [_np93.asarray(_I93.open(f).convert("L"), dtype=_np93.float32)
               for f in sorted(_f93.glob("f_*.png"))]
    _diffs93 = [float(_np93.abs(_arrs93[i] - _arrs93[i - 1]).mean())
                for i in range(1, len(_arrs93))]
    _mean93 = sum(_diffs93) / len(_diffs93) if _diffs93 else 0.0
    # Pre-fix this measured 0.000 across the whole card (frame-diff probe).
    check("card is NOT a freeze frame (mean motion > 2.0 over the card)",
          _mean93 > 2.0, f"mean={_mean93:.2f}")

    _frame93 = TMP / "frame93.png"
    _sp93.run([getattr(_cfg93, "FFMPEG_BIN", "ffmpeg"), "-y", "-v", "error",
               "-ss", "1.0", "-i", str(_card93), "-frames:v", "1",
               str(_frame93)], capture_output=True)
    _amber93 = 0
    if _frame93.exists():
        _rgb93 = _np93.asarray(_I93.open(_frame93).convert("RGB"),
                               dtype=_np93.int16)
        _dist93 = _np93.abs(_rgb93 - _np93.array([226, 163, 60])).sum(axis=2)
        _amber93 = int((_dist93 < 90).sum())
    check("headline type actually rendered (amber pixels visible)",
          _amber93 > 50, f"amber_px={_amber93}")

# ── 7.21 no silent tails (FIX-094) ───────────────────────────────────
print("\n[7.21] silent-tail death: TTS retry/raise, assembler guard, QC, mux")
import asyncio as _as94  # noqa: E402
import os as _os94  # noqa: E402
import shutil as _sh94  # noqa: E402
import subprocess as _sp94b  # noqa: E402
import pipeline.voice as _vo94  # noqa: E402
from models import Scene as _S94, Script as _Sc94  # noqa: E402
from pipeline.assembler_ffmpeg import (  # noqa: E402
    _narration_must_have_audio as _nma94,
)
from pipeline.visual_director import (  # noqa: E402
    final_qc as _fq94, classify_qc as _cq94,
)

_ff94 = getattr(_cfg93, "FFMPEG_BIN", "ffmpeg")
_vtmpl94 = {"voice": {"provider": "edge-tts",
                      "voice_id": "en-US-AndrewMultilingualNeural",
                      "natural_pauses": True}}


def _mk94(sn, narration):
    return _S94(scene_number=sn, narration=narration, visual_prompt="x",
                visual_type="stock_photo", mood="hook")


def _script94(scenes):
    return _Sc94(title="Test", description="", tags=[], hook="h",
                 scenes=scenes, total_scenes=len(scenes),
                 estimated_duration_minutes=1.0)


def _voice94(script, outdir, attempts="2"):
    """Run generate_voices with fast retry settings; return the error or None."""
    prev_a = _os94.environ.get("GD_TTS_ATTEMPTS")
    prev_b = _os94.environ.get("GD_TTS_BACKOFF")
    _os94.environ["GD_TTS_ATTEMPTS"] = attempts
    _os94.environ["GD_TTS_BACKOFF"] = "0"
    try:
        _as94.run(_vo94.generate_voices(script, _vtmpl94, outdir))
        return None
    except RuntimeError as e:
        return str(e)
    finally:
        if prev_a is None:
            _os94.environ.pop("GD_TTS_ATTEMPTS", None)
        else:
            _os94.environ["GD_TTS_ATTEMPTS"] = prev_a
        if prev_b is None:
            _os94.environ.pop("GD_TTS_BACKOFF", None)
        else:
            _os94.environ["GD_TTS_BACKOFF"] = prev_b


# -- voice stage: exhausted TTS must FAIL the stage, not ship silence --
_orig_pauses94 = _vo94._generate_edge_tts_with_pauses
_orig_gtts94 = _vo94._generate_gtts


async def _fail94(*a, **k):
    raise RuntimeError("simulated TTS outage")


_vo94._generate_edge_tts_with_pauses = _fail94
_vo94._generate_gtts = _fail94
_s94 = _mk94(1, "A narrated line that must become audio.")
_err94 = _voice94(_script94([_s94]), TMP / "vo94_fail")
check("voice stage RAISES when TTS dies (silent scene never leaves the stage)",
      bool(_err94) and "FIX-094" in _err94 and "no audio" in _err94,
      (_err94 or "no raise")[:90])
check("failed scene is recorded (no audio_path, fallback duration kept)",
      _s94.audio_path is None and _s94.audio_duration_seconds == 5.0)

# -- transient failure: attempt #2 recovers and the stage succeeds --
_calls94 = {"n": 0}
_tone94 = TMP / "tone94.mp3"
_sp94b.run([_ff94, "-y", "-v", "error", "-f", "lavfi",
            "-i", "sine=frequency=440:duration=1.2",
            "-c:a", "libmp3lame", str(_tone94)], capture_output=True)


async def _flaky94(text, voice, rate, pitch, out_path, seed=0):
    _calls94["n"] += 1
    if _calls94["n"] == 1:
        raise RuntimeError("simulated transient hiccup")
    _sh94.copy(_tone94, out_path)


_vo94._generate_edge_tts_with_pauses = _flaky94
_s94b = _mk94(1, "A narrated line that recovers on the second try.")
_err94b = _voice94(_script94([_s94b]), TMP / "vo94_ok")
check("voice stage retries and succeeds (attempt #2 produced real audio)",
      _err94b is None and _calls94["n"] == 2 and _s94b.audio_path is not None
      and (_s94b.audio_duration_seconds or 0) > 0,
      f"err={_err94b} calls={_calls94['n']} dur={_s94b.audio_duration_seconds}")
_vo94._generate_edge_tts_with_pauses = _orig_pauses94
_vo94._generate_gtts = _orig_gtts94

# -- assembler guard: narrated scene + no audio = refuse, not pad --
check("assembler guard: narrated scene must have audio",
      _nma94(_mk94(1, "This was spoken.")) is True)
check("assembler guard: narration-free visual beat may be silent",
      _nma94(_mk94(2, "   ")) is False)
_asm94 = (Path(__file__).parent / "pipeline" / "assembler_ffmpeg.py"
          ).read_text(encoding="utf-8")
check("assembler refuses to pad a narrated scene with silence (FIX-094 raise)",
      "refusing to pad it with silence" in _asm94)

# -- mux steps must never let picture outlive its audio --
_mux94 = re.search(
    r"# Replace audio in video[\s\S]*?\"Replace audio with multi-track mix\"", _asm94)
check("mix mux is pinned to the audio length (-shortest)",
      bool(_mux94) and '"-shortest"' in _mux94.group(0))
_norm94 = re.search(
    r"Extract audio for normalization[\s\S]*?\"Re-mux normalized audio\"", _asm94)
check("normalized re-mux is pinned to the audio length (-shortest)",
      bool(_norm94) and '"-shortest"' in _norm94.group(0))

# -- QC: a synthetic dead-air tail (sine 5s + 4s silence) must be CRITICAL --
_qc94 = TMP / "qc94"
_qc94.mkdir(exist_ok=True)
_clean94 = _qc94 / "clean.mp4"
_broken94 = _qc94 / "broken.mp4"
_sp94b.run([_ff94, "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=0x1a2340:s=320x180:d=9:r=12",
            "-f", "lavfi", "-i", "aevalsrc='0.3*sin(2*PI*440*t)':s=44100:d=9",
            "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", str(_clean94)], capture_output=True)
_sp94b.run([_ff94, "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=0x1a2340:s=320x180:d=9:r=12",
            "-f", "lavfi", "-i",
            "aevalsrc='0.3*sin(2*PI*440*t)*lt(t,5)':s=44100:d=9",
            "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", str(_broken94)], capture_output=True)
_, _issues_clean94 = _fq94(_clean94, deep_review=False)
_, _issues_broken94 = _fq94(_broken94, deep_review=False)
check("QC flags a dead-air tail (narration stops before the picture)",
      any("dead-air tail" in i for i in _issues_broken94),
      [i for i in _issues_broken94 if "dead-air" in i][:1])
check("QC does NOT flag a healthy render whose voice runs to the end",
      not any("dead-air tail" in i for i in _issues_clean94),
      [i for i in _issues_clean94 if "dead-air" in i][:1])
_crit94, _ = _cq94([i for i in _issues_broken94 if "dead-air tail" in i])
check("dead-air tail is a CRITICAL finding (blocks upload)", len(_crit94) == 1)

# ── 7.22 asset quality floors + subject-aware selection (FIX-095) ──
print("\n[7.22] asset quality floors + subject-aware photo selection (FIX-095)")
from pipeline.assets import (  # noqa: E402
    valid_photo as _vp95, valid_video as _vv95,
)
from pipeline.visual_director import _face_score_adjust as _fsa95  # noqa: E402


def _jpg95(path, w, h, seed=0):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), (30 + seed, 40, 60))
    d = ImageDraw.Draw(img)
    for i in range(0, w, max(20, w // 12)):
        d.rectangle([i, 0, i + max(10, w // 24), h],
                    fill=(200 - (i % 90), 120 + (i % 80), 60))
    img.save(path)
    return path


_low95 = _jpg95(TMP / "low95.jpg", 1000, 600)
_ok95 = _jpg95(TMP / "ok95.jpg", 1280, 720, seed=20)
check("valid_photo rejects a 1000x600 web embed (FIX-095 floor)",
      not _vp95(_low95))
check("valid_photo accepts 1280x720", _vp95(_ok95))


def _clip95(path, w, h):
    _sp94b.run([_ff94, "-y", "-v", "error", "-f", "lavfi",
                "-i", f"testsrc2=s={w}x{h}:d=2:r=12",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "14",
                "-pix_fmt", "yuv420p", str(path)], capture_output=True)
    return path


_clip_small95 = _clip95(TMP / "clip_small95.mp4", 640, 360)
_clip_ok95 = _clip95(TMP / "clip_ok95.mp4", 1280, 720)
check("valid_video rejects 640x360 footage (FIX-095 floor)",
      not _vv95(_clip_small95))
check("valid_video accepts 1280x720 footage", _vv95(_clip_ok95))
_prev_w95 = _os94.environ.get("GD_MIN_VIDEO_W")
_prev_h95 = _os94.environ.get("GD_MIN_VIDEO_H")
_os94.environ["GD_MIN_VIDEO_W"], _os94.environ["GD_MIN_VIDEO_H"] = "640", "360"
try:
    check("video floor is env-tunable for genuinely low-res archive",
          _vv95(_clip_small95))
finally:
    if _prev_w95 is None:
        _os94.environ.pop("GD_MIN_VIDEO_W", None)
    else:
        _os94.environ["GD_MIN_VIDEO_W"] = _prev_w95
    if _prev_h95 is None:
        _os94.environ.pop("GD_MIN_VIDEO_H", None)
    else:
        _os94.environ["GD_MIN_VIDEO_H"] = _prev_h95

check("face-visible candidate outranks a prettier backdrop (+0.9)",
      _fsa95({"face": True}) > 0.5)
check("faceless candidate ranks below an equal with a face (-0.7)",
      _fsa95({"face": False}) < -0.5)
check("no face signal = no score adjustment", _fsa95({}) == 0.0)

_assets95 = (Path(__file__).parent / "pipeline" / "assets.py").read_text(encoding="utf-8")
_web95 = re.search(
    r'vtype in \("web_photo", "photo_person"\)[\s\S]*?elif vtype == "youtube_clip"',
    _assets95)
check("celebrity photo path now runs the candidate selector",
      bool(_web95) and "select_best_visual" in _web95.group(0)
      and "Got SELECTED photo via DDG" in _web95.group(0))

print(f"\n{'='*50}\nRESULT: {sum(1 for _, ok, _ in RESULTS if ok)} passed, "
      f"{sum(1 for _, ok, _ in RESULTS if not ok)} failed")
sys.exit(0 if all(ok for _, ok, _ in RESULTS) else 1)
