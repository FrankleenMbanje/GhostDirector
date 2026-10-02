"""
GhostDirector — Trending Celebrity News Short (The Fame Files)

End-to-end daily producer: pick today's trending story → research → script →
voice → assets → 9:16 render → thumbnail → metadata → compliance → upload.

Run from main.py:
  python main.py --trending                    # auto-pick + produce + upload
  python main.py --trending --privacy private  # produce without going live
  python main.py --trending-list               # show today's top candidates
  python main.py --trending-story "headline"   # force a specific story

The Fame Files channel stays clean of the rise-and-fall lane by construction:
this module only ever produces from trending_news.discover_trending(), which
lane-guards rise-and-fall-shaped headlines.
"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

import click

import config
from models import Script
from utils.logger import get_logger
from utils.ffmpeg_cmd import get_duration
from utils.channel_state import draw_persona, apply_persona_to_template, log_packaging

log = get_logger("trending_short")

# Channel → template for the trending-lane short. The Fame Files owns
# breaking celebrity news; Rise and Ruin runs the same engine on
# collapse-shaped stories (falling moguls, crumbling empires) with its own
# tone and branding.
_CHANNEL_TEMPLATES = {
    config.CHANNEL_FAMEFILES: "famefiles_trending",
    config.CHANNEL_RISEANDRUIN: "riseandruin_trending",
}
TEMPLATE_NAME = _CHANNEL_TEMPLATES[config.CHANNEL_FAMEFILES]

# Long-form companion (operator schedule 2026-09-26): the daily cycle is a
# short FIRST (discovery engine) then an 8-min+ documentary on the SAME
# story. celebrity_8min carries the 8-min target + the exclusive-split
# thumbnail pin.
_CHANNEL_LONGFORM_TEMPLATES = {
    config.CHANNEL_FAMEFILES: "celebrity_8min",
    config.CHANNEL_RISEANDRUIN: "celebrity_8min",
}
LONGFORM_TEMPLATE_NAME = _CHANNEL_LONGFORM_TEMPLATES[config.CHANNEL_FAMEFILES]

_BRAND_LINES = {
    config.CHANNEL_FAMEFILES: (
        "Follow The Fame Files — tomorrow's trending files drop daily.",
        "#shorts #celebritynews #famefiles"),
    config.CHANNEL_RISEANDRUIN: (
        "Follow Rise and Ruin — every empire falls. We cover the fall.",
        "#shorts #riseandfall #riseandruin"),
}
MAX_SHORT_SECONDS = 58      # hard cap under the 59s Shorts limit


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
def _project_dir_for(title: str) -> Path:
    from slugify import slugify
    slug = slugify(title, max_length=50)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    d = config.OUTPUT_DIR / f"{slug}_{stamp}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "scenes").mkdir(exist_ok=True)
    return d


def _today_harare() -> str:
    """Today's date in the production timezone (Phase 10) — the daily
    idempotency key. Africa/Harare per operator spec."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Africa/Harare")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def _story_to_topic(story: dict) -> str:
    """The headline IS the research query — it's the freshest summary of the
    story, and the researcher's own DDG grounding expands from it."""
    t = (story.get("title") or "").strip()
    return t or "celebrity news today"


def _today_runs(channel: str) -> list[dict]:
    from pipeline.trending_news import _load_ledger
    today = datetime.now(timezone.utc).date().isoformat()
    return [r for r in _load_ledger().get("runs", [])
            if (r.get("at") or "").startswith(today) and r.get("channel") == channel]


def _story_seen(title: str, recorder) -> bool:
    """Operator rule (2026-09-26): NEVER repeat a video or topic. A story is
    'seen' when the file ledger has ever recorded a run with this title OR
    the database says it was produced before (best-effort)."""
    t = (title or "").strip().lower()
    if not t:
        return False
    try:
        from pipeline.trending_news import _load_ledger
        for r in _load_ledger().get("runs", []):
            if (r.get("title") or "").strip().lower() == t:
                return True
    except Exception:
        pass
    try:
        return recorder.story_seen_before(title)
    except Exception:
        return False


