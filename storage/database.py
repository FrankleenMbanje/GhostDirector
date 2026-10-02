"""
GhostDirector — Persistent production state (Phase 7/8/9).

Supabase PostgreSQL is the production source of truth for production STATE
(project name, run lock, stage transitions, scenes/assets, QC, uploads).
SQLite mirrors the identical schema for local development and the test suite
(zero external services; production flips DATABASE_URL to
``postgresql+psycopg://...``).

Generated media NEVER goes in the database: rows carry paths/URLs and
metadata only (see docs/DATABASE.md).

Everything degrades gracefully: when DATABASE_URL is unset or unreachable,
every function no-ops with a logged warning and the pipeline keeps working
exactly as before (JSON ledgers remain the local fallback).

Secrets never live here: DATABASE_URL comes from the environment (.env).
"""

import os
import json
import socket
from datetime import datetime, timezone, timedelta
from pathlib import Path

from utils.logger import get_logger

log = get_logger("storage")

try:
    from sqlalchemy import (
        create_engine, Column, Integer, BigInteger, String, Text, Float,
        Boolean, DateTime, ForeignKey, Index, UniqueConstraint, text as sql_text,
    )
    from sqlalchemy.orm import (
        declarative_base, relationship, sessionmaker,
    )
    from sqlalchemy.exc import SQLAlchemyError
    _SA_OK = True
except Exception as _e:  # pragma: no cover - driver must exist in the venv
    log.warning(f"SQLAlchemy unavailable ({_e}); production state disabled")
    _SA_OK = False

Base = declarative_base() if _SA_OK else object

# ─────────────────────────────────────────────────
# Connection management
# ─────────────────────────────────────────────────
_engine = None
_Session = None


def database_url() -> str | None:
    """DATABASE_URL from the environment; None when unconfigured."""
    url = (os.environ.get("DATABASE_URL") or "").strip()
    return url or None


def _default_sqlite_path() -> str:
    return (Path(os.environ.get("GD_OUTPUT_DIR") or "output") / "ghostdirector.db").as_posix()


def normalize_database_url(url: str | None) -> str:
    """Rewrite provider URL schemes to the explicit psycopg (v3) dialect.

    Supabase (and most PaaS consoles) hand out ``postgres://`` or
    ``postgresql://`` strings; SQLAlchemy needs the driver name spelled out.
    SQLite URLs (and already-normalized ones) pass through untouched.
    """
    if not url:
        return ""
    u = url.strip()
    if u.startswith("postgres://"):
        return "postgresql+psycopg://" + u[len("postgres://"):]
    if u.startswith("postgresql://"):
        return "postgresql+psycopg://" + u[len("postgresql://"):]
    return u


def _is_pooler_url(url_obj) -> bool:
    """True when the URL points at a connection pooler (Supavisor/pgbouncer).

    Supabase's transaction pooler (port 6543) multiplexes many clients onto
    few server connections and therefore cannot honour server-side prepared
    statements; the engine must disable them and stop assuming session
    affinity.
    """
    try:
        host = (url_obj.host or "").lower()
        port = url_obj.port
        query = {str(k).lower(): str(v).lower() for k, v in (url_obj.query or {}).items()}
        if query.get("pgbouncer") in ("true", "1"):
            return True
        if query.get("pool_mode") == "transaction":
            return True
        if host.endswith("pooler.supabase.com") or ".pooler." in host:
            return True
        if port == 6543:
            return True
    except Exception:
        return False
    return False


def resolve_connection(url: str | None = None) -> tuple[str, dict]:
    """Resolve DATABASE_URL into (sqlalchemy_url, create_engine kwargs).

    Handles PostgreSQL/Supabase specifics without hard-coding anything:

    * driver normalization (postgres:// → postgresql+psycopg://)
    * TLS default for Supabase hosts (sslmode=require, overridable with
      GD_DB_SSLMODE)
    * pooler awareness: transaction poolers (Supavisor/pgbouncer, port 6543)
      get NullPool + disabled prepared statements; direct/session connections
      get a small pooled engine with pre-ping and recycling

    When DATABASE_URL is unset this returns the local SQLite mirror URL so
    development and the test suite run with zero external services.
    """
    raw = normalize_database_url(url if url is not None else database_url())
    if not raw:
        raw = "sqlite:///" + _default_sqlite_path()
    kwargs: dict = {"future": True}
    if raw.startswith("sqlite"):
        return raw, kwargs
    try:
        from sqlalchemy.engine import make_url
        from sqlalchemy.pool import NullPool

        u = make_url(raw)
        query = {k: v for k, v in (u.query or {}).items()}
        keys = {str(k).lower() for k in query}
        host = (u.host or "").lower()
        forced = (os.environ.get("GD_DB_SSLMODE") or "").strip()
        if forced:
            query["sslmode"] = forced
        elif "sslmode" not in keys and (
                host.endswith("supabase.co") or host.endswith("supabase.com")
                or host.endswith("supabase.net")):
            query["sslmode"] = "require"
        if query != dict(u.query or {}):
            u = u.set(query=query)
            raw = u.render_as_string(hide_password=False)
        connect_args = {"connect_timeout": 10}
        if _is_pooler_url(u):
            connect_args["prepare_threshold"] = None   # psycopg3: no prep stmts
            kwargs["poolclass"] = NullPool
        else:
            kwargs.update(pool_pre_ping=True, pool_recycle=1800,
                          pool_size=5, max_overflow=5)
        kwargs["connect_args"] = connect_args
    except Exception as e:
        log.warning(f"Could not tune database URL ({str(e)[:120]}); using defaults")
        kwargs.setdefault("connect_args", {"connect_timeout": 10})
        kwargs.setdefault("pool_pre_ping", True)
    return raw, kwargs


