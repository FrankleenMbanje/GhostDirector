"""
GhostDirector — daily diagnosis + monetization gap (FIX-084)

Reads the day's outcome + the channel memory + the competitor topic queue
and says, in plain lines: what shipped, what failed and why (best-known
cause), where the channel stands against the monetization bar, and the
next actions. Pure functions — the scheduler supplies the IO, so this is
unit-testable and can never take a production day down.

Nothing here changes production by itself; it tells the truth daily, on
the cloud, in the job summary.
"""

from datetime import datetime, timedelta, timezone

SUBS_TARGET = 1000
SHORTS_VIEWS_TARGET = 10_000_000      # YPP shorts bar: 10M views / 90 days
WINDOW_DAYS = 90
RATE_WINDOW_DAYS = 14                  # pace measured over the last 2 weeks


def parse_daily_result(text: str) -> dict:
    """'result=FULL\\nshorts=3/3\\ndoc=ok' -> dict (tolerant of junk)."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def recent_short_rate(rows: list[dict] | None,
                      days: int = RATE_WINDOW_DAYS) -> float:
    """Views/day earned by shorts published in the last `days`."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    total = 0
    for r in rows or []:
        if r.get("format") != "short":
            continue
        try:
            published = datetime.fromisoformat(
                (r.get("published") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if published >= cutoff:
            total += int(r.get("views") or 0)
    return total / float(days)


def monetization_gap(channel_stats: dict | None, rate_views_per_day: float,
                     window_days: int = WINDOW_DAYS) -> dict:
    """The honest ETA.

    Caveat: the YPP bar counts SHORTS views over a rolling 90 days; the API
    gives us lifetime views, so `views_to_go` is a proxy that only ever
    understates the distance on an established channel and overstates work
    already done within the window. Stated in the report, never hidden.
    """
    stats = channel_stats or {}
    subs = int(stats.get("subscribers") or 0)
    total_views = int(stats.get("total_views") or 0)
    views_to_go = max(SHORTS_VIEWS_TARGET - total_views, 0)
    required_rate = views_to_go / float(window_days)
    eta_days = (round(views_to_go / rate_views_per_day)
                if rate_views_per_day > 0 else None)
    return {
        "subs": subs,
        "subs_to_go": max(SUBS_TARGET - subs, 0),
        "total_views": total_views,
        "views_to_go": views_to_go,
        "rate_views_per_day": round(rate_views_per_day, 1),
        "required_rate": round(required_rate, 1),
        "eta_days": eta_days,
        "on_track": bool(eta_days is not None and eta_days <= window_days),
    }


def diagnose(daily: dict, memory: dict | None = None,
             queued_topics: list[str] | None = None,
             channel_stats: dict | None = None) -> list[str]:
    """Plain-language diagnosis + next actions for the daily intel report."""
    lines: list[str] = []
    result = (daily.get("result") or "?").upper()
    shorts = daily.get("shorts") or "?"
    doc = daily.get("doc") or "?"
    lines.append(f"DAY: {result} | shorts {shorts} | doc {doc}")

    if result == "FAILED":
        lines.append("WHY: nothing shipped — check Gemini quota/503 first "
                     "(output/_llm_quota_quarantine.json), then token health.")
    elif result == "PARTIAL":
        if doc not in ("ok", "skipped"):
            lines.append("WHY: the doc stage failed after its retries "
                         "(503-class). The shorts shipped so the day was kept; "
                         "the next run retries the doc on a fresh story.")
        if str(shorts).split("/")[0] == "0":
            lines.append("WHY: no short shipped — treat as a failed day. The "
                         "workflow's outer retry has already fired; if it "
                         "repeats, check the story funnel (no fresh candidates?).")

    if memory:
        sh = memory.get("shorts") or {}
        lg = memory.get("longs") or {}
        if sh.get("n") and lg.get("n"):
            lines.append(
                f"BASELINE: shorts median {sh.get('median_views')} views "
                f"(n={sh['n']}) vs docs median {lg.get('median_views')} "
                f"(n={lg['n']}) — shorts are the growth engine; docs are "
                f"watch-time experiments until the channel has an audience.")
        rate = recent_short_rate(memory.get("rows"))
        gap = monetization_gap(
            channel_stats or memory.get("channel_stats") or {}, rate)
        lines.append(
            f"MONETIZATION: {gap['subs_to_go']} subs to go (have "
            f"{gap['subs']}/{SUBS_TARGET}); {gap['views_to_go']:,} views to the "
            f"10M bar (lifetime proxy). Pace {gap['rate_views_per_day']:.0f} "
            f"views/day → {gap['eta_days']} days at this pace; need "
            f"{gap['required_rate']:.0f} views/day for the {WINDOW_DAYS}-day bar.")
        if not gap["on_track"]:
            lines.append(
                "ACTION: pace is far below the 90-day bar. More uploads at the "
                "current per-short reach cannot close it — the lever is "
                "reach-per-short: cold-open hooks, in-lane stories only, and "
                "sequels on any breakout (winner loop).")
        median = sh.get("median_views") or 0
        if median and median < 2000:
            lines.append(
                "ACTION: shorts median under 2k — A/B the first two seconds "
                "before increasing volume.")
    if queued_topics:
        lines.append(f"COMPETITORS: {len(queued_topics)} outlier topics queued; "
                     f"top: {queued_topics[0][:70]}")
    else:
        lines.append("COMPETITORS: no fresh outlier topics queued — check the "
                     "watch-list (main.py --add-competitor).")
    return lines
