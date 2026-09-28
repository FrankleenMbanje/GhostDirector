"""Phase 5 tests — vidIQ layer: thumbnail style library + channel audit.

Run with: venv/Scripts/python.exe test_phase5_units.py

Covers:
  A9a thumbnail_styles  — ranking, user templates, coercion, seeding
  A9b thumbnail renderer— all 7 layouts + all bg treatments on synthetic bg,
                          hero-number extraction, output integrity
  A10 channel_audit     — offline audit, profile persistence, recommendations,
                          report rendering (network functions NOT called)
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

_passed = 0
_failed = 0


def check(name: str, cond: bool):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}")


# ──────────────────────────────────────────────
def test_thumbnail_styles():
    print("\n[A9a] thumbnail_styles library")
    from pipeline import thumbnail_styles as TS

    check("13 curated styles", len(TS.STYLES) == 13)
    check("unique ids", len(TS.CURATED_IDS) == len(set(TS.CURATED_IDS)))
    check("all layouts known", all(s["text_layout"] in TS._KNOWN_LAYOUTS for s in TS.STYLES))

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # User template leads the ranking
        (tmp / "my_style.json").write_text(json.dumps({
            "id": "mine", "name": "Mine", "text_layout": "left_stack",
        }), encoding="utf-8")
        ranked = TS.recommend_styles("true crime")
        # The seeded example template lives in the real template dir and, by
        # design, leads the ranking (operator taste beats curated priors).
        check("user template leads ranking", ranked[0]["source"] == "user")

        # Coercion: unknown layout falls back, never crashes
        (tmp / "bad.json").write_text(json.dumps({
            "id": "bad", "text_layout": "hologram",
        }), encoding="utf-8")
        loaded = TS.load_user_templates(tmp)
        bad = [t for t in loaded if t["id"] == "bad"]
        check("unknown layout coerced to left_stack",
              bad and bad[0]["text_layout"] == "left_stack")
        check("user source tagged", bad and bad[0]["source"] == "user")

        # Unreadable JSON is skipped, not fatal
        (tmp / "broken.json").write_text("{not json", encoding="utf-8")
        ids = [t["id"] for t in TS.load_user_templates(tmp)]
        check("broken json skipped", "broken" not in ids)

    # Niche ranking puts tagged styles first (user templates excluded here)
    ranked = TS.recommend_styles("finance business", user_first=False)
    check("finance niche ranks money/price first",
          ranked[0]["id"] in ("money-green", "price-tag"))


# ──────────────────────────────────────────────
def test_thumbnail_renderer():
    print("\n[A9b] style renderer: all layouts + treatments")
    from PIL import Image
    from pipeline import thumbnail as T
    from pipeline.thumbnail_styles import STYLES, _KNOWN_LAYOUTS

    W, H = 1280, 720
    bg = Image.new("RGB", (W, H), (90, 60, 40))
    # Add variance so treatments visibly do something
    for x in range(0, W, 40):
        for y in range(0, H, 40):
            bg.putpixel((x, y), (200, 180, 30))

    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        rows = T.render_style_variants(
            bg, ["THE", "$9", "BILLION", "LIE"], 96, W, H, out,
            hook_text="THE $9 BILLION LIE from $9 billion dollar company",
            niche="true crime", only_styles=[s["id"] for s in STYLES])
        by_id = {r["id"]: r for r in rows}
        check("all 13 curated styles rendered", len(rows) == 13)
        for s in STYLES:
            f = out / by_id[s["id"]]["file"]
            check(f"file exists: {s['id']}", f.exists())
            with Image.open(f) as im:
                check(f"correct size: {s['id']}", im.size == (W, H))
            check(f"variant naming: {s['id']}",
                  by_id[s["id"]]["variant"] == f"style_{s['id']}")

        # Hero-number extraction
        img = T._layout_hero_number(bg.copy(), ["$9", "BILLION", "LIE"], 96, W, H,
                                    {"palette": {"accent": "#7CFC00"}})
        check("hero number renders", img.size == (W, H))
        img2 = T._layout_hero_number(bg.copy(), ["nine", "billion", "dollars"], 96, W, H,
                                     {"palette": {}})
        check("hero number fallback works", img2.size == (W, H))

        # Unknown layout falls back to left_stack painter
        painter = T._LAYOUTS.get("hologram", T._layout_left_stack)
        img3 = painter(bg.copy(), ["FALLBACK"], 96, W, H, {"palette": {}})
        check("unknown layout falls back", img3.size == (W, H))

        # Empty words never crash
        img4 = T._layout_left_stack(bg.copy(), [], 96, W, H, {"palette": {}})
        check("empty words safe", img4.size == (W, H))


# ──────────────────────────────────────────────
def test_channel_audit():
    print("\n[A10] channel_audit (offline paths)")
    import unittest.mock as mock
    from pipeline import channel_audit as CA

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        old_profile = CA.PROFILE_PATH
        CA.PROFILE_PATH = tmp / "channel_profile.json"
        try:
            # Profile creation with defaults
            p = CA.ensure_profile()
            check("profile created with niche", bool(p.get("niche")))
            check("profile has goal", bool(p.get("goal")))
            check("profile file written", CA.PROFILE_PATH.exists())

            # Updates
            p2 = CA.ensure_profile(niche="finance documentaries")
            check("profile niche updated", p2["niche"] == "finance documentaries")

            # Offline audit — hermetic: stub the network/live-data sources so
            # the test passes with or without OAuth on the host machine.
            with mock.patch.object(CA, "_resolve_my_channel", return_value=None), \
                 mock.patch.object(CA, "_load_analytics_summary",
                                   return_value={"videos": {}, "last_sync": None}), \
                 mock.patch("utils.channel_state._load", return_value={}):
                a = CA.audit()
            check("audit returns rows", isinstance(a["audit_rows"], list) and len(a["audit_rows"]) >= 4)
            checks = [r["check"] for r in a["audit_rows"]]
            check("identity check present", "Channel identity" in checks)
            check("cadence check present", "Upload cadence" in checks)
            check("length check present", "Video length" in checks)
            identity = next(r for r in a["audit_rows"] if r["check"] == "Channel identity")
            check("identity flagged action without OAuth", identity["status"] == "action")
            check("no live channel offline", a["channel"] is None)
            check("analytics empty offline", a["analytics_summary"]["videos"] == {})

            # Recommendations offline
            with mock.patch.object(CA, "_resolve_my_channel", return_value=None):
                recs = CA.recommendations(a)
            check("recommendations non-empty", len(recs) >= 5)
            check("oauth action first", "OAuth" in recs[0] or "client_secrets" in recs[0])

            # Report rendering
            report = CA.render_audit_report(a)
            check("report has header", "CHANNEL AUDIT" in report)
            check("report has next steps", "WHAT TO DO NEXT" in report)
            check("report has health checks", "HEALTH CHECKS" in report)
        finally:
            CA.PROFILE_PATH = old_profile

    # Benchmarks sanity
    check("8-min midroll benchmark", CA.REC_BENCHMARKS["min_video_length_for_watch_pages"] == 480)
    check("ctr target", CA.REC_BENCHMARKS["ctr_target_percent"] == 4.0)


# ──────────────────────────────────────────────
if __name__ == "__main__":
    test_thumbnail_styles()
    test_thumbnail_renderer()
    test_channel_audit()

    print("\n" + "═" * 58)
    total = _passed + _failed
    print(f"  Phase 5 (vidIQ layer): {_passed}/{total} checks passed"
          + ("  ✓" if _failed == 0 else f"  ✗ {_failed} FAILED"))
    print("═" * 58)
    sys.exit(1 if _failed else 0)
