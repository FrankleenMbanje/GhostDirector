"""Render cached media to a new review folder. No research, TTS, upload, or API calls."""
import argparse
import os
import sys
import shutil
from pathlib import Path

os.environ["GEMINI_API_KEY"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from models import Script
from pipeline.assembler_ffmpeg import assemble_video
from pipeline.visual_director import final_qc, classify_qc


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--template", default="famefiles_trending")
    parser.add_argument("--reuse-prepared", type=Path)
    args = parser.parse_args()
    source = args.project.resolve()
    dest = args.destination.resolve()
    if source == dest or dest.exists():
        raise SystemExit("Proof destination must be a new directory distinct from the original")
    script = Script.load(source / "script.json")
    for sc in script.scenes:
        for name in ("audio_path", "video_path", "photo_path", "broll_video_path"):
            value = getattr(sc, name)
            if value and not Path(value).exists():
                raise SystemExit(f"Missing cached {name} in scene {sc.scene_number}: {value}")
    dest.mkdir(parents=True)
    if args.reuse_prepared:
        from pipeline.assembler_ffmpeg import PREPARED_V
        cached = args.reuse_prepared.resolve() / "edit_media"
        (dest / "_temp").mkdir()
        for p in cached.glob(f"*.{PREPARED_V}.mp4"):
            shutil.copy2(p, dest / "_temp" / p.name)
    script.save(dest / "script.json")
    template = config.load_template(args.template)
    video = assemble_video(script, template, dest)
    resolution = tuple(map(int, template["visuals"]["resolution"].split("x")))
    ok, issues = final_qc(video, expected_size=resolution, report_dir=dest,
                          project_dir=dest, deep_review=False)
    critical, _ = classify_qc(issues)
    print("Proof:", video)
    print("QC:", "PASS" if not critical else "FAIL", issues)
    raise SystemExit(1 if critical else 0)