def _override_metadata(meta: dict, script: Script, story: dict,
                       channel: str = config.CHANNEL_FAMEFILES,
                       longform: bool = False) -> dict:
    """Rewrite the long-form metadata into Fame Files packaging.

    Shorts carry the story headline; long-forms carry the script's
    documentary title (FIX-068): the short already shipped with the exact
    headline, and reusing it makes the uploader's FIX-054 idempotency guard
    treat the doc as a duplicate and skip the upload entirely.
    """
    if longform:
        title = (script.title or story.get("title")
                 or meta.get("title") or "").strip()
    else:
        title = (story.get("title") or script.title or "").strip()
    # Guard: the packaged title must describe the story the SCRIPT tells.
    # A headline from a different story here = a live video titled as a
    # different event than its narration (shipped live once, 2026-09-22).
    if story.get("title") and script.title:
        head = set(story["title"].lower().split())
        scr = set(script.title.lower().split())
        stop = {"the", "a", "an", "of", "and", "for", "to", "in", "on", "his",
                "her", "its", "with", "at", "is", "was", "new"}
        if head and scr and len((head - stop) & (scr - stop)) < 2:
            log.warning(
                f"Story/script title mismatch — using the script's own title "
                f"({script.title!r}) over the headline ({story['title']!r})")
            title = script.title.strip()
    if len(title) > 95:
        title = title[:92].rstrip(" .,!?:-") + "..."

    hook = (script.hook or script.description or "").strip()
    follow_line, tag_line = _BRAND_LINES.get(
        channel, _BRAND_LINES[config.CHANNEL_FAMEFILES])
    lines = [
        hook or title,
        "",
        follow_line,
        "",
        tag_line,
    ]
    # Source credit (copyright hygiene): cite the outlet we broke from.
    src = story.get("source")
    if src and src.lower() not in ("ddg", "google news"):
        lines += ["", f"Source: {src}"]

    tags = list(dict.fromkeys((meta.get("tags") or []) + (
        ["celebrity news", "trending", "entertainment news", "news today"]
        + (["rise and fall", "documentary", "empire", "downfall", "rise and ruin"]
           if channel == config.CHANNEL_RISEANDRUIN else
           ["fame files", "celebs", "shorts"])
        if not longform else
        ["celebrity news", "documentary", "entertainment", "celebs exposed",
         "deep dive", "fame files"]
    )))[:25]

    return {
        "title": title,
        "description": "\n".join(lines).strip(),
        "tags": tags,
        "category": meta.get("category", "24"),
        "language": meta.get("language", "en"),
    }


# ──────────────────────────────────────────────
# Candidate listing
# ──────────────────────────────────────────────
async def list_candidates(top: int | None = None) -> list[dict]:
    """Fetch + score today's candidates (used by both --trending-list and the
    auto-picker)."""
    from pipeline.trending_news import discover_trending
    return await discover_trending(limit=top or config.TRENDING_TOP_N)


