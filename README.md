# GhostDirector — The Fame Files production system

An automated celebrity-news video factory: it picks today's trending story,
researches it, writes the script, sources and edits real visuals with
editorial judgment, renders long-form documentaries and 9:16 Shorts, gates
them through strict final-render QC + compliance, and uploads them
**unlisted** for human review before anything goes public.

Born as a personal automation project, hardened through 60+ production
fixes (FIX-001…FIX-060, see `UPGRADE_PLAN.md`) into a persistent,
recoverable, daily production system.

---

## What it does

1. **Trend selection** — sweeps DuckDuckGo News + Google News for fresh
   celebrity stories, scores them, and picks the top candidate
   (`--trending-list` shows today's board).
2. **Research** — Gemini-powered multi-source research with fact anchoring.
3. **Script** — template-driven scriptwriting (long-form documentary and
   Shorts personas), quality gate with one rewrite pass, and a
   **deterministic Shorts word-budget enforcer** (FIX-060) so a Short can
   never render past the 60s platform limit.
4. **Visuals** — candidate pools from Pexels / DuckDuckGo / public-domain
   archive / YouTube excerpts, pixel-inspected (sharpness, exposure,
   letterbox), ranked by Gemini vision against the scene's narration, with
   saliency-aimed crops. A per-project variety ledger (perceptual dhash)
   blocks near-duplicate celebrity portraits (FIX-059).
5. **Edit** — punch-cuts on sentence boundaries, camera moves that never
   repeat adjacently, motivated hard cuts, dissolves as accents, and
   **J-cut pre-laps** where the next scene's first words breathe over the
   previous shot (silence-probe gated so speech can never clip) (FIX-059).
6. **Music** — story-aware mood resolution: tragedy never rides an upbeat
   bed; legal thrillers get subtle tension; announcements get confident
   cinematic energy (FIX-058/061). Two-act bed structure, ducked under the
   voice, sparse SFX with a whoosh budget.
7. **QC hard gate** — the **exported file** is fully decoded: every frame
   probed for flat/blank runs ≥0.25s (CRITICAL), per-scene guaranteed
   sampling, frozen/black frame detection, loudness, Gemini editorial
   review of sampled frames, Shorts duration check on the final encode.
   Failures trigger the automatic repair loop and re-QC (FIX-056…058).
8. **Compliance** — YouTube policy checks (duration, disclosure, credits,
   spam patterns). A compliance FAIL blocks upload. Everything uploads
   **unlisted by default** — the operator reviews and publishes.
9. **Persistence** — PostgreSQL (production) tracks stories, videos,
   scenes, assets, QC results, uploads, runs, and the schedule
   (Phase 7+). SQLite mirrors the schema for local dev/tests.
10. **Daily automation** — 10:00 Africa/Harare, idempotent and
    restart-safe: the database run-lock guarantees one production per day
    and recovers crashed runs instead of duplicating them (Phase 9/10).

## Repository layout

```
main.py                  CLI entry point (production, ops, growth, thumbnails)
config.py                Configuration; env-driven paths + API keys
models.py                Script/Scene dataclasses
pipeline/
  researcher.py          multi-source research
  scriptwriter.py        script generation + validation + FIX-060 enforcer
  quality_gate.py        editorial score + rewrite pass
  voice.py, timestamps.py  TTS + word-level timing
  assets.py              candidate sourcing/inspection/selection, variety ledger,
                         fallback-prevention cascade, repair_scene_assets
  assembler_ffmpeg.py    the edit: cuts, J/L pre-laps, transitions, captions,
                         music resolution, SFX restraint, mix, timeline truth
  visual_director.py     frame inspection, final_qc hard gate, flat-frame scan,
                         qc_gate repair loop
  trending_short.py      the Fame Files daily production lane
  metadata.py, compliance.py, uploader.py, thumbnail.py
  abtest.py, analytics.py, channel_audit.py, topic_scout.py
storage/
  database.py            SQLAlchemy schema + run lock + persistence helpers
  production_state.py    best-effort run/video recorders (never fatal)
  scheduler.py           daily 10:00 Africa/Harare scheduler (idempotent)
migrations/              Alembic; initial schema in versions/
templates/               per-channel/per-format template configs
scripts/schedule_daily.ps1  Windows Task Scheduler registration
tests: test_phase1_units.py … test_phase6_units.py  (306 checks)
```

## Setup

```powershell
py -3.14 -m venv venv
venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env   # then fill in your keys (never commit .env)
```

Required environment (see `.env.example`):

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL URL — production state source of truth |
| `GEMINI_API_KEY` | research/scripts/visual selection/image gen |
| `PEXELS_API_KEY` | stock footage + photos |
| `PIXABAY_API_KEY` | optional secondary stock source |
| `ELEVENLABS_API_KEY` | optional premium TTS |
| `GD_TIMEZONE` / `GD_DAILY_HOUR` | scheduler overrides (default Africa/Harare 10:00) |
| `GD_OUTPUT_DIR` | machine-specific output location override |

YouTube upload uses OAuth: the first upload per channel opens a browser
consent and stores `youtube_token.<channel>.json` (git-ignored).

### Database

Production: create a PostgreSQL database and set `DATABASE_URL` (never a
committed value). Then either let the runtime bootstrap the schema
(`storage.database.init_db()` runs on first use) or manage it explicitly:

```bash
venv/Scripts/python -m alembic upgrade head
```

Local dev/tests need no server — the layer transparently falls back to a
SQLite mirror (or honors a `sqlite:///...` DATABASE_URL).

## Daily automation

```powershell
# One-time, elevated PowerShell (registers Windows Task Scheduler, 10:00 daily):
powershell -ExecutionPolicy Bypass -File scripts\schedule_daily.ps1
```

Or run the scheduler as a persistent process:
`venv\Scripts\python main.py --schedule-loop`.

The run is **idempotent**: if today's video already shipped (or a run is
active), the scheduler refuses to duplicate; crashed runs are reclaimed and
retried via the run lock.