def database_info() -> dict:
    """Credential-free connection summary (used by `main.py --status`)."""
    configured = bool(database_url())
    try:
        resolved, kwargs = resolve_connection()
    except Exception:
        return {"configured": configured, "source": "unknown"}
    info = {
        "configured": configured,
        "source": "DATABASE_URL" if configured else "sqlite-mirror",
        "driver": "sqlite" if resolved.startswith("sqlite") else "postgresql+psycopg",
        "pooler": "prepare_threshold" in (kwargs.get("connect_args") or {}),
        "pool": "NullPool" if kwargs.get("poolclass") is not None else "default",
    }
    try:
        from sqlalchemy.engine import make_url
        u = make_url(resolved)
        info["host"] = u.host or "local-file"
        info["database"] = u.database or Path(_default_sqlite_path()).name
    except Exception:
        pass
    return info


def get_engine():
    """Engine for DATABASE_URL, or a local SQLite mirror when unset."""
    global _engine, _Session
    if _engine is not None:
        return _engine
    if not _SA_OK:
        return None
    configured = database_url()
    resolved, kwargs = resolve_connection(configured)
    if not configured:
        log.info(f"DATABASE_URL unset — using local SQLite mirror at {resolved}")
    _engine = create_engine(resolved, **kwargs)
    _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def _session():
    eng = get_engine()
    if eng is None:
        return None
    return _Session()


# ─────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────
RUN_ACTIVE_STATUSES = ("DISCOVERING", "RESEARCHING", "SCRIPTING", "ASSET_FETCHING",
                       "ASSEMBLING", "QC", "COMPLIANCE", "READY_FOR_UPLOAD",
                       "UPLOADING", "RETRYING")
RUN_TERMINAL_STATUSES = ("PUBLISHED", "COMPLETED", "FAILED", "ABANDONED")


class Story(Base):
    __tablename__ = "stories"

    id = Column(Integer, primary_key=True)
    title = Column(Text, nullable=False)
    source = Column(String(200))
    source_url = Column(Text)
    discovered_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    story_date = Column(DateTime(timezone=True))
    trend_score = Column(Float)
    topic = Column(String(120))
    status = Column(String(30), default="discovered")   # discovered|selected|rejected|produced
    research_summary = Column(Text)
    verification_status = Column(String(30))            # unverified|verified|flagged
    selected_reason = Column(Text)
    rejected_reason = Column(Text)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


class VideoProject(Base):
    __tablename__ = "video_projects"

    id = Column(Integer, primary_key=True)
    story_id = Column(Integer, ForeignKey("stories.id"), nullable=True)
    format = Column(String(20), nullable=False)          # short|long_form
    project_name = Column(String(240), nullable=False)   # output/<dir> name
    project_dir = Column(Text)
    status = Column(String(30), default="RESEARCHING")
    target_duration = Column(Float)
    actual_duration = Column(Float)
    script_version = Column(Integer, default=1)
    render_version = Column(Integer, default=0)
    qc_status = Column(String(20))                       # pass|warn|fail
    compliance_status = Column(String(20))
    upload_status = Column(String(20))                   # pending|uploaded|failed|skipped
    youtube_video_id = Column(String(32))
    youtube_url = Column(Text)
    visibility = Column(String(20))                      # unlisted|public|private
    thumbnail_status = Column(String(20))
    error = Column(Text)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    story = relationship("Story")
    scenes = relationship("SceneRecord", back_populates="video", cascade="all, delete-orphan")
    assets = relationship("AssetRecord", back_populates="video", cascade="all, delete-orphan")
    qc_results = relationship("QCResult", back_populates="video", cascade="all, delete-orphan")
    uploads = relationship("UploadRecord", back_populates="video", cascade="all, delete-orphan")
    scripts = relationship("ScriptRecord", back_populates="video", cascade="all, delete-orphan")
    narrations = relationship("NarrationRecord", back_populates="video", cascade="all, delete-orphan")
    music_selections = relationship("MusicSelection", back_populates="video", cascade="all, delete-orphan")
    compliance_results = relationship("ComplianceResult", back_populates="video", cascade="all, delete-orphan")
    asset_usage = relationship("AssetUsage", back_populates="video", cascade="all, delete-orphan")
    events = relationship("PipelineEvent", back_populates="video", cascade="all, delete-orphan")


class SceneRecord(Base):
    __tablename__ = "scenes"

    id = Column(Integer, primary_key=True)
    video_id = Column(Integer, ForeignKey("video_projects.id"), nullable=False)
    scene_index = Column(Integer, nullable=False)
    narration = Column(Text)
    duration = Column(Float)
    visual_type = Column(String(40))
    selected_asset = Column(Text)
    candidate_count = Column(Integer)
    transition_type = Column(String(40))
    j_cut = Column(Boolean, default=False)
    l_cut = Column(Boolean, default=False)
    music_state = Column(String(40))
    caption_state = Column(String(40))
    render_status = Column(String(30), default="pending")
    repair_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    video = relationship("VideoProject", back_populates="scenes")


class AssetRecord(Base):
    __tablename__ = "assets"

    id = Column(Integer, primary_key=True)
    video_id = Column(Integer, ForeignKey("video_projects.id"), nullable=True)
    scene_index = Column(Integer)
    source = Column(String(120))            # pexels|ddg|youtube|archive|gemini|generic
    url = Column(Text)
    asset_type = Column(String(30))         # video|photo|illustration
    width = Column(Integer)
    height = Column(Integer)
    duration = Column(Float)
    subject = Column(String(240))
    relevance = Column(Float)
    quality_score = Column(Float)
    editorial_score = Column(Float)
    selected = Column(Boolean, default=False)
    rejection_reason = Column(Text)
    local_path = Column(Text)
    fingerprint = Column(String(64))        # dhash — integrates the variety ledger
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    video = relationship("VideoProject", back_populates="assets")


