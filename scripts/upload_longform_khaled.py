"""
Targeted upload repair (FIX-068): upload the already-rendered, QC-passed
long-form doc whose upload was skipped by the FIX-054 title-collision guard.

The 09:36 trending run packaged the doc with the story HEADLINE — the exact
title the companion short already shipped under — so the uploader's
idempotency check "found the video on the channel" and returned the SHORT's
video ID without uploading anything. This script:
  1. regenerates metadata with the SCRIPT's documentary title (longform=True),
  2. uploads final_16x9.mp4 + thumbnail as UNLISTED via upload_video,
  3. records the upload on the doc's own video_projects row
     (record_upload keyed by project_name) and in the story ledger.

Run:  venv/Scripts/python.exe scripts/upload_longform_khaled.py
NOTE (FIX-058): __main__ guard — multiprocessing children re-import this.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config

PROJECT = Path(config.OUTPUT_DIR) / (
    "dj-khaled-omits-drake-from-top-5-after-rappers-mak_20260927_012125"
)


def main() -> int:
    from models import Script
    from pipeline import trending_short
    from pipeline.trending_short import _override_metadata
    from pipeline.metadata import generate_metadata
    from pipeline.uploader import upload_video
    from pipeline.trending_news import record_run

    script = Script.load(PROJECT / "script.json")
    template = config.load_template(trending_short.LONGFORM_TEMPLATE_NAME)

    # Step 1: correct metadata — documentary title, not the story headline.
    meta = generate_metadata(script, template, PROJECT)
    meta = _override_metadata(meta, script, {}, channel=config.CHANNEL_FAMEFILES,
                              longform=True)
    (PROJECT / "metadata.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Title: {meta['title']}")

    # Step 2: upload the finished, QC-passed render.
    video_path = PROJECT / "final_16x9.mp4"
    thumb_path = PROJECT / "thumbnail.png"
    assert video_path.exists(), f"missing {video_path}"
    video_id = upload_video(
        project_dir=PROJECT,
        video_path=video_path,
        thumbnail_path=thumb_path if thumb_path.exists() else None,
        privacy_status="unlisted",
        channel=config.CHANNEL_FAMEFILES,
    )
    print(f"Uploaded: https://youtu.be/{video_id} (unlisted)")

    # Step 3: bookkeeping — uploads row keyed to THIS project + ledger.
    try:
        from storage.production_state import RunRecorder
        rec = RunRecorder(schedule_name="manual-repair", command="upload repair",
                          channel=config.CHANNEL_FAMEFILES, fmt="long")
        rec.video(PROJECT.name, project_dir=str(PROJECT), fmt="long",
                  visibility="unlisted")
        rec.upload(video_id, f"https://youtu.be/{video_id}", "unlisted")
    except Exception as e:
        print(f"DB bookkeeping warning: {str(e)[:140]}")
    try:
        record_run({"title": meta["title"]}, str(PROJECT), video_id,
                   config.CHANNEL_FAMEFILES)
    except Exception as e:
        print(f"Ledger warning: {str(e)[:140]}")

    print("OK: long-form uploaded unlisted and recorded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