## Operating

```bash
# Today's trending board
venv/Scripts/python main.py --trending-list

# Manual daily production (same path the scheduler uses)
venv/Scripts/python main.py --daily

# A specific story as a Short, unlisted
venv/Scripts/python main.py --trending --trending-story "HEADLINE" --channel famefiles --privacy unlisted

# Long-form documentary (8-min template), unlisted
venv/Scripts/python main.py "TOPIC" --template celebrity_8min --auto --upload --privacy unlisted --channel famefiles

# Resume an interrupted project (skips completed stages)
venv/Scripts/python main.py --trending --resume output/<project-dir>

# Production status from the database
venv/Scripts/python main.py --status

# Tests
for t in 1 2 3 4 5 6; do venv/Scripts/python test_phase${t}_units.py; done
```

## Proof render

A proof render is a real production run inspected at the file level:
`--trending` (or a manual topic) → verify in the project dir:
`final_qc_report.json` verdict, flat-frame scan = 0 runs, duration within
limits, `timeline.json` cut styles (`prelap_hard_cut` / `hard_cut` /
`xfade`), music mood, then the DB rows (`--status`).

## Recovery / resume

- Crashed mid-run? Re-run the same command — research/script/assets are
  cached per project and `--resume <dir>` skips completed stages.
- The run lock expires after 90 minutes without a heartbeat; the next run
  reclaims it (retry counter incremented, error preserved).
- Every upload is unlisted; QC/compliance failures never block human
  review, only public publishing.

## Git workflow

`main` is production; develop on `development` or feature branches, run
the full suite, proof-render, then merge. GitHub is the source of truth
for **code**; the database is the source of truth for **production
state**. Secrets live only in `.env` / token files (all git-ignored).

## Known limitations

- Gemini daily quotas can degrade long-form script expansion on heavy
  days; the chain walker degrades gracefully and logs it.
- Pre-laps shift audio only (video cuts at scene boundaries) — true
  picture pre-laps are a documented next pass.
- Pexels is the only configured stock API when `PIXABAY_API_KEY` is unset.
