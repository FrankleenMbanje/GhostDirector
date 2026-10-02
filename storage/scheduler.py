"""
GhostDirector — daily 10:00 Africa/Harare production scheduler (Phase 10).

Restart-safe: the wall clock decides when a run is due, and the DATABASE
decides whether today's run already happened (run lock + scheduled_date
idempotency). A restart never duplicates or skips a day. When DATABASE_URL
is unset, the lock file keeps single-instance behavior (degraded but safe:
no idempotency history without a database).

Windows deployment: scripts/schedule_daily.ps1 registers a Task Scheduler
entry that runs `python main.py --daily` at 10:00 local time. The task
survives reboots and does not need a terminal open.
"""

import os
import sys
import time
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.logger import get_logger  # noqa: E402

log = get_logger("scheduler")

HARARE = ZoneInfo(
    os.environ.get("GD_TIMEZONE") or "Africa/Harare")
RUN_HOUR = int(os.environ.get("GD_DAILY_HOUR") or 8)    # 08:00 local (operator: 8am daily)
RUN_MINUTE = 0
SCHEDULE_NAME = "famefiles-daily"
POLL_SECONDS = 60      # wake-up cadence of the persistent loop
STALE_RUN_HOURS = 6    # a run older than this without progress is failed

# FIX-080 (operator order 2026-10-02): EVERY day = 3 shorts + 1 doc, every
# upload PUBLIC, and a failure must cost one stage — never the day.
SHORTS_PER_DAY_DEFAULT = 3
STAGE_ATTEMPTS_DEFAULT = 2     # in-run retry per stage (Gemini 503 storms)
STAGE_COOLDOWN_S = 300         # pause between a stage's attempts


def now_harare() -> datetime:
    return datetime.now(HARARE)


def date_str_harare(dt: datetime | None = None) -> str:
    return (dt or now_harare()).strftime("%Y-%m-%d")


def next_run_time(after: datetime | None = None) -> datetime:
    """Next 10:00 Africa/Harare strictly after `after`."""
    ref = after or now_harare()
    candidate = ref.replace(hour=RUN_HOUR, minute=RUN_MINUTE, second=0, microsecond=0)
    if candidate <= ref:
        candidate += timedelta(days=1)
    return candidate


def is_due(now: datetime | None = None) -> bool:
    """Due when local time is past 10:00 today and the day isn't done."""
    n = now or now_harare()
    due_time = n.replace(hour=RUN_HOUR, minute=RUN_MINUTE, second=0, microsecond=0)
    return n >= due_time


def _build_stages(channel: str) -> list[list[str]]:
    """The day's production slate: N shorts first, then one 8-min doc.

    FIX-080: shorts run FIRST so a long-form failure at the tail can never
    cost the day's distribution (the discovery engine — 343-1605 views vs
    12-49 on long-form). Every stage uploads PUBLIC; the final QC gate and
    the compliance gate remain the safety net. GD_SHORTS_PER_DAY overrides
    the count (clamped to >= 1); GD_SHORTS_ONLY=1 skips the doc entirely.
    The no-repeat ledger keeps every short on a different story.
    """
    try:
        count = max(int(os.environ.get("GD_SHORTS_PER_DAY")
                        or SHORTS_PER_DAY_DEFAULT), 1)
    except ValueError:
        count = SHORTS_PER_DAY_DEFAULT
    stages = [["--trending", "--channel", channel, "--privacy", "public"]
              for _ in range(count)]
    if os.environ.get("GD_SHORTS_ONLY", "") != "1":
        stages.append(["--trending", "--longform", "--channel", channel,
                       "--privacy", "public"])
    return stages


def _write_daily_result(result: str, shorts_ok: int, shorts_total: int,
                        doc_state: str) -> None:
    """Machine-readable outcome for the workflow job summary + artifacts."""
    try:
        out = Path("output") / "_daily_result.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"result={result}\nshorts={shorts_ok}/{shorts_total}\n"
                       f"doc={doc_state}\n", encoding="utf-8")
    except OSError:
        pass


def _db():
    try:
        from storage import database as db
        return db
    except Exception:
        return None


