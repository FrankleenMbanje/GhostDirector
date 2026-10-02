# GhostDirector / The Fame Files — Architecture (as inspected 2026-09-25)

Verified against the live codebase. This file is the map; behavior lives in the modules.

## Entry points
- `main.py` — click CLI. `python main.py "TOPIC" --template celebrity_8min [--shorts] [--auto] [--upload] [--privacy public] [--channel famefiles]`; `--trending` (Fame Files daily short, auto-picks top story, uploads via `pipeline/trending_short.run_cli`); `--trending-list`; `--resume <dir>`; `--audit`, `--find-competitors`, `--sync-analytics`, `--ab-apply`, `--thumbnails`.
- `pipeline/trending_short.py` — the Fame Files production lane: story pick → script → quality gate → **FIX-060 word-budget enforcement** → voices → assets → `assemble_video` → QC gate (with repair hook) → thumbnail/metadata/compliance → upload (privacy from CLI, default public — operator order 2026-10-02; QC + compliance gates remain the safety net).
- `pipeline/shorts.py` — 9:16 companion slicing for long-form projects.

## Production flow (main.py)
research (`pipeline/researcher.py`) → script (`pipeline/scriptwriter.py`, FIX-047 word budget, humanize pass, chain `config.gemini_candidates()`) → voices (`pipeline/voice.py`, edge-tts default) → timestamps (`pipeline/timestamps.py`, Whisper) → assets (`pipeline/assets.py`) → assembly (`pipeline/assembler_ffmpeg.py`) → QC gate (`pipeline/visual_director.qc_gate`) → metadata (`pipeline/metadata.py`) → compliance (`pipeline/compliance.py`) → upload (`pipeline/uploader.py`, per-channel tokens via `config.youtube_token_path(channel)`).

## Where the fixes live
- **FIX-056** visual quality: `pipeline/visual_director.py` (candidate pools, pixel inspection, saliency-aimed crops, final QC).
- **FIX-057** strict QC + repair: `visual_director.py` (`frame_information_score`, per-scene guaranteed sampling, fallback ledger `scenes/scene_fallback.json`, `repair_scene_assets` in `assets.py`), wired via `qc_gate(repair=...)` from `main.py`/`trending_short.py`.
- **FIX-058** editorial + hard QC gate: `visual_director.py` (`scan_flat_frames` full-decode ≥0.25s runs → CRITICAL; `HOOK_CARD_GRACE_S`; `CRITICAL_PATTERNS`), `assembler_ffmpeg.py` (`resolve_music_mood`, SFX whoosh budget `transition_sfx: restrained`), `utils/caption_renderer.py` (`_editorial_news_style`), `templates/*_trending.json`, `assets.py` (`_gemini_illustration` + `config.GEMINI_IMAGE_CANDIDATES` chain).
- **FIX-059** edit language: `assembler_ffmpeg.py` (`_audio_leads_by` EOF-anchored silence probe, `_cut_styles` motivated chooser, amix `leads` delays, caption/SFX lead compensation, timeline `cut_styles`/`pre_lap_seconds`), `assets.py` (variety ledger `scene_fingerprint.json`, `_image_dhash`, `_rotate_person_queries`, registration at 9 acceptance points), `visual_director.py` (person-presence WARNING).
- **FIX-060** shorts duration: `scriptwriter.enforce_shorts_word_budget` (55s deterministic trim, hook+finale protected), wired in `trending_short.py` before voices; QC/compliance still verify the final file.

## Config / env
- `config.py` — all constants; `.env` via python-dotenv. Keys: `GEMINI_API_KEY`, `PEXELS_API_KEY`, `PIXABAY_API_KEY` (unset), `ELEVENLABS_API_KEY`. `GEMINI_IMAGE_CANDIDATES` chain for image gen. Channels: `CHANNEL_FAMEFILES`, `CHANNEL_RISEANDRUIN`; token files `youtube_token.<channel>.json` (secrets on disk — git-ignored).
- Templates: `templates/*.json` with `_base.json` deep-merge (`config.load_template`).

## State (pre-database)
- `db/*.json` — analytics, channel profiles/state, competitors, topic queue, trending news cache, used-broll ledger.
- Per-project `output/<project>/` — `script.json`, `research.json`, `timeline.json` (scene starts, music, cut styles), `scenes/scene_fallback.json`, `scene_fingerprint.json`, `final_16x9.mp4` / `final_9x16_short.mp4`, `final_qc_report.json`, `compliance_report.json`, `edit_media/`, `thumbnail*.png`.
- Permanent ban ledgers: `output/banned_media.json`, `output/banned_domains.json`.
- There is **no scheduler** and **no run lock**; duplication is prevented only by humans.

## Tests
`test_phase1_units.py` … `test_phase5_units.py` — 271 checks total (56/27/47/71/70). Phase 1 includes the FIX-059 pre-lap + variety sections.

## Known constraints (2026-09-25)
- Gemini daily quotas degrade on busy days: chain walker degrades to `gemini-flash-lite-latest`; humanize/expansion passes may ship short drafts with warnings.
- Long-form scripts may render under target when quota dies (444-word/44-scene case) — accepted, logged.
- `UPLC` ops: detached scripts calling `assemble_video` MUST keep the `if __name__ == "__main__"` guard (FIX-026 process pool re-imports `__main__` on Windows).
