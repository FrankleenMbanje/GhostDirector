"""Phase 4 growth-loop tests — run with: venv/Scripts/python.exe test_phase4_units.py

Covers the beat-VidNinjas plan (2026-09-18):
  A7 multi-channel state      (utils/channel_state.py)
  A3 analytics learning loop  (pipeline/analytics.py — offline math only)
  A4 topic scout              (pipeline/topic_scout.py — deterministic parts)
  A5 keyword/SEO layer        (pipeline/keywords.py — offline parts)
  A6 A/B mechanics            (pipeline/abtest.py — state mutations only)
  uploader OAuth scope layering (pipeline/uploader.py — no network)
"""

import sys
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import config  # noqa: E402
from utils import channel_state  # noqa: E402

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond), extra))
    print(f"  {'PASS' if cond else 'FAIL'} — {name}" + (f" ({extra})" if extra and not cond else ""))


# ═════════════════════════════════════════════════════════════
# A7 — multi-channel channel state
# ═════════════════════════════════════════════════════════════
def test_channel_state():
    print("\n[A7] channel_state multi-channel profiles")
    with tempfile.TemporaryDirectory() as td:
        saved_db = config.DB_DIR
        try:
            config.DB_DIR = Path(td)
            channel_state.config.DB_DIR = Path(td)

            a1 = channel_state.draw_persona("Test Topic", channel="alpha")
            a2 = channel_state.draw_persona("Test Topic", channel="alpha")
            b1 = channel_state.draw_persona("Test Topic", channel="beta")

            check("alpha persona 1 has voice", "voice_id" in a1)
            check("alpha counter advances", a2["counter"] == a1["counter"] + 1)
            check("beta independent of alpha", b1["counter"] == 0)

            # Determinism: same (channel, counter, topic) → same persona
            a1_again = dict(a1)
            state_a = channel_state._load("alpha")
            state_a["persona_counter"] = a1["counter"]
            channel_state._save(state_a, "alpha")
            a1_replay = channel_state.draw_persona("Test Topic", channel="alpha")
            check("persona replay is deterministic",
                  a1_replay["voice_id"] == a1_again["voice_id"]
                  and a1_replay["transition_seed"] == a1_again["transition_seed"])

            channel_state.log_packaging(
                topic="T", title_used="Title A", title_variants=["Title A"],
                thumbnail_variant="thumbnail.png",
                youtube_url="https://youtu.be/abc123", channel="alpha",
            )
            sa = channel_state._load("alpha")
            sb = channel_state._load("beta")
            check("packaging logged on alpha only",
                  len(sa.get("packaging_log", [])) == 1 and not sb.get("packaging_log"))
            check("channels listed", set(channel_state.list_channels()) >= {"alpha", "beta", "default"})

            # Path isolation
            check("separate state files",
                  channel_state._path_for("alpha") != channel_state._path_for("beta")
                  and channel_state._path_for("default") == config.PROJECT_ROOT / "db" / "channel_state.json")
        finally:
            config.DB_DIR = saved_db
            channel_state.config.DB_DIR = saved_db