class QCResult(Base):
    __tablename__ = "qc_results"

    id = Column(Integer, primary_key=True)
    video_id = Column(Integer, ForeignKey("video_projects.id"), nullable=False)
    qc_version = Column(String(30))
    flat_frames = Column(Integer, default=0)
    frozen_frames = Column(Integer, default=0)
    black_frames = Column(Integer, default=0)
    sharpness = Column(Float)
    visual_repetition = Column(Text)
    audio_checks = Column(Text)
    caption_checks = Column(Text)
    compliance_checks = Column(Text)
    gemini_review = Column(Text)
    critical_errors = Column(Integer, default=0)
    warnings = Column(Integer, default=0)
    verdict = Column(String(20))            # pass|warn|fail
    repair_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    video = relationship("VideoProject", back_populates="qc_results")


class UploadRecord(Base):
    __tablename__ = "uploads"

    id = Column(Integer, primary_key=True)
    video_id = Column(Integer, ForeignKey("video_projects.id"), nullable=False)
    youtube_video_id = Column(String(32))
    youtube_url = Column(Text)
    uploaded_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    visibility = Column(String(20))
    thumbnail_status = Column(String(20))
    result = Column(String(20))             # success|failed
    error = Column(Text)
    retry_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    video = relationship("VideoProject", back_populates="uploads")


class ProductionRun(Base):
    __tablename__ = "production_runs"

    id = Column(Integer, primary_key=True)
    run_uuid = Column(String(64), unique=True, nullable=False)
    schedule_name = Column(String(80))
    command = Column(Text)
    channel = Column(String(40))
    format = Column(String(20))
    story_id = Column(Integer, ForeignKey("stories.id"), nullable=True)
    status = Column(String(30), default="DISCOVERING")
    current_stage = Column(String(40))
    project_name = Column(String(240))
    error = Column(Text)
    retry_count = Column(Integer, default=0)
    host = Column(String(120))
    pid = Column(Integer)
    lock_expires_at = Column(DateTime(timezone=True))
    scheduled_date = Column(String(10), index=True)      # YYYY-MM-DD (Africa/Harare)
    started_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    ended_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    story = relationship("Story")


class ScheduleState(Base):
    __tablename__ = "schedule_state"

    id = Column(Integer, primary_key=True)
    schedule_name = Column(String(80), unique=True, nullable=False)
    next_run = Column(DateTime(timezone=True))
    last_run = Column(DateTime(timezone=True))
    last_result = Column(String(30))
    last_error = Column(Text)
    run_lock = Column(String(64))
    lock_expires_at = Column(DateTime(timezone=True))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


class PerformanceSnapshot(Base):
    """Phase 25: real metrics only, imported from authorized sources (YouTube
    Analytics / Studio exports). Never fabricated."""
    __tablename__ = "performance_snapshots"

    id = Column(Integer, primary_key=True)
    video_id = Column(Integer, ForeignKey("video_projects.id"), nullable=False)
    views = Column(BigInteger)
    likes = Column(BigInteger)
    comments = Column(BigInteger)
    avg_percentage_viewed = Column(Float)
    watch_time_minutes = Column(Float)
    subscribers_gained = Column(BigInteger)
    captured_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    video = relationship("VideoProject")


class ScriptRecord(Base):
    """A generated (or rewritten) script version for a project.

    Text and metrics only — the script file itself lives in the project dir
    and is referenced by path.
    """
    __tablename__ = "scripts"

    id = Column(Integer, primary_key=True)
    video_id = Column(Integer, ForeignKey("video_projects.id"), nullable=False)
    version = Column(Integer, default=1)
    title = Column(Text)
    format = Column(String(20), default="short")
    scene_count = Column(Integer)
    word_count = Column(Integer)
    hook = Column(Text)
    hook_overlay_text = Column(String(240))
    target_duration = Column(Float)
    voice_duration = Column(Float)
    gate_score = Column(Float)
    gate_verdict = Column(String(20))
    model = Column(String(80))
    script_path = Column(Text)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    video = relationship("VideoProject", back_populates="scripts")


class NarrationRecord(Base):
    """Per-scene narration/voice data (provider, voice, audio file, timing)."""
    __tablename__ = "narrations"

    id = Column(Integer, primary_key=True)
    video_id = Column(Integer, ForeignKey("video_projects.id"), nullable=False)
    scene_index = Column(Integer, nullable=False)
    text = Column(Text)
    word_count = Column(Integer)
    provider = Column(String(40))          # edge-tts|elevenlabs|gtts|...
    voice = Column(String(80))
    duration = Column(Float)
    audio_path = Column(Text)
    timestamps = Column(Text)              # word-level timing JSON (may be large)
    status = Column(String(20), default="generated")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    video = relationship("VideoProject", back_populates="narrations")


class MusicSelection(Base):
    """The music track chosen for a project and why (story-mood resolution)."""
    __tablename__ = "music_selections"

    id = Column(Integer, primary_key=True)
    video_id = Column(Integer, ForeignKey("video_projects.id"), nullable=False)
    mood = Column(String(40))
    track = Column(String(400))
    track_path = Column(Text)
    source = Column(String(60))            # assets/music|generated|archive
    duration = Column(Float)
    resolved_by = Column(String(60))       # template|story-signal|fallback
    reason = Column(Text)
    license_note = Column(String(200))
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    video = relationship("VideoProject", back_populates="music_selections")


class ComplianceResult(Base):
    """Editorial/compliance gate verdict + findings for a project."""
    __tablename__ = "compliance_results"

    id = Column(Integer, primary_key=True)
    video_id = Column(Integer, ForeignKey("video_projects.id"), nullable=False)
    verdict = Column(String(20))           # pass|warn|fail
    policy_basis = Column(Text)
    findings = Column(Text)                # JSON list
    summary = Column(Text)
    rules_version = Column(String(30))
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    video = relationship("VideoProject", back_populates="compliance_results")