# ──────────────────────────────────────────────
# Production
# ──────────────────────────────────────────────
async def run_trending_short(
    story: dict | None = None,
    upload: bool = True,
    privacy: str = "public",
    channel: str = config.CHANNEL_FAMEFILES,
    resume_dir: Path | None = None,
    longform: bool = False,
) -> dict:
    """Produce (and optionally upload) today's trending short.

    Privacy defaults to PUBLIC (operator order 2026-10-02): videos go live
    on upload; the final QC gate and the compliance gate remain the safety
    net, and every video lands in PUBLISH_QUEUE.md for a verify-and-pin pass.

    story=None auto-picks the top-scored candidate. resume_dir reuses an
    interrupted project's research.json/script.json (research quota is never
    spent twice). Returns
    {"ok": bool, "video_id": ..., "url": ..., "title": ..., "project_dir": ...}.
    """
    from pipeline.trending_news import record_story, record_run

    # Phase 9/21: production-state persistence — best-effort, never fatal.
    # The recorder tracks the run AND the video record; a daily gate refusal
    # on "already published" is a hard stop (duplicate prevention).
    from storage.production_state import RunRecorder
    recorder = RunRecorder(
        schedule_name="famefiles-daily" if channel == config.CHANNEL_FAMEFILES else f"{channel}-daily",
        command="trending short", channel=channel, fmt="short",
        date_str=_today_harare(),
    )
    import os as _os
    within_daily = bool(_os.environ.get("GD_WITHIN_DAILY"))
    if recorder.duplicate and not within_daily:
        say("[yellow]Today's production already exists — skipping duplicate run.[/yellow]")
        return {"ok": False, "reason": "duplicate_daily_run"}

    console = None
    try:
        from rich.console import Console
        console = Console()
    except Exception:
        pass

    def say(msg: str) -> None:
        if console:
            console.print(msg)
        else:
            print(msg)

    banner = ("The Fame Files — trending news short" 
              if channel == config.CHANNEL_FAMEFILES 
              else f"{channel} — trending news short")
    say(f"\n[bold magenta]═══ {banner} ═══[/bold magenta]")

    # ── Resume: pin the story to the interrupted project ──
    # FIX: a resume that auto-picks a *fresh* story pairs it with the OLD
    # project's script — a live video with a title describing a different
    # story than its content. The story recorded in trending_story.json
    # (or the script's own title, as a fallback) is authoritative here.
    if resume_dir:
        pinned = resume_dir / "trending_story.json"
        if pinned.exists():
            try:
                story = json.loads(pinned.read_text(encoding="utf-8"))
                say(f"[dim]Resume: story pinned from trending_story.json — {story.get('title')!r}[/dim]")
            except Exception as e:
                log.warning(f"trending_story.json unreadable ({e})")
        if story is None:
            try:
                s = json.loads((resume_dir / "script.json").read_text(encoding="utf-8"))
                story = {"title": s.get("title") or resume_dir.name, "url": "",
                         "source": "resume", "published": ""}
                say(f"[yellow]⚠ Resume: no trending_story.json — pinning to script title: {story['title']!r}[/yellow]")
            except Exception:
                story = {"title": resume_dir.name.rsplit("_", 1)[0].replace("-", " "),
                         "url": "", "source": "resume", "published": ""}

    # ── Auto-pick: top-scored fresh candidate (after resume pinning) ──
    if story is None:
        cands = await list_candidates()
        if not cands:
            say("[red]✗ No fresh trending stories found — try again later.[/red]")
            return {"ok": False, "reason": "no_fresh_stories"}
        # No-repeat rule: take the first candidate whose story was NEVER
        # produced before (ledger + DB). Only when every candidate is a
        # repeat do we fall back to the top one — a repeat beats no video.
        story = None
        for c in cands:
            if _story_seen(c.get("title") or "", recorder):
                say(f"[dim]Skipping already-produced story: {(c.get('title') or '')[:60]}[/dim]")
                continue
            story = c
            break
        if story is None:
            story = cands[0]
            say("[yellow]⚠ All candidates already produced — taking the top one anyway.[/yellow]")
        else:
            say("[dim]Auto-picked the top-scored fresh (never-produced) story.[/dim]")

    topic = _story_to_topic(story)
    say(f"\n[bold yellow]* Story:[/bold yellow] {story.get('title')}")

    # ── Daily guard: one production run per day (sane quota/news hygiene) ──
    today = _today_runs(channel)
    if today:
        say(f"[yellow]⚠ {len(today)} trending run(s) already today on {channel}: "
            f"{today[-1].get('title')!r}. Continuing anyway.[/yellow]")

    record_story(story, status="seen", channel=channel)

    # ── Template + persona (per-channel rotation, same as main pipeline) ──
    template = config.load_template(
        (_CHANNEL_LONGFORM_TEMPLATES if longform else _CHANNEL_TEMPLATES).get(
            channel, LONGFORM_TEMPLATE_NAME if longform else TEMPLATE_NAME))
    say(f"[dim]Channel: {channel} — template: {template.get('name', '?')}[/dim]")
    persona = None
    if resume_dir:  # reuse the interrupted run's persona, like main.py
        from utils import channel_state as _cs
        persona = _cs._load(channel).get("last_persona")
    if persona is None:
        persona = draw_persona(topic, channel)
    apply_persona_to_template(template, persona)

    if resume_dir:
        project_dir = resume_dir
        say(f"[dim]Resuming in: {project_dir}[/dim]\n")
    else:
        project_dir = _project_dir_for(topic)
        say(f"[dim]Output directory: {project_dir}[/dim]\n")
        # Persist the story alongside the project: any resume of this dir
        # must produce THIS story, never a fresh auto-pick (see resume pin).
        (project_dir / "trending_story.json").write_text(
            json.dumps(story, indent=2, ensure_ascii=False), encoding="utf-8")

    script_path = project_dir / "script.json"
    recorder.stage("RESEARCHING")
    recorder.story(
        title=story.get("title") or topic, source=story.get("source") or "",
        url=story.get("url") or "", score=story.get("score"),
    )
    recorder.video(project_dir.name, project_dir=str(project_dir),
                   fmt="longform" if longform else "short",
                   visibility=privacy)

    # ── Step 1: Research (skipped when the resumed project already has it) ──
    if resume_dir and (project_dir / "research.json").exists():
        from models import ResearchResult
        research = ResearchResult.load(project_dir / "research.json")
        say(f"[dim]Resume: research.json loaded — {len(research.key_facts)} facts\n[/dim]")
    else:
        say("[bold blue]Step 1/6 — Researching story[/bold blue]")
        from pipeline.researcher import research_topic
        research = await research_topic(topic, template)
        research.save(project_dir / "research.json")
        say(f"[green]OK[/green] {len(research.key_facts)} facts, {len(research.key_people)} people\n")

    # ── Step 2: Script (skipped when the resumed project already has it) ──
    if resume_dir and script_path.exists():
        from models import Script as ScriptModel
        script = ScriptModel.load(script_path)
        say(f"[dim]Resume: script.json loaded — \"{script.title}\", {script.total_scenes} scenes\n[/dim]")
    else:
        say("[bold blue]Step 2/6 — Writing script[/bold blue]")
        from pipeline.scriptwriter import generate_script
        script = await generate_script(research, template)

        # Long-form FLOOR (operator rule: docs are always 8 min+): measure
        # the draft and expand with the research on hand BEFORE any voice/
        # asset quota is spent. Shorts skip this (FIX-060 caps them instead).
        if longform:
            from pipeline.scriptwriter import enforce_longform_length
            _w0 = sum(len((s.narration or "").split()) for s in script.scenes)
            # FIX-068: min_ratio 1.06 — the words→seconds estimate ran ~3.4%
            # fast vs real TTS (a 1.0 floor shipped 7:44 vs the 8:00 YPP
            # floor). 6% headroom keeps the RENDER over 8:00, not just the
            # estimate.
            script = await enforce_longform_length(
                script, research=research, template=template, min_ratio=1.06)
            _w1 = sum(len((s.narration or "").split()) for s in script.scenes)
            say(f"[dim]Long-form floor: {_w0}→{_w1} words "
                f"(target {template.get('script', {}).get('target_duration_minutes')} min)[/dim]")

        script.save(script_path)
        say(f"[green]OK[/green] \"{script.title}\" — {script.total_scenes} scenes\n")

        # ── Quality gate (the human-editor pass): script must earn ≥8/10 or
        # it gets ONE targeted rewrite attempt feeding the gate's issues back.
        from pipeline.quality_gate import evaluate_script_only
        gate = evaluate_script_only(script, is_short=not longform)
        say(f"[dim]Quality gate (script): {gate['score']}/10 {gate['verdict'].upper()}"
            + (f" — {gate['issues'][:3]}" if gate['issues'] else "") + "[/dim]")
        if gate["score"] < 8.0:
            from pipeline.scriptwriter import _call_gemini
            fix_prompt = (
                f"You wrote this {'long-form documentary' if longform else 'short-form'} script. It scored "
                f"{gate['score']}/10 on a professional retention audit. Rewrite the "
                "COMPLETE script fixing every issue below. Keep all facts, names, "
                "numbers exactly; keep the same scene count and fields.\nISSUES:\n- "
                + "\n- ".join(gate["issues"]) + "\n\nSCENES:\n"
                + json.dumps(
                    [{"scene_number": s.scene_number, "narration": s.narration,
                      "visual_prompt": s.visual_prompt, "mood": s.mood,
                      "sfx_cue": s.sfx_cue, "broll_keywords": s.broll_keywords}
                     for s in script.scenes], indent=1, ensure_ascii=False))
            try:
                fixed = await _call_gemini(fix_prompt)
                fmap = {s.get("scene_number"): s for s in fixed.get("scenes", [])}
                if fmap:
                    _orig_narrations = {s.scene_number: s.narration for s in script.scenes}
                    for s in script.scenes:
                        if s.scene_number in fmap:
                            s.narration = fmap[s.scene_number].get("narration", s.narration)
                    # Long-form guard: a rewrite must never shrink the doc
                    # back under the 8-min floor — revert to the original
                    # narrations if the rewrite lost words.
                    if longform:
                        _w_orig = sum(len((n or "").split()) for n in _orig_narrations.values())
                        _w_new = sum(len((s.narration or "").split()) for s in script.scenes)
                        if _w_new < _w_orig:
                            say("[yellow]Rewrite shrank the long-form script — keeping the original.[/yellow]")
                            for s in script.scenes:
                                s.narration = _orig_narrations[s.scene_number]
                    script.save(script_path)
                    gate2 = evaluate_script_only(script, is_short=not longform)
                    say(f"[dim]Quality gate (rewrite): {gate2['score']}/10[/dim]")
                    if gate2["score"] > gate["score"]:
                        gate = gate2
            except Exception as e:
                log.warning(f"quality-gate rewrite pass failed: {e}")

    # FIX-060: hard Shorts length enforcement — runs on FRESH and RESUMED
    # scripts alike (the rewrite loop cannot force a short enough draft, and
    # a resumed over-budget script.json must not render past 60s either).
    # Found live: a 206-word draft rendered 79.2s and compliance blocked it.
    # Long-form mode skips this (the 8-min+ doc is governed by the
    # enforce_longform_length FLOOR in the main pipeline instead).
    if not longform:
        from pipeline.scriptwriter import enforce_shorts_word_budget
        script = enforce_shorts_word_budget(script, max_seconds=55.0)
        script.save(script_path)

    # ── Step 3: Voice + timestamps ──
    say("[bold blue]Step 3/6 — Voiceover + timestamps[/bold blue]")
    from pipeline.voice import generate_voices
    from pipeline.timestamps import generate_timestamps
    script = await generate_voices(script, template, project_dir)
    script = generate_timestamps(script, project_dir)
    script.save(script_path)
    total = sum(s.audio_duration_seconds or s.duration_target_seconds or 5.0
                for s in script.scenes)
    say(f"[green]OK[/green] {total:.1f}s of voice\n")
    if total > MAX_SHORT_SECONDS + 6:
        say(f"[yellow]⚠ Voice runs {total:.0f}s — Shorts get cropped by YouTube past 59s. "
            f"Consider trimming script.json and resuming.[/yellow]")

    # ── Step 4: Assets ──
    say("[bold blue]Step 4/6 — Fetching visuals[/bold blue]")
    if resume_dir and not (project_dir / ".media_flushed_v4").exists():
        # A re-render must not silently inherit the previous run's downloaded
        # media — stale b-roll (e.g. footage the new relevance gate would
        # reject) survives in scenes/ otherwise. Keep photos (they pass the
        # strict photo gate and cost quota to re-fetch); flush every video
        # layer and the assembled cuts so they rebuild under the new rules.
        # Marker-guarded: runs ONCE per ruleset, not on every resume.
        # v3: rebuild cuts under the auto-grade rules (dark b-roll/stills get
        # colorist-lifted at cut-render time).
        import glob as _glob
        flushed = 0
        for pattern in ("scenes/scene_*_broll.mp4", "scenes/scene_*_broll.source.json",
                        "scenes/scene_*_video.mp4", "edit_media/scene_*_prepared*.mp4",
                        "edit_media/scene_*_cut*.mp4"):
            for f in _glob.glob(str(project_dir / pattern)):
                Path(f).unlink(missing_ok=True)
                flushed += 1
        if flushed:
            say(f"[dim]Resume: flushed {flushed} stale video artifacts — rebuilding under current quality rules[/dim]")
        (project_dir / ".media_flushed_v4").touch()
    from pipeline.assets import fetch_assets
    recorder.stage("ASSET_FETCHING")
    script = await fetch_assets(script, template, project_dir)
    script.save(script_path)
    say("[green]OK[/green] Assets ready\n")

    # ── Step 5: Render ──
    _is_vertical = not longform
    say(f"[bold blue]Step 5/6 — Assembling {'9:16 render' if _is_vertical else '16:9 long-form render'}[/bold blue]")
    recorder.stage("ASSEMBLING")
    from pipeline.assembler_ffmpeg import assemble_video
    final_video = assemble_video(script, template, project_dir)
    say(f"[green]OK[/green] {final_video.name}\n")
    recorder.scenes_from_timeline(project_dir)

    # FIX-056: final QC on the finished short — resolution/AR truth,
    # frame grid, black/frozen frames, sharpness, loudness. One rebuild
    # attempt on critical failure; warnings never block (unlisted review).
    from pipeline.visual_director import qc_gate
    final_video, shippable, qc_issues = qc_gate(
        Path(final_video), expected_size=(1080, 1920) if _is_vertical else (1920, 1080),
        rebuild=lambda: assemble_video(script, template, project_dir),
        report_dir=project_dir,
        project_dir=project_dir,
        repair={"script": script, "template": template},
    )
    recorder.stage("QC")
    recorder.qc(project_dir)
    if shippable:
        say(f"[green]OK[/green] Final QC pass{': ' + '; '.join(qc_issues) if qc_issues else ''}\n")
    else:
        recorder.fail("final QC failed: " + "; ".join(qc_issues)[:300])
        say(f"[bold red]✗ Final QC FAILED:[/bold red] {qc_issues}")
        record_story(story, status="skipped", channel=channel)
        return {"ok": False, "reason": "final_qc_failed",
                "issues": qc_issues, "project_dir": str(project_dir)}

    # ── Step 6: Thumbnail + metadata + compliance ──
    say("[bold blue]Step 6/6 — Thumbnail, metadata, compliance[/bold blue]")
    from pipeline.thumbnail import generate_thumbnail
    # generate_thumbnail promotes template.thumbnail.preferred_styles[0]
    # (the EXCLUSIVE news-split) to thumbnail.png automatically.
    thumb_path = generate_thumbnail(script, template, project_dir)

    from pipeline.metadata import generate_metadata
    meta = generate_metadata(script, template, project_dir)
    meta = _override_metadata(meta, script, story, channel=channel,
                              longform=longform)
    (project_dir / "metadata.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    from pipeline.compliance import check_compliance
    report = check_compliance(script, meta, project_dir)
    verdict = report.get("verdict")
    say(f"Compliance: [bold]{(verdict or '?').upper()}[/bold] → {project_dir / 'compliance_report.json'}\n")

    # ── Final quality gate: the full editor's pass on the finished project ──
    from pipeline.quality_gate import evaluate_project
    gate = evaluate_project(project_dir, script_path=script_path,
                            is_short=not longform)
    say(f"Quality gate: [bold]{gate['score']}/10 {gate['verdict'].upper()}[/bold] "
        f"{gate['breakdown']}\n")
    if gate["verdict"] == "redo":
        record_story(story, status="skipped", channel=channel)
        return {"ok": False, "reason": "quality_gate_redo",
                "score": gate["score"], "issues": gate["issues"][:6],
                "project_dir": str(project_dir)}

    if verdict == "fail" and upload:
        record_story(story, status="skipped", channel=channel)
        return {"ok": False, "reason": "compliance_fail",
                "project_dir": str(project_dir), "verdict": verdict}

    recorder.stage("COMPLIANCE" if verdict != "fail" else "READY_FOR_UPLOAD")
    # ── Upload ──
    video_id = None
    if upload and verdict != "fail":
        recorder.stage("UPLOADING")
        if longform:
            # The 8-min flow exports both aspect ratios; YouTube gets the
            # 16:9 master (the 9:16 file is the companion, uploaded
            # separately by the pipeline when configured).
            _lf = project_dir / "final_16x9.mp4"
            if _lf.exists():
                final_video = _lf
        from pipeline.uploader import upload_video
        video_id = upload_video(
            project_dir=project_dir,
            video_path=final_video,
            thumbnail_path=thumb_path,
            privacy_status=privacy,
            channel=channel,
        )
        # FIX-081: verify the bytes actually landed. The FIX-054 idempotency
        # guard can hand back an existing id, and a same-title short once let
        # a doc stage exit 0 with NO doc on the channel (run 11, 2026-10-02).
        # A mismatch fails the stage loudly so the day reads PARTIAL
        # (doc=failed) instead of pretending the doc shipped.
        try:
            from pipeline.uploader import verify_upload_duration
            ok_u, note_u = verify_upload_duration(
                video_id, get_duration(Path(final_video)), channel=channel)
            if not ok_u:
                raise RuntimeError(f"upload verification failed: {note_u}")
            log.info(f"Upload verified — {note_u}")
        except RuntimeError:
            raise
        except Exception as e:
            log.warning(f"Upload verification skipped ({e})")
        recorder.upload(video_id, f"https://youtu.be/{video_id}", privacy)
        say(f"[green]✓ Uploaded to {channel}:[/green] https://youtu.be/{video_id} (privacy={privacy})")

    # ── Ledgers ──
    record_story(story, status="used" if video_id else "produced",
                 channel=channel, video_id=video_id, project_dir=str(project_dir),
                 fmt="longform" if longform else "short")

    # FIX-077: the short↔doc bridge. Whichever sibling ships second gets a
    # comment linking the first (posted automatically; the operator pins it
    # with one click at publish time — the Data API cannot pin).
    if video_id:
        try:
            from pipeline.trending_news import find_companion_video_id
            from pipeline.uploader import post_companion_bridge
            want = "short" if longform else "longform"
            companion = find_companion_video_id(story.get("title") or "", want)
            if companion:
                post_companion_bridge(video_id, companion, companion_is_doc=(want == "longform"))
            else:
                log.info("No companion video yet for this story — bridge skipped")
        except Exception as e:
            log.warning(f"Bridge comment step failed (non-fatal): {e}")
    if video_id:
        recorder.complete()
    else:
        recorder.video_update(upload_status="skipped" if not upload else "failed")
    record_run(story, str(project_dir), video_id, channel)
    log_packaging(
        topic=story.get("title") or topic,
        title_used=meta["title"],
        title_variants=[],
        thumbnail_variant=Path(thumb_path).name if thumb_path else "thumbnail.png",
        youtube_url=f"https://youtu.be/{video_id}" if video_id else None,
        channel=channel,
    )

    # FIX-078: publish fast-path — every shipped video lands in the queue
    # with a Studio link and a +6h publish-by deadline.
    if video_id:
        try:
            from pipeline.trending_news import append_publish_queue
            append_publish_queue(story.get("title") or topic,
                                 "longform" if longform else "short",
                                 video_id, packaged_title=meta["title"])
        except Exception as e:
            log.warning(f"Publish queue update failed (non-fatal): {e}")

    return {
        "ok": True,
        "video_id": video_id,
        "url": f"https://youtu.be/{video_id}" if video_id else None,
        "title": meta["title"],
        "project_dir": str(project_dir),
    }


def run_cli(story_title: str | None = None, upload: bool = True,
            privacy: str = "public", resume_dir: Path | None = None,
            channel: str | None = None, longform: bool = False) -> None:
    """Synchronous CLI entry used by main.py."""
    story = None
    if story_title:
        story = {"title": story_title, "url": "", "source": "manual",
                 "published": ""}
    result = asyncio.run(run_trending_short(story=story, upload=upload,
                                            privacy=privacy,
                                            channel=channel or config.CHANNEL_FAMEFILES,
                                            resume_dir=resume_dir,
                                            longform=longform))
    if not result.get("ok"):
        raise click.ClickException(f"Trending short failed: {result.get('reason')}")