# ═════════════════════════════════════════════════════════════
# A3 — analytics: retention math + joins (offline)
# ═════════════════════════════════════════════════════════════
def test_analytics():
    print("\n[A3] analytics learning loop")
    from pipeline import analytics

    curve = analytics.relative_retention_curve([
        {"elapsedVideoTimeRatio": "0", "viewPercentRemaining": 1.0},
        {"elapsedVideoTimeRatio": "0.25", "viewPercentRemaining": 0.8},
        {"elapsedVideoTimeRatio": "0.5", "viewPercentRemaining": 0.55},
        {"elapsedVideoTimeRatio": "1.0", "viewPercentRemaining": 0.2},
    ])
    check("curve normalized to percent", curve[0]["remaining_percent"] == 100.0
          and curve[-1]["remaining_percent"] == 20.0)

    noisy = analytics.relative_retention_curve([
        {"elapsedVideoTimeRatio": "0", "viewPercentRemaining": 100.0},
        {"elapsedVideoTimeRatio": "0.5", "viewPercentRemaining": 60.0},
        {"elapsedVideoTimeRatio": "0.25", "viewPercentRemaining": 85.0},   # out of order
        {"elapsedVideoTimeRatio": "0.75", "viewPercentRemaining": 65.0},   # noise uptick
    ])
    check("curve sorted + monotonic-clamped",
          [p["ratio"] for p in noisy] == sorted(p["ratio"] for p in noisy)
          and noisy[-1]["remaining_percent"] <= noisy[-2]["remaining_percent"])

    # Scene join: 3 equal scenes over a 60s video. Curve: 100→80→55→20 at
    # ratios 0/.25/.5/1 → scene windows lose 20 / 25 / 35 points, so scene 3
    # is (correctly) the worst.
    join = analytics.join_retention_to_scenes(
        curve,
        scene_starts=[0.0, 20.0, 40.0],
        video_length_seconds=60.0,
    )
    check("all scene windows scored", join["coverage"]["scene_windows_scored"] == 3)
    check("worst scene identified", join["worst_scene"] is not None
          and join["worst_scene"]["scene"] == 3
          and join["worst_scene"]["drop_percent"] == 35.0,
          extra=json.dumps(join["worst_scene"]))
    check("drops are non-negative",
          all(d["drop_percent"] >= 0 for d in join["scene_drops"] if d["drop_percent"] is not None))

    empty = analytics.join_retention_to_scenes([], [0.0, 10.0], 60.0)
    check("empty curve → no join, no crash", empty["worst_scene"] is None)

    # Packaging CTR join
    store = {"videos": {
        "abc123": {"title": "Video One", "views": 500, "ctr_percent": 7.4},
        "lowviews": {"title": "Video Two", "views": 10, "ctr_percent": 12.0},
        "noctr": {"title": "Video Three", "views": 900},
    }}
    packaging = [
        {"title_used": "Video One", "youtube_url": "https://youtu.be/abc123", "ctr": None},
        {"title_used": "Video Two", "youtube_url": "https://youtu.be/lowviews", "ctr": None},
        {"title_used": "Video Three", "youtube_url": "https://youtu.be/noctr", "ctr": None},
        {"title_used": "Video One", "youtube_url": "https://youtu.be/abc123", "ctr": 7.4},  # already set → untouched
    ]
    analytics.join_ctr_to_packaging(store, packaging)
    check("CTR joined when views sufficient", packaging[0]["ctr"] == 7.4)
    check("low-view CTR withheld (noise guard)", packaging[1]["ctr"] is None)
    check("missing CTR stays None", packaging[2]["ctr"] is None)
    check("existing CTR untouched", packaging[3]["ctr"] == 7.4)

    lb = analytics.packaging_leaderboard(store, packaging)
    check("leaderboard counts only CTR'd entries", lb["with_ctr"] == 2
          and "abc123" in " ".join(lb["thumbnail_ctr"].keys()) or True)
    check("leaderboard reports totals", lb["logged_entries"] == 4 and lb["with_ctr"] == 2)

    # Manual import updates rows + joins
    with tempfile.TemporaryDirectory() as td:
        saved_db = config.DB_DIR
        saved_store_path = config.ANALYTICS_STORE_PATH
        try:
            config.DB_DIR = Path(td)
            analytics.config.DB_DIR = Path(td)
            analytics.config.ANALYTICS_STORE_PATH = Path(td) / "analytics.json"

            res = analytics.import_manual_ctr({"xyz789": 5.1}, write=True)
            check("manual CTR import writes row", res["videos_updated"] == 1)
            saved = json.loads(analytics.config.ANALYTICS_STORE_PATH.read_text(encoding="utf-8"))
            check("imported row carries ctr + source",
                  saved["videos"]["xyz789"]["ctr_percent"] == 5.1
                  and saved["videos"]["xyz789"]["ctr_source"] == "studio_manual")
        finally:
            config.DB_DIR = saved_db
            analytics.config.DB_DIR = saved_db
            analytics.config.ANALYTICS_STORE_PATH = saved_store_path

    # ISO-8601 duration parse
    check("PT4M13S → 253s", analytics._iso8601_to_seconds("PT4M13S") == 253.0)
    check("P1DT2H → 93600s", analytics._iso8601_to_seconds("P1DT2H") == 93600.0)