class AssetUsage(Base):
    """Rotation-ledger mirror: which real asset was used where, and how often.

    Backs duplicate-prevention/variety analysis across videos (the JSON
    `db/used_broll.json` ledger stays the pipeline's fast local path; this
    table is the durable, queryable history).
    """
    __tablename__ = "asset_usage"
    __table_args__ = (
        UniqueConstraint("project_name", "scene_index", "url",
                         name="uq_asset_usage_project_scene_url"),
    )

    id = Column(Integer, primary_key=True)
    video_id = Column(Integer, ForeignKey("video_projects.id"), nullable=True)
    project_name = Column(String(240), index=True)
    story_title = Column(Text)
    scene_index = Column(Integer)
    url = Column(Text)
    title = Column(String(400))
    channel = Column(String(200))
    query = Column(String(300))
    asset_type = Column(String(40))
    fingerprint = Column(String(64), index=True)     # dhash — variety truth
    times_used = Column(Integer, default=1)
    last_used = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    video = relationship("VideoProject", back_populates="asset_usage")


class PipelineEvent(Base):
    """Append-only pipeline state history: stage changes, warnings, errors,
    retries. This is the audit trail `main.py --status --events` reads and the
    source of the 'errors/failures' + 'timestamps' + 'retry information'
    persistence required for recovery diagnostics."""
    __tablename__ = "pipeline_events"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("production_runs.id"), nullable=True, index=True)
    video_id = Column(Integer, ForeignKey("video_projects.id"), nullable=True)
    project_name = Column(String(240))
    stage = Column(String(40))
    level = Column(String(10), default="info")       # info|warning|error
    message = Column(Text)
    error_type = Column(String(140))
    retry_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    video = relationship("VideoProject", back_populates="events")
    run = relationship("ProductionRun")


class SchedulerRun(Base):
    """One scheduler invocation (tick decision), independent of whether it
    started a production run. Makes the 10:00 Africa/Harare schedule's
    history durable and restart-verifiable."""
    __tablename__ = "scheduler_runs"

    id = Column(Integer, primary_key=True)
    schedule_name = Column(String(80), index=True)
    scheduled_for = Column(String(10))               # YYYY-MM-DD (Africa/Harare)
    run_id = Column(Integer, ForeignKey("production_runs.id"), nullable=True)
    result = Column(String(30))                      # started|skipped|published|failed
    detail = Column(Text)
    error = Column(Text)
    started_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    ended_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    run = relationship("ProductionRun")


def init_db() -> bool:
    """Create all tables. Returns True when a database is actually reachable."""
    eng = get_engine()
    if eng is None:
        return False
    try:
        Base.metadata.create_all(eng)
        return True
    except SQLAlchemyError as e:
        log.warning(f"Database unreachable ({str(e)[:140]}) — production state disabled")
        globals()["_engine"] = None
        return False


# ─────────────────────────────────────────────────
# Run locking / idempotency (Phase 9)
# ─────────────────────────────────────────────────
LOCK_TTL_MINUTES = 90          # a crashed run's lock expires and is reclaimable


def _uuid() -> str:
    import uuid
    return uuid.uuid4().hex


def acquire_daily_lock(schedule_name: str, date_str: str, command: str = "",
                       channel: str = "", fmt: str = "short") -> dict:
    """Idempotent daily-run gate. Returns a dict:
    {"acquired": bool, "run_id": int|None, "reason": str}

    Rules:
    - A run for scheduled_date==date_str with an ACTIVE status is already
      in progress (or its lock is still fresh) → not acquired.
    - A run for scheduled_date==date_str that COMPLETED (PUBLISHED) → the
      day already shipped → not acquired.
    - A FAILED/ABANDONED run for the date is RETRYABLE: its record is reused
      (same run_uuid), lock re-acquired, retry_count incremented.
    - A stale lock (expired lock_expires_at) is reclaimable.
    """
    if not _SA_OK:
        return {"acquired": False, "run_id": None, "reason": "storage disabled"}
    try:
        init_db()
        s = _session()
        if s is None:
            return {"acquired": False, "run_id": None, "reason": "no database"}
        try:
            rows = (
                s.query(ProductionRun)
                .filter(ProductionRun.scheduled_date == date_str)
                .order_by(ProductionRun.id.desc())
                .all()
            )
            now = datetime.now(timezone.utc)
            for r in rows:
                active = r.status in RUN_ACTIVE_STATUSES
                lock_live = (r.lock_expires_at is not None
                             and r.lock_expires_at.replace(tzinfo=timezone.utc) > now)
                if r.status == "PUBLISHED":
                    return {"acquired": False, "run_id": r.id,
                            "reason": f"already published today (run {r.id})"}
                if active and lock_live:
                    return {"acquired": False, "run_id": r.id,
                            "reason": f"run {r.id} active with live lock"}
                if active and not lock_live:
                    # Crashed run: reclaim it (same row, retry)
                    r.retry_count = (r.retry_count or 0) + 1
                    r.status = "RETRYING"
                    r.error = None
                    r.host = socket.gethostname()
                    r.pid = os.getpid()
                    r.lock_expires_at = now + timedelta(minutes=LOCK_TTL_MINUTES)
                    s.commit()
                    log.info(f"Reclaimed crashed run {r.id} (retry {r.retry_count})")
                    return {"acquired": True, "run_id": r.id, "reason": "recovered crashed run"}
                if r.status in ("FAILED", "ABANDONED"):
                    r.retry_count = (r.retry_count or 0) + 1
                    r.status = "RETRYING"
                    r.error = None
                    r.host = socket.gethostname()
                    r.pid = os.getpid()
                    r.lock_expires_at = now + timedelta(minutes=LOCK_TTL_MINUTES)
                    r.started_at = now
                    r.ended_at = None
                    s.commit()
                    log.info(f"Retrying failed run {r.id} (attempt {r.retry_count + 1})")
                    return {"acquired": True, "run_id": r.id, "reason": "retrying failed run"}
            # No run for today → create one
            run = ProductionRun(
                run_uuid=_uuid(), command=command, channel=channel, format=fmt,
                schedule_name=schedule_name,
                status="DISCOVERING", current_stage="DISCOVERING",
                host=socket.gethostname(), pid=os.getpid(),
                lock_expires_at=now + timedelta(minutes=LOCK_TTL_MINUTES),
                scheduled_date=date_str,
            )
            s.add(run)
            s.commit()
            return {"acquired": True, "run_id": run.id, "reason": "new run"}
        finally:
            s.close()
    except SQLAlchemyError as e:
        log.warning(f"Lock acquisition failed ({str(e)[:140]}) — proceeding unlocked")
        return {"acquired": False, "run_id": None, "reason": "database error (unlocked)"}


