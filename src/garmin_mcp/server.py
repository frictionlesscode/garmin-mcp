"""FastMCP app and tool registration. Read tools only -- writes land in M4.
Run directly for local dev / MCP Inspector, or via the Dockerfile:

    python -m garmin_mcp.server
"""

import logging
import os
from importlib.metadata import version as pkg_version

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from garmin_mcp import garmin
from garmin_mcp.models import (
    ActivityDetail,
    ActivityTrend,
    BodyTrend,
    BpTrend,
    DeleteBpResult,
    DeleteHydrationResult,
    DeleteWeightResult,
    HydrationTrend,
    LogBpResult,
    LogHydrationResult,
    LogWeightResult,
    Readiness,
    TrainingLoad,
    Vo2MaxTrend,
    ZoneSummary,
)
from garmin_mcp.oauth import SingleUserOAuthProvider

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

_port = int(os.environ.get("MCP_PORT", "8000"))
_public_url = os.environ.get("MCP_PUBLIC_URL", f"http://127.0.0.1:{_port}")

auth_provider = SingleUserOAuthProvider(base_url=_public_url)

mcp = FastMCP(name="garmin-mcp", auth=auth_provider)


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "version": pkg_version("garmin-mcp"),
            "garmin_token_expires_at": garmin.get_token_expiry(),
        }
    )


@mcp.tool
def get_activities(days: int = 7, activity_type: str | None = None) -> list[dict]:
    """List Garmin activities from the last N days, optionally filtered by type
    (e.g. "running", "cycling", "strength_training"). Filtering happens against
    the actual type values Garmin returns, not Garmin's own (inconsistent)
    filter API. `possible_duplicate_of` flags when another activity in the
    same result set is the same type and started within 30 minutes -- a watch
    recording and a separate software push (e.g. a Tonal upload) can both land
    in Garmin for one real session; don't sum calories/effort across a flagged
    pair without checking which one has real content."""
    return garmin.get_activities(days=days, activity_type=activity_type)


@mcp.tool
def get_activity_detail(activity_id: str) -> ActivityDetail:
    """Full detail for one activity: HR zone minutes, lap splits, per-exercise
    strength sets (reps/weight/duration, strength_training only), parsed pack
    load in lbs when the title/notes name one (e.g. a ruck), weather, and
    notes. zone_boundaries_bpm gives the real bpm cutoffs behind hr_zones, as
    half-open intervals [low, high) -- z5's upper is null since Garmin
    doesn't return a cap. zone_config_id is a short hash of those boundaries:
    two activities with the same id used the same zone config and their
    zone minutes are comparable; different ids (e.g. before/after you change
    zone settings) mean they are NOT comparable -- don't sum or diff
    hr_zones across differing zone_config_id values."""
    return garmin.get_activity_detail(activity_id)


@mcp.tool
def get_sleep(days: int = 7) -> list[dict]:
    """Nightly sleep summaries (stage minutes, score, resting HR, start/end
    time, respiration, SpO2, restless moments) for the last N days, most
    recent first. sleep_start/sleep_end let you reconcile the reported total
    against the actual window if they seem to disagree."""
    return garmin.get_sleep(days=days)


@mcp.tool
def get_daily_stats(days: int = 7) -> list[dict]:
    """Daily wellness metrics (HRV with 7-day baseline and highest 5-min
    reading, resting HR, body battery, steps, active calories, stress average
    plus time-in-band percentages) for the last N days, most recent first."""
    return garmin.get_daily_stats(days=days)


@mcp.tool
def get_vo2max(days: int = 90) -> Vo2MaxTrend:
    """VO2max measurement history. Garmin only recomputes this after
    qualifying outdoor running/walking activities with HR and GPS, so
    `points` will be sparse and irregular -- real measurement dates only,
    never interpolated. `change_28d` is null unless at least two
    measurements fall in that window; don't fit a trend to one point."""
    return garmin.get_vo2max(days=days)


@mcp.tool
def get_body_trend(days: int = 30) -> BodyTrend:
    """Weight history over the last N days with 7-/28-day averages and a
    linear weekly trend. Reads the local GarminDB-synced database (see
    scripts/sync_garmindb.py), not the live Garmin API -- check `coverage`
    before trusting `trend_lb_per_week`: sync gaps happen (a weigh-in logged
    for a past date can land behind the incremental sync's already-advanced
    pointer and go unseen until the next full resync). When coverage is too
    sparse, trend_lb_per_week is null and trend_suppressed_reason explains
    why, rather than reporting a rate fitted to too little data."""
    return garmin.get_body_trend(days=days)


@mcp.tool
def get_activity_trend(days: int = 90) -> ActivityTrend:
    """Daily steps/active-calories/intensity-minutes/floors trend, with
    7-/28-day step averages, a weekly step trend, and a training-day vs
    rest-day step split (a rest-day step decline is the cleaner signal for
    non-exercise activity/NEAT dropping during a deficit than total steps,
    which rise on training days regardless). Reads the local GarminDB-synced
    database, not the live API -- check coverage_days before trusting the
    trend; this account is new, so coverage will be thin for a while and
    naturally improve as the daily sync accumulates history."""
    return garmin.get_activity_trend(days=days)


