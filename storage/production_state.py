"""
GhostDirector — production-state recorder (Phase 9/23).

Bridges the working pipeline to the persistent store WITHOUT restructuring
it: every call is best-effort, degrades silently when DATABASE_URL is unset
(the JSON ledgers remain the local fallback), and never raises into the
production path. Run-stage transitions follow the Phase 9 state machine:

DISCOVERING → RESEARCHING → SCRIPTING → ASSET_FETCHING → ASSEMBLING → QC
→ COMPLIANCE → READY_FOR_UPLOAD → UPLOADING → PUBLISHED  (or FAILED)

The recorder derives project names from whatever context it gets; the
pipeline keeps running even when every database call fails.
"""

import json
from pathlib import Path

from utils.logger import get_logger

log = get_logger("production_state")


def _db():
    try:
        from storage import database as db
        return db
    except Exception:  # storage layer missing/broken → pipeline continues
        return None


class RunRecorder:
    """Tracks one production run end-to-end."""

    def __init__(self, schedule_name: str = "manual", command: str = "",
                 channel: str = "", fmt: str = "short", date_str: str | None = None):
        self.db = _db()
        self.run_id = None
        self.video_id = None
        self.project_name = ""
        self.schedule_name = schedule_name
        self.date_str = date_str
        if self.db is not None:
            try:
                res = self.db.acquire_daily_lock(
                    schedule_name, date_str or "manual", command=command,
                    channel=channel, fmt=fmt)
                if res.get("acquired"):
                    self.run_id = res.get("run_id")
                elif res.get("run_id") and "published" in (res.get("reason") or ""):
                    self.run_id = None  # day already shipped — caller decides
                else:
                    self.run_id = res.get("run_id")
                log.info(f"Run lock: {res.get('reason')} (run_id={self.run_id})")
            except Exception as e:
                log.warning(f"Run lock unavailable ({str(e)[:100]})")

    @property
    def duplicate(self) -> bool:
        """True when today's run already completed and this must not proceed."""
        if self.db is None:
            return False
        try:
            res = self.db.acquire_daily_lock(self.schedule_name, self.date_str or "manual")
            return (not res.get("acquired")) and "published" in (res.get("reason") or "")
        except Exception:
            return False

    def stage(self, stage: str, error: str | None = None) -> None:
        if self.run_id and self.db is not None:
            try:
                self.db.update_run_stage(self.run_id, stage, error=error)
            except Exception:
                pass

    def fail(self, error: str) -> None:
        if self.run_id and self.db is not None:
            try:
                self.db.release_run(self.run_id, "FAILED", error=error)
            except Exception:
                pass

    def complete(self) -> None:
        if self.run_id and self.db is not None:
            try:
                self.db.release_run(self.run_id, "PUBLISHED")
            except Exception:
                pass

    # ── Domain records ─────────────────────────────
    def story(self, title: str, source: str = "", url: str = "",
              score: float | None = None, topic: str = "",
              summary: str = "") -> None:
        if self.db is None:
            return
        try:
            sid = self.db.upsert_story(title, source=source, source_url=url,
                                       trend_score=score, topic=topic,
                                       research_summary=summary)
            self._story_id = sid
        except Exception:
            pass

    def video(self, project_name: str, project_dir: str = "", fmt: str = "short",
              visibility: str = "unlisted") -> None:
        self.project_name = project_name
        if self.db is None:
            return
        try:
            self.video_id = self.db.upsert_video(
                project_name, project_dir=project_dir, fmt=fmt,
                story_id=getattr(self, "_story_id", None), visibility=visibility)
        except Exception:
            pass

    def video_update(self, **fields) -> None:
        if self.db is None or not self.project_name:
            return
        try:
            self.db.update_video(self.project_name, **fields)
        except Exception:
            pass

    def scenes_from_timeline(self, project_dir: str | Path) -> None:
        """Record scene truth from timeline.json (post-assembly)."""
        if self.db is None or not self.project_name:
            return
        try:
            tl = json.loads((Path(project_dir) / "timeline.json").read_text(encoding="utf-8"))
            scenes = []
            for i, sc in enumerate(tl.get("scenes", []), 1):
                scenes.append({
                    "scene_index": sc.get("scene_number", i),
                    "narration": None,
                    "duration": sc.get("duration_seconds"),
                    "visual_type": None,
                    "selected_asset": (sc.get("cuts") or [{}])[0].get("file"),
                    "candidate_count": None,
                    "transition_type": None,
                    "j_cut": bool(sc.get("pre_lap_seconds")),
                    "music_state": tl.get("music_mood"),
                })
            self.db.record_scenes(self.project_name, scenes)
        except Exception:
            pass

    # ── Phase 22: script / narration / music / compliance / usage ──
    def script(self, script, project_dir: str | Path | None = None, version: int = 1,
               gate: dict | None = None, model: str = "",
               voice_duration: float | None = None, fmt: str = "short") -> None:
        """Persist script text/metrics. Duck-typed — no model import here."""
        if self.db is None or not self.project_name:
            return
        try:
            scenes = list(getattr(script, "scenes", None) or [])
            words = sum(len((getattr(s, "narration", "") or "").split())
                        for s in scenes)
            data = {
                "title": getattr(script, "title", "") or "",
                "hook": getattr(script, "hook", "") or "",
                "hook_overlay_text": getattr(script, "hook_overlay_text", "") or "",
                "scene_count": getattr(script, "total_scenes", None) or len(scenes),
                "word_count": words,
                "target_duration": getattr(script, "estimated_duration_minutes", None),
            }
            path = str(Path(project_dir) / "script.json") if project_dir else ""
            self.db.record_script(self.project_name, data, version=version, fmt=fmt,
                                  path=path, gate=gate, model=model,
                                  voice_duration=voice_duration)
        except Exception:
            pass

    def narrations(self, script, provider: str = "", voice: str = "") -> None:
        """Persist per-scene voice data (provider, audio file, duration, words)."""
        if self.db is None or not self.project_name:
            return
        try:
            rows = []
            for i, s in enumerate(getattr(script, "scenes", None) or [], 1):
                text = getattr(s, "narration", "") or ""
                rows.append({
                    "scene_index": getattr(s, "scene_number", i) or i,
                    "text": text,
                    "word_count": len(text.split()),
                    "provider": provider or "edge-tts",
                    "voice": voice,
                    "duration": getattr(s, "audio_duration_seconds", None),
                    "audio_path": getattr(s, "audio_path", None),
                    "timestamps": getattr(s, "timestamps", None),
                })
            self.db.record_narrations(self.project_name, rows)
        except Exception:
            pass

    def music_from_timeline(self, project_dir: str | Path) -> None:
        """Persist the music decision the assembler recorded in timeline.json."""
        if self.db is None or not self.project_name:
            return
        try:
            tl = json.loads((Path(project_dir) / "timeline.json")
                            .read_text(encoding="utf-8"))
            track = tl.get("music_track") or ""
            mood = tl.get("music_mood") or ""
            if not (track or mood):
                return
            self.db.record_music(self.project_name, {
                "mood": mood,
                "track": Path(str(track)).name if track else "",
                "track_path": str(track) if track else "",
                "source": "assets/music" if track else "none",
                "resolved_by": "story-signal" if mood else "template",
                "reason": f"assembler music_mood={mood or 'unset'}",
            })
        except Exception:
            pass

    def compliance(self, report: dict | None = None,
                   project_dir: str | Path | None = None) -> None:
        """Persist the compliance verdict + findings (inline report or file)."""
        if self.db is None or not self.project_name:
            return
        try:
            if report is None and project_dir is not None:
                report = json.loads((Path(project_dir) / "compliance_report.json")
                                    .read_text(encoding="utf-8"))
            if isinstance(report, dict):
                self.db.record_compliance(self.project_name, report)
        except Exception:
            pass

    def asset_usage(self, entries: list[dict], story_title: str = "") -> None:
        """Persist the scene→asset rotation truth (provenance + reuse count)."""
        if self.db is None or not self.project_name or not entries:
            return
        try:
            self.db.record_asset_usage(self.project_name, entries,
                                       story_title=story_title)
        except Exception:
            pass

    def asset_usage_from_script(self, script, project_dir: str | Path | None = None,
                                story_title: str = "") -> None:
        """Build asset-usage rows from the script's resolved scene assets.

        Screens the per-project variety ledger (scenes/*.source.json) for the
        dhash fingerprint so the durable usage history carries the same
        identity as the local rotation ledger.
        """
        if self.db is None or not self.project_name:
            return
        try:
            prints: dict[str, str] = {}
            if project_dir is not None:
                for sidecar in sorted(Path(project_dir).glob("scenes/*.source.json")):
                    try:
                        meta = json.loads(sidecar.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    fp = meta.get("dhash") or meta.get("fingerprint") or ""
                    url = meta.get("url") or ""
                    if fp and url:
                        prints[url] = fp
            entries = []
            for i, s in enumerate(getattr(script, "scenes", None) or [], 1):
                url = getattr(s, "source_url", None) or ""
                entries.append({
                    "scene_index": getattr(s, "scene_number", i) or i,
                    "url": url,
                    "title": getattr(s, "source_title", "") or "",
                    "channel": getattr(s, "source_channel", "") or "",
                    "asset_type": getattr(s, "visual_type", "") or "",
                    "fingerprint": prints.get(url, ""),
                })
            self.asset_usage(entries, story_title=story_title)
        except Exception:
            pass

    def event(self, message: str, level: str = "info", stage: str = "",
              error_type: str = "") -> None:
        """Append a pipeline event tied to this run (warnings/errors/retries)."""
        if self.db is None:
            return
        try:
            self.db.record_event(run_id=self.run_id, project_name=self.project_name,
                                 stage=stage, message=message, level=level,
                                 error_type=error_type)
        except Exception:
            pass

    def story_seen_before(self, title: str) -> bool:
        """Database-backed duplicate-story guard (best-effort)."""
        if self.db is None:
            return False
        try:
            return bool(self.db.story_produced_before(title))
        except Exception:
            return False

    # ── Quality / upload records ──
    def qc(self, project_dir: str | Path) -> None:
        if self.db is None or not self.project_name:
            return
        try:
            report = json.loads(
                (Path(project_dir) / "final_qc_report.json").read_text(encoding="utf-8"))
            self.db.record_qc(self.project_name, report)
            self.video_update(qc_status=report.get("verdict", "FAIL").lower())
        except Exception:
            pass

    def upload(self, youtube_video_id: str, url: str, visibility: str,
               thumbnail_status: str = "uploaded", result: str = "success",
               error: str | None = None) -> None:
        if self.db is None or not self.project_name:
            return
        try:
            self.db.record_upload(self.project_name, youtube_video_id, url,
                                  visibility, thumbnail_status=thumbnail_status,
                                  result=result, error=error)
            self.video_update(
                upload_status="uploaded" if result == "success" else "failed",
                youtube_video_id=youtube_video_id, youtube_url=url,
                visibility=visibility, thumbnail_status=thumbnail_status)
        except Exception:
            pass
