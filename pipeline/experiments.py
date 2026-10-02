"""
GhostDirector — experiment engine (FIX-086)

The cloud changes ONE production variable at a time, measures the arms
against the channel's own outcome data, and promotes the winner to the
default. Registry: db/experiments.json (seeded with sensible defaults on
first run). Pure functions for assignment/evaluation; IO kept thin.

Rules that keep this honest:
  * Assignment alternates strictly by recorded count, so arms stay balanced
    even across multi-short days.
  * An experiment decides ONLY when both arms have >= min_videos AND the
    leader's median views beat the other by >= 25%. Otherwise it stays open
    (or closes as inconclusive after its window) — no promotion without
    evidence, no silent parameter changes.
  * Every decision records its evidence (n, medians) in the registry.
"""

import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from utils.logger import get_logger

log = get_logger("experiments")

REGISTRY_PATH = Path("db") / "experiments.json"
DECIDE_MARGIN = 1.25          # leader must beat the runner-up by 25%
WINDOW_DAYS = 7
MIN_VIDEOS_PER_ARM = 6

DEFAULT_EXPERIMENTS = [
    {
        "variable": "real_audio_hook",
        "variants": ["on", "off"],
        "description": "Real clip of the subject with its own audio as the "
                       "3s opener (FIX-085) vs the previous hook treatment.",
    },
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def load(path: Path | None = None) -> dict:
    p = Path(path or REGISTRY_PATH)
    try:
        reg = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(reg, dict) and isinstance(reg.get("experiments"), list):
            reg.setdefault("promoted", {})
            return reg
    except Exception:
        pass
    return {"version": 1, "promoted": {}, "experiments": []}


def save(reg: dict, path: Path | None = None) -> None:
    p = Path(path or REGISTRY_PATH)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(reg, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning(f"Experiment registry save failed: {e}")


def find(reg: dict, variable: str) -> dict | None:
    for exp in reg.get("experiments", []):
        if exp.get("variable") == variable:
            return exp
    return None


def ensure_defaults(reg: dict, now: datetime | None = None) -> dict:
    """Seed the registry with the default experiments on first run."""
    now = now or _now()
    for spec in DEFAULT_EXPERIMENTS:
        if find(reg, spec["variable"]) is None:
            reg.setdefault("experiments", []).append({
                **spec,
                "status": "running",
                "started": now.isoformat(),
                "window_days": WINDOW_DAYS,
                "min_videos_per_arm": MIN_VIDEOS_PER_ARM,
                "assignment": {v: [] for v in spec["variants"]},
                # pending = assigned but not yet recorded (the run that owns
                # the assignment may still be in flight); folded into the
                # alternation so overlapping runs can never stack one arm.
                "pending": {v: 0 for v in spec["variants"]},
                "winner": None,
                "decided_at": None,
                "evidence": None,
            })
    return reg


def arm_counts(exp: dict) -> dict:
    return {v: len(exp.get("assignment", {}).get(v, [])) for v in exp["variants"]}


def assign(exp: dict) -> str:
    """Next variant, strictly alternating by count (recorded + pending)."""
    total = sum(arm_counts(exp).values()) \
        + sum(int(n) for n in (exp.get("pending") or {}).values())
    return exp["variants"][total % len(exp["variants"])]


def _median(values):
    vals = [v for v in values if isinstance(v, (int, float))]
    return round(statistics.median(vals), 1) if vals else None


def evaluate(exp: dict, rows_by_id: dict, now: datetime | None = None) -> dict:
    """Evidence for one experiment: pending | decided | inconclusive."""
    now = now or _now()
    stats = {}
    for v in exp["variants"]:
        views = [rows_by_id[i]["views"] for i in exp.get("assignment", {}).get(v, [])
                 if i in rows_by_id]
        stats[v] = {"n": len(views), "median_views": _median(views)}
    min_n = int(exp.get("min_videos_per_arm") or MIN_VIDEOS_PER_ARM)
    if any(stats[v]["n"] < min_n for v in exp["variants"]):
        need = {v: max(0, min_n - stats[v]["n"]) for v in exp["variants"]}
        return {"status": "pending", "stats": stats,
                "reason": f"needs more videos: {need}"}
    ranked = sorted(exp["variants"],
                    key=lambda v: (stats[v]["median_views"] or 0), reverse=True)
    leader, second = ranked[0], ranked[1]
    lead, follow = stats[leader]["median_views"] or 0, stats[second]["median_views"] or 0
    if follow > 0 and lead >= follow * DECIDE_MARGIN:
        return {"status": "decided", "stats": stats, "winner": leader,
                "reason": f"{leader} median {lead} beats {second} {follow} "
                          f"by >= {(DECIDE_MARGIN - 1) * 100:.0f}%"}
    try:
        started = datetime.fromisoformat(exp.get("started") or "")
    except ValueError:
        started = now
    if now - started >= timedelta(days=int(exp.get("window_days") or WINDOW_DAYS)):
        return {"status": "inconclusive", "stats": stats,
                "reason": f"window closed with no significant leader "
                          f"({leader} {lead} vs {second} {follow})"}
    return {"status": "pending", "stats": stats,
            "reason": f"gap not significant yet ({leader} {lead} vs "
                      f"{second} {follow})"}


def run_cycle(reg: dict, rows_by_id: dict, now: datetime | None = None) -> list[str]:
    """Evaluate every running experiment; apply decisions; return report lines."""
    now = now or _now()
    lines: list[str] = []
    for exp in reg.get("experiments", []):
        if exp.get("status") != "running":
            lines.append(f"{exp['variable']}: {exp.get('status')} "
                         f"(winner={exp.get('winner')})")
            continue
        verdict = evaluate(exp, rows_by_id, now)
        stats = " | ".join(
            f"{v}: n={verdict['stats'][v]['n']} med={verdict['stats'][v]['median_views']}"
            for v in exp["variants"])
        if verdict["status"] == "decided":
            exp["status"] = "decided"
            exp["winner"] = verdict["winner"]
            exp["decided_at"] = now.isoformat()
            exp["evidence"] = verdict["stats"]
            reg.setdefault("promoted", {})[exp["variable"]] = verdict["winner"]
            lines.append(f"{exp['variable']}: DECIDED -> {verdict['winner']} "
                         f"({verdict['reason']}) [{stats}]")
        elif verdict["status"] == "inconclusive":
            exp["status"] = "inconclusive"
            exp["decided_at"] = now.isoformat()
            exp["evidence"] = verdict["stats"]
            lines.append(f"{exp['variable']}: INCONCLUSIVE — keeping the "
                         f"current default ({verdict['reason']}) [{stats}]")
        else:
            lines.append(f"{exp['variable']}: running — {verdict['reason']} [{stats}]")
    return lines


def active(variable: str, default: str = "on") -> str:
    """The promoted value for a variable (production default)."""
    reg = load()
    return reg.get("promoted", {}).get(variable, default)


def assign_variant(variable: str, default: str = "on") -> str:
    """The variant assigned to the NEXT video of this variable.

    Returns the promoted default when no experiment is running (so
    production behavior is unchanged until an experiment exists)."""
    reg = load()
    ensure_defaults(reg)
    exp = find(reg, variable)
    save(reg)
    if not exp or exp.get("status") != "running":
        return reg.get("promoted", {}).get(variable, default)
    variant = assign(exp)
    pending = exp.setdefault("pending", {v: 0 for v in exp["variants"]})
    pending[variant] = int(pending.get(variant, 0)) + 1
    save(reg)
    return variant


def record(variable: str, variant: str, video_id: str) -> None:
    """Attach a shipped video to its experimental arm (idempotent)."""
    if not video_id:
        return
    reg = load()
    exp = find(reg, variable)
    if not exp or exp.get("status") != "running":
        return
    ids = exp.setdefault("assignment", {}).setdefault(variant, [])
    if video_id not in ids:
        ids.append(video_id)
        pending = exp.get("pending") or {}
        if int(pending.get(variant, 0)) > 0:
            pending[variant] = int(pending[variant]) - 1
    save(reg)