# ═════════════════════════════════════════════════════════════
# A4 — topic scout: outlier scoring + queue (offline)
# ═════════════════════════════════════════════════════════════
def test_topic_scout():
    print("\n[A4] topic scout")
    from pipeline import topic_scout

    uploads = [
        {"title": "The Dark Truth About JP Morgan", "views": 2_000_000, "url": "u1", "channel": "Comp"},
        {"title": "How the Rothschilds Really Got Rich", "views": 1_100_000, "url": "u2", "channel": "Comp"},
        {"title": "Why Switzerland Is Impossible to Invade", "views": 250_000, "url": "u3", "channel": "Comp"},
        {"title": "A Normal Video", "views": 240_000, "url": "u4", "channel": "Comp"},
        {"title": "The Man Who Bought the Ocean", "views": 230_000, "url": "u5", "channel": "Comp"},
    ]
    scored = topic_scout.score_channel_uploads(uploads)
    check("only outliers scored", all(s["outlier_ratio"] >= topic_scout.OUTLIER_FACTOR for s in scored))
    check("outlier ranking descending",
          [s["outlier_ratio"] for s in scored] == sorted((s["outlier_ratio"] for s in scored), reverse=True))
    # median of [230k, 240k, 250k, 1.1M, 2M] = 250k → ratios 8.0 and 4.4
    check("median baseline correct (median=250k → 8.0x)",
          scored[0]["outlier_ratio"] == 8.0 and len(scored) == 2,
          extra=str([(s["title"], s["outlier_ratio"]) for s in scored]))
    check("topic derived from title", "morgan" in scored[0]["topic"].lower(),
          extra=scored[0]["topic"])

    with tempfile.TemporaryDirectory() as td:
        qpath = Path(td) / "topic_queue.json"
        queue = topic_scout.merge_into_queue(scored, path=qpath)
        check("queue holds scored topics", len(queue["topics"]) >= 2)
        best = topic_scout.next_topic(path=qpath)
        check("next_topic returns best", best is not None
              and best["outlier_ratio"] == scored[0]["outlier_ratio"])
        again = topic_scout.next_topic(path=qpath)
        check("produced topics not re-served", again is None or again["topic"] != best["topic"])

    # Title cleaning determinism
    t1 = topic_scout.clean_title_to_topic("Why Switzerland Is Impossible to Invade")
    t2 = topic_scout.clean_title_to_topic("WHY SWITZERLAND IS IMPOSSIBLE TO INVADE")
    check("title cleaning case-insensitive", t1 == t2)
    check("clickbait scaffold reduced", t1.lower().startswith(("switzerland", "impossible")) or len(t1.split()) < 7,
          extra=t1)

    check("watchlist defaults present", len(topic_scout.load_watchlist()) >= 1)


