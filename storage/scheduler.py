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

import json
import os
import sys
import time
import subprocess
from datetime import datetime, time as day_time, timedelta, timezone
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

# Daily release cadence (Africa/Harare): one morning, one afternoon, two
# evening uploads. The render job may start after today's morning slot (GitHub
# starts at 10:30 local), so the helper schedules the whole slate for the next
# day rather than placing some videos into past slots. Keep two hours of
# upload/render headroom before a slot to avoid missing publishAt mid-run.
DAILY_RELEASE_SLOTS = ((9, 0), (14, 0), (18, 0), (20, 0))
PUBLISH_SLOT_LEAD = timedelta(hours=2)

# FIX-080 (operator order 2026-10-02): EVERY day = 3 shorts + 1 doc.
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


def _daily_publish_times(count: int, now: datetime | None = None) -> list[str]:
    """Return `count` future UTC publishAt timestamps in the daily slot order.

    If the complete requested slate no longer fits in today's remaining slots,
    roll it together to tomorrow. Larger slates then continue in order.
    """
    if count <= 0:
        return []
    ref = now or now_harare()
    ref = ref.replace(tzinfo=HARARE) if ref.tzinfo is None else ref.astimezone(HARARE)
    publish_not_before = ref + PUBLISH_SLOT_LEAD
    start_day = ref.date()
    today_slots = [datetime.combine(start_day, day_time(h, m), tzinfo=HARARE)
                   for h, m in DAILY_RELEASE_SLOTS]
    same_day_count = min(count, len(DAILY_RELEASE_SLOTS))
    first_slots_available = [slot for slot in today_slots
                             if slot >= publish_not_before]
    # If the entire requested slate no longer fits today, roll it together to
    # tomorrow. Otherwise schedule only in future slots today, then continue
    # the remaining sequence on later days.
    if len(first_slots_available) < same_day_count:
        start_day += timedelta(days=1)

    scheduled: list[datetime] = []
    day = start_day
    while len(scheduled) < count:
        for hour, minute in DAILY_RELEASE_SLOTS:
            candidate = datetime.combine(day, day_time(hour, minute), tzinfo=HARARE)
            if candidate >= publish_not_before:
                scheduled.append(candidate)
                if len(scheduled) == count:
                    break
        day += timedelta(days=1)
    return [dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            for dt in scheduled]