@mcp.tool
def get_zone_summary(days: int = 28) -> ZoneSummary:
    """Weekly heart-rate time-in-zone rollup across all activities in range --
    minutes and percentage per zone, plus activity_count. `zone_boundaries_bpm`
    reports the actual bpm cutoffs Garmin used (read from a real activity,
    since there's no zone-config endpoint to query directly) -- there's no
    way to know from the API whether these are Garmin's default %HRmax zones
    or a custom model, so no model name is guessed, just the real numbers."""
    return garmin.get_zone_summary(days=days)


@mcp.tool
def get_training_load(days: int = 42) -> TrainingLoad:
    """Acute (7-day) vs. chronic (28-day weekly average) training load, from a
    TRIMP-style figure this server computes itself -- always check the
    `method` field, which spells out that this is NOT Garmin/Firstbeat's
    Training Load. That figure is null for every Tonal-pushed activity
    (Firstbeat never processes externally-uploaded FIT files, confirmed via
    tonal-garmin-sync investigation -- not something fixable here), so it
    can't cover strength sessions at all. This version applies duration x
    HR-intensity uniformly to every session with avg_hr, strength included.
    Check `sessions_missing_hr` and `coverage_note` for sessions excluded for
    lacking HR data, and `ratio_suppressed_reason` for when the acute/chronic
    windows have too few sessions or span a zone_config_id change and aren't
    directly comparable. Ratio is a plain number -- no "optimal"/
    "overreaching" label, interpretation depends on the training plan."""
    return garmin.get_training_load(days=days)


@mcp.tool
def get_readiness() -> Readiness:
    """Composite snapshot for 'how should I train today': HRV, resting HR
    (with 7-day baselines), sleep score, body battery, recent activities, and
    days since last rest day. Data only -- no coaching or invented score."""
    return garmin.get_readiness()


@mcp.tool
def log_weight(weight_lb: float, date: str | None = None) -> LogWeightResult:
    """Log a weigh-in to Garmin Connect. Defaults to today. Idempotent per
    date -- replaces any existing weigh-in for that date instead of adding a
    duplicate. Rejects weight_lb outside 80-500."""
    return garmin.log_weight(weight_lb, date_str=date)


@mcp.tool
def delete_weight(date: str | None = None) -> DeleteWeightResult:
    """Delete any weigh-in(s) logged for a date. Defaults to today. Use this
    to correct a mis-dated or mistaken log_weight call."""
    return garmin.delete_weight(date_str=date)


@mcp.tool
def get_bp_trend(days: int = 90) -> BpTrend:
    """Blood pressure history: raw systolic/diastolic/pulse readings plus
    7-day averages for both. Deliberately excludes Garmin's own hypertension
    stage classification (confirmed it auto-categorizes every reading,
    e.g. "STAGE_1_HIGH") -- that's a clinical judgment, not something this
    server emits. No composite score, no interpretation."""
    return garmin.get_bp_trend(days=days)


@mcp.tool
def log_bp(systolic: int, diastolic: int, pulse: int, date: str | None = None, notes: str | None = None) -> LogBpResult:
    """Log a blood pressure reading to Garmin Connect. Defaults to today.
    pulse is required (Garmin's API validates it as an int 20-250 with no
    default). Accepts a single already-averaged reading -- if the reading
    came from a device that does its own multi-reading averaging (e.g. a
    Microlife MAM), pass that averaged value directly rather than logging
    each sub-reading separately."""
    return garmin.log_bp(systolic, diastolic, pulse, date_str=date, notes=notes)


@mcp.tool
def delete_bp(date: str | None = None) -> DeleteBpResult:
    """Delete any BP reading(s) logged for a date. Defaults to today."""
    return garmin.delete_bp(date_str=date)


@mcp.tool
def get_hydration(days: int = 30) -> HydrationTrend:
    """Daily hydration intake vs. goal, plus Garmin's own activity+weather
    -derived sweat_loss_ml estimate (populates even with zero logged intake).
    goal_ml auto-adjusts for activity and heat. Reads the local GarminDB
    -synced database, not the live API."""
    return garmin.get_hydration(days=days)


@mcp.tool
def log_hydration(ml: float, date: str | None = None) -> LogHydrationResult:
    """Log a hydration entry (water/fluid intake in ml) to Garmin Connect.
    Defaults to today. This ADDS to the day's running total rather than
    replacing it -- Garmin Connect itself treats hydration as cumulative
    (its API even accepts negative values specifically for subtracting), so
    calling this multiple times in a day logs separate drinks rather than
    overwriting. new_total_ml in the response reflects the running total
    after this entry."""
    return garmin.log_hydration(ml, date_str=date)


@mcp.tool
def delete_hydration(date: str | None = None) -> DeleteHydrationResult:
    """Clear a day's hydration total back to zero. There's no native delete
    for hydration, so this reads the current total and subtracts it exactly."""
    return garmin.delete_hydration(date_str=date)


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host=os.environ.get("MCP_HOST", "127.0.0.1"),
        port=_port,
    )
