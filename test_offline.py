import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from models import Script, Scene, VideoProject
import config
from pipeline.voice import generate_voices
from pipeline.timestamps import generate_timestamps
from pipeline.assembler_ffmpeg import assemble_video

async def run_offline_test():
    print("Starting Offline End-to-End Test...")
    
    # 1. Create a dummy script
    scene1 = Scene(
        scene_number=1,
        narration="This is the first scene. We are testing the amazing Ghost Director pipeline.",
        visual_prompt="None",
        visual_type="stock_video",
        mood="dramatic",
        duration_target_seconds=6.0,
        video_path=str(Path("assets/test/bunny.mp4").resolve())
    )
    
    scene2 = Scene(
        scene_number=2,
        narration="Here is a beautiful photo of the Taj Mahal. Notice the cinematic Ken Burns effect and the pop-in captions.",
        visual_prompt="None",
        visual_type="stock_photo",
        mood="dramatic",
        duration_target_seconds=8.0,
        photo_path=str(Path("assets/test/taj.jpg").resolve())
    )
    
    script = Script(
        title="Offline Test Video",
        description="A test video generated entirely offline without API keys.",
        tags=["test", "offline"],
        hook="This is the first scene.",
        scenes=[scene1, scene2],
        total_scenes=2,
        estimated_duration_minutes=0.25
    )
    
    # 2. Setup output directory
    output_dir = Path("output/offline_test").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Load template (Shorts format for max retention pop-in captions)
    template = config.load_template("short_hook")
    
    # 3. Voice Generation (Edge-TTS is offline/free)
    print("\n--- Generating Voices ---")
    script = await generate_voices(script, template, output_dir)
    
    # 4. Word Timestamps (Whisper is local)
    print("\n--- Generating Timestamps ---")
    script = generate_timestamps(script, output_dir)
    
    # 5. FFmpeg Assembly (Local)
    print("\n--- Assembling Video ---")
    final_video = assemble_video(script, template, output_dir)

    # 6. DaVinci Resolve export bundle + compliance gate (same as main.py)
    print("\n--- Resolve Export + Compliance ---")
    try:
        from pipeline.assembler_resolve import export_resolve_bundle
        export_resolve_bundle(script, template, output_dir)
    except Exception as e:
        print(f"Resolve export failed: {e}")
    try:
        from pipeline.metadata import generate_metadata
        from pipeline.compliance import check_compliance
        generate_metadata(script, template, output_dir)
        report = check_compliance(script, {}, output_dir)
        print(f"Compliance verdict: {report['verdict'].upper()}")
    except Exception as e:
        print(f"Compliance check failed: {e}")

    print(f"\nSUCCESS! Test video generated at: {final_video}")

if __name__ == "__main__":
    asyncio.run(run_offline_test())
