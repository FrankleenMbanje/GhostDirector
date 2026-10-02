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
        # Operator schedule (2026-09-26): a SHORT first (the discovery
        # engine — our shorts pull 343-1605 views vs 12-49 on long-form),
        # then the 8-min+ long-form doc on the SAME fresh story (the
        # no-repeat picker + run idempotency keep the two from colliding;
        # the ledger records both runs). Long-form failure never blocks
        # the short from having shipped.
        ok = True
        # GD_SHORTS_ONLY=1: run ONLY the short stage (multi-short days — the
        # long-form doc rides its own run so a 503 storm can't cost both).
        stages = (["--trending", "--channel", channel, "--privacy", "unlisted"],)
        if os.environ.get("GD_SHORTS_ONLY", "") != "1":
            stages += ("--trending", "--longform", "--channel", channel,
                       "--privacy", "unlisted"),
        for fmt_args in stages:
            cmd = [sys.executable, "main.py", *fmt_args]
            log.info(f"Daily production command: {cmd}")
            proc = subprocess.run(cmd, cwd=Path.cwd(), env=env)
            if proc.returncode != 0:
                ok = False
                log.warning(f"Daily stage failed (exit {proc.returncode}): {fmt_args}")
        if db is not None and run_id:
            if ok:
                # The trending run uploaded (or intentionally skipped upload);
                # mark the day complete either way — the video record carries
                # the truth.
                db.release_run(run_id, "PUBLISHED")
            else:
                db.release_run(run_id, "FAILED", error=f"exit code {proc.returncode}")
        if db is not None:
            db.set_schedule_state(SCHEDULE_NAME, last_run=datetime.now(timezone.utc),
                                  last_result="success" if ok else "failed")
        print(f"[daily] Production for {today}: "
              f"{'COMPLETE' if ok else 'FAILED (state persisted; safe to retry)'}")
        return 0 if ok else 1
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
