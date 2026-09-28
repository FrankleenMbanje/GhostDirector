"""Phase 2 unit tests — run with: venv/Scripts/python.exe test_phase2_units.py

Covers (UPGRADE_PLAN.md §8 Phase 2):
  2.6  Anchor extraction (years/decades/money/percent/age/figures),
       corpus matching + tolerance, report shape, deterministic strip fallback
  2.8  B-roll memory: record/load/rotate, bounded ledger
"""

import sys
import json
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import config
from models import Script, Scene, ResearchResult
from pipeline.anchors import (
    extract_anchors, validate_script_anchors, _corpus_text, _corpus_values,
    _parse_word_number,
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


def make_research(**kw) -> ResearchResult:
    defaults = dict(
        topic="Test",
        summary="In 1990 he founded the company. By 2019 it was worth $1.2 billion.",
        timeline=[
            {"date": "1990", "event": "Company founded in New York"},
            {"date": "2019", "event": "Net worth reached $1.2 billion"},
        ],
        key_people=[{"name": "Jane Doe", "role": "Founder", "search_query": "Jane Doe"}],
        key_facts=[
            "The company was valued at $1.2 billion in 2019",
            "She was 34 years old at the time",
            "Sales grew 42 percent year over year",
            "The archive contained 50,000 pages of documents",
        ],
        narrative_arc="Rise and fall",
        source_queries=["test topic"],
    )
    defaults.update(kw)
    return ResearchResult(**defaults)


def make_scene(n, narration, **kw) -> Scene:
    defaults = dict(
        scene_number=n, narration=narration, visual_prompt="x",
        visual_type="stock_video", mood="dramatic",
    )
    defaults.update(kw)
    return Scene(**defaults)


# ── 2.6 extraction ────────────────────────────────────────────────────
print("\n[2.6] Anchor extraction")
a = extract_anchors("In 1990 he started. By 2019 it collapsed.", 1)
check("years extracted", {x.value for x in a} == {1990.0, 2019.0}, str(a))

a = extract_anchors("In the 1990s everything changed.", 1)
check("decade extracted", a and a[0].claim_type == "decade" and a[0].value == 1990.0, str(a))
a = extract_anchors("From the 1990s through 1995 he thrived.", 1)
check("year inside decade not double-flagged",
      len([x for x in a if x.claim_type == "year"]) == 0, str(a))

a = extract_anchors("It was worth $1.2 billion. Then $900,000 more. Roughly $3.5B total.", 1)
vals = sorted(x.value for x in a)
check("money with suffixes", vals == [900000.0, 1200000000.0, 3500000000.0], str(vals))

a = extract_anchors("He earned nine hundred million dollars.", 1)
check("word-number money", a and a[0].value == 900000000.0, str(a))

a = extract_anchors("Sales grew 42 percent. Then 37% more.", 1)
check("percent both forms", {x.value for x in a} == {42.0, 37.0}, str(a))

a = extract_anchors("She was 34 years old. At the age of 45 he quit. He died aged 77.", 1)
check("ages all forms", {x.value for x in a} == {34.0, 45.0, 77.0}, str(a))

a = extract_anchors("The archive held 50,000 pages and 120000 photos.", 1)
check("large figures", {x.value for x in a} == {50000.0, 120000.0}, str(a))

check("word parser: two hundred", _parse_word_number("two hundred") == 200.0)
check("word parser: twenty two", _parse_word_number("twenty two") == 22.0)
check("word parser: one point two", abs(_parse_word_number("one point two") - 1.2) < 0.01)

# ── 2.6 validation ────────────────────────────────────────────────────
print("\n[2.6] Validation against research payload")
research = make_research()

clean = Script(
    title="T", description="", tags=[], hook="",
    scenes=[
        make_scene(1, "Founded in 1990 in a small garage."),
        make_scene(2, "By 2019 the company was worth $1.2 billion."),
        make_scene(3, "She was 34 years old, and sales grew 42 percent."),
        make_scene(4, "The archive contained 50,000 pages."),
    ],
    total_scenes=4, estimated_duration_minutes=0.5,
)
rep = validate_script_anchors(clean, research)
check("anchored script passes", not rep.unanchored, str(rep.as_dict()))
check("report counts anchors", rep.anchors_checked >= 5, str(rep.anchors_checked))

dirty = Script(
    title="T", description="", tags=[], hook="",
    scenes=[
        make_scene(1, "Founded in 1990."),
        make_scene(2, "By 2021 it was worth $7.3 billion."),
        make_scene(3, "She was 51 years old then."),
    ],
    total_scenes=3, estimated_duration_minutes=0.4,
)
rep2 = validate_script_anchors(dirty, research)
unanchored = {(x.scene_number, x.claim_type) for x in rep2.unanchored}
check("unanchored money caught", (2, "money") in unanchored, str(unanchored))
check("unanchored age caught", (3, "age") in unanchored, str(unanchored))
check("anchored year passes alongside", (1, "year") not in unanchored, str(unanchored))

# Money tolerance: "$1.19 billion" vs corpus "$1.2 billion" (0.8% off) → anchored
close = Script(title="T", description="", tags=[], hook="",
               scenes=[make_scene(1, "It hit $1.19 billion in value.")],
               total_scenes=1, estimated_duration_minutes=0.1)
rep3 = validate_script_anchors(close, research)
check("money 2% tolerance", not rep3.unanchored, str(rep3.as_dict()))

# Empty research → everything flagged
rep4 = validate_script_anchors(clean, make_research(summary="", timeline=[],
                                                    key_facts=[], key_people=[], source_queries=[]))
check("empty research flags all", rep4.anchors_checked > 0 and len(rep4.unanchored) == rep4.anchors_checked)

# Deterministic strip fallback (no LLM): hedge replacement must clear the report
print("\n[2.6] Deterministic strip fallback")
from pipeline.scriptwriter import _enforce_fact_anchors  # noqa: E402
import asyncio  # noqa: E402

# Offline env: mock the Gemini client so the LLM rewrite pass fails fast
# and the deterministic strip path runs (the safety net under any LLM outage).
# Tests must never depend on network or model availability.
import unittest.mock  # noqa: E402
import pipeline.scriptwriter as sw  # noqa: E402
_orig_key = config.GEMINI_API_KEY
config.GEMINI_API_KEY = ""

async def run_strip():
    s = Script(title="T", description="", tags=[], hook="",
               scenes=[
                   make_scene(1, "Founded in 1990."),
                   make_scene(2, "By 2021 it was worth $7.3 billion."),
               ],
               total_scenes=2, estimated_duration_minutes=0.3)
    with unittest.mock.patch.object(sw.genai, "Client", side_effect=RuntimeError("offline test")):
        report = await _enforce_fact_anchors(s, research)
    return s, report

stripped_script, strip_report = asyncio.run(run_strip())
config.GEMINI_API_KEY = _orig_key
check("strip removed unanchored money", "$7.3 billion" not in stripped_script.scenes[1].narration,
      stripped_script.scenes[1].narration)
check("strip kept anchored content", "1990" in stripped_script.scenes[0].narration)
check("final report clean after strip", not strip_report["unanchored"], str(strip_report))

# ── 2.8 B-roll memory ─────────────────────────────────────────────────
print("\n[2.8] B-roll memory")
import pipeline.assets as assets_mod  # noqa: E402

tmp_db = Path("output/_phase2_test_db")
tmp_db.mkdir(parents=True, exist_ok=True)
orig_path = assets_mod._BROLL_MEMORY_PATH
assets_mod._BROLL_MEMORY_PATH = tmp_db / "used_broll.json"
assets_mod._BROLL_MEMORY_PATH.unlink(missing_ok=True)

s1 = make_scene(1, "n")
s1.visual_type = "web_photo"
s1.source_url = "https://example.com/a.jpg"
s1.source_title = "Photo A"
assets_mod._broll_memory = assets_mod._load_used_broll()
assets_mod._broll_memory.clear()
assets_mod._record_used(s1, "gold car")
check("record adds entry", len(assets_mod._broll_memory) == 1, str(len(assets_mod._broll_memory)))

s2 = make_scene(2, "n")
s2.source_url = "https://example.com/a.jpg"  # same URL → update, not duplicate
assets_mod._record_used(s2, "gold car")
check("same URL deduped", len(assets_mod._broll_memory) == 1)

assets_mod._save_used_broll()
assets_mod._broll_memory = assets_mod._load_used_broll()  # simulate new process
loaded = assets_mod._broll_memory
check("persist + reload", len(loaded) == 1 and loaded[0]["url"] == "https://example.com/a.jpg"
      and loaded[0].get("query") == "gold car", str(loaded))

s3 = make_scene(3, "n")
s3.source_url = "https://example.com/b.jpg"
assets_mod._record_used(s3, "dark hallway")
usage = assets_mod._query_usage()
check("usage counts queries", usage.get("gold car") == 1 and usage.get("dark hallway") == 1, str(usage))

# Tier-3 rotation: least-used generic query sorts first
generic = ["abstract minimalist background", "modern office building", "dark abstract smoke"]
prior = {"abstract minimalist background": 3, "modern office building": 1, "dark abstract smoke": 0}
rotated = sorted(generic, key=lambda q: prior.get(q.lower(), 0))
check("tier-3 rotation order", rotated[0] == "dark abstract smoke", str(rotated))

# Ledger is bounded — fresh module-level list (clear any entries earlier
# tests appended), then verify the MAX cap actually truncates.
assets_mod._broll_memory.clear()
assets_mod._BROLL_MEMORY_MAX = 3
for i in range(10):
    sx = make_scene(10 + i, "n")
    sx.source_url = f"https://example.com/x{i}.jpg"
    assets_mod._record_used(sx, f"query {i}")
check("ledger bounded", len(assets_mod._broll_memory) == 3, str(len(assets_mod._broll_memory)))

assets_mod._BROLL_MEMORY_PATH = orig_path
shutil.rmtree(tmp_db, ignore_errors=True)

print(f"\n{'='*50}\nRESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
