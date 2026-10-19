# The Fame Files: local daily studio

The production program runs on this PC without Codex and does not call a paid AI or image-generation API. It discovers current celebrity stories online, researches one in depth, prepares a long-form edit and produces three thumbnail options. Uploads remain disabled.

## Use

- Open `Open_Local_Studio.cmd`, then visit http://127.0.0.1:9042 . Keep that launcher window open while using the dashboard. The Windows daily task runs independently of the dashboard.
- Double-click `run_today.cmd` to run production manually.
- Read `output/local-studio/status.json` or the dashboard for the actual stage and any reason a run stopped.
- Completed projects appear under `output/local-studio/runs/YYYY-MM-DD/`. The video, three thumbnails, captions, narration, rendered timeline clips, sources, licence ledger and quality report stay together.

## Daily start

The Windows task is `The Fame Files - Local Daily 14`. Its trigger is **14:00 Africa/Johannesburg**, every day. The registered mode requires this user to be signed in. Keep the PC powered and connected to the internet. Wake-to-run and missed-start catch-up are requested; Windows power settings determine whether sleep can be interrupted. A shut-down PC cannot produce a video.

The task runs `venv/Scripts/pythonw.exe -m local_studio --daily` from this repository. It does not need Codex, an open browser or Resolve to produce the review MP4. A file lock prevents overlapping jobs, and a completion receipt prevents a second video for the same date. If a completion receipt points to a missing video, the system flags that inconsistency instead of falsely reporting success.

`scripts/schedule_daily.ps1` can register the task again. If Windows refuses both supported task modes, it has a normal per-user Startup shortcut fallback. See the actual installed mode in `output/local-studio/schedule.json`; do not infer it from this fallback description.

## First setup and current readiness

The local Qwen3-VL 4B model is approximately 3.3GB. The first run finishes downloading it before research starts. Subsequent runs reuse the downloaded model. Installation can take time on this connection; source/render tests passing do not mean the model or a complete daily video has passed.

Run `venv/Scripts/python.exe -m local_studio --doctor` to see readiness. The first real end-to-end run still has to pass live research, visual screening, script, narration and final render gates. Do not call the system fully production-tested based only on the unit tests or design previews.

## What the production gates enforce

- Fresh dated discovery, followed by at least six readable articles across three news domains. At least twenty distinct facts require exact supporting evidence, with the supported facts actually drawing on multiple sources.
- An original six-chapter script with short factual graphic copy. Title, thumbnail implications and narration receive automated evidence checks. Weak research triggers another candidate, not invented detail.
- At least three independent moving-video originals, including at least two genuine celebrity video originals. Different cuts or filenames from one interview do not count as different originals. Clean real photographs supplement the edit.
- No original source exceeds 30% of screen time. Video is at least 55%; graphic-only material is at most 20%; stock footage is excluded. Source excerpts do not overlap or loop. Archive and context labels prevent presenting illustrative material as the current event.
- Commons sources must have a supported reuse licence and no detected dispute/deletion/watermark flags. Local vision checks screen identity, framing and publisher marks. Marks are not erased or cropped away. Automated screening can miss problems; inspect the local review before publishing.
- Stock footage acquisition is removed. The edit uses genuine licensed subject material and original graphics.
- Narration is the same Andrew voice used in the approved sample, via keyless online Edge TTS. Real decoded audio duration must be **300 seconds or more**. Short scripts are rejected; no silent padding or artificial stretching makes them pass.
- The actual exported MP4 is decoded and checked. Blank-picture checks run before captions and again on the final encode. Captions are supplied separately as SRT as well as burned into the review video.

A day may finish with a clear blocked report if clean licensed footage, source depth or network access is insufficient. A free system cannot guarantee usable rights-cleared footage for every trending celebrity. These gates deliberately preserve the visual standard rather than manufacture a weak five-minute video.

## Free graphics and thumbnails

The map, typography and factual cards in the approved sample were code-rendered graphics; paid image generation was not needed. The reusable production template is in `local_studio/motion`, using the same charcoal, warm-white and gold design language. Real photographs remain sourced media, not generated celebrity likenesses.

`local_studio/thumbnail.py` creates three 1280x720 options and phone-size previews using Pillow and genuine licensed photographs. It applies the researched Canvas Design and YouTube thumbnail brief principles. `docs/THUMBNAIL_DESIGN.md` records the design direction. No CTR result is claimed before a real audience test.

Remotion's current free licence covers individuals and teams of up to three, including automation: https://www.remotion.dev/docs/license/pricing and https://www.remotion.dev/docs/license/faq . Qwen3-VL 4B is Apache-2.0: https://ollama.com/library/qwen3-vl:4b . Pexels content and API access are free under their rules: https://www.pexels.com/license/ and https://www.pexels.com/api/documentation/ . Edge TTS uses an online service without a paid API key and can be unavailable: https://github.com/rany2/edge-tts . Internet, electricity and storage are still required.

An optional offline Kokoro voice path is included for future use but is not the default or a readiness requirement. Its separate Python 3.13 dependencies are in `requirements-offline-voice.txt`; optional partial downloads are not counted as installed models.

## Revision in Resolve

Each successful project retains `timeline_media/` numbered in edit order, `edit_plan.json`, `narration.wav` and `captions.srt`. These are available for revisions in Resolve. The automation does not require Resolve Studio's external scripting features, and it does not claim that a native DRP or editable Fusion composition has been generated unless a separate verified import actually ran.

## Stop or remove

Disable the named Windows task in Task Scheduler to stop recurring production. `scripts/schedule_daily.ps1 -Remove` removes that task. If the installed mode is the Startup fallback, also remove its named shortcut from the user's Startup folder and close its scheduler process. Existing outputs are preserved. Do not re-enable the retired GitHub upload workflows.