def update_run_stage(run_id: int | None, stage: str, error: str | None = None) -> None:
    """Transaction-safe stage/status update; extends the lock heartbeat.

    Every transition is also appended to pipeline_events, so the run's full
    state history (and any failure) survives restarts and is queryable for
    recovery diagnostics.
    """
    if not run_id or not _SA_OK:
        return
    try:
        s = _session()
        if s is None:
            return
        try:
            r = s.get(ProductionRun, run_id)
            if r is None:
                return
            previous = r.current_stage
            r.current_stage = stage
            r.status = stage
            if error is not None:
                r.error = error[:900]
            r.lock_expires_at = datetime.now(timezone.utc) + timedelta(minutes=LOCK_TTL_MINUTES)
            if stage in RUN_TERMINAL_STATUSES:
                r.ended_at = datetime.now(timezone.utc)
                r.lock_expires_at = None
            retries = r.retry_count or 0
            project = r.project_name or ""
            s.commit()
        finally:
            s.close()
        if previous != stage or error:
            level = "error" if (error or stage in ("FAILED", "ABANDONED")) else "info"
            record_event(run_id=run_id, project_name=project, stage=stage,
                         message=(error or f"stage {previous or '-'} → {stage}"),
                         level=level, retry_count=retries)
    except SQLAlchemyError as e:
        log.warning(f"update_run_stage failed ({str(e)[:120]})")


def release_run(run_id: int | None, final_status: str, error: str | None = None) -> None:
    """Terminal transition; releases the lock."""
    update_run_stage(run_id, final_status, error=error)


def heartbeat(run_id: int | None, stage: str | None = None) -> None:
    """Keep the lock alive during long stages (call between stages)."""
    update_run_stage(run_id, stage or "ASSET_FETCHING")


# ─────────────────────────────────────────────────
# Domain persistence helpers (light-touch from the pipeline)
# ─────────────────────────────────────────────────
def upsert_story(title: str, source: str = "", source_url: str = "",
                 trend_score: float | None = None, topic: str = "",
                 research_summary: str = "", status: str = "selected") -> int | None:
    """Insert or update the story row for `title` (duplicate prevention).

    The same headline must never create two rows: an existing story with the
    same normalized title is updated in place (status/score/summary), which
    keeps the story ledger idempotent across resumes and retries.
    """
    if not _SA_OK:
        return None
    try:
        init_db()
        s = _session()
        if s is None:
            return None
        try:
            clean = title[:500]
            norm = " ".join((clean or "").lower().split())
            st = None
            for cand in (s.query(Story)
                         .filter(Story.title.ilike(f"%{clean[:60]}%"))
                         .order_by(Story.id.desc()).limit(25).all()):
                if " ".join((cand.title or "").lower().split()) == norm:
                    st = cand
                    break
            if st is None:
                st = Story(title=clean)
                s.add(st)
            st.source = (source[:200] if source else None) or st.source
            st.source_url = source_url or st.source_url
            st.trend_score = trend_score if trend_score is not None else st.trend_score
            st.topic = ((topic or "")[:120] or None) or st.topic
            st.research_summary = research_summary or st.research_summary
            st.status = status
            s.commit()
            return st.id
        finally:
            s.close()
    except SQLAlchemyError as e:
        log.warning(f"upsert_story failed ({str(e)[:120]})")
        return None


def upsert_video(project_name: str, project_dir: str = "", fmt: str = "short",
                 story_id: int | None = None, visibility: str = "public") -> int | None:
    if not _SA_OK:
        return None
    try:
        init_db()
        s = _session()
        if s is None:
            return None
        try:
            v = (s.query(VideoProject)
                 .filter(VideoProject.project_name == project_name)
                 .one_or_none())
            if v is None:
                v = VideoProject(project_name=project_name[:240], format=fmt)
            v.project_dir = project_dir
            v.format = fmt
            if story_id:
                v.story_id = story_id
            v.visibility = visibility
            s.add(v)
            s.commit()
            return v.id
        finally:
            s.close()
    except SQLAlchemyError as e:
        log.warning(f"upsert_video failed ({str(e)[:120]})")
        return None