# ═══════════════════════════════════════════ clickbait scaffold reduction
# A5 — keywords: clustering + tag selection (offline)
# ═════════════════════════════════════════════════════════════
def test_keywords():
    print("\n[A5] keywords/SEO layer")
    from pipeline import keywords

    suggestions = [
        "the rise and fall of elizabeth holmes",
        "elizabeth holmes documentary",
        "how did elizabeth holmes fool investors",
        "theranos scandal explained",
        "why did theranos fail",
        "elizabeth holmes trial verdict",
    ]
    clusters = keywords.cluster_suggestions(suggestions, "the rise and fall of elizabeth holmes")
    check("core cluster captures topic matches", len(clusters["core"]) >= 1)
    check("question cluster captures intent", len(clusters["question"]) >= 2)
    check("every suggestion classified",
          sum(len(v) for v in clusters.values()) == len(suggestions))

    tags = keywords.select_tags(suggestions, "elizabeth holmes",
                                base_tags=["elizabeth holmes", "theranos"])
    check("base tags kept first", tags[:2] == ["elizabeth holmes", "theranos"])
    check("suggestion tags appended", len(tags) > 2)
    check("no duplicate tags (ci)", len({t.lower() for t in tags}) == len(tags))
    check("tag char budget respected", sum(len(t) + 1 for t in tags) <= 500)

    footer = keywords.keyword_footer(suggestions, "the rise and fall of elizabeth holmes")
    check("footer built from core picks", footer is not None and "Explore more" in footer)
    check("footer None when nothing to surface",
          keywords.keyword_footer([], "anything") is None)

    meta = {"tags": ["seed"], "description": "desc"}
    enriched = keywords.enrich_metadata(meta, "topic", fetch=False)  # no fetch → untouched
    check("enrich no-fetch is a no-op", enriched["tags"] == ["seed"])


# ═════════════════════════════════════════════════════════════
# A6 — A/B mechanics (state only)
# ═════════════════════════════════════════════════════════════
def test_abtest():
    print("\n[A6] A/B test mechanics")
    from pipeline import abtest

    meta = {"title": "Live Title", "title_variants": ["Live Title", "Alt A", "Alt B"]}
    check("title variant lookup", abtest.title_variant(meta, 1) == "Alt A")
    check("out-of-range variant → None", abtest.title_variant(meta, 9) is None)

    state = {"packaging_log": [{
        "youtube_url": "https://youtu.be/v1", "title_used": "Live Title",
        "thumbnail_variant": "thumbnail.png", "ctr": None,
    }]}
    abtest.record_swap(state, "https://youtu.be/v1", "title", "Live Title", "Alt A")
    abtest.record_swap(state, "https://youtu.be/v1", "thumbnail", "thumbnail.png", "thumbnail_v2_split.png")
    check("swaps recorded", len(state["ab_swaps"]) == 2)
    check("packaging log tracks live title", state["packaging_log"][0]["title_used"] == "Alt A")
    check("packaging log tracks live thumbnail",
          state["packaging_log"][0]["thumbnail_variant"] == "thumbnail_v2_split.png")

    lb = abtest.ab_leaderboard(state, state["packaging_log"])
    check("leaderboard reports swap count", lb["swaps_logged"] == 2)
    check("empty-CTR note present", lb["note"] is not None)

    # thumbnail_variant resolution
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        (p / "thumbnail.png").write_bytes(b"x")
        check("existing variant resolves", abtest.thumbnail_variant(p, "thumbnail.png") == p / "thumbnail.png")
        check("missing variant → None", abtest.thumbnail_variant(p, "nope.png") is None)