class DailyGate:
    """File lock (single instance) + DB lock (idempotency) combined."""

    def __init__(self, name: str = SCHEDULE_NAME):
        self.name = name
        self.lock_path = Path("output") / f".{name}.lock"

    def acquire_file_lock(self) -> bool:
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            if self.lock_path.exists():
                return False
            self.lock_path.write_text(str(os.getpid()), encoding="utf-8")
            return True
        except OSError:
            return False

    def release_file_lock(self) -> None:
        try:
            self.lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def run_daily(force: bool = False, channel: str = "famefiles") -> int:
    """One daily production cycle. Returns process exit code.

    Idempotent: when the DB says today's run already PUBLISHED, this is a
    no-op. When a crashed run exists, it is reclaimed and resumed per the
    run lock rules (the pipeline itself is resume-safe by design).
    """
    db = _db()
    gate = DailyGate()
    today = date_str_harare()

    if db is not None:
        res = db.acquire_daily_lock(SCHEDULE_NAME, today,
                                    command="main.py --daily", channel=channel,
                                    fmt="short")
        log.info(f"Daily gate: {res.get('reason')} (run_id={res.get('run_id')})")
        if not res.get("acquired"):
            if "published" in (res.get("reason") or ""):
                print(f"[daily] Today ({today}) already published — nothing to do.")
                return 0
            if "active with live lock" in (res.get("reason") or ""):
                print(f"[daily] A run for {today} is already in progress "
                      f"({res.get('reason')}). Not duplicating.")
                return 0
            if "database error" in (res.get("reason") or ""):
                log.warning("DB unavailable — falling back to the file lock only")
        run_id = res.get("run_id")
        if run_id is None and not force and not gate.acquire_file_lock():
            print("[daily] Another scheduler instance holds the file lock.")
            return 0
    else:
        run_id = None
        if not force and not gate.acquire_file_lock():
            print("[daily] Another scheduler instance holds the file lock.")
            return 0

    try:
        if run_id and db is not None:
            db.update_run_stage(run_id, "DISCOVERING")
        print(f"[daily] Starting production for {today} "
              f"(Africa/Harare {now_harare().strftime('%H:%M')})")
        # GD_WITHIN_DAILY marks the sanctioned child run: the trending
        # pipeline's own duplicate check defers to the scheduler's gate
        # (the parent already holds the day's lock for this production).
        env = dict(os.environ, GD_WITHIN_DAILY=today)
        # Operator schedule (2026-09-26): the SHORT (discovery engine) ships
        # first, then the 8-min doc on a fresh story. FIX-080 (2026-10-02):
        # 3 shorts + 1 doc every day, ALL PUBLIC, with a per-stage in-run
        # retry — a transient Gemini 503 storm costs one stage, not the day.
        # Exit 0 counts as "the day shipped": partial days are reported via
        # output/_daily_result.txt, so the workflow's outer retry only fires
        # when NOTHING shipped (no wasted duplicate short runs).
        stages = _build_stages(channel)
        try:
            attempts = max(int(os.environ.get("GD_STAGE_ATTEMPTS")
                               or STAGE_ATTEMPTS_DEFAULT), 1)
        except ValueError:
            attempts = STAGE_ATTEMPTS_DEFAULT
        results: list[tuple[str, bool]] = []
        for stage_args in stages:
            label = "longform" if "--longform" in stage_args else "short"
            stage_ok = False
            for run_no in range(1, attempts + 1):
                cmd = [sys.executable, "main.py", *stage_args]
                log.info(f"Daily production command ({label} {run_no}/{attempts}): {cmd}")
                proc = subprocess.run(cmd, cwd=Path.cwd(), env=env)
                if proc.returncode == 0:
                    stage_ok = True
                    break
                log.warning(f"Daily stage failed (exit {proc.returncode}): {stage_args}")
                if run_no < attempts:
                    log.info(f"Cooling down {STAGE_COOLDOWN_S}s before retrying the {label} stage")
                    time.sleep(STAGE_COOLDOWN_S)
            results.append((label, stage_ok))
        shorts = [s_ok for lbl, s_ok in results if lbl == "short"]
        shorts_ok, shorts_total = sum(shorts), len(shorts)
        doc_marks = [d_ok for lbl, d_ok in results if lbl == "longform"]
        doc_state = ("ok" if doc_marks and doc_marks[0]
                     else "failed" if doc_marks else "skipped")
        any_ok = any(s_ok for _, s_ok in results)
        full_ok = all(s_ok for _, s_ok in results)
        result_txt = "FULL" if full_ok else ("PARTIAL" if any_ok else "FAILED")
        _write_daily_result(result_txt, shorts_ok, shorts_total, doc_state)
        if db is not None and run_id:
            if any_ok:
                # The day shipped at least one video; the video record
                # carries the truth about what else failed.
                db.release_run(run_id, "PUBLISHED")
            else:
                db.release_run(run_id, "FAILED",
                               error=f"all stages failed ({result_txt})")
        if db is not None:
            db.set_schedule_state(SCHEDULE_NAME, last_run=datetime.now(timezone.utc),
                                  last_result=("success" if full_ok
                                               else result_txt.lower()))
        print(f"[daily] Production for {today}: {result_txt} "
              f"(shorts {shorts_ok}/{shorts_total}, doc {doc_state})")
        return 0 if any_ok else 1
    finally:
        gate.release_file_lock()


def loop_forever(poll_seconds: int = POLL_SECONDS) -> None:  # pragma: no cover
    """Persistent scheduler loop (restart-safe: the DB decides idempotency)."""
    db = _db()
    log.info(f"Scheduler loop started — next {RUN_HOUR:02d}:{RUN_MINUTE:02d} "
             f"Africa/Harare: {next_run_time().isoformat()}")
    while True:
        try:
            n = now_harare()
            if is_due(n):
                if db is not None:
                    db.set_schedule_state(SCHEDULE_NAME, next_run=next_run_time(n))
                run_daily()
            time.sleep(poll_seconds)
        except KeyboardInterrupt:
            log.info("Scheduler stopped by operator")
            return
        except Exception as e:
            log.error(f"Scheduler loop error: {e}")
            time.sleep(poll_seconds)


if __name__ == "__main__":
    if "--loop" in sys.argv:
        loop_forever()
    else:
        raise SystemExit(run_daily(force="--force" in sys.argv))
