"""Read-only SQLite access to the local GarminDB mirror. GarminDB
(scripts/sync_garmindb.py, run nightly) populates this database; trend
tools read it directly instead of hitting the live Garmin API on every call.

SQLITE_PATH points at GarminDB's own `garmin.db` (its weight/sleep/rhr
tables), not a separate database we maintain -- there's no value in
mirroring GarminDB's output into a second copy.
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from statistics import mean

from garmin_mcp.models import ActivityTrend, BodyTrend, BodyTrendCoverage, HydrationTrend

MIN_DAYS_FOR_STEPS_TREND = 5

# GarminDB stores a flat 100 in days_summary.hydration_goal on every day
# checked, regardless of the account's real goal -- confirmed against the
# live API for the same day, which reported ~2839ml (Garmin's real
# activity/heat-adjusted goal). 100ml is not a plausible real goal for a
# human, so treat anything this low as GarminDB's broken placeholder rather
# than compute a percentage against it -- that's exactly the "confident
# wrong number" BUG-2 exists to prevent.
IMPLAUSIBLE_GOAL_ML = 200

# Below this many points, or with a gap this large between consecutive
# points, a fitted weekly slope reads as more precise than the data
# supports -- see BUG-2: a real GarminDB sync gap once produced
# trend_lb_per_week: -0.74 from 4 points spread across 9 days with 5 days
# silently missing. Suppressing isn't about these exact numbers being
# special, just about not reporting a two-decimal rate off data this sparse.
MIN_POINTS_FOR_TREND = 3
MAX_GAP_DAYS_FOR_TREND = 7

# GarminDB stores weight in whatever unit "settings.metric" in
# GarminConnectConfig.json selects -- with metric=false (statute) it's
# already pounds. Confirmed against the live Garmin API's value for the same date; no conversion
# needed. If metric is ever flipped to true, this would need to divide by
# 0.45359237 instead.


def _db_path() -> str:
    path = os.environ.get("SQLITE_PATH")
    if not path:
        raise RuntimeError("SQLITE_PATH is not set.")
    return path


@contextmanager
def _connect():
    conn = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True)
    try:
        yield conn
    finally:
        conn.close()


def get_body_trend(days: int = 30) -> BodyTrend:
    end = date.today()
    start = end - timedelta(days=days - 1)

    with _connect() as conn:
        rows = conn.execute(
            "SELECT day, weight FROM weight WHERE day >= ? AND day <= ? ORDER BY day",
            (start.isoformat(), end.isoformat()),
        ).fetchall()

    points = [
        {"date": str(day)[:10], "weight_lb": round(weight, 1)}
        for day, weight in rows
        if weight is not None
    ]

    def _avg_since(cutoff: date) -> float | None:
        window = [p["weight_lb"] for p in points if date.fromisoformat(p["date"]) >= cutoff]
        return round(mean(window), 1) if window else None

    avg_7d = _avg_since(end - timedelta(days=6))
    avg_28d = _avg_since(end - timedelta(days=27))

    dates = [date.fromisoformat(p["date"]) for p in points]
    days_spanned = (dates[-1] - dates[0]).days + 1 if dates else None
    largest_gap_days = max(
        (b - a).days for a, b in zip(dates, dates[1:])
    ) if len(dates) >= 2 else None
    coverage = BodyTrendCoverage(
        point_count=len(points), days_spanned=days_spanned, largest_gap_days=largest_gap_days
    )

    trend_lb_per_week = None
    trend_suppressed_reason = None
    if len(points) < MIN_POINTS_FOR_TREND:
        trend_suppressed_reason = f"fewer than {MIN_POINTS_FOR_TREND} weigh-ins in range"
    elif largest_gap_days is not None and largest_gap_days > MAX_GAP_DAYS_FOR_TREND:
        trend_suppressed_reason = f"largest gap between weigh-ins is {largest_gap_days} days"
    else:
        base = dates[0]
        xs = [(d - base).days for d in dates]
        ys = [p["weight_lb"] for p in points]
        x_mean, y_mean = mean(xs), mean(ys)
        denom = sum((x - x_mean) ** 2 for x in xs)
        if denom:
            slope_per_day = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denom
            trend_lb_per_week = round(slope_per_day * 7, 2)

    return BodyTrend(
        points=points,
        avg_7d=avg_7d,
        avg_28d=avg_28d,
        trend_lb_per_week=trend_lb_per_week,
        trend_suppressed_reason=trend_suppressed_reason,
        coverage=coverage,
    )


def _duration_str_to_minutes(value: str | None) -> float | None:
    """GarminDB stores durations as 'HH:MM:SS(.ffffff)' text, not seconds."""
    if not value:
        return None
    try:
        h, m, s = value.split(":")
        return round(int(h) * 60 + int(m) + float(s) / 60, 1)
    except (ValueError, AttributeError):
        return None


def get_activity_trend(days: int = 90) -> ActivityTrend:
    """Reads GarminDB's garmin_summary.db (days_summary table), not the live
    API. Classifies a day as a training day using days_summary's own
    `activities` count column -- no separate live call needed.
    """
    end = date.today()
    start = end - timedelta(days=days - 1)

    db_path = _db_path()
    summary_db_path = os.path.join(os.path.dirname(db_path), "garmin_summary.db")

    with sqlite3.connect(f"file:{summary_db_path}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT day, steps, calories_active_avg, intensity_time, floors, activities "
            "FROM days_summary WHERE day >= ? AND day <= ? ORDER BY day",
            (start.isoformat(), end.isoformat()),
        ).fetchall()

    points = [
        {
            "date": str(day)[:10],
            "steps": steps,
            "active_calories": _round_local(active_cal),
            "intensity_minutes": _duration_str_to_minutes(intensity_time),
            "floors": floors,
            "is_training_day": bool(activities),
        }
        for day, steps, active_cal, intensity_time, floors, activities in rows
    ]

    def _avg_steps_since(cutoff: date) -> float | None:
        window = [
            p["steps"] for p in points
            if p["steps"] is not None and date.fromisoformat(p["date"]) >= cutoff
        ]
        return round(mean(window), 0) if window else None

    steps_avg_7d = _avg_steps_since(end - timedelta(days=6))
    steps_avg_28d = _avg_steps_since(end - timedelta(days=27))

    dated_steps = [
        (date.fromisoformat(p["date"]), p["steps"]) for p in points if p["steps"] is not None
    ]
    steps_trend_per_week = None
    if len(dated_steps) >= MIN_DAYS_FOR_STEPS_TREND:
        base = dated_steps[0][0]
        xs = [(d - base).days for d, _ in dated_steps]
        ys = [s for _, s in dated_steps]
        x_mean, y_mean = mean(xs), mean(ys)
        denom = sum((x - x_mean) ** 2 for x in xs)
        if denom:
            slope_per_day = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denom
            steps_trend_per_week = round(slope_per_day * 7, 0)

    training_steps = [p["steps"] for p in points if p["is_training_day"] and p["steps"] is not None]
    rest_steps = [p["steps"] for p in points if not p["is_training_day"] and p["steps"] is not None]

    return ActivityTrend(
        points=points,
        steps_avg_7d=steps_avg_7d,
        steps_avg_28d=steps_avg_28d,
        steps_trend_per_week=steps_trend_per_week,
        training_day_avg_steps=round(mean(training_steps), 0) if training_steps else None,
        rest_day_avg_steps=round(mean(rest_steps), 0) if rest_steps else None,
        coverage_days=len(points),
    )


def _round_local(value):
    return round(value) if value is not None else None


def get_hydration_trend(days: int = 30) -> HydrationTrend:
    """Reads GarminDB's garmin_summary.db (days_summary), not the live API.
    sweat_loss is Garmin's own activity+weather-derived estimate -- it
    populates even on days with zero logged intake, confirmed live (a ruck
    day showed a real sweat_loss value with hydration_intake at 0).
    """
    end = date.today()
    start = end - timedelta(days=days - 1)

    db_path = _db_path()
    summary_db_path = os.path.join(os.path.dirname(db_path), "garmin_summary.db")

    with sqlite3.connect(f"file:{summary_db_path}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT day, hydration_intake, hydration_goal, sweat_loss "
            "FROM days_summary WHERE day >= ? AND day <= ? ORDER BY day",
            (start.isoformat(), end.isoformat()),
        ).fetchall()

    points = []
    for day, ml, goal_ml, sweat_loss in rows:
        if goal_ml is not None and goal_ml < IMPLAUSIBLE_GOAL_ML:
            goal_ml = None  # discard GarminDB's broken placeholder, not the real goal
        pct = round(ml / goal_ml * 100, 1) if ml is not None and goal_ml else None
        points.append(
            {
                "date": str(day)[:10],
                "ml": ml,
                "goal_ml": goal_ml,
                "sweat_loss_ml": sweat_loss,
                "pct_of_goal": pct,
            }
        )

    recent_ml = [
        p["ml"] for p in points
        if p["ml"] is not None and date.fromisoformat(p["date"]) >= end - timedelta(days=6)
    ]
    avg_7d_ml = round(mean(recent_ml), 0) if recent_ml else None

    days_below_goal = sum(
        1 for p in points if p["ml"] is not None and p["goal_ml"] and p["ml"] < p["goal_ml"]
    )

    return HydrationTrend(
        points=points, avg_7d_ml=avg_7d_ml, days_below_goal=days_below_goal, coverage_days=len(points)
    )
