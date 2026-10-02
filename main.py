"""
GhostDirector — Main CLI Entry Point

Usage:
    python main.py "The Untold Story of Diddy" --template celebrity
    python main.py "The Rise of MrBeast" --template celebrity --tts elevenlabs
    python main.py "Why Kanye Lost Everything" --template celebrity --mode resolve
    python main.py --batch topics.txt --template celebrity --auto
"""

import sys
import asyncio
import json
from pathlib import Path
from datetime import datetime

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from slugify import slugify

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

import config
from models import VideoProject, Script
from utils.logger import get_logger
from utils.channel_state import draw_persona, apply_persona_to_template, log_packaging

log = get_logger("main")
console = Console()


def _verify_llm(allow_offline: bool = False):
    """Fail fast on a stale Gemini model id before burning a full run.

    allow_offline (FIX-046): resume runs with research+script already on disk
    need ZERO LLM calls — but verify_gemini_models probes models LIVE and
    raises when every candidate is quota-exhausted, killing runs that would
    have completed. When every LLM stage is already satisfied, a dead probe
    is downgraded to a warning and the run proceeds (any *needed* later call
    still surfaces its own clear error).
    """
    try:
        config.verify_gemini_models()
    except Exception as e:
        if allow_offline:
            console.print(
                f"[yellow]⚠ Gemini check skipped ({e}) — continuing: all LLM "
                f"stages already satisfied by resume artifacts.[/yellow]"
            )
            return
        console.print(f"[bold red]✗ Config error:[/bold red] {e}")
        sys.exit(1)


def _create_project_dir(topic: str) -> Path:
    """Create a unique output directory for this video project."""
    slug = slugify(topic, max_length=50)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    project_dir = config.OUTPUT_DIR / f"{slug}_{timestamp}"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "scenes").mkdir(exist_ok=True)
    return project_dir


def _display_banner():
    """Show the GhostDirector banner."""
    banner = """
    G H O S T  D I R E C T O R
    Automated YouTube Video Factory
    """
    console.print(Panel(banner, style="bold cyan", title="v1.0"))


def _display_script_review(script: Script):
    """Display the script for user review."""
    table = Table(title=f"📜 Script: {script.title}", show_lines=True)
    table.add_column("#", style="cyan", width=4)
    table.add_column("Narration", style="white", max_width=60)
    table.add_column("Visual", style="green", max_width=30)
    table.add_column("Type", style="yellow", width=12)
    table.add_column("Mood", style="magenta", width=10)
    table.add_column("~Sec", style="dim", width=5)

    for scene in script.scenes:
        table.add_row(
            str(scene.scene_number),
            scene.narration[:100] + ("..." if len(scene.narration) > 100 else ""),
            scene.visual_prompt[:40] + ("..." if len(scene.visual_prompt) > 40 else ""),
            scene.visual_type,
            scene.mood,
            str(int(scene.duration_target_seconds)),
        )

    console.print(table)
    console.print(f"\n[bold]Total scenes:[/bold] {script.total_scenes}")
    console.print(f"[bold]Est. duration:[/bold] ~{script.estimated_duration_minutes} min\n")


