"""Regression checks for production failures observed in Actions run 38576021057."""
import asyncio
import json
import tempfile
import subprocess
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch, MagicMock

from models import Scene, Script
from pipeline import analytics, voice, diagnose
from pipeline.assembler_ffmpeg import _sentence_groups, _compute_cut_points
from pipeline import assembler_ffmpeg
from pipeline.trending_short import fit_short_to_measured_voice
from pipeline.visual_director import classify_qc


def script_for(durations):
    return Script(title="Test", description="", tags=[], hook="Hook", scenes=[
        Scene(scene_number=i + 1, narration=f"Complete beat {i}.", visual_prompt="test",
              visual_type="stock_photo", mood="neutral", audio_duration_seconds=d,
              duration_target_seconds=d) for i, d in enumerate(durations)],
        total_scenes=len(durations), estimated_duration_minutes=1)


class ProductionHealthTests(unittest.TestCase):
    def test_mixed_24fps_footage_and_stills_preserve_duration(self):
        from utils.ffmpeg_cmd import cut_video_segment, photo_to_video, get_duration
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw, still, footage, joined = [root / n for n in ("raw.mp4", "still.mp4", "footage.mp4", "joined.mp4")]
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                            "testsrc2=size=320x180:rate=24000/1001:duration=6",
                            "-c:v", "libx264", str(raw)], check=True, capture_output=True)
            with patch("config.GEMINI_API_KEY", ""):
                photo_to_video(Path("assets/test/taj.jpg").resolve(), still, 2, 320, 180)
                cut_video_segment(raw, footage, .5, 3, 320, 180)
            def track(path):
                probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
                                       check=True, capture_output=True, text=True)
                return json.loads(probe.stdout)["streams"][0]
            self.assertEqual(track(still)["time_base"], track(footage)["time_base"])
            self.assertEqual(track(still)["r_frame_rate"], track(footage)["r_frame_rate"])
            listing = root / "concat.txt"
            listing.write_text(f"file '{still.as_posix()}'\nfile '{footage.as_posix()}'\n")
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                            "-c:v", "libx264", str(joined)], check=True, capture_output=True)
            self.assertAlmostEqual(get_duration(joined), 5, delta=.15)

    def test_parallel_jobs_keep_paired_footage(self):
        script = script_for([10])
        script.scenes[0].broll_video_path = "real-interview.mp4"
        captured = []
        def prepare(job):
            captured.append(job[0])
            return 1, "prepared.mp4", True, []
        with patch.object(assembler_ffmpeg, "ProcessPoolExecutor", side_effect=RuntimeError("serial fixture")), \
             patch.object(assembler_ffmpeg, "_prepare_scene_entry", side_effect=prepare):
            assembler_ffmpeg._prepare_all_scenes_parallel(script, {}, Path("output"))
        self.assertEqual(captured[0]["broll_video_path"], "real-interview.mp4")

    def test_sentence_groups_remain_contiguous(self):
        words = "A short sentence. This sentence has many more words than all the others. Tiny. Last sentence ends here.".split()
        stamps = [{"word": w, "start": i, "end": i + .8} for i, w in enumerate(words)]
        groups = _sentence_groups(stamps, 3)
        self.assertEqual([w for g in groups for w in g], stamps)
        self.assertTrue(all(a[-1]["end"] <= b[0]["start"] for a, b in zip(groups, groups[1:])))

    def test_missing_timestamps_cannot_disable_cadence(self):
        for limit in (4, 6):
            cuts, count = _compute_cut_points([], 23, max_shot=limit)
            self.assertEqual((cuts[0], cuts[-1]), (0, 23))
            self.assertEqual(count, len(cuts) - 1)
            self.assertLessEqual(max(b - a for a, b in zip(cuts, cuts[1:])), limit + .001)

    def test_measured_slow_voice_fits_without_cutting_words(self):
        s = script_for([6, 12, 12, 12, 12, 12])
        dropped = fit_short_to_measured_voice(s)
        self.assertEqual(dropped, [5])
        self.assertEqual([sc.scene_number for sc in s.scenes], [1, 2, 3, 4, 6])
        self.assertEqual(s.scenes[-1].narration, "Complete beat 5.")
        self.assertEqual(sum(sc.audio_duration_seconds for sc in s.scenes), 54)

    def test_oversized_hook_and_ending_fail_before_render(self):
        with self.assertRaises(ValueError):
            fit_short_to_measured_voice(script_for([35, 30]))

    def test_replays_and_small_percentage_tail_preserved(self):
        curve = analytics.relative_retention_curve([
            {"elapsedVideoTimeRatio": 0, "viewPercentRemaining": .8},
            {"elapsedVideoTimeRatio": .5, "viewPercentRemaining": 2.2}])
        self.assertEqual([p["remaining_percent"] for p in curve], [80, 220])
        self.assertEqual(analytics.relative_retention_curve([
            {"elapsedVideoTimeRatio": 1, "viewPercentRemaining": .8}], "percent")[0]["remaining_percent"], .8)

    def test_full_youtube_urls(self):
        for url in ("https://youtube.com/watch?v=abc123&feature=share",
                    "https://youtu.be/abc123?t=10", "https://youtube.com/shorts/abc123"):
            self.assertEqual(analytics._video_id_from(url), "abc123")

    def test_lifetime_views_do_not_fake_eligibility(self):
        gap = diagnose.monetization_gap({"subscribers": 70, "total_views": 20_000_000}, 3000)
        self.assertIsNone(gap["views_to_go"])
        self.assertIsNone(gap["eta_days"])
        self.assertFalse(gap["on_track"])
        self.assertIsNone(gap["watch_hours_to_go"])

    def test_dated_earn_snapshot_keeps_rounded_views_inexact(self):
        gap = diagnose.monetization_gap({"subscribers": 74, "earn_snapshot": {
            "as_of": "2026-10-10", "valid_public_watch_hours": 10,
            "valid_public_shorts_views": None, "shorts_views_display": "22K"}}, 3000)
        self.assertEqual(gap["watch_hours_to_go"], 3990)
        self.assertEqual(gap["earn_as_of"], "2026-10-10")
        self.assertIsNone(gap["views_to_go"])

    def test_growth_separates_content_and_dates(self):
        yt, yta = MagicMock(), MagicMock()
        yt.channels().list().execute.return_value = {"items": [
            {"id": "channel", "snippet": {"title": "Fame Files"}, "statistics": {"subscriberCount": "70"}}]}
        response = {"columnHeaders": [{"name": x} for x in
                    ("creatorContentType", "views", "engagedViews", "estimatedMinutesWatched")],
                    "rows": [["shorts", 1000, 700, 60], ["videoOnDemand", 100, 90, 120]]}
        yta.reports().query().execute.side_effect = [response, response,
            {"columnHeaders": [{"name": "subscribersGained"}, {"name": "subscribersLost"}], "rows": [[8, 3]]}]
        result = analytics.channel_growth_metrics(yt, yta, date(2026, 10, 15))
        self.assertEqual(result["window_90d"]["start"], "2026-07-18")
        self.assertEqual(result["window_365d"]["content_types"]["videoOnDemand"]["watch_hours"], 2)
        self.assertEqual(result["window_365d"]["content_types"]["shorts"]["watch_hours"], 1)
        self.assertEqual(result["subscribers_net_28d"], 5)
        self.assertIsNone(result["valid_public_watch_hours"])

    def test_sync_preserves_manual_ctr_and_dry_run(self):
        initial = {"videos": {"id": {"ctr_percent": 7.5, "ctr_source": "studio_manual"}},
                   "channels": {"famefiles": {"earn_snapshot": {"as_of": "2026-10-10"}}}}
        with patch.object(analytics, "load_store", return_value=initial), \
             patch("pipeline.uploader._yt_service_with_scopes"), \
             patch.object(analytics, "_yt_analytics_service"), \
             patch.object(analytics, "channel_growth_metrics", return_value={}), \
             patch.object(analytics, "_list_my_uploads", return_value=[{"video_id": "id", "title": "Test", "published_at": "2026-10-14"}]), \
             patch.object(analytics, "_video_metrics", return_value={"views": 10}), \
             patch.object(analytics, "_video_length_seconds", return_value=(100, "Test")), \
             patch.object(analytics, "_find_project_for_title", return_value=None), \
             patch.object(analytics, "save_store") as save, \
             patch("utils.channel_state._load", return_value={"packaging_log": [{"title_used": "Test"}]}), \
             patch("utils.channel_state._save") as state_save:
            analytics.sync_channel("famefiles", write=False)
        self.assertEqual(initial["videos"]["id"]["ctr_percent"], 7.5)
        self.assertEqual(initial["channels"]["famefiles"]["earn_snapshot"]["as_of"], "2026-10-10")
        save.assert_not_called()
        state_save.assert_not_called()

    def test_voice_cache_tracks_narration(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            s = script_for([5])
            generated = []
            async def fake_tts(text, voice_id, rate, pitch, output, seed=0):
                generated.append(text)
                output.write_bytes(b"a" * 2048)
            with patch.object(voice, "_generate_edge_tts_with_pauses", side_effect=fake_tts), \
                 patch.object(voice, "_get_audio_duration", return_value=5), \
                 patch.object(voice, "trim_clip_edges", return_value=5):
                asyncio.run(voice.generate_voices(s, {}, root))
                asyncio.run(voice.generate_voices(s, {}, root))
                (root / "scenes/scene_1_timestamps.json").write_text("[{}]")
                s.scenes[0].narration = "Rewritten spoken words."
                asyncio.run(voice.generate_voices(s, {}, root))
            self.assertEqual(generated, ["Complete beat 0.", "Rewritten spoken words."])
            self.assertFalse((root / "scenes/scene_1_timestamps.json").exists())

    def test_broken_editorial_frames_are_critical(self):
        critical, warnings = classify_qc(["QC: scene 1 FALLBACK_USED", "QC: scene 2 frame review: mismatch", "QC: CRITICAL edit cadence"])
        self.assertEqual(len(critical), 3)
        self.assertFalse(warnings)


if __name__ == "__main__":
    unittest.main()
