"""initial schema — stories, video_projects, scenes, assets, qc_results,
uploads, production_runs, schedule_state, performance_snapshots

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-25
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=200)),
        sa.Column("source_url", sa.Text()),
        sa.Column("discovered_at", sa.DateTime(timezone=True)),
        sa.Column("story_date", sa.DateTime(timezone=True)),
        sa.Column("trend_score", sa.Float()),
        sa.Column("topic", sa.String(length=120)),
        sa.Column("status", sa.String(length=30)),
        sa.Column("research_summary", sa.Text()),
        sa.Column("verification_status", sa.String(length=30)),
        sa.Column("selected_reason", sa.Text()),
        sa.Column("rejected_reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "video_projects",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("story_id", sa.Integer(), sa.ForeignKey("stories.id"), nullable=True),
        sa.Column("format", sa.String(length=20), nullable=False),
        sa.Column("project_name", sa.String(length=240), nullable=False),
        sa.Column("project_dir", sa.Text()),
        sa.Column("status", sa.String(length=30)),
        sa.Column("target_duration", sa.Float()),
        sa.Column("actual_duration", sa.Float()),
        sa.Column("script_version", sa.Integer()),
        sa.Column("render_version", sa.Integer()),
        sa.Column("qc_status", sa.String(length=20)),
        sa.Column("compliance_status", sa.String(length=20)),
        sa.Column("upload_status", sa.String(length=20)),
        sa.Column("youtube_video_id", sa.String(length=32)),
        sa.Column("youtube_url", sa.Text()),
        sa.Column("visibility", sa.String(length=20)),
        sa.Column("thumbnail_status", sa.String(length=20)),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "scenes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("video_id", sa.Integer(), sa.ForeignKey("video_projects.id"), nullable=False),
        sa.Column("scene_index", sa.Integer(), nullable=False),
        sa.Column("narration", sa.Text()),
        sa.Column("duration", sa.Float()),
        sa.Column("visual_type", sa.String(length=40)),
        sa.Column("selected_asset", sa.Text()),
        sa.Column("candidate_count", sa.Integer()),
        sa.Column("transition_type", sa.String(length=40)),
        sa.Column("j_cut", sa.Boolean()),
        sa.Column("l_cut", sa.Boolean()),
        sa.Column("music_state", sa.String(length=40)),
        sa.Column("caption_state", sa.String(length=40)),
        sa.Column("render_status", sa.String(length=30)),
        sa.Column("repair_count", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "assets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("video_id", sa.Integer(), sa.ForeignKey("video_projects.id"), nullable=True),
        sa.Column("scene_index", sa.Integer()),
        sa.Column("source", sa.String(length=120)),
        sa.Column("url", sa.Text()),
        sa.Column("asset_type", sa.String(length=30)),
        sa.Column("width", sa.Integer()),
        sa.Column("height", sa.Integer()),
        sa.Column("duration", sa.Float()),
        sa.Column("subject", sa.String(length=240)),
        sa.Column("relevance", sa.Float()),
        sa.Column("quality_score", sa.Float()),
        sa.Column("editorial_score", sa.Float()),
        sa.Column("selected", sa.Boolean()),
        sa.Column("rejection_reason", sa.Text()),
        sa.Column("local_path", sa.Text()),
        sa.Column("fingerprint", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "qc_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("video_id", sa.Integer(), sa.ForeignKey("video_projects.id"), nullable=False),
        sa.Column("qc_version", sa.String(length=30)),
        sa.Column("flat_frames", sa.Integer()),
        sa.Column("frozen_frames", sa.Integer()),
        sa.Column("black_frames", sa.Integer()),
        sa.Column("sharpness", sa.Float()),
        sa.Column("visual_repetition", sa.Text()),
        sa.Column("audio_checks", sa.Text()),
        sa.Column("caption_checks", sa.Text()),
        sa.Column("compliance_checks", sa.Text()),
        sa.Column("gemini_review", sa.Text()),
        sa.Column("critical_errors", sa.Integer()),
        sa.Column("warnings", sa.Integer()),
        sa.Column("verdict", sa.String(length=20)),
        sa.Column("repair_count", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "uploads",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("video_id", sa.Integer(), sa.ForeignKey("video_projects.id"), nullable=False),
        sa.Column("youtube_video_id", sa.String(length=32)),
        sa.Column("youtube_url", sa.Text()),
        sa.Column("uploaded_at", sa.DateTime(timezone=True)),
        sa.Column("visibility", sa.String(length=20)),
        sa.Column("thumbnail_status", sa.String(length=20)),
        sa.Column("result", sa.String(length=20)),
        sa.Column("error", sa.Text()),
        sa.Column("retry_count", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "production_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_uuid", sa.String(length=64), nullable=False, unique=True),
        sa.Column("schedule_name", sa.String(length=80)),
        sa.Column("command", sa.Text()),
        sa.Column("channel", sa.String(length=40)),
        sa.Column("format", sa.String(length=20)),
        sa.Column("story_id", sa.Integer(), sa.ForeignKey("stories.id"), nullable=True),
        sa.Column("status", sa.String(length=30)),
        sa.Column("current_stage", sa.String(length=40)),
        sa.Column("project_name", sa.String(length=240)),
        sa.Column("error", sa.Text()),
        sa.Column("retry_count", sa.Integer()),
        sa.Column("host", sa.String(length=120)),
        sa.Column("pid", sa.Integer()),
        sa.Column("lock_expires_at", sa.DateTime(timezone=True)),
        sa.Column("scheduled_date", sa.String(length=10), index=True),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "schedule_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("schedule_name", sa.String(length=80), nullable=False, unique=True),
        sa.Column("next_run", sa.DateTime(timezone=True)),
        sa.Column("last_run", sa.DateTime(timezone=True)),
        sa.Column("last_result", sa.String(length=30)),
        sa.Column("last_error", sa.Text()),
        sa.Column("run_lock", sa.String(length=64)),
        sa.Column("lock_expires_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "performance_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("video_id", sa.Integer(), sa.ForeignKey("video_projects.id"), nullable=False),
        sa.Column("views", sa.BigInteger()),
        sa.Column("likes", sa.BigInteger()),
        sa.Column("comments", sa.BigInteger()),
        sa.Column("avg_percentage_viewed", sa.Float()),
        sa.Column("watch_time_minutes", sa.Float()),
        sa.Column("subscribers_gained", sa.BigInteger()),
        sa.Column("captured_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_production_runs_scheduled_date", "production_runs", ["scheduled_date"])


def downgrade() -> None:
    op.drop_table("performance_snapshots")
    op.drop_table("schedule_state")
    op.drop_table("production_runs")
    op.drop_table("uploads")
    op.drop_table("qc_results")
    op.drop_table("assets")
    op.drop_table("scenes")
    op.drop_table("video_projects")
    op.drop_table("stories")