async def run_pipeline(
    topic: str,
    template_name: str = "celebrity",
    tts_provider: str = "edge-tts",
    render_mode: str = "ffmpeg",
    generate_shorts: bool = False,
    review: bool = False,
    auto: bool = False,
    upload: bool = False,
    privacy: str = "public",
    force_upload: bool = False,
    push_resolve: bool = False,
    resolve_render: bool = False,
    push_no_auto: bool = False,
    channel: str = "default",
    resume_dir: Path | None = None,
):
    """
    Run the full GhostDirector pipeline for a single topic.

    resume_dir: an existing project dir whose research.json/script.json are
    loaded and steps 1-2 skipped (stage-level resume — quota already spent
    on them is never spent twice; P5 backlog item, found necessary by the
    first live run when a long LLM-retry window outlived a shell timeout).
    """
    _display_banner()

    # ── Setup ──
    # FIX-046: a resume run whose research+script are already persisted needs
    # no LLM — don't let a quota-dead live probe kill it.
    _verify_llm(
        allow_offline=bool(resume_dir)
        and (resume_dir / "research.json").exists()
        and (resume_dir / "script.json").exists()
    )
    console.print(f"\n[bold yellow]* Topic:[/bold yellow] {topic}")
    console.print(f"[bold yellow]* Template:[/bold yellow] {template_name}")
    if channel != "default":
        console.print(f"[bold yellow]* Channel:[/bold yellow] {channel}")
    console.print(f"[bold yellow]* TTS:[/bold yellow] {tts_provider}")
    console.print(f"[bold yellow]* Render:[/bold yellow] {render_mode}\n")

    template = config.load_template(template_name)    # Phase 3.12: rotate voice/palette/transition-seed per video so
    # consecutive uploads don't share a fingerprint. Plan A7: rotation and
    # the packaging ledger are per-channel profiles. On resume, reuse the
    # project's original persona instead of drawing a fresh one.
    persona = None
    if resume_dir:
        from utils import channel_state as _cs
        persona = _cs._load(channel).get("last_persona")
    if persona is None:
        persona = draw_persona(topic, channel)

    apply_persona_to_template(template, persona)

    if resume_dir:
        project_dir = resume_dir
        console.print(f"[dim]Resuming in: {project_dir}[/dim]")
    else:
        project_dir = _create_project_dir(topic)
        console.print(f"[dim]Output directory: {project_dir}[/dim]\n")

    project = VideoProject(
        topic=topic,
        template_name=template_name,
        render_mode=render_mode,
        tts_provider=tts_provider,
        generate_shorts=generate_shorts,
        project_dir=str(project_dir),
    )

    # Override TTS provider in template if specified via CLI
    if tts_provider != template.get("voice", {}).get("provider", "edge-tts"):
        template.setdefault("voice", {})["provider"] = tts_provider

    # ────────────────────────────────────
    # STEP 1-2: Research + Script (skipped on resume when artifacts exist)
    # ────────────────────────────────────
    resume_research = resume_dir and (project_dir / "research.json").exists()
    resume_script = resume_dir and (project_dir / "script.json").exists()

    if resume_research:
        from models import ResearchResult
        research = ResearchResult.load(project_dir / "research.json")
        project.research = research
        project.research_path = str(project_dir / "research.json")
        console.print(f"[dim]Resume: research.json loaded — {len(research.key_facts)} facts\n[/dim]")
    else:
        console.rule("[bold blue]Step 1/7 — Researching Topic")
        from pipeline.researcher import research_topic

        project.status = "researching"
        research = await research_topic(topic, template)
        research_path = project_dir / "research.json"
        research.save(research_path)
        project.research = research
        project.research_path = str(research_path)
        console.print(f"[green]OK[/green] Research complete — {len(research.key_facts)} facts, {len(research.key_people)} people\n")

    script_path = project_dir / "script.json"
    if resume_script:
        from models import Script
        script = Script.load(script_path)
        project.script = script
        project.script_path = str(script_path)
        console.print(f"[dim]Resume: script.json loaded — \"{script.title}\", {script.total_scenes} scenes\n[/dim]")
    else:
        console.rule("[bold blue]Step 2/7 — Writing Script")
        from pipeline.scriptwriter import generate_script

        project.status = "scripting"
        script = await generate_script(research, template)
        script.save(script_path)
        project.script = script
        project.script_path = str(script_path)

    # ── Script Review ──
    if review and not auto:
        _display_script_review(script)
        proceed = click.confirm("Proceed with this script?", default=True)
        if not proceed:
            console.print("[yellow]Pipeline stopped. Edit script.json and re-run.[/yellow]")
            project.save(project_dir / "project.json")
            return

    console.print(f"[green]OK[/green] Script ready — \"{script.title}\"\n")

    # FIX-063: long-form length FLOOR. FIX-060 stops a Short shipping long;
    # nothing stopped a long-form shipping SHORT (quota-truncated drafts were
    # observed). A template with target_duration_minutes now gets its draft
    # verified and expanded with the research already on hand before any
    # voice/asset quota is spent on it.
    try:
        from pipeline.scriptwriter import enforce_longform_length
        _scenes_before = len(script.scenes)
        _words_before = sum(len((s.narration or "").split()) for s in script.scenes)
        script = await enforce_longform_length(
            script, research=locals().get("research"), template=template,
            min_ratio=1.0)
        _words_after = sum(len((s.narration or "").split()) for s in script.scenes)
        # Save on ANY change: a word-only expansion keeps the scene count and
        # would otherwise leave a stale script.json behind (audio and captions
        # would then describe a different script than the file on disk).
        if len(script.scenes) != _scenes_before or _words_after != _words_before:
            script.save(script_path)
            console.print(
                f"[green]OK[/green] Script expanded for the "
                f"{template.get('script', {}).get('target_duration_minutes')}-min target "
                f"({_scenes_before}→{len(script.scenes)} scenes, "
                f"{_words_before}→{_words_after} words)\n")
    except Exception as _len_err:
        log.warning(f"FIX-063 long-form length pass skipped: {_len_err}")

    # ────────────────────────────────────
    # STEP 3: Voice Generation
    # ────────────────────────────────────
    console.rule("[bold blue]Step 3/7 — Generating Voiceover")
    from pipeline.voice import generate_voices

    project.status = "voicing"
    script = await generate_voices(script, template, project_dir)
    script.save(script_path)  # Update with audio paths
    console.print(f"[green]OK[/green] All {len(script.scenes)} voice clips generated\n")

    # ────────────────────────────────────
    # STEP 4: Word Timestamps
    # ────────────────────────────────────
    console.rule("[bold blue]Step 4/7 — Extracting Word Timestamps")
    from pipeline.timestamps import generate_timestamps

    project.status = "timestamps"
    script = generate_timestamps(script, project_dir)
    script.save(script_path)  # Update with timestamps
    console.print(f"[green]OK[/green] Timestamps extracted for all scenes\n")

    # ────────────────────────────────────
    # STEP 5: Asset Fetching
    # ────────────────────────────────────
    console.rule("[bold blue]Step 5/7 — Downloading Stock Footage & Photos")
    from pipeline.assets import fetch_assets

    project.status = "assets"
    script = await fetch_assets(script, template, project_dir)
    script.save(script_path)  # Update with asset paths
    console.print(f"[green]OK[/green] All assets downloaded\n")

    # ────────────────────────────────────
    # STEP 6: Video Assembly
    # ────────────────────────────────────
    console.rule("[bold blue]Step 6/7 — Assembling Video")
    project.status = "assembling"

    if render_mode == "resolve":
        from pipeline.assembler_resolve import assemble_resolve_project
        console.print("[yellow]Creating DaVinci Resolve project bundle...[/yellow]")
        final_path = assemble_resolve_project(script, template, project_dir)
        project.final_video_path = str(final_path)
        console.print(f"[green]OK[/green] Resolve project bundle ready in: {project_dir / 'resolve_project'}\n")
    else:
        from pipeline.assembler_ffmpeg import assemble_video
        final_path = assemble_video(script, template, project_dir)
        project.final_video_path = str(final_path)
        console.print(f"[green]OK[/green] Video rendered: {final_path.name}\n")

        # FIX-056: final quality-control pass — inspect the ENTIRE render
        # (resolution truth, frame grid, black/frozen frames, sharpness,
        # loudness) before anything may ship. One rebuild on critical
        # failure; warnings never block (uploads are unlisted for review).
        try:
            from pipeline.visual_director import qc_gate
            vw, vh = map(int, template.get("visuals", {}).get(
                "resolution", "1920x1080").split("x"))
            final_path, shippable, qc_issues = qc_gate(
                Path(final_path), expected_size=(vw, vh),
                rebuild=lambda: assemble_video(script, template, project_dir),
                report_dir=project_dir,
                project_dir=project_dir,
                repair={"script": script, "template": template},
            )
            project.final_video_path = str(final_path)
            if shippable:
                console.print(f"[green]OK[/green] Final QC pass ({len(qc_issues)} warnings)\n" if qc_issues
                              else "[green]OK[/green] Final QC pass\n")
            else:
                console.print(f"[bold red]✗ Final QC FAILED:[/bold red] {qc_issues}")
                console.print("  Upload blocked — fix the issues and re-run.")
                project.status = "qc_failed"
                project.save(project_dir / "project.json")
                sys.exit(1)
        except SystemExit:
            raise
        except Exception as qc_err:
            console.print(f"[yellow]⚠ Final QC skipped ({qc_err})[/yellow]")

        # Always generate the DaVinci Resolve bundle (FIX-032): FCPXML 1:1 edit
        # + captions SRT + EDL fallback + push script. FIX-034: --push-resolve
        # zero-click pushes the whole edit into a running Resolve Studio.
        try:
            from pipeline.assembler_resolve import export_resolve_bundle, resolve_studio_installed
            auto_push = push_resolve or (
                not push_no_auto and resolve_studio_installed()
            )
            export_resolve_bundle(
                script, template, project_dir,
                push=auto_push, render=resolve_render,
            )
            console.print(f"[green]OK[/green] DaVinci Resolve bundle: resolve_project/timeline.fcpxml (+ captions.srt, timeline.edl)\n")
        except Exception as res_err:
            log.warning(f"Could not generate DaVinci assets: {res_err}")

    if generate_shorts:
        from pipeline.shorts import generate_short
        template.setdefault("shorts", {})["enabled"] = True
        short_path = generate_short(script, template, project_dir)
        if short_path:
            project.final_shorts_path = str(short_path)
            console.print(f"[green]OK[/green] Short rendered: {short_path.name}\n")

    # ────────────────────────────────────
    # STEP 7: Thumbnail & Metadata
    # ────────────────────────────────────
    console.rule("[bold blue]Step 7/7 — Thumbnail & Metadata")
    from pipeline.thumbnail import generate_thumbnail
    from pipeline.metadata import generate_metadata

    project.status = "finishing"
    thumb_path = generate_thumbnail(script, template, project_dir)
    project.thumbnail_path = str(thumb_path)

    metadata = generate_metadata(script, template, project_dir)
    project.metadata_path = str(project_dir / "metadata.json")

    # Plan A5: suggestion-backed SEO enrichment, seeded with the operator's
    # ORIGINAL topic (autocomplete works on search-shaped queries, not on
    # creative titles). Runs before the compliance gate so the enriched
    # package is what gets policy-checked. Network-safe: failures log.
    if config.SEO_AUTOCOMPLETE:
        try:
            from pipeline.keywords import enrich_metadata
            enrich_metadata(metadata, topic=topic)
            (project_dir / "metadata.json").write_text(
                json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as seo_err:
            log.warning(f"SEO enrichment skipped: {seo_err}")

    # FIX-033: YouTube compliance gate — check the finished package against
    # metadata-spam / inauthentic-content (AI slop) / advertiser-friendly /
    # chapters / thumbnail / Shorts rules BEFORE any upload decision.
    # Writes compliance_report.json with a pass/warn/fail verdict.
    from pipeline.compliance import check_compliance
    compliance = check_compliance(script, metadata, project_dir)

    # ────────────────────────────────────
    # STEP 8: YouTube Upload
    # ────────────────────────────────────
    if upload:
        console.rule("[bold blue]Step 8/8 — Uploading to YouTube")
        from pipeline.uploader import upload_video
        
        if not project.final_video_path or not Path(project.final_video_path).exists():
            console.print("[red]✗ No final video found to upload.[/red]")
        else:
            project.status = "uploading"

            # Compliance gate: refuse to upload a FAIL verdict (override: --force-upload)
            verdict = None
            report_path = project_dir / "compliance_report.json"
            if report_path.exists():
                try:
                    verdict = json.loads(report_path.read_text(encoding="utf-8")).get("verdict")
                except Exception:
                    verdict = None
            if verdict == "fail" and not force_upload:
                console.print("[bold red]✗ Compliance check FAILED — upload blocked.[/bold red]")
                console.print(f"  Findings: {report_path}")
                console.print("  Fix the issues and re-run, or pass --force-upload to override.")
            else:
                try:
                    video_id = upload_video(
                        project_dir=project_dir,
                        video_path=Path(project.final_video_path),
                        thumbnail_path=Path(project.thumbnail_path) if project.thumbnail_path else None,
                        privacy_status=privacy,
                        channel=channel
                    )
                    project.youtube_url = f"https://youtu.be/{video_id}"
                    console.print(f"[green]✓[/green] Uploaded to YouTube: {project.youtube_url}\n")

                    # Step 8 used to ship only the 16:9 — the rendered 9:16
                    # short silently never went live. Upload it too, with a
                    # " #shorts" title so the idempotency guard doesn't
                    # mistake it for the long video and skip it. The short
                    # gets its own QC gate (9:16 expected).
                    short_path = project_dir / "final_9x16_short.mp4"
                    if short_path.exists():
                        try:
                            from pipeline.visual_director import qc_gate
                            _sp, short_ok, short_issues = qc_gate(
                                short_path, expected_size=(1080, 1920),
                                report_dir=project_dir,
                                project_dir=project_dir,
                            )
                            if not short_ok:
                                raise RuntimeError(f"final QC failed: {short_issues}")
                            short_id = upload_video(
                                project_dir=project_dir,
                                video_path=short_path,
                                thumbnail_path=Path(project.thumbnail_path) if project.thumbnail_path else None,
                                privacy_status=privacy,
                                channel=channel,
                                title_override=f"{metadata['title']} #shorts",
                            )
                            console.print(f"[green]✓[/green] Short uploaded: https://youtu.be/{short_id}\n")
                        except Exception as e:
                            console.print(f"[bold red]✗ Short upload failed:[/bold red] {e}")
                except Exception as e:
                    console.print(f"[bold red]✗ Upload Failed:[/bold red] {e}")

    # ── Save final project state ──
    project.status = "done"
    project.save(project_dir / "project.json")

    # Phase 2.9 data layer: log which packaging went live (CTR join later).
    thumb_variant = "thumbnail.png (v1_focal)"
    try:
        concepts = json.loads((project_dir / "thumbnail_concepts.json").read_text(encoding="utf-8"))
        thumb_variant = concepts.get("concepts", [{}])[0].get("file", thumb_variant)
    except Exception:
        pass
    log_packaging(
        topic=topic,
        title_used=metadata["title"],
        title_variants=metadata.get("title_variants", []),
        thumbnail_variant=thumb_variant,
        youtube_url=project.youtube_url,
        channel=channel,
    )

    # ── Summary ──
    console.print("\n")
    console.rule("[bold green]OK DONE")
    summary = Table(title="Video Generation Complete", show_lines=True)
    summary.add_column("Item", style="cyan")
    summary.add_column("Value", style="white")
    summary.add_row("Title", script.title)
    summary.add_row("Scenes", str(script.total_scenes))
    summary.add_row("Duration", f"~{script.estimated_duration_minutes} min")
    summary.add_row("Output", str(project_dir))
    if project.final_video_path:
        size_mb = Path(project.final_video_path).stat().st_size / (1024 * 1024)
        summary.add_row("Video", f"{Path(project.final_video_path).name} ({size_mb:.1f} MB)")
    summary.add_row("Thumbnail", Path(thumb_path).name)
    if project.youtube_url:
        summary.add_row("YouTube", project.youtube_url)
    summary.add_row("Mode", render_mode.upper())
    if compliance:
        summary.add_row("Compliance", f"{compliance['verdict'].upper()} (see compliance_report.json)")
    console.print(summary)
    console.print("\n")


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────
@click.command()
@click.argument("topic", required=False)
@click.option("--template", "-t", default="celebrity", help="Template name (e.g., celebrity, documentary)")
@click.option("--tts", default="edge-tts", type=click.Choice(["edge-tts", "elevenlabs"]), help="TTS provider")
@click.option("--mode", "-m", default="ffmpeg", type=click.Choice(["ffmpeg", "resolve"]), help="Render mode")
@click.option("--shorts", is_flag=True, help="Also generate a 9:16 Short")
@click.option("--review", is_flag=True, help="Review script before rendering")
@click.option("--auto", is_flag=True, help="Skip all prompts (for batch mode)")
@click.option("--batch", type=click.Path(exists=True), help="Path to a .txt file with one topic per line")
@click.option("--upload", is_flag=True, help="Automatically upload to YouTube after rendering")
@click.option("--force-upload", is_flag=True, help="Upload even when the compliance check FAILS")
@click.option("--push-resolve", is_flag=True, help="Push the edit into running DaVinci Resolve Studio after rendering")
@click.option("--resolve-render", is_flag=True, help="With --push-resolve: also queue a YouTube-preset render from Resolve Studio")
@click.option("--no-auto-resolve", is_flag=True, help="Disable auto-push when Resolve Studio is detected running")
@click.option("--privacy", default="public", type=click.Choice(["public", "private", "unlisted"]), help="YouTube video privacy status (default public — operator order 2026-10-02; the QC + compliance gates still block bad renders)")
@click.option("--channel", default="default", help="Channel profile: riseandruin | famefiles (per-channel persona/state/token files)")
@click.option("--trending", "trending_flag", is_flag=True, help="The Fame Files: pick today's trending celebrity story, produce a 9:16 news short, upload (combine with --privacy / --no-upload)")
@click.option("--trending-list", "trending_list", is_flag=True, help="The Fame Files: show today's top trending celebrity stories, then exit")
@click.option("--trending-story", metavar="HEADLINE", help="With --trending: force a specific story headline instead of auto-picking")
@click.option("--longform", "longform_flag", is_flag=True, help="With --trending: produce the 8-min+ long-form documentary on the story instead of the 9:16 short")
@click.option("--no-upload", "no_upload", is_flag=True, help="With --trending: produce but skip the upload step")
@click.option("--resume", "resume_dir", type=click.Path(exists=True, file_okay=False), help="Resume an interrupted project directory (skips research/script stages whose artifacts exist)")
@click.option("--sync-analytics", is_flag=True, help="Sync retention analytics for the channel's uploads into db/analytics.json (A3)")
@click.option("--import-ctr", multiple=True, metavar="VIDEO_ID=CTR", help="Import a Studio impressions-CTR number, e.g. --import-ctr dQw4w9WgXcQ=7.5 (repeatable)")
@click.option("--scout", is_flag=True, help="Score the competitor watch-list and refresh db/topic_queue.json (A4)")
@click.option("--add-competitor", metavar="@HANDLE_OR_URL", help="Add a competitor channel to the scout watch-list")
@click.option("--next-topic", "next_topic_flag", is_flag=True, help="Pop the highest-scoring queued topic and exit")
@click.option("--ab-apply", "ab_video_id", metavar="VIDEO_ID", help="Apply an A/B swap to a live video (needs --project plus --title-index and/or --thumb)")
@click.option("--project", "ab_project", type=click.Path(exists=True), help="Project directory for --ab-apply")
@click.option("--title-index", type=int, help="Title variant index to swap in via --ab-apply")
@click.option("--thumb", "thumb_file", help="Thumbnail variant filename to swap in via --ab-apply")
@click.option("--ab-leaderboard", is_flag=True, help="Show which packaging variants earn the most clicks (A6)")
@click.option("--audit", is_flag=True, help="vidIQ-style channel audit: health checks + what-to-do-next (A10)")
@click.option("--find-competitors", "find_competitors", is_flag=True, help="Discover competitor channels for your niche (A10; combine with --niche)")
@click.option("--niche", default=None, help="Niche string for --find-competitors and thumbnail style ranking (overrides db/channel_profile.json)")
@click.option("--compare", "compare_flag", is_flag=True, help="My channel vs the competitor watch-list: subs, videos, efficiency (A10)")
@click.option("--channel-memory", "channel_memory_flag", is_flag=True, help="Learn from EVERY video on the channel (manual + automated): packaging features + outcomes -> db/channel_memory.json")
@click.option("--thumbnails", "thumbnails_project", type=click.Path(exists=True, file_okay=False), metavar="PROJECT_DIR", help="Re-run the thumbnail style matrix on an existing project (A9)")
@click.option("--styles", default=None, metavar="ID,ID,...", help="With --thumbnails: comma-separated style ids to render (default: recommended for the niche)")
@click.option("--pick", "pick_out", type=click.Path(), metavar="OUT.png", help="With --thumbnails: also write the chosen composite as OUT.png")
@click.option("--daily", "daily_flag", is_flag=True, help="Run today's Fame Files production (idempotent — safe to re-run; needs DATABASE_URL for run locking)")
@click.option("--status", "status_flag", is_flag=True, help="Show production status from the database (runs, uploads, schedules)")
@click.option("--schedule-loop", "schedule_loop", is_flag=True, help="Run the persistent daily-10:00 Africa/Harare scheduler loop in this process")
def main(topic, template, tts, mode, shorts, review, auto, batch, upload, force_upload, push_resolve, resolve_render, no_auto_resolve, privacy, channel, resume_dir, sync_analytics, import_ctr, scout, add_competitor, next_topic_flag, ab_video_id, ab_project, title_index, thumb_file, ab_leaderboard, audit, find_competitors, niche, compare_flag, channel_memory_flag, thumbnails_project, styles, pick_out, trending_flag, trending_list, trending_story, longform_flag, no_upload, daily_flag, status_flag, schedule_loop):
    """GhostDirector — Automated Faceless YouTube Video Factory.

    Production: python main.py "TOPIC" --template celebrity_4min --shorts
    Growth ops: --audit | --find-competitors | --compare | --scout | --next-topic
                | --sync-analytics | --import-ctr | --ab-apply | --ab-leaderboard
    Thumbnails: python main.py --thumbnails output/<project> [--styles id,id] [--pick out.png]
    """

    # ── Production system ops (Phase 10/24) ──
    if status_flag:
        from storage.database import status_summary
        st = status_summary()
        if not st.get("available"):
            console.print("[red]Database unavailable — set DATABASE_URL.[/red]")
            return
        console.print(Panel.fit("[bold]GhostDirector production status[/bold]"))
        if st.get("active"):
            console.print("[bold]Active runs:[/bold]", st["active"])
        if st.get("failed"):
            console.print("[bold red]Failed runs:[/bold red]", st["failed"])
        console.print("[bold]Recent runs:[/bold]")
        for r in st.get("recent", []):
            console.print(f"  run {r['id']} {r['date']} {r['format']}: {r['status']} ({r['stage']})")
        console.print("[bold]Latest uploads:[/bold]")
        for u in st.get("uploads", []):
            console.print(f"  video {u['video_id']}: {u['yt']} ({u['visibility']}, {u['result']}) at {u['at']}")
        if st.get("schedules"):
            console.print("[bold]Schedules:[/bold]", st["schedules"])
        return
    if schedule_loop:
        from storage.scheduler import loop_forever
        loop_forever()
        return
    if daily_flag:
        from storage.scheduler import run_daily
        channel_for_daily = (channel if channel in (config.CHANNEL_FAMEFILES,
                                                    config.CHANNEL_RISEANDRUIN)
                             else config.CHANNEL_FAMEFILES)
        raise SystemExit(run_daily(channel=channel_for_daily))

    # ── The Fame Files: daily trending celebrity news short ──
    if trending_list:
        from pipeline.trending_short import list_candidates
        cands = asyncio.run(list_candidates())
        if not cands:
            console.print("[red]✗ No fresh trending stories found.[/red]")
            return
        table = Table(title="The Fame Files — trending today")
        table.add_column("#", justify="right")
        table.add_column("Story", max_width=70)
        table.add_column("Source")
        table.add_column("Score", justify="right")
        table.add_column("When", justify="right")
        for i, c in enumerate(cands, 1):
            when = f"{c['when_hours_ago']:.0f}h" if c.get("when_hours_ago") is not None else "—"
            table.add_row(str(i), c["title"], c.get("source") or "—",
                          f"{c['score']:.1f}", when)
        console.print(table)
        console.print("[dim]Produce one: python main.py --trending --trending-story \"<headline>\" --privacy public[/dim]")
        return
    if trending_flag:
        from pipeline.trending_short import run_cli
        # The generic default channel would record trending runs under
        # "default" and pollute the per-channel ledgers — trending shorts are
        # Fame Files work unless an explicit real channel is given.
        trend_channel = (channel if channel in (config.CHANNEL_FAMEFILES,
                                                config.CHANNEL_RISEANDRUIN)
                         else config.CHANNEL_FAMEFILES)
        run_cli(story_title=trending_story,
                upload=not no_upload,
                privacy=privacy,
                channel=trend_channel,
                resume_dir=Path(resume_dir) if resume_dir else None,
                longform=longform_flag)
        return

    # ── Growth operations (beat-VidNinjas plan A3/A4/A6/A7) ──
    if sync_analytics:
        from pipeline.analytics import sync_channel
        result = sync_channel(channel=channel)
        console.print(f"[green]OK[/green] Analytics sync: {result['synced']} synced, {result['skipped']} skipped")
        for note in result.get("notes", []):
            console.print(f"  · {note}")
        return
    if import_ctr:
        from pipeline.analytics import import_manual_ctr
        ctr_map = {}
        for pair in import_ctr:
            try:
                vid, ctr = pair.rsplit("=", 1)
                ctr_map[vid.strip()] = float(ctr)
            except ValueError:
                console.print(f"[red]✗ Bad --import-ctr pair (want VIDEO_ID=CTR): {pair}[/red]")
        if not ctr_map:
            return
        result = import_manual_ctr(ctr_map, channel=channel)
        console.print(f"[green]OK[/green] CTR import: {result['videos_updated']} videos updated, "
                      f"{result['packaging_entries_with_ctr']} packaging entries carry CTR")
        return
    if add_competitor:
        from pipeline.topic_scout import add_competitor as _add_competitor
        channels = _add_competitor(add_competitor)
        console.print(f"[green]OK[/green] Watch-list ({len(channels)}): {', '.join(channels)}")
        return
    if scout:
        from pipeline.topic_scout import run_scout
        report = run_scout()
        table = Table(title="Topic Scout — competitor outliers", show_lines=False)
        table.add_column("Channel", style="cyan", max_width=40)
        table.add_column("Uploads", justify="right")
        table.add_column("Outliers", justify="right")
        for ch, info in report.get("per_channel", {}).items():
            table.add_row(ch, str(info["uploads"]), str(info["outliers"]))
        for fail in report.get("channels_failed", []):
            table.add_row(fail, "—", "failed")
        console.print(table)
        console.print(f"[green]OK[/green] Queue now holds {report['queue_size']} scored topics — pop one with --next-topic")
        return
    if next_topic_flag:
        from pipeline.topic_scout import next_topic
        entry = next_topic()
        if entry:
            console.print(Panel(
                entry["topic"],
                title=f"Next topic — outlier {entry['outlier_ratio']}× · {entry['views']:,} views",
                style="bold cyan",
            ))
            console.print(f"[dim]Source: {entry.get('title')}\n{entry.get('url')}[/dim]")
            console.print("Run: python main.py \"<topic above>\" --template celebrity_4min --shorts")
        else:
            console.print("[yellow]Queue empty (or everything already produced) — run --scout first.[/yellow]")
        return
    if ab_leaderboard:
        from pipeline.abtest import ab_leaderboard as _leaderboard
        from utils import channel_state
        state = channel_state._load(channel)
        lb = _leaderboard(state, state.get("packaging_log", []))
        console.print_json(json.dumps(lb, indent=2, ensure_ascii=False))
        return

    # ── A9: Thumbnail style studio ──
    if thumbnails_project:
        from pipeline.thumbnail_styles import (recommend_styles, load_user_templates,
                                               seed_example_template, STYLES as _curated)
        from pipeline.thumbnail import render_style_variants
        from PIL import Image
        seed_example_template()
        user = load_user_templates()
        if styles:
            ids = [s.strip() for s in styles.split(",") if s.strip()]
            library = {s["id"]: s for s in (user + list(_curated))}
            chosen = [library[i] for i in ids if i in library]
            missing = [i for i in ids if i not in library]
            if missing:
                console.print(f"[yellow]Unknown style ids: {', '.join(missing)}[/yellow]")
        else:
            chosen = recommend_styles(niche or config.CHANNEL_NICHE)
        if not chosen:
            console.print("[red]✗ No styles resolved — check --styles ids or the template dir.[/red]")
            return
        proj = Path(thumbnails_project)
        # Reuse the project's composited background (v1 already has bg+gradient)
        bg_path = proj / "thumbnail_v1_focal.png"
        if not bg_path.exists():
            console.print("[red]✗ No thumbnail_v1_focal.png in that project.[/red]")
            return
        hook = ""
        try:
            meta = json.loads((proj / "metadata.json").read_text(encoding="utf-8"))
            hook = meta.get("hook_overlay_text", "") + " " + meta.get("title", "")
        except Exception:
            pass
        bg = Image.open(bg_path).convert("RGB")
        rows = render_style_variants(bg, hook.split()[:4] or ["WATCH"], 96,
                                     bg.size[0], bg.size[1], proj,
                                     hook_text=hook, niche=niche or config.CHANNEL_NICHE,
                                     only_styles=[s["id"] for s in chosen])
        console.print(f"[green]OK[/green] Rendered {len(rows)} style variants into {proj.name}:")
        for r in rows:
            console.print(f"  • {r['id']:<16} {r['file']}  [dim]{r.get('approach','')}[/dim]")
        if pick_out:
            first = proj / rows[0]["file"] if rows else None
            if first and first.exists():
                Path(pick_out).parent.mkdir(parents=True, exist_ok=True)
                import shutil
                shutil.copyfile(first, pick_out)
                console.print(f"[green]OK[/green] Picked {rows[0]['id']} → {pick_out}")
        return

    # ── A10: Channel audit / competitor discovery / compare ──
    if audit:
        from pipeline.channel_audit import audit as run_audit, render_audit_report
        report = run_audit()
        console.print(render_audit_report(report))
        return
    if find_competitors:
        from pipeline.channel_audit import suggest_channels
        result = suggest_channels(niche=niche)
        if result["errors"]:
            for e in result["errors"]:
                console.print(f"[yellow]search failed: {e}[/yellow]")
        if not result["candidates"]:
            console.print("[red]✗ No candidate channels found — try a broader --niche.[/red]")
            return
        table = Table(title=f"Competitor candidates — '{result['niche']}'")
        table.add_column("Channel", style="bold")
        table.add_column("Subs", justify="right")
        table.add_column("URL", overflow="fold")
        for c in result["candidates"]:
            table.add_row(c["name"], f"{c['subscribers']:,}", c["url"])
        console.print(table)
        console.print("[dim]Add one: python main.py --add-competitor <url>[/dim]")
        return
    if compare_flag:
        from pipeline.channel_audit import compare_channels
        result = compare_channels()
        table = Table(title="My channel vs watch-list")
        for col in ("Channel", "Subs", "Videos", "Views/video", "Views/sub"):
            table.add_column(col)
        for r in result["rows"]:
            if r.get("error"):
                table.add_row(r["name"], "—", "—", "—", f"[red]{r['error'][:40]}[/red]")
                continue
            table.add_row(
                ("[bold green]" if r.get("is_me") else "") + r["name"],
                f"{r.get('subscribers', 0):,}",
                str(r.get("video_count", "—")),
                f"{r.get('views_per_video', '—'):,}" if isinstance(r.get("views_per_video"), int) else "—",
                str(r.get("views_per_sub", "—")),
            )
        console.print(table)
        return
    if channel_memory_flag:
        from pipeline.channel_memory import build_memory, render_report
        mem_channel = (channel if channel in (config.CHANNEL_FAMEFILES,
                                              config.CHANNEL_RISEANDRUIN)
                       else config.CHANNEL_FAMEFILES)
        memory = build_memory(channel=mem_channel)
        console.print(render_report(memory))
        return
    if ab_video_id:
        from pipeline.abtest import ab_set
        if not ab_project or (title_index is None and not thumb_file):
            console.print("[red]✗ --ab-apply needs --project <dir> and --title-index and/or --thumb <file>.[/red]")
            return
        result = ab_set(ab_video_id, Path(ab_project), title_index=title_index,
                        thumb_file=thumb_file, channel=channel)
        if result["ok"]:
            console.print(f"[green]OK[/green] A/B swap applied: {'; '.join(result['changes'])}")
        else:
            console.print(f"[red]✗ A/B swap failed:[/red] {'; '.join(result['changes'])}")
        return

    if batch:
        # Batch mode: read topics from file
        topics = Path(batch).read_text(encoding="utf-8").strip().splitlines()
        topics = [t.strip() for t in topics if t.strip()]
        console.print(f"[bold]Batch mode: {len(topics)} topics[/bold]\n")
        for i, t in enumerate(topics):
            console.print(f"\n[bold cyan]═══ Video {i + 1}/{len(topics)} ═══[/bold cyan]")
            try:
                asyncio.run(run_pipeline(
                    topic=t, template_name=template, tts_provider=tts,
                    render_mode=mode, generate_shorts=shorts,
                    review=False, auto=True, upload=upload, privacy=privacy,
                    force_upload=force_upload, push_resolve=push_resolve,
                    resolve_render=resolve_render, push_no_auto=no_auto_resolve,
                    channel=channel,
                ))
            except Exception as e:
                console.print(f"[bold red]✗ Failed:[/bold red] {e}")
                continue
    elif topic:
        asyncio.run(run_pipeline(
            topic=topic, template_name=template, tts_provider=tts,
            render_mode=mode, generate_shorts=shorts,
            review=review, auto=auto, upload=upload, privacy=privacy,
            force_upload=force_upload, push_resolve=push_resolve,
            resolve_render=resolve_render, push_no_auto=no_auto_resolve,
            channel=channel,
            resume_dir=Path(resume_dir) if resume_dir else None,
        ))
    elif resume_dir:
        # --resume without a topic: load the topic from the saved project
        import json as _json
        proj_file = Path(resume_dir) / "project.json"
        saved_topic = ""
        if proj_file.exists():
            try:
                saved_topic = _json.loads(proj_file.read_text(encoding="utf-8")).get("topic", "")
            except Exception:
                pass
        if not saved_topic:
            # Interrupted runs die before project.json is written; research.json
            # (saved at the very first stage) always carries the topic.
            research_file = Path(resume_dir) / "research.json"
            if research_file.exists():
                try:
                    saved_topic = _json.loads(research_file.read_text(encoding="utf-8")).get("topic", "")
                except Exception:
                    pass
        if not saved_topic:
            console.print("[red]✗ --resume could not determine the topic (no project.json with a topic). Pass TOPIC explicitly.[/red]")
            sys.exit(1)
        asyncio.run(run_pipeline(
            topic=saved_topic, template_name=template, tts_provider=tts,
            render_mode=mode, generate_shorts=shorts,
            review=False, auto=True, upload=upload, privacy=privacy,
            force_upload=force_upload, push_resolve=push_resolve,
            resolve_render=resolve_render, push_no_auto=no_auto_resolve,
            channel=channel,
            resume_dir=Path(resume_dir),
        ))
    else:
        console.print("[red]Error: Provide a topic or use --batch[/red]")
        console.print("Usage: python main.py \"Your Topic Here\" --template celebrity")
        sys.exit(1)


if __name__ == "__main__":
    main()
