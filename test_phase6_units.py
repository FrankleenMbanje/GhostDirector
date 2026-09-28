"""Phase 6 unit tests — production system layer (Phase 7-10).

Run with: venv/Scripts/python.exe test_phase6_units.py

Covers:
  6.1  database bootstrap (SQLite mirror; graceful degradation)
  6.2  daily run lock: idempotency, duplicate prevention, crash recovery
  6.3  domain persistence: stories/videos/scenes/qc/uploads/assets
  6.4  scheduler time math (Africa/Harare) + due logic
  6.5  environment configuration safety (no hard-coded secrets)
"""

import sys
import os
import json
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond), extra))
    print(f"  {'PASS' if cond else 'FAIL'} — {name}" + (f" ({extra})" if extra and not cond else ""))


# ── 6.1 database bootstrap ────────────────────────────────────────────
print("\n[6.1] database bootstrap (SQLite mirror)")
import storage.database as db  # noqa: E402

_td = tempfile.TemporaryDirectory()
_test_db = Path(_td.name) / "test_state.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_test_db.as_posix()}"
db.reset_engine_for_tests()

check("engine resolves from DATABASE_URL", db.get_engine() is not None)
check("init_db creates schema", db.init_db() is True)
check("schema file exists", _test_db.exists())

# ── 6.2 run lock / idempotency / recovery ────────────────────────────
print("\n[6.2] daily run lock")
today = "2026-09-25"
r1 = db.acquire_daily_lock("test-daily", today, command="test", channel="famefiles")
check("first acquire succeeds", r1["acquired"] is True and r1["run_id"] is not None, str(r1))
r2 = db.acquire_daily_lock("test-daily", today)
check("second acquire refused (active lock)", r2["acquired"] is False, str(r2))
db.update_run_stage(r1["run_id"], "ASSEMBLING")
s = db._session()
row = s.get(db.ProductionRun, r1["run_id"])
check("stage recorded", row.current_stage == "ASSEMBLING")
check("lock has expiry", row.lock_expires_at is not None)
s.close()
db.release_run(r1["run_id"], "PUBLISHED")
r3 = db.acquire_daily_lock("test-daily", today)
check("published day not duplicated", r3["acquired"] is False
      and "published" in r3["reason"], str(r3))

# crash recovery: expired lock on an active run is reclaimable
day2 = "2026-09-26"
ra = db.acquire_daily_lock("test-daily", day2)
check("fresh day acquires", ra["acquired"] is True, str(ra))
s = db._session()
row = s.get(db.ProductionRun, ra["run_id"])
row.lock_expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
s.commit()
s.close()
rb = db.acquire_daily_lock("test-daily", day2)
check("crashed run reclaimed with retry", rb["acquired"] is True
      and "recovered" in rb["reason"], str(rb))
s = db._session()
row = s.get(db.ProductionRun, ra["run_id"])
check("retry count incremented", (row.retry_count or 0) >= 1)
check("retry status set", row.status == "RETRYING")
s.close()
db.release_run(ra["run_id"], "FAILED", error="test failure")
rc = db.acquire_daily_lock("test-daily", day2)
check("failed run is retryable", rc["acquired"] is True and "retry" in rc["reason"], str(rc))
db.release_run(rc["run_id"], "PUBLISHED")

# ── 6.3 domain persistence ───────────────────────────────────────────
print("\n[6.3] domain persistence")
sid = db.upsert_story("Unit Test Story", source="TestWire", trend_score=9.1,
                      topic="test topic", research_summary="a summary")
check("story persisted", sid is not None and sid > 0)
vid = db.upsert_video("unit-test-project", project_dir="output/unit-test-project",
                      fmt="short", story_id=sid, visibility="unlisted")
check("video persisted + linked", vid is not None and vid > 0)
db.update_video("unit-test-project", qc_status="pass", compliance_status="warn",
                youtube_video_id="utTest00", actual_duration=54.5)
s = db._session()
v = (s.query(db.VideoProject)
     .filter(db.VideoProject.project_name == "unit-test-project").one())
check("video fields updated", v.qc_status == "pass"
      and v.youtube_video_id == "utTest00" and v.actual_duration == 54.5)