def _build_stages(channel: str) -> list[list[str]]:
    """The day's production slate: N shorts first, then one 8-min doc.

    FIX-080: shorts run FIRST so a long-form failure at the tail can never
    cost the day's distribution (the discovery engine — 343-1605 views vs
    12-49 on long-form). The final QC gate and the compliance gate remain
    the safety net. GD_SHORTS_PER_DAY overrides
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


def _doc_state(doc_marks: list[bool], gated_reason: str) -> str:
    """FIX-102: ok | failed | skipped | gated for the day's long-form stage.

    A deliberate gate-skip must never be reported as a failure, or every
    gated day reads like a broken pipeline and the operator learns to ignore
    the summary. A real failure is never excused by a stale marker.
    """
    ok = bool(doc_marks) and doc_marks[0]
    if gated_reason and ok:
        return "gated"
    if ok:
        return "ok"
    if doc_marks:
        return "failed"
    return "skipped"


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


def _queued_topics(limit: int = 5) -> list[str]:
    """Top topics from the competitor scan queue (db/topic_queue.json)."""
    try:
        data = json.loads((Path("db") / "topic_queue.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    items = data.get("queue") or data.get("topics") or []
    if isinstance(items, dict):
        items = list(items.values())
    topics: list[str] = []
    for it in (items if isinstance(items, list) else []):
        if isinstance(it, dict) and it.get("topic"):
            topics.append(str(it["topic"]))
        elif isinstance(it, str):
            topics.append(it)
    return topics[:limit]


def _run_intel(channel: str, env: dict) -> None:
    """FIX-084: daily intelligence after production — never fatal.

    Learns from the WHOLE catalogue (manual videos included) into
    db/channel_memory.json, refreshes the competitor outlier queue, then
    writes a plain-language diagnosis + monetization gap to
    output/_daily_intel.txt (surfaced in the workflow job summary).
    GD_INTEL=0 disables it.
    """
    if os.environ.get("GD_INTEL", "") == "0":
        return
    parts: list[str] = []
    for label, args in (("CHANNEL MEMORY (manual + automated)", ["--channel-memory"]),
                        ("ANALYTICS (watch hours + subscriber conversion)", ["--sync-analytics"]),
                        ("COMPETITOR SCAN (watch-list outliers)", ["--scout"])):
        try:
            proc = subprocess.run([sys.executable, "main.py", *args, "--channel", channel],
                                  cwd=Path.cwd(), env=env, capture_output=True,
                                  text=True, timeout=900)
            body = (proc.stdout or "")[-6000:].strip()
            if proc.returncode != 0:
                body += f"\n[{label}] exited {proc.returncode}\n{(proc.stderr or '')[-600:]}"
            parts.append(f"## {label}\n{body}")
        except Exception as e:
            parts.append(f"## {label}\nskipped ({e})")
    try:
        from pipeline.diagnose import diagnose, parse_daily_result
        result_path = Path("output") / "_daily_result.txt"
        daily = parse_daily_result(
            result_path.read_text(encoding="utf-8") if result_path.exists() else "")
        memory = None
        mem_path = Path("db") / "channel_memory.json"
        if mem_path.exists():
            memory = json.loads(mem_path.read_text(encoding="utf-8"))
        parts.insert(0, "## DIAGNOSIS\n" + "\n".join(
            diagnose(daily, memory, _queued_topics())))
        # FIX-089: report any automated upload that is no longer PUBLIC.
        # The post-upload guard only covers the upload window; this reads
        # the live status the catalogue refresh just captured (the 10-02
        # Strictly doc logged "public confirmed" and was unlisted by
        # morning). Manual uploads are never flagged.
        try:
            from pipeline.channel_memory import delivery_audit as _audit
            _audit_lines = _audit((memory or {}).get("rows", []))
        except Exception as e_a:
            _audit_lines = [f"DELIVERY AUDIT — skipped ({e_a})"]
        parts.insert(1, "## DELIVERY AUDIT\n" + "\n".join(_audit_lines))
        # FIX-086: evaluate running experiments against today's catalogue.
        try:
            from pipeline import experiments as _EX
            reg = _EX.load()
            _EX.ensure_defaults(reg)
            rows_by_id = {r.get("id"): r for r in (memory or {}).get("rows", [])}
            lines_x = _EX.run_cycle(reg, rows_by_id)
            _EX.save(reg)
            parts.insert(1, "## EXPERIMENTS\n" + "\n".join(lines_x))
        except Exception as e_x:
            log.warning(f"Experiment cycle skipped ({e_x})")
    except Exception as e:
        parts.insert(0, f"## DIAGNOSIS\nskipped ({e})")
    try:
        out = Path("output") / "_daily_intel.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n\n".join(parts), encoding="utf-8")
        log.info("Daily intel written to output/_daily_intel.txt")
    except OSError as e:
        log.warning(f"Daily intel write failed: {e}")


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
        # The scheduler owns this run's gate marker and per-stage release
        # timestamps. Keep GD_DOC_GATE available as the documented opt-out.
        env.pop("GD_DOC_GATE_MARKER", None)
        env.pop("GD_PUBLISH_AT", None)
        # Operator schedule (2026-09-26): the SHORT (discovery engine) ships
        # first, then the 8-min doc on a fresh story. FIX-080 (2026-10-02):
        # 3 shorts + 1 doc per day, scheduled into spaced release slots,
        # with per-stage retries — transient Gemini 503s cost one stage, not the day.
        # Exit 0 counts as "the day shipped": partial days are reported via
        # output/_daily_result.txt, so the workflow's outer retry only fires
        # when NOTHING shipped (no wasted duplicate short runs).
        stages = _build_stages(channel)
        _gate_marker = Path("output") / "_doc_gated.txt"
        try:
            _gate_marker.unlink(missing_ok=True)
        except OSError:
            pass
        publish_times = _daily_publish_times(len(stages), now_harare())
        try:
            attempts = max(int(os.environ.get("GD_STAGE_ATTEMPTS")
                               or STAGE_ATTEMPTS_DEFAULT), 1)
        except ValueError:
            attempts = STAGE_ATTEMPTS_DEFAULT
        results: list[tuple[str, bool]] = []
        for stage_index, stage_args in enumerate(stages):
            label = "longform" if "--longform" in stage_args else "short"
            stage_ok = False
            stage_env = dict(env, GD_PUBLISH_AT=publish_times[stage_index])
            if "--longform" in stage_args:
                stage_env["GD_DOC_GATE_MARKER"] = str(_gate_marker.resolve())
            else:
                stage_env.pop("GD_DOC_GATE_MARKER", None)
            log.info(f"Daily release slot ({label}): {publish_times[stage_index]} UTC")
            for run_no in range(1, attempts + 1):
                cmd = [sys.executable, "main.py", *stage_args]
                log.info(f"Daily production command ({label} {run_no}/{attempts}): {cmd}")
                proc = subprocess.run(cmd, cwd=Path.cwd(), env=stage_env)
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
        # FIX-102: a gate-skipped doc is not a failure. The doc stage exits 0
        # after leaving output/_doc_gated.txt (no breakout short to ride), so
        # the day reports "doc=gated" — distinct from "skipped" (no doc stage
        # at all, GD_SHORTS_ONLY=1) and from "failed". Without this every
        # gated day would read as a broken pipeline in the summary and the
        # daily diagnosis, and the operator would learn to ignore both.
        try:
            _gated = _gate_marker.read_text(encoding="utf-8").strip()
        except OSError:
            _gated = ""
        try:
            _gate_marker.unlink(missing_ok=True)
        except OSError:
            pass
        doc_state = _doc_state(doc_marks, _gated)
        any_ok = any(s_ok for _, s_ok in results)
        full_ok = all(s_ok for _, s_ok in results)
        result_txt = "FULL" if full_ok else ("PARTIAL" if any_ok else "FAILED")
        _write_daily_result(result_txt, shorts_ok, shorts_total, doc_state)
        # FIX-084: learn from the catalogue + competitors, then diagnose.
        _run_intel(channel, env)
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


def _plan_now() -> datetime:
    """The clock the day's slate is planned from.

    The cloud workflow sets GD_PLAN_NOW to the run's start, so every per-video
    job plans the same publish slots no matter when its runner actually starts.
    """
    raw = (os.environ.get("GD_PLAN_NOW") or "").strip()
    if raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(HARARE)
        except ValueError:
            log.warning(f"GD_PLAN_NOW={raw!r} is not an ISO timestamp — using the wall clock")
    return now_harare()


def _stage_attempts() -> int:
    try:
        return max(int(os.environ.get("GD_STAGE_ATTEMPTS") or STAGE_ATTEMPTS_DEFAULT), 1)
    except ValueError:
        return STAGE_ATTEMPTS_DEFAULT


def _stage_result_path(index: int) -> Path:
    return Path("output") / f"_stage_{index}.txt"


def _write_stage_result(index: int, label: str, status: str, gated: str = "") -> None:
    """Per-video outcome, written by the stage job and read by finalize."""
    out = _stage_result_path(index)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"stage={index}\nlabel={label}\nstatus={status}\ngated={gated}\n",
                   encoding="utf-8")


def _read_stage_result(index: int) -> dict[str, str]:
    try:
        text = _stage_result_path(index).read_text(encoding="utf-8")
    except OSError:
        return {}
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()
    return fields


def run_stage(index: int, channel: str = "famefiles") -> int:
    """Produce ONE video of the day's slate (1-based) in this process.

    Each video runs in its own cloud job with its own time budget and its own
    publish slot (GD_PUBLISH_AT). The slate itself is planned from GD_PLAN_NOW,
    so every job agrees on which slot belongs to which video. Stages past the
    end of today's slate (e.g. the doc under GD_SHORTS_ONLY=1) are a recorded
    no-op. Returns 0 when the video shipped (or was skipped), 1 when it failed.
    """
    stages = _build_stages(channel)
    if not 1 <= index <= len(stages):
        _write_stage_result(index, "skipped", "skipped")
        print(f"[stage {index}] not part of today's slate ({len(stages)} stages) — skipped.")
        return 0

    stage_args = stages[index - 1]
    label = "longform" if "--longform" in stage_args else "short"
    publish_times = _daily_publish_times(len(stages), _plan_now())
    env = dict(os.environ, GD_WITHIN_DAILY=date_str_harare())
    env.pop("GD_DOC_GATE_MARKER", None)
    env["GD_PUBLISH_AT"] = publish_times[index - 1]
    gate_marker = Path("output") / "_doc_gated.txt"
    if label == "longform":
        try:
            gate_marker.unlink(missing_ok=True)
        except OSError:
            pass
        env["GD_DOC_GATE_MARKER"] = str(gate_marker.resolve())

    print(f"[stage {index}] {label} — release slot {env['GD_PUBLISH_AT']}")
    attempts = _stage_attempts()
    ok = False
    for run_no in range(1, attempts + 1):
        cmd = [sys.executable, "main.py", *stage_args]
        log.info(f"Stage {index} command ({label} {run_no}/{attempts}): {cmd}")
        proc = subprocess.run(cmd, cwd=Path.cwd(), env=env)
        if proc.returncode == 0:
            ok = True
            break
        log.warning(f"Stage {index} failed (exit {proc.returncode}): {stage_args}")
        if run_no < attempts:
            log.info(f"Cooling down {STAGE_COOLDOWN_S}s before retrying stage {index}")
            time.sleep(STAGE_COOLDOWN_S)

    gated = ""
    if label == "longform":
        # FIX-102: a gate-skipped doc exits 0 and leaves its reason behind.
        try:
            gated = gate_marker.read_text(encoding="utf-8").strip()
        except OSError:
            gated = ""
        try:
            gate_marker.unlink(missing_ok=True)
        except OSError:
            pass
    _write_stage_result(index, label, "ok" if ok else "failed", gated)
    print(f"[stage {index}] {label}: {'ok' if ok else 'FAILED'}"
          + (f" (doc gated: {gated})" if gated else ""))
    return 0 if ok else 1


def finalize_day(channel: str = "famefiles") -> int:
    """Roll the per-video stage results into the day's outcome, then learn.

    Run after every stage job (even when some failed). Writes the same
    output/_daily_result.txt and daily intel as the single-process run, and
    the day ships when at least one video did. Returns 1 only when NOTHING
    shipped.
    """
    stages = _build_stages(channel)
    results: list[tuple[str, bool, str]] = []
    for index, stage_args in enumerate(stages, start=1):
        fields = _read_stage_result(index)
        label = "longform" if "--longform" in stage_args else "short"
        results.append((label, fields.get("status") == "ok", fields.get("gated", "")))
    shorts = [ok for lbl, ok, _ in results if lbl == "short"]
    doc_marks = [ok for lbl, ok, _ in results if lbl == "longform"]
    gated = next((g for lbl, _, g in results if lbl == "longform" and g), "")
    doc_state = _doc_state(doc_marks, gated)
    any_ok = any(ok for _, ok, _ in results)
    full_ok = all(ok for _, ok, _ in results)
    result_txt = "FULL" if full_ok else ("PARTIAL" if any_ok else "FAILED")
    _write_daily_result(result_txt, sum(shorts), len(shorts), doc_state)
    print(f"[daily] Production for {date_str_harare()}: {result_txt} "
          f"(shorts {sum(shorts)}/{len(shorts)}, doc {doc_state})")

    _run_intel(channel, dict(os.environ, GD_WITHIN_DAILY=date_str_harare()))
    db = _db()
    if db is not None:
        db.set_schedule_state(SCHEDULE_NAME, last_run=datetime.now(timezone.utc),
                              last_result=("success" if full_ok else result_txt.lower()))
    return 0 if any_ok else 1


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