def update_video(project_name: str, **fields) -> None:
    if not _SA_OK:
        return
    allowed = {"status", "target_duration", "actual_duration", "script_version",
               "render_version", "qc_status", "compliance_status", "upload_status",
               "youtube_video_id", "youtube_url", "visibility", "thumbnail_status",
               "error", "project_dir"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    try:
        s = _session()
        if s is None:
            return
        try:
            v = (s.query(VideoProject)
                 .filter(VideoProject.project_name == project_name)
                 .one_or_none())
            if v is None:
                return
            for k, val in fields.items():
                setattr(v, k, val)
            s.commit()
        finally:
            s.close()
    except SQLAlchemyError as e:
        log.warning(f"update_video failed ({str(e)[:120]})")


def record_scenes(project_name: str, scenes: list[dict]) -> None:
    """Replace the scene rows for a project (post-assembly truth)."""
    if not _SA_OK:
        return
    try:
        s = _session()
        if s is None:
            return
        try:
            v = (s.query(VideoProject)
                 .filter(VideoProject.project_name == project_name)
                 .one_or_none())
            if v is None:
                return
            v.scenes.clear()
            for sc in scenes:
                v.scenes.append(SceneRecord(
                    scene_index=sc.get("scene_index"),
                    narration=(sc.get("narration") or "")[:2000],
                    duration=sc.get("duration"),
                    visual_type=sc.get("visual_type"),
                    selected_asset=sc.get("selected_asset"),
                    candidate_count=sc.get("candidate_count"),
                    transition_type=sc.get("transition_type"),
                    j_cut=bool(sc.get("j_cut")),
                    l_cut=bool(sc.get("l_cut")),
                    music_state=sc.get("music_state"),
                    caption_state=sc.get("caption_state", "burned"),
                    render_status=sc.get("render_status", "rendered"),
                ))
            s.commit()
        finally:
            s.close()
    except SQLAlchemyError as e:
        log.warning(f"record_scenes failed ({str(e)[:120]})")


def record_qc(project_name: str, report: dict) -> None:
    if not _SA_OK:
        return
    try:
        s = _session()
        if s is None:
            return
        try:
            v = (s.query(VideoProject)
                 .filter(VideoProject.project_name == project_name)
                 .one_or_none())
            if v is None:
                return
            issues = report.get("issues") or []
            crit = [i for i in issues if "CRITICAL" in i or any(
                p in i for p in ("no video stream", "resolution", "aspect ratio",
                                 "NO audio", "suspiciously short", "undecodable", "crashed"))]
            v.qc_results.append(QCResult(
                qc_version="final_qc/1.1",
                flat_frames=sum(1 for f in (report.get("frame_details") or [])
                                if f.get("label") in ("solid", "gradient")),
                frozen_frames=int(report.get("max_frozen_run") or 0),
                black_frames=int(report.get("black_frames") or 0),
                sharpness=report.get("median_sharpness"),
                visual_repetition=json.dumps(
                    [i for i in issues if "variety" in i.lower()])[:900],
                audio_checks=json.dumps(
                    [i for i in issues if "LUFS" in i or "audio" in i.lower()])[:900],
                caption_checks=json.dumps(
                    [i for i in issues if "caption" in i.lower()])[:900],
                gemini_review=json.dumps(
                    [i for i in issues if "frame review" in i.lower()])[:900],
                critical_errors=len(crit),
                warnings=len(issues) - len(crit),
                verdict=report.get("verdict", "FAIL").lower()[:20],
            ))
            s.commit()
        finally:
            s.close()
    except SQLAlchemyError as e:
        log.warning(f"record_qc failed ({str(e)[:120]})")


def record_upload(project_name: str, youtube_video_id: str, url: str,
                  visibility: str, thumbnail_status: str = "uploaded",
                  result: str = "success", error: str | None = None) -> None:
    if not _SA_OK:
        return
    try:
        s = _session()
        if s is None:
            return
        try:
            v = (s.query(VideoProject)
                 .filter(VideoProject.project_name == project_name)
                 .one_or_none())
            if v is None:
                return
            v.uploads.append(UploadRecord(
                youtube_video_id=youtube_video_id, youtube_url=url,
                visibility=visibility, thumbnail_status=thumbnail_status,
                result=result, error=error,
            ))
            s.commit()
        finally:
            s.close()
    except SQLAlchemyError as e:
        log.warning(f"record_upload failed ({str(e)[:120]})")


def record_asset(project_name: str, scene_index: int, source: str, url: str,
                 asset_type: str, fingerprint: str = "", local_path: str = "",
                 selected: bool = True, rejection_reason: str | None = None,
                 width: int | None = None, height: int | None = None) -> None:
    if not _SA_OK:
        return
    try:
        s = _session()
        if s is None:
            return
        try:
            v = (s.query(VideoProject)
                 .filter(VideoProject.project_name == project_name)
                 .one_or_none())
            if v is None:
                return
            v.assets.append(AssetRecord(
                scene_index=scene_index, source=source[:120], url=url,
                asset_type=asset_type, fingerprint=(fingerprint or "")[:64],
                local_path=local_path, selected=selected,
                rejection_reason=rejection_reason, width=width, height=height,
            ))
            s.commit()
        finally:
            s.close()
    except SQLAlchemyError as e:
        log.warning(f"record_asset failed ({str(e)[:120]})")


def _video_for(s, project_name: str):
    if not project_name:
        return None
    return (s.query(VideoProject)
            .filter(VideoProject.project_name == project_name)
            .one_or_none())


def record_script(project_name: str, data: dict, version: int = 1,
                  fmt: str = "short", path: str = "", gate: dict | None = None,
                  model: str = "", voice_duration: float | None = None) -> int | None:
    """Persist one script version for a project. Text/metrics only.

    `data` is a plain dict (the recorder builds it from models.Script) so the
    storage layer never has to import pipeline dataclasses.
    """
    if not _SA_OK:
        return None
    try:
        init_db()
        s = _session()
        if s is None:
            return None
        try:
            v = _video_for(s, project_name)
            if v is None:
                return None
            gate = gate or {}
            scenes = data.get("scenes") or []
            row = ScriptRecord(
                version=version, title=(data.get("title") or "")[:2000],
                format=fmt, scene_count=data.get("scene_count") or len(scenes),
                word_count=data.get("word_count"),
                hook=(data.get("hook") or "")[:4000],
                hook_overlay_text=(data.get("hook_overlay_text") or "")[:240] or None,
                target_duration=data.get("target_duration"),
                voice_duration=voice_duration,
                gate_score=gate.get("score"),
                gate_verdict=(gate.get("verdict") or "")[:20] or None,
                model=(model or "")[:80] or None,
                script_path=path,
            )
            v.scripts.append(row)
            s.commit()
            return row.id
        finally:
            s.close()
    except SQLAlchemyError as e:
        log.warning(f"record_script failed ({str(e)[:120]})")
        return None


def record_narrations(project_name: str, narrations: list[dict]) -> None:
    """Replace the narration rows for a project (post-voice truth)."""
    if not _SA_OK:
        return
    try:
        s = _session()
        if s is None:
            return
        try:
            v = _video_for(s, project_name)
            if v is None:
                return
            v.narrations.clear()
            for n in narrations:
                ts = n.get("timestamps")
                v.narrations.append(NarrationRecord(
                    scene_index=n.get("scene_index"),
                    text=(n.get("text") or "")[:4000],
                    word_count=n.get("word_count"),
                    provider=(n.get("provider") or "")[:40] or None,
                    voice=(n.get("voice") or "")[:80] or None,
                    duration=n.get("duration"),
                    audio_path=n.get("audio_path"),
                    timestamps=(json.dumps(ts)[:20000] if ts else None),
                    status=(n.get("status") or "generated")[:20],
                ))
            s.commit()
        finally:
            s.close()
    except SQLAlchemyError as e:
        log.warning(f"record_narrations failed ({str(e)[:120]})")


def record_music(project_name: str, selection: dict) -> None:
    """Persist the music decision (mood, track, why it was chosen)."""
    if not _SA_OK:
        return
    try:
        init_db()
        s = _session()
        if s is None:
            return
        try:
            v = _video_for(s, project_name)
            if v is None:
                return
            v.music_selections.append(MusicSelection(
                mood=(selection.get("mood") or "")[:40] or None,
                track=(selection.get("track") or "")[:400] or None,
                track_path=selection.get("track_path"),
                source=(selection.get("source") or "")[:60] or None,
                duration=selection.get("duration"),
                resolved_by=(selection.get("resolved_by") or "")[:60] or None,
                reason=(selection.get("reason") or "")[:2000] or None,
                license_note=(selection.get("license_note") or "")[:200] or None,
            ))
            s.commit()
        finally:
            s.close()
    except SQLAlchemyError as e:
        log.warning(f"record_music failed ({str(e)[:120]})")


def record_compliance(project_name: str, report: dict) -> None:
    """Persist the compliance gate verdict + findings."""
    if not _SA_OK or not isinstance(report, dict):
        return
    try:
        init_db()
        s = _session()
        if s is None:
            return
        try:
            v = _video_for(s, project_name)
            if v is None:
                return
            findings = report.get("findings") or []
            v.compliance_results.append(ComplianceResult(
                verdict=(report.get("verdict") or "")[:20] or None,
                policy_basis=(str(report.get("policy_basis") or ""))[:4000] or None,
                findings=json.dumps(findings)[:20000],
                summary=(json.dumps(report.get("summary"))
                         if isinstance(report.get("summary"), (dict, list))
                         else str(report.get("summary") or ""))[:4000] or None,
                rules_version=(report.get("rules_version") or "")[:30] or None,
            ))
            v.compliance_status = (report.get("verdict") or "")[:20] or None
            s.commit()
        finally:
            s.close()
    except SQLAlchemyError as e:
        log.warning(f"record_compliance failed ({str(e)[:120]})")


def record_asset_usage(project_name: str, entries: list[dict],
                       story_title: str = "") -> int:
    """Upsert one row per scene visual; times_used = distinct projects.

    The JSON rotation ledger stays the pipeline's fast local path; this table
    is the durable cross-video history (which URL/dhash was used where).
    """
    if not _SA_OK or not entries:
        return 0
    written = 0
    try:
        from sqlalchemy import or_ as _or
        init_db()
        s = _session()
        if s is None:
            return 0
        try:
            v = _video_for(s, project_name)
            now = datetime.now(timezone.utc)
            for e in entries:
                url = (e.get("url") or "").strip()
                fp = (e.get("fingerprint") or "").strip()[:64]
                scene_index = e.get("scene_index")
                if not url and not fp:
                    continue
                q = s.query(AssetUsage).filter(AssetUsage.project_name == project_name)
                if scene_index is not None:
                    q = q.filter(AssetUsage.scene_index == scene_index)
                match = []
                if url:
                    match.append(AssetUsage.url == url)
                if fp:
                    match.append(AssetUsage.fingerprint == fp)
                row = q.filter(_or(*match)).first()
                if row is None:
                    row = AssetUsage(project_name=project_name[:240],
                                     story_title=story_title,
                                     scene_index=scene_index)
                    s.add(row)
                seen = {r[0] for r in
                        s.query(AssetUsage.project_name).filter(_or(*match)).all()}
                seen.add(project_name)
                row.url = url or row.url
                row.fingerprint = fp or row.fingerprint
                row.title = (e.get("title") or "")[:400] or row.title
                row.channel = (e.get("channel") or "")[:200] or row.channel
                row.query = (e.get("query") or "")[:300] or row.query
                row.asset_type = (e.get("asset_type") or "")[:40] or row.asset_type
                row.story_title = story_title or row.story_title
                if v is not None:
                    row.video_id = v.id
                row.times_used = max(1, len(seen))
                row.last_used = now
                written += 1
            s.commit()
            return written
        finally:
            s.close()
    except SQLAlchemyError as e:
        log.warning(f"record_asset_usage failed ({str(e)[:120]})")
        return written


def record_event(run_id: int | None = None, video_id: int | None = None,
                 project_name: str = "", stage: str = "", message: str = "",
                 level: str = "info", error_type: str = "",
                 retry_count: int = 0) -> None:
    """Append a pipeline event (stage change / warning / error / retry)."""
    if not _SA_OK:
        return
    try:
        init_db()
        s = _session()
        if s is None:
            return
        try:
            s.add(PipelineEvent(
                run_id=run_id, video_id=video_id,
                project_name=project_name[:240] if project_name else None,
                stage=(stage or "")[:40] or None,
                level=(level or "info")[:10],
                message=(message or "")[:4000] or None,
                error_type=(error_type or "")[:140] or None,
                retry_count=retry_count or 0,
            ))
            s.commit()
        finally:
            s.close()
    except SQLAlchemyError as e:
        log.warning(f"record_event failed ({str(e)[:120]})")


def recent_events(limit: int = 25) -> list[dict]:
    """Newest pipeline events (diagnostics for `--status`)."""
    if not _SA_OK:
        return []
    try:
        init_db()
        s = _session()
        if s is None:
            return []
        try:
            rows = (s.query(PipelineEvent)
                    .order_by(PipelineEvent.id.desc()).limit(limit).all())
            return [{"at": str(r.created_at), "level": r.level, "stage": r.stage,
                     "run_id": r.run_id, "project": r.project_name,
                     "message": (r.message or "")[:200]} for r in rows]
        finally:
            s.close()
    except SQLAlchemyError:
        return []


def record_scheduler_run(schedule_name: str, scheduled_for: str, result: str,
                         run_id: int | None = None, detail: str = "",
                         error: str | None = None,
                         started_at: datetime | None = None,
                         ended_at: datetime | None = None) -> int | None:
    """Persist one scheduler invocation so tick history survives restarts."""
    if not _SA_OK:
        return None
    try:
        init_db()
        s = _session()
        if s is None:
            return None
        try:
            row = SchedulerRun(
                schedule_name=(schedule_name or "")[:80],
                scheduled_for=scheduled_for,
                run_id=run_id, result=(result or "")[:30],
                detail=(detail or "")[:2000] or None,
                error=(error or "")[:900] or None,
                started_at=started_at or datetime.now(timezone.utc),
                ended_at=ended_at,
            )
            s.add(row)
            s.commit()
            return row.id
        finally:
            s.close()
    except SQLAlchemyError as e:
        log.warning(f"record_scheduler_run failed ({str(e)[:120]})")
        return None


def story_produced_before(title: str) -> bool:
    """Duplicate-story prevention: has this story already been produced?

    Matches on the normalized title against stories that reached a produced
    state (or already carry a video record). Used by the pipeline as a
    database-backed guard alongside the JSON used-story ledger.
    """
    if not _SA_OK or not title:
        return False
    try:
        init_db()
        s = _session()
        if s is None:
            return False
        try:
            norm = " ".join(title.lower().split())[:500]
            rows = (s.query(Story, VideoProject)
                    .outerjoin(VideoProject, VideoProject.story_id == Story.id)
                    .all())
            for st, _vid in rows:
                if st.status not in ("produced", "used"):
                    continue
                if " ".join((st.title or "").lower().split())[:500] == norm:
                    return True
            return False
        finally:
            s.close()
    except SQLAlchemyError:
        return False


def get_schedule_state(name: str):
    if not _SA_OK:
        return None
    try:
        init_db()
        s = _session()
        if s is None:
            return None
        try:
            return (s.query(ScheduleState)
                    .filter(ScheduleState.schedule_name == name)
                    .one_or_none())
        finally:
            s.close()
    except SQLAlchemyError:
        return None


def set_schedule_state(name: str, next_run=None, last_run=None,
                       last_result: str = "", last_error: str | None = None) -> None:
    if not _SA_OK:
        return
    try:
        init_db()
        s = _session()
        if s is None:
            return
        try:
            row = (s.query(ScheduleState)
                   .filter(ScheduleState.schedule_name == name)
                   .one_or_none())
            if row is None:
                row = ScheduleState(schedule_name=name)
            if next_run is not None:
                row.next_run = next_run
            if last_run is not None:
                row.last_run = last_run
            if last_result:
                row.last_result = last_result[:30]
            if last_error is not None:
                row.last_error = last_error[:900]
            s.add(row)
            s.commit()
        finally:
            s.close()
    except SQLAlchemyError as e:
        log.warning(f"set_schedule_state failed ({str(e)[:120]})")


def status_summary() -> dict:
    """Phase 24: CLI status view. Degrades to empty when no DB."""
    if not _SA_OK:
        return {"available": False}
    try:
        init_db()
        s = _session()
        if s is None:
            return {"available": False}
        try:
            now = datetime.now(timezone.utc)
            active = (s.query(ProductionRun)
                      .filter(ProductionRun.status.in_(RUN_ACTIVE_STATUSES))
                      .order_by(ProductionRun.id.desc()).limit(5).all())
            recent = (s.query(ProductionRun)
                      .order_by(ProductionRun.id.desc()).limit(10).all())
            failed = (s.query(ProductionRun)
                      .filter(ProductionRun.status.in_(("FAILED", "ABANDONED")))
                      .order_by(ProductionRun.id.desc()).limit(5).all())
            uploads = (s.query(UploadRecord)
                       .order_by(UploadRecord.id.desc()).limit(10).all())
            sched = (s.query(ScheduleState).all())
            counts = {
                "stories": s.query(Story).count(),
                "videos": s.query(VideoProject).count(),
                "scenes": s.query(SceneRecord).count(),
                "assets": s.query(AssetRecord).count(),
                "scripts": s.query(ScriptRecord).count(),
                "narrations": s.query(NarrationRecord).count(),
                "music": s.query(MusicSelection).count(),
                "qc": s.query(QCResult).count(),
                "compliance": s.query(ComplianceResult).count(),
                "uploads": s.query(UploadRecord).count(),
                "runs": s.query(ProductionRun).count(),
                "events": s.query(PipelineEvent).count(),
            }
            return {
                "available": True,
                "db": database_info(),
                "counts": counts,
                "active": [{"id": r.id, "stage": r.current_stage, "since": str(r.started_at),
                            "host": r.host, "date": r.scheduled_date} for r in active],
                "recent": [{"id": r.id, "date": r.scheduled_date, "status": r.status,
                            "stage": r.current_stage, "format": r.format} for r in recent],
                "failed": [{"id": r.id, "date": r.scheduled_date, "error": (r.error or "")[:120]}
                           for r in failed],
                "uploads": [{"video_id": u.video_id, "yt": u.youtube_video_id,
                             "visibility": u.visibility, "result": u.result,
                             "at": str(u.uploaded_at)} for u in uploads],
                "schedules": [{"name": x.schedule_name, "next": str(x.next_run),
                               "last": str(x.last_run), "result": x.last_result}
                              for x in sched],
                "events": recent_events(10),
            }
        finally:
            s.close()
    except SQLAlchemyError:
        return {"available": False}


def reset_engine_for_tests() -> None:
    """Tests (or config changes) point DATABASE_URL elsewhere; dispose the
    cached engine so SQLite files release and the next get_engine()
    reconnects to the new URL."""
    global _engine, _Session
    if _engine is not None:
        try:
            _engine.dispose()
        except Exception:
            pass
    _engine = None
    _Session = None