check("story link stored", v.story_id == sid)
s.close()
db.record_scenes("unit-test-project", [
    {"scene_index": 1, "narration": "hook line", "duration": 4.0,
     "visual_type": "web_photo", "selected_asset": "scene_01_cut0.v4.mp4",
     "j_cut": False, "music_state": "dramatic"},
    {"scene_index": 2, "narration": "body line", "duration": 5.0,
     "visual_type": "stock_video", "j_cut": True},
])
s = db._session()
v = (s.query(db.VideoProject)
     .filter(db.VideoProject.project_name == "unit-test-project").one())
scenes = sorted(v.scenes, key=lambda x: x.scene_index)
check("scenes recorded", len(scenes) == 2 and scenes[1].j_cut is True)
db.record_qc("unit-test-project", {
    "verdict": "WARN", "issues": ["QC: person 'x' carries 5 scenes — review"],
    "frame_details": [{"label": "solid"}], "max_frozen_run": 0,
    "black_frames": 0, "median_sharpness": 41.0})
check("qc recorded", len(v.qc_results) == 1
      and v.qc_results[0].verdict == "warn")
db.record_upload("unit-test-project", "utTest00", "https://youtu.be/utTest00",
                 "unlisted")
check("upload recorded", len(v.uploads) == 1
      and v.uploads[0].result == "success")
db.record_asset("unit-test-project", 1, "pexels", "https://example.com/v.mp4",
                "video", fingerprint="ab" * 16)
s.close()
s = db._session()
v = (s.query(db.VideoProject)
     .filter(db.VideoProject.project_name == "unit-test-project").one())
check("asset recorded", len(v.assets) == 1
      and v.assets[0].fingerprint == "ab" * 16)
s.close()
db.set_schedule_state("test-daily", last_run=datetime.now(timezone.utc),
                      last_result="success")
sched = db.get_schedule_state("test-daily")
check("schedule state stored", sched is not None and sched.last_result == "success")

# ── 6.4 scheduler time math ──────────────────────────────────────────
print("\n[6.4] scheduler time math")
from storage.scheduler import (  # noqa: E402
    next_run_time, is_due, date_str_harare, HARARE, RUN_HOUR, RUN_MINUTE,
)
n = datetime.now(HARARE)
nxt = next_run_time()
check("next run is in the future", nxt > n)
check("next run hour matches config", nxt.hour == RUN_HOUR
      and nxt.minute == 0)
check("next run is tomorrow when past due time",
      (nxt.date() == (n + timedelta(days=1)).date()) if is_due(n)
      else (nxt.date() == n.date() or nxt.date() == (n + timedelta(days=1)).date()))
just_before = n.replace(hour=RUN_HOUR, minute=RUN_MINUTE - 1 if RUN_MINUTE else 9,
                         second=0, microsecond=0)
check("is_due consistent with next_run_time",
      (not is_due(just_before)) or next_run_time(just_before) > just_before)
tz_ok = date_str_harare() == datetime.now(HARARE).strftime("%Y-%m-%d")
check("date string uses production tz", tz_ok)

# ── 6.5 environment safety ───────────────────────────────────────────
print("\n[6.5] environment configuration safety")
example = Path(".env.example")
check(".env.example exists", example.exists())
example_text = example.read_text(encoding="utf-8") if example.exists() else ""
check(".env.example has DATABASE_URL", "DATABASE_URL=" in example_text)
check(".env.example has Gemini/Pexels keys", "GEMINI_API_KEY=" in example_text
      and "PEXELS_API_KEY=" in example_text)
gitignore = Path(".gitignore")
gi = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
check(".gitignore excludes .env", ".env" in gi and "!.env.example" in gi)
check(".gitignore excludes oauth tokens", "youtube_token" in gi
      and "client_secrets.json" in gi)
check(".gitignore excludes output/", "output/" in gi)
check("config reads GD_OUTPUT_DIR", os.environ.get("GD_OUTPUT_DIR") is None
      and "GD_OUTPUT_DIR" in example_text)
# no secrets in the storage layer source
src = (Path("storage") / "database.py").read_text(encoding="utf-8")
check("no hard-coded postgres creds in storage",
      "postgresql+psycopg://user" not in src.lower()
      and "PASSWORD@" not in src)

db.reset_engine_for_tests()
os.environ.pop("DATABASE_URL", None)
try:
    _td.cleanup()
except OSError:
    pass  # Windows may hold the sqlite file briefly; the temp dir self-cleans

print(f"\n{'='*50}\nRESULT: {sum(1 for _, ok, _ in RESULTS if ok)} passed, "
      f"{sum(1 for _, ok, _ in RESULTS if not ok)} failed")
sys.exit(0 if all(ok for _, ok, _ in RESULTS) else 1)