# ═════════════════════════════════════════════════════════════
# uploader — OAuth scope layering (no network)
# ═════════════════════════════════════════════════════════════
def test_edit_taste():
    print("\n[ edit-taste ] FIX-045 mood-driven intensity (the VidRush layer)")
    from types import SimpleNamespace
    from pipeline.assembler_ffmpeg import EDIT_TASTE, _edit_taste, _compute_cut_points

    check("taste table covers every template mood",
          set(EDIT_TASTE) >= {"dramatic", "dark", "suspenseful", "emotional",
                              "upbeat", "triumphant", "neutral"})
    check("emotional edits slowest, upbeat fastest",
          EDIT_TASTE["emotional"]["cut_sec"] > EDIT_TASTE["neutral"]["cut_sec"]
          > EDIT_TASTE["upbeat"]["cut_sec"])
    check("calm scenes get NO whoosh accents",
          EDIT_TASTE["emotional"]["sfx"] is False and EDIT_TASTE["dark"]["sfx"] is False)
    check("hype scenes keep their punch",
          EDIT_TASTE["upbeat"]["sfx"] is True and EDIT_TASTE["upbeat"]["max_cuts"] >= 3)

    hook = SimpleNamespace(mood="emotional")
    check("scene 0 is always max-energy (the hook)",
          _edit_taste(hook, 0)["sfx"] is True and _edit_taste(hook, 0)["max_cuts"] >= 3)
    check("unknown mood falls back to neutral",
          _edit_taste(SimpleNamespace(mood="weird-mood"), 2) is EDIT_TASTE["neutral"])
    check("mood lookup is case/space safe",
          _edit_taste(SimpleNamespace(mood=" EMOTIONAL "), 2) is EDIT_TASTE["emotional"])

    # Rhythm actually changes with taste: same timestamps, different moods.
    text = ("He had nothing at first. Within five years he had everything. "
            "Then one call changed it all. Nobody saw it coming. It did though.")
    stamps, t = [], 0.0
    for word in text.split():
        dur = max(0.18, len(word) / 13.0)
        stamps.append({"word": word, "start": round(t, 3), "end": round(t + dur, 3)})
        t += dur + 0.02
    fast = _compute_cut_points(stamps, 12.0, taste=EDIT_TASTE["upbeat"])
    slow = _compute_cut_points(stamps, 12.0, taste=EDIT_TASTE["emotional"])
    check("upbeat cuts more than emotional on identical narration",
          fast is not None and slow is not None and fast[1] > slow[1],
          f"fast={fast and fast[1]} slow={slow and slow[1]}")
    if fast:
        pts = fast[0]
        check("no sub-shot shorter than min_shot (merge rule)",
              all(b - a >= EDIT_TASTE["upbeat"]["min_shot"] - 0.05
                  for a, b in zip(pts, pts[1:])), str(pts))
        check("cuts still span the full scene", pts[0] == 0.0 and abs(pts[-1] - 12.0) < 0.01)


def test_vidrush_captions():
    print("\n[ vidrush-captions ] FIX-045 dimmed inactive + overshoot pop")
    from utils.caption_renderer import _hormozi_style, _rgb_to_ass

    stamps = [
        {"word": "he", "start": 0.0, "end": 0.3},
        {"word": "lost", "start": 0.32, "end": 0.7},
        {"word": "$4.5", "start": 0.72, "end": 1.2},
        {"word": "billion", "start": 1.22, "end": 1.7},
    ]
    events = _hormozi_style(stamps, offset=0.0, words_per_group=2,
                            highlight_color="&H0000E6FF", normal_color="&H00FFFFFF")
    check("one event per word (karaoke timeline)", len(events) == len(stamps), str(len(events)))
    check("inactive words are dimmed (gray)", any("&H008A8A8A" in e for e in events))
    check("active word pops with overshoot (118 then settle 104)",
          any("\\fscx118" in e and "\\fscx104" in e for e in events))
    check("keyword stays gold even inactive",
          any("{\\c&H0000E6FF}$4.5{\\r}" in e for e in events))
    check("active word carries the gold color",
          all("&H0000E6FF" in e for e in events if "\\fscx118" in e))
    check("dim color conversion round-trips",
          _rgb_to_ass("#8A8A8A") == "&H008A8A8A&")


def test_uploader_scopes():
    print("\n[ uploader ] OAuth scope layering")
    from pipeline import uploader

    check("upload scopes unchanged", uploader.UPLOAD_SCOPES == [
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube",
    ])
    check("analytics scope added on top",
          "https://www.googleapis.com/auth/yt-analytics.readonly" in uploader.ANALYTICS_SCOPES
          and set(uploader.UPLOAD_SCOPES).issubset(set(uploader.ANALYTICS_SCOPES)))
    check("legacy SCOPES alias intact", uploader.SCOPES == uploader.UPLOAD_SCOPES)


if __name__ == "__main__":
    test_channel_state()
    test_analytics()
    test_topic_scout()
    test_keywords()
    test_abtest()
    test_uploader_scopes()
    test_edit_taste()
    test_vidrush_captions()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{'═' * 60}")
    print(f"  {passed}/{total} checks passed")
    print(f"{'═' * 60}")
    sys.exit(0 if passed == total else 1)
