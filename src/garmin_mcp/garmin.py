"""Thin wrapper over garminconnect.Garmin. Owns the client singleton, unit
conversion, and shaping raw Garmin responses into the compact contracts in
models.py. No MCP/FastMCP concerns live here.
"""

import base64
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from statistics import mean
from zoneinfo import ZoneInfo

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from garmin_mcp import store
from garmin_mcp.models import (
    Activity,
    ActivityDetail,
    ActivityTrend,
    BodyTrend,
    BodyTrendCoverage,
    BpReading,
    BpTrend,
    DailyStats,
    DeleteBpResult,
    DeleteHydrationResult,
    DeleteWeightResult,
    ExerciseSet,
    HrZones,
    HydrationTrend,
    LogBpResult,
    LogHydrationResult,
    LogWeightResult,
    Readiness,
    SleepNight,
    Split,
    TrainingLoad,
    TrainingLoadPoint,
    Vo2MaxTrend,
    ZoneSummary,
    ZoneWeek,
)

logger = logging.getLogger(__name__)

TZ = ZoneInfo(os.environ.get("TZ", "America/New_York"))
METERS_PER_MILE = 1609.344
GRAMS_PER_LB = 453.59237
MIN_WEIGHT_LB = 80.0
MAX_WEIGHT_LB = 500.0

_client: Garmin | None = None


class GarminAuthError(RuntimeError):
    """Tokens missing or expired. Re-run scripts/login.py."""


class GarminRateLimitError(RuntimeError):
    """Garmin is rate-limiting this account (HTTP 429)."""


class TrendDataUnavailableError(RuntimeError):
    """The local GarminDB-synced database doesn't exist yet."""


class InvalidWeightError(ValueError):
    """weight_lb outside the accepted range."""


def _token_dir() -> str:
    token_dir = os.environ.get("GARMIN_TOKEN_DIR")
    if not token_dir:
        raise GarminAuthError("GARMIN_TOKEN_DIR is not set.")
    return token_dir


def get_client() -> Garmin:
    global _client
    if _client is None:
        client = Garmin()
        try:
            client.login(tokenstore=_token_dir())
        except GarminConnectAuthenticationError as e:
            raise GarminAuthError(
                f"Garmin login failed: {e}. Re-run scripts/login.py to refresh tokens."
            ) from e
        _client = client
    return _client


def get_token_expiry() -> str | None:
    """Read the persisted token's exp claim straight off disk -- no network
    call, no login. Used by /health, which must stay fast and not depend on
    Garmin being reachable. Returns an ISO 8601 UTC timestamp, or None if the
    token file or its expiry claim can't be found.
    """
    token_dir = os.environ.get("GARMIN_TOKEN_DIR")
    if not token_dir:
        return None
    path = os.path.join(token_dir, "garmin_tokens.json")
    try:
        with open(path) as f:
            data = json.load(f)
        token = data.get("di_token")
        if not token:
            return None
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        exp = payload.get("exp")
        if exp is None:
            return None
        return datetime.fromtimestamp(int(exp), tz=timezone.utc).isoformat()
    except (OSError, ValueError, KeyError, IndexError):
        return None


def _call(fn, *args, **kwargs):
    """Call a garminconnect client method with bounded backoff on 429s and a
    clear error on auth failure. Never retries auth failures -- that's how
    accounts get locked out.
    """
    delays = (1, 2, 4)
    for attempt, delay in enumerate((*delays, None)):
        try:
            return fn(*args, **kwargs)
        except GarminConnectAuthenticationError as e:
            raise GarminAuthError(
                f"Garmin session expired: {e}. Re-run scripts/login.py to refresh tokens."
            ) from e
        except GarminConnectTooManyRequestsError as e:
            if delay is None:
                raise GarminRateLimitError(
                    f"Garmin rate-limited this account (429) after {len(delays)} retries: {e}"
                ) from e
            logger.warning("Garmin 429, retrying in %ss", delay)
            time.sleep(delay)
        except GarminConnectConnectionError as e:
            if delay is None:
                raise
            logger.warning("Garmin connection error, retrying in %ss: %s", delay, e)
            time.sleep(delay)


def _today() -> date:
    return datetime.now(TZ).date()


def _or_none(value, sentinel=-1):
    """Garmin uses -1 (and sometimes null) as its 'no data' marker."""
    if value is None or value == sentinel:
        return None
    return value


def _round_or_none(value, digits=0):
    value = _or_none(value)
    if value is None:
        return None
    result = round(value, digits)
    return int(result) if digits == 0 else result


def _detect_duplicates(raw_activities: list[dict], window_minutes: int = 30) -> dict[str, str | None]:
    """Map activity id -> id of a same-type activity starting within
    window_minutes, if any. BUG-4: a watch recording and a separate software
    push (e.g. Tonal) can both land in Garmin for the same real session --
    confirmed live on 2026-08-07 (two strength_training entries, same start
    minute, one manufacturer=GARMIN one manufacturer=DEVELOPMENT). Fixing the
    push side is out of scope here; flagging it is what we can do from the
    read side so a caller doesn't silently double-count training load.
    """
    parsed = [
        (str(a["activityId"]), a.get("activityType", {}).get("typeKey"), datetime.fromisoformat(a["startTimeLocal"]))
        for a in raw_activities
    ]
    result: dict[str, str | None] = {}
    for i, (id_a, type_a, start_a) in enumerate(parsed):
        match = None
        for j, (id_b, type_b, start_b) in enumerate(parsed):
            if i != j and type_a == type_b and abs((start_a - start_b).total_seconds()) <= window_minutes * 60:
                match = id_b
                break
        result[id_a] = match
    return result


def _shape_activity(a: dict, duplicate_of: str | None = None) -> Activity:
    return Activity(
        id=str(a["activityId"]),
        date=a["startTimeLocal"][:10],
        type=a.get("activityType", {}).get("typeKey", "unknown"),
        duration_min=_round_or_none(a.get("duration", 0) / 60, 1) if a.get("duration") else None,
        distance_mi=_round_or_none(a["distance"] / METERS_PER_MILE, 2) if a.get("distance") else None,
        avg_hr=_round_or_none(a.get("averageHR")),
        max_hr=_round_or_none(a.get("maxHR")),
        calories=_round_or_none(a.get("calories")),
        training_effect=_round_or_none(a.get("aerobicTrainingEffect"), 1),
        possible_duplicate_of=duplicate_of,
    )


def get_activities(days: int = 7, activity_type: str | None = None) -> list[Activity]:
    """activity_type is matched against the raw activityType.typeKey client-side,
    not passed to Garmin's API filter -- Garmin's activitytype query param only
    accepts top-level types and 400s on sub-types like "strength_training",
    which is exactly the value it returns in results. Filtering ourselves
    sidesteps needing Garmin's parent/sub-type table at all.
    """
    end = _today()
    start = end - timedelta(days=days - 1)
    client = get_client()
    raw = _call(client.get_activities_by_date, start.isoformat(), end.isoformat())
    dup_map = _detect_duplicates(raw)
    activities = [_shape_activity(a, dup_map.get(str(a["activityId"]))) for a in raw]
    if activity_type:
        activities = [a for a in activities if a["type"] == activity_type]
    return activities


def _shape_exercise_set(s: dict) -> ExerciseSet:
    ex = (s.get("exercises") or [{}])[0]
    name = ex.get("name") or ex.get("category") or "Unknown"
    weight = s.get("weight")
    return ExerciseSet(
        exercise=name.replace("_", " ").title(),
        reps=_round_or_none(s.get("repetitionCount")),
        weight_lb=round(weight / GRAMS_PER_LB, 1) if weight is not None else None,
        duration_sec=_round_or_none(s.get("duration"), 1),
    )


LOAD_LB_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*lb", re.IGNORECASE)


def _parse_load_lb(*texts: str | None) -> float | None:
    """Garmin has no pack-weight field, so a ruck's load only exists as text
    in the activity title (e.g. "ruck 30 lb") -- confirmed live. Checked
    against the activity name first, then description/notes, since that's
    where it was actually found.
    """
    for text in texts:
        if not text:
            continue
        match = LOAD_LB_PATTERN.search(text)
        if match:
            return float(match.group(1))
    return None


def _zone_boundaries_and_config_id(hr_zones_raw: list[dict]) -> tuple[dict[str, list[int | None]] | None, str | None]:
    """Half-open bpm intervals per zone: lower bound is the real
    zoneLowBoundary Garmin recorded for this activity, upper bound is the
    next zone's low (z5's upper is unknown -- Garmin doesn't return a cap --
    so it's left null rather than guessed). zone_config_id is a short hash of
    the low-boundary tuple: same id means the same zone config produced both
    activities' minutes, different id means they aren't comparable. Computed
    entirely from data already in hand -- no profile lookup, no inference.
    """
    lows: dict[int, int] = {
        z["zoneNumber"]: z["zoneLowBoundary"]
        for z in hr_zones_raw
        if z.get("zoneNumber") and z.get("zoneLowBoundary") is not None
    }
    if not lows:
        return None, None
    boundaries: dict[str, list[int | None]] = {}
    for zone_num in sorted(lows):
        upper = lows.get(zone_num + 1)
        boundaries[f"z{zone_num}"] = [lows[zone_num], upper]
    config_id = hashlib.sha256(
        json.dumps(sorted(lows.items())).encode()
    ).hexdigest()[:6]
    return boundaries, config_id


def get_activity_detail(activity_id: str) -> ActivityDetail:
    client = get_client()
    summary = _call(client.get_activity, activity_id)
    splits_raw = _call(client.get_activity_splits, activity_id)
    hr_zones_raw = _call(client.get_activity_hr_in_timezones, activity_id)

    activity_type = summary.get("activityTypeDTO", {}).get("typeKey", "unknown")

    try:
        weather = _call(client.get_activity_weather, activity_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to fetch weather for %s: %s", activity_id, e)
        weather = None

    # Only strength_training activities have exercise sets; other types
    # either 404 or return nothing, so don't spend a call on them.
    sets: list[ExerciseSet] = []
    if activity_type == "strength_training":
        try:
            sets_raw = _call(client.get_activity_exercise_sets, activity_id)
            sets = [_shape_exercise_set(x) for x in sets_raw.get("exerciseSets", [])]
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to fetch exercise sets for %s: %s", activity_id, e)

    s = summary.get("summaryDTO", {})

    zones = {f"z{z['zoneNumber']}": _round_or_none(z["secsInZone"] / 60, 1) for z in hr_zones_raw}
    hr_zones = HrZones(
        z1=zones.get("z1"), z2=zones.get("z2"), z3=zones.get("z3"),
        z4=zones.get("z4"), z5=zones.get("z5"),
    )

    splits = [
        Split(
            lap=lap.get("lapIndex", i + 1),
            duration_min=_round_or_none(lap["duration"] / 60, 1) if lap.get("duration") else None,
            distance_mi=_round_or_none(lap["distance"] / METERS_PER_MILE, 2) if lap.get("distance") else None,
            avg_hr=_round_or_none(lap.get("averageHR")),
        )
        for i, lap in enumerate(splits_raw.get("lapDTOs", []))
    ]

    notes = summary.get("description") or None
    zone_boundaries_bpm, zone_config_id = _zone_boundaries_and_config_id(hr_zones_raw)
    return ActivityDetail(
        id=str(activity_id),
        date=s.get("startTimeLocal", "")[:10],
        type=activity_type,
        duration_min=_round_or_none(s.get("duration", 0) / 60, 1) if s.get("duration") else None,
        hr_zones=hr_zones,
        zone_boundaries_bpm=zone_boundaries_bpm,
        zone_config_id=zone_config_id,
        splits=splits,
        sets=sets,
        load_lb=_parse_load_lb(summary.get("activityName"), notes),
        temperature_f=_round_or_none((weather or {}).get("temp"), 1),
        humidity_pct=_round_or_none((weather or {}).get("relativeHumidity"), 1),
        notes=notes,
    )


def _week_start(d: date) -> date:
    """Sunday of the week containing d, matching Garmin's own firstDayOfWeek
    profile setting (confirmed via get_userprofile_settings: dayName
    "sunday")."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


def get_zone_summary(days: int = 28) -> ZoneSummary:
    """Weekly HR time-in-zone rollup across all activities in range. There is
    no dedicated zone-config endpoint in this library (checked
    get_userprofile_settings, get_user_profile, get_device_settings -- none
    expose zone bpm boundaries), so zone_boundaries_bpm is read from a real
    activity's own get_activity_hr_in_timezones response instead of a
    profile call, and reports the actual bpm cutoffs observed rather than
    guessing whether they're Garmin's default %HRmax zones or a custom
    (e.g. Karvonen) model -- Garmin's API doesn't say which, so this doesn't
    guess a label.
    """
    client = get_client()
    end = _today()
    start = end - timedelta(days=days - 1)
    raw_activities = _call(client.get_activities_by_date, start.isoformat(), end.isoformat())

    weeks: dict[date, dict] = {}
    boundaries: dict[str, int] | None = None

    for a in raw_activities:
        activity_id = str(a["activityId"])
        activity_date = date.fromisoformat(a["startTimeLocal"][:10])
        week = _week_start(activity_date)
        bucket = weeks.setdefault(
            week, {"z1": 0.0, "z2": 0.0, "z3": 0.0, "z4": 0.0, "z5": 0.0, "count": 0}
        )
        try:
            hr_zones_raw = _call(client.get_activity_hr_in_timezones, activity_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to fetch hr zones for %s: %s", activity_id, e)
            continue
        if not hr_zones_raw:
            continue
        bucket["count"] += 1
        for z in hr_zones_raw:
            zone_num = z.get("zoneNumber")
            if zone_num and 1 <= zone_num <= 5:
                bucket[f"z{zone_num}"] += z.get("secsInZone", 0) / 60
                if boundaries is None:
                    boundaries = {}
                if f"z{zone_num}" not in boundaries and z.get("zoneLowBoundary") is not None:
                    boundaries[f"z{zone_num}"] = z["zoneLowBoundary"]

    result_weeks = []
    for week_start in sorted(weeks):
        b = weeks[week_start]
        total = b["z1"] + b["z2"] + b["z3"] + b["z4"] + b["z5"]
        result_weeks.append(
            ZoneWeek(
                week_start=week_start.isoformat(),
                z1_min=round(b["z1"], 1), z2_min=round(b["z2"], 1),
                z3_min=round(b["z3"], 1), z4_min=round(b["z4"], 1), z5_min=round(b["z5"], 1),
                total_min=round(total, 1),
                z1_pct=round(b["z1"] / total * 100, 1) if total else 0.0,
                z2_pct=round(b["z2"] / total * 100, 1) if total else 0.0,
                z3_pct=round(b["z3"] / total * 100, 1) if total else 0.0,
                z4_pct=round(b["z4"] / total * 100, 1) if total else 0.0,
                z5_pct=round(b["z5"] / total * 100, 1) if total else 0.0,
                activity_count=b["count"],
            )
        )

    return ZoneSummary(weeks=result_weeks, zone_boundaries_bpm=boundaries)


TRIMP_MALE_COEFFICIENT = 1.92  # Banister's exponential weighting constant. No sex field
# exists anywhere in this account's reach, so this is a fixed choice, not a per-user
# setting -- flagged in TrainingLoad.method rather than silently assumed.

MIN_SESSIONS_ACUTE = 2
MIN_SESSIONS_CHRONIC = 4


def _trimp_intensity_fraction(avg_hr: float, resting_hr: float | None, zone_lows: dict[int, int]) -> float | None:
    """0..1 position of avg_hr within this activity's own zone ladder --
    NOT true %HRR from a real HRmax, since Garmin's API exposes no HRmax
    anywhere (confirmed by the same gap get_zone_summary already documents).
    Floor is the day's real resting_hr when we have one (from get_stats),
    else z1's low as a fallback. Ceiling is extrapolated one zone past z5
    using the z4->z5 slope, since Garmin never returns a cap for z5. Result
    is clamped to [0, 1] -- deliberately not letting a few bpm above the
    extrapolated ceiling blow up the exponential term.
    """
    if not zone_lows:
        return None
    floor = resting_hr if resting_hr is not None else zone_lows.get(1)
    if floor is None:
        return None
    z4, z5 = zone_lows.get(4), zone_lows.get(5)
    if z5 is not None and z4 is not None:
        ceiling = z5 + (z5 - z4)
    elif z5 is not None:
        ceiling = z5 * 1.1
    else:
        top_zone = max(zone_lows)
        ceiling = zone_lows[top_zone] * 1.1
    if ceiling <= floor:
        return None
    fraction = (avg_hr - floor) / (ceiling - floor)
    return max(0.0, min(1.0, fraction))


def get_training_load(days: int = 42) -> TrainingLoad:
    """Acute (7d):chronic (28d weekly avg) load from a TRIMP-style figure we
    compute ourselves -- explicitly NOT Garmin/Firstbeat's activityTrainingLoad.
    That field returns null for Tonal-pushed strength_training activities
    (Firstbeat only runs on-device, never against uploaded FIT files -- see
    tonal-garmin-sync investigation, not fixable from this side), which made
    the old Firstbeat-backed version of this tool blind to every strength
    session. This version applies the same duration x HR-intensity formula
    uniformly to every session with avg_hr, regardless of modality.

    Intensity is derived from where avg_hr falls in that session's own
    zone_config_id ladder (see _trimp_intensity_fraction) -- there's no real
    HRmax available from Garmin's API to build a textbook %HRR from, so this
    is the same HR model get_zone_summary already uses, not a third one.
    trimp is null (not zero, not volume-based) for sessions with no avg_hr.

    zone_config_id travels with every point. If the 7d/28d windows being
    compared for `ratio` don't share one config, the sums are still reported
    individually but `ratio` and `ratio_suppressed_reason` flag it rather
    than silently comparing two different HR models. Below
    MIN_SESSIONS_ACUTE/MIN_SESSIONS_CHRONIC real sessions in a window, that
    window's total is null with coverage_note explaining why -- same
    convention as BodyTrend's trend_suppressed_reason.

    No "overreaching"/"optimal" verdict is returned -- raw numbers only,
    interpretation is left to the caller.
    """
    client = get_client()
    end = _today()
    start = end - timedelta(days=days - 1)
    raw_activities = _call(client.get_activities_by_date, start.isoformat(), end.isoformat())

    resting_hr_by_date: dict[str, float | None] = {}

    def _resting_hr_for(day_str: str) -> float | None:
        if day_str not in resting_hr_by_date:
            try:
                stats = _call(client.get_stats, day_str)
                resting_hr_by_date[day_str] = stats.get("restingHeartRate")
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed to fetch resting HR for %s: %s", day_str, e)
                resting_hr_by_date[day_str] = None
        return resting_hr_by_date[day_str]

    points: list[TrainingLoadPoint] = []
    missing_hr_count = 0

    for a in raw_activities:
        activity_id = str(a["activityId"])
        activity_type = a.get("activityType", {}).get("typeKey", "unknown")
        activity_date = a["startTimeLocal"][:10]
        avg_hr = a.get("averageHR")
        duration_min = a.get("duration", 0) / 60 if a.get("duration") else None

        if avg_hr is None or duration_min is None:
            missing_hr_count += 1
            points.append(
                TrainingLoadPoint(
                    date=activity_date, activity_id=activity_id, activity_type=activity_type,
                    trimp=None, zone_config_id=None,
                )
            )
            continue

        try:
            hr_zones_raw = _call(client.get_activity_hr_in_timezones, activity_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to fetch hr zones for %s: %s", activity_id, e)
            hr_zones_raw = []

        zone_boundaries_bpm, zone_config_id = _zone_boundaries_and_config_id(hr_zones_raw)
        zone_lows = {
            int(k[1:]): v[0] for k, v in (zone_boundaries_bpm or {}).items() if v and v[0] is not None
        }
        resting_hr = _resting_hr_for(activity_date)
        fraction = _trimp_intensity_fraction(avg_hr, resting_hr, zone_lows)

        trimp = None
        if fraction is not None:
            trimp = round(duration_min * fraction * math.exp(TRIMP_MALE_COEFFICIENT * fraction), 1)

        points.append(
            TrainingLoadPoint(
                date=activity_date, activity_id=activity_id, activity_type=activity_type,
                trimp=trimp, zone_config_id=zone_config_id,
            )
        )
    points.sort(key=lambda p: p["date"])

    acute_cutoff = (end - timedelta(days=6)).isoformat()
    chronic_cutoff = (end - timedelta(days=27)).isoformat()
    acute_points = [p for p in points if p["date"] >= acute_cutoff and p["trimp"] is not None]
    chronic_points = [p for p in points if p["date"] >= chronic_cutoff and p["trimp"] is not None]

    acute_7d = None
    if len(acute_points) >= MIN_SESSIONS_ACUTE:
        acute_7d = round(sum(p["trimp"] for p in acute_points), 1)

    chronic_28d = None
    if len(chronic_points) >= MIN_SESSIONS_CHRONIC:
        chronic_28d = round(sum(p["trimp"] for p in chronic_points) / 4, 1)

    ratio = None
    ratio_suppressed_reason = None
    if acute_7d is None:
        ratio_suppressed_reason = f"fewer than {MIN_SESSIONS_ACUTE} sessions with HR data in the last 7 days"
    elif chronic_28d is None:
        ratio_suppressed_reason = f"fewer than {MIN_SESSIONS_CHRONIC} sessions with HR data in the last 28 days"
    else:
        config_ids = {p["zone_config_id"] for p in (acute_points + chronic_points) if p["zone_config_id"]}
        if len(config_ids) > 1:
            ratio_suppressed_reason = (
                f"acute/chronic windows span more than one zone config ({', '.join(sorted(config_ids))}) "
                "-- not directly comparable"
            )
        else:
            ratio = round(acute_7d / chronic_28d, 2) if chronic_28d else None

    coverage_note = None
    if missing_hr_count:
        coverage_note = (
            f"{missing_hr_count} session(s) had no avg_hr and are excluded (trimp: null) -- "
            "load is understated to that extent, not zero."
        )

    return TrainingLoad(
        method="trimp_zone_relative_v1 (independent calculation -- not Garmin/Firstbeat Training Load)",
        acute_7d=acute_7d,
        chronic_28d=chronic_28d,
        ratio=ratio,
        ratio_suppressed_reason=ratio_suppressed_reason,
        points=points,
        sessions_missing_hr=missing_hr_count,
        coverage_note=coverage_note,
    )


def _local_epoch_ms_to_iso(ms) -> str | None:
    """Garmin's *Local timestamp fields are epoch milliseconds that already
    have the local UTC offset baked in -- confirmed against a night where
    both GMT and Local variants were present (exactly a 4h/14400000ms EDT
    offset apart). Interpreting the Local value as UTC (not converting again
    via TZ) yields the correct local wall-clock time.
    """
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None).isoformat()


def _shape_sleep_night(day: date, raw: dict | None) -> SleepNight:
    dto = (raw or {}).get("dailySleepDTO") or {}
    if not dto:
        return SleepNight(
            date=day.isoformat(), total_hr=None, deep_min=None, light_min=None,
            rem_min=None, awake_min=None, score=None, resting_hr=None,
            sleep_start=None, sleep_end=None, respiration_avg=None,
            respiration_low=None, spo2_avg=None, spo2_low=None, restless_moments=None,
        )
    overall = dto.get("sleepScores", {}).get("overall", {})
    total_seconds = dto.get("sleepTimeSeconds")
    return SleepNight(
        date=day.isoformat(),
        total_hr=_round_or_none(total_seconds / 3600, 2) if total_seconds else None,
        deep_min=_round_or_none(dto.get("deepSleepSeconds", 0) / 60) if dto.get("deepSleepSeconds") is not None else None,
        light_min=_round_or_none(dto.get("lightSleepSeconds", 0) / 60) if dto.get("lightSleepSeconds") is not None else None,
        rem_min=_round_or_none(dto.get("remSleepSeconds", 0) / 60) if dto.get("remSleepSeconds") is not None else None,
        awake_min=_round_or_none(dto.get("awakeSleepSeconds", 0) / 60) if dto.get("awakeSleepSeconds") is not None else None,
        score=_round_or_none(overall.get("value")),
        resting_hr=_round_or_none((raw or {}).get("restingHeartRate")),
        sleep_start=_local_epoch_ms_to_iso(dto.get("sleepStartTimestampLocal")),
        sleep_end=_local_epoch_ms_to_iso(dto.get("sleepEndTimestampLocal")),
        respiration_avg=_round_or_none(dto.get("averageRespirationValue"), 1),
        respiration_low=_round_or_none(dto.get("lowestRespirationValue"), 1),
        spo2_avg=_round_or_none(dto.get("averageSpO2Value"), 1),
        spo2_low=_round_or_none(dto.get("lowestSpO2Value"), 1),
        restless_moments=_round_or_none(raw.get("restlessMomentsCount")),
    )


def get_sleep(days: int = 7) -> list[SleepNight]:
    client = get_client()
    end = _today()
    nights = []
    for i in range(days):
        day = end - timedelta(days=i)
        raw = _call(client.get_sleep_data, day.isoformat())
        nights.append(_shape_sleep_night(day, raw))
    return list(reversed(nights))


def _shape_daily_stats(day: date, stats: dict, hrv: dict | None) -> DailyStats:
    hrv_summary = (hrv or {}).get("hrvSummary", {})
    return DailyStats(
        date=day.isoformat(),
        hrv_ms=_round_or_none(hrv_summary.get("lastNightAvg")),
        hrv_highest_5min=_round_or_none(hrv_summary.get("lastNight5MinHigh")),
        hrv_7d_baseline=_round_or_none(hrv_summary.get("weeklyAvg"), 1),
        resting_hr=_round_or_none(stats.get("restingHeartRate")),
        body_battery_high=_round_or_none(stats.get("bodyBatteryHighestValue")),
        body_battery_low=_round_or_none(stats.get("bodyBatteryLowestValue")),
        steps=_round_or_none(stats.get("totalSteps")),
        active_calories=_round_or_none(stats.get("activeKilocalories")),
        stress_avg=_round_or_none(stats.get("averageStressLevel")),
        stress_rest_pct=_round_or_none(stats.get("restStressPercentage"), 1),
        stress_low_pct=_round_or_none(stats.get("lowStressPercentage"), 1),
        stress_medium_pct=_round_or_none(stats.get("mediumStressPercentage"), 1),
        stress_high_pct=_round_or_none(stats.get("highStressPercentage"), 1),
    )


def get_daily_stats(days: int = 7) -> list[DailyStats]:
    client = get_client()
    end = _today()
    out = []
    for i in range(days):
        day = end - timedelta(days=i)
        stats = _call(client.get_stats, day.isoformat())
        hrv = _call(client.get_hrv_data, day.isoformat())
        out.append(_shape_daily_stats(day, stats, hrv))
    return list(reversed(out))


def get_vo2max(days: int = 90) -> Vo2MaxTrend:
    """Garmin only recomputes VO2max after qualifying activities (sustained
    outdoor running/walking with HR + GPS), so most days have no new
    measurement. Garmin's endpoint returns the most recently known value
    regardless of which date is queried in between real measurements (a
    behavior documented against this library: python-garminconnect#74) --
    rather than trust the requested date, this dedupes on the response's own
    `generic.calendarDate`, so only genuine new measurements produce a point.
    """
    client = get_client()
    end = _today()
    seen_dates: set[str] = set()
    points: list[dict] = []
    for i in range(days):
        day = end - timedelta(days=i)
        raw = _call(client.get_max_metrics, day.isoformat())
        entry = (raw[0] if raw else None) if isinstance(raw, list) else raw
        if not entry:
            continue
        generic = entry.get("generic") or {}
        cal_date = generic.get("calendarDate")
        vo2 = generic.get("vo2MaxValue")
        if cal_date and vo2 is not None and cal_date not in seen_dates:
            seen_dates.add(cal_date)
            points.append({"date": cal_date, "vo2max": vo2})
    points.sort(key=lambda p: p["date"])

    current = points[-1]["vo2max"] if points else None
    change_28d = None
    if len(points) >= 2:
        cutoff = (end - timedelta(days=28)).isoformat()
        recent = [p for p in points if p["date"] >= cutoff]
        if len(recent) >= 2:
            change_28d = round(recent[-1]["vo2max"] - recent[0]["vo2max"], 1)

    return Vo2MaxTrend(
        points=points, current=current, change_28d=change_28d, measurement_count=len(points)
    )


def get_body_trend(days: int = 30) -> BodyTrend:
    """Reads the local GarminDB-synced SQLite database, not the live Garmin
    API -- see store.py. Requires scripts/sync_garmindb.py to have run at
    least once.
    """
    try:
        return store.get_body_trend(days=days)
    except sqlite3.OperationalError as e:
        raise TrendDataUnavailableError(
            f"No local trend database found ({e}). Run scripts/sync_garmindb.py first."
        ) from e


def get_activity_trend(days: int = 90) -> ActivityTrend:
    """Reads the local GarminDB-synced database (garmin_summary.db's
    days_summary rollup table), not the live Garmin API. Coverage will be
    thin until the sync has been running for a while -- see coverage_days.
    """
    try:
        return store.get_activity_trend(days=days)
    except sqlite3.OperationalError as e:
        raise TrendDataUnavailableError(
            f"No local trend database found ({e}). Run scripts/sync_garmindb.py first."
        ) from e


def get_hydration(days: int = 30) -> HydrationTrend:
    """Reads the local GarminDB-synced database (garmin_summary.db's
    days_summary), not the live API.
    """
    try:
        return store.get_hydration_trend(days=days)
    except sqlite3.OperationalError as e:
        raise TrendDataUnavailableError(
            f"No local trend database found ({e}). Run scripts/sync_garmindb.py first."
        ) from e


def get_readiness() -> Readiness:
    client = get_client()
    today = _today()

    stats = _call(client.get_stats, today.isoformat())
    hrv = _call(client.get_hrv_data, today.isoformat())
    hrv_summary = (hrv or {}).get("hrvSummary", {})

    sleep_raw = _call(client.get_sleep_data, today.isoformat())
    if not (sleep_raw or {}).get("dailySleepDTO"):
        sleep_raw = _call(client.get_sleep_data, (today - timedelta(days=1)).isoformat())
    sleep_score = _round_or_none(
        (sleep_raw or {}).get("dailySleepDTO", {}).get("sleepScores", {}).get("overall", {}).get("value")
    )

    recent = get_activities(days=14)
    recent_sorted = sorted(recent, key=lambda a: a["date"], reverse=True)
    last_3 = recent_sorted[:3]

    active_dates = {a["date"] for a in recent}
    days_since_rest = None
    for offset in range(14):
        day = (today - timedelta(days=offset)).isoformat()
        if day not in active_dates:
            days_since_rest = offset
            break

    return Readiness(
        date=today.isoformat(),
        hrv_ms=_round_or_none(hrv_summary.get("lastNightAvg")),
        hrv_7d_baseline=_round_or_none(hrv_summary.get("weeklyAvg"), 1),
        resting_hr=_round_or_none(stats.get("restingHeartRate")),
        rhr_7d_baseline=_round_or_none(stats.get("lastSevenDaysAvgRestingHeartRate"), 1),
        sleep_score=sleep_score,
        body_battery=_round_or_none(stats.get("bodyBatteryMostRecentValue")),
        last_3_activities=last_3,
        days_since_rest=days_since_rest,
    )


def _delete_weigh_ins_for(client: Garmin, day: date) -> int:
    existing = _call(client.get_daily_weigh_ins, day.isoformat())
    deleted = 0
    for entry in (existing or {}).get("dateWeightList", []):
        pk = entry.get("samplePk")
        if pk is not None:
            _call(client.delete_weigh_in, weight_pk=str(pk), cdate=day.isoformat())
            deleted += 1
    return deleted


def delete_weight(date_str: str | None = None) -> DeleteWeightResult:
    """Delete any weigh-in(s) logged for a date. Defaults to today."""
    client = get_client()
    day = date.fromisoformat(date_str) if date_str else _today()
    deleted = _delete_weigh_ins_for(client, day)
    return DeleteWeightResult(ok=True, date=day.isoformat(), deleted_count=deleted)


def log_weight(weight_lb: float, date_str: str | None = None) -> LogWeightResult:
    """Write a weigh-in to Garmin Connect. Idempotent per date: any existing
    weigh-in(s) for that date are deleted first, so repeated calls replace
    rather than duplicate. unitKey="lbs" is passed straight through -- this
    garminconnect version accepts lbs natively and converts server-side, so
    no manual kg conversion is needed here.
    """
    if not (MIN_WEIGHT_LB <= weight_lb <= MAX_WEIGHT_LB):
        raise InvalidWeightError(
            f"weight_lb must be between {MIN_WEIGHT_LB} and {MAX_WEIGHT_LB}, got {weight_lb}"
        )

    client = get_client()
    day = date.fromisoformat(date_str) if date_str else _today()

    _delete_weigh_ins_for(client, day)

    local_noon = datetime.combine(day, dtime(12, 0), tzinfo=TZ)
    result = _call(
        client.add_weigh_in,
        weight=weight_lb,
        unitKey="lbs",
        timestamp=local_noon.isoformat(),
    )

    return LogWeightResult(
        ok=True,
        date=day.isoformat(),
        weight_lb=weight_lb,
        garmin_response_status=204 if result is None else 200,
    )


def get_bp_trend(days: int = 90) -> BpTrend:
    """Live API, single range call (get_blood_pressure accepts a date range
    directly, unlike weigh-ins). Deliberately drops Garmin's own
    category/categoryName clinical classification (e.g. "STAGE_1_HIGH") from
    every reading -- confirmed live that Garmin auto-classifies BP into
    hypertension stages, which is exactly the kind of interpretation this
    server doesn't emit; only raw systolic/diastolic/pulse are returned.
    """
    client = get_client()
    end = _today()
    start = end - timedelta(days=days - 1)

    raw = _call(client.get_blood_pressure, start.isoformat(), end.isoformat())
    points: list[BpReading] = []
    for day_summary in (raw or {}).get("measurementSummaries", []):
        for m in day_summary.get("measurements", []):
            ts = m.get("measurementTimestampLocal", "")
            if not ts:
                continue
            points.append(
                BpReading(
                    date=ts[:10],
                    systolic=m["systolic"],
                    diastolic=m["diastolic"],
                    pulse=m.get("pulse"),
                )
            )
    points.sort(key=lambda p: p["date"])

    def _avg_since(field: str, cutoff: date) -> float | None:
        window = [p[field] for p in points if date.fromisoformat(p["date"]) >= cutoff]
        return round(mean(window), 1) if window else None

    cutoff_7d = end - timedelta(days=6)
    systolic_avg_7d = _avg_since("systolic", cutoff_7d)
    diastolic_avg_7d = _avg_since("diastolic", cutoff_7d)

    dates = [date.fromisoformat(p["date"]) for p in points]
    days_spanned = (dates[-1] - dates[0]).days + 1 if dates else None
    largest_gap_days = max((b - a).days for a, b in zip(dates, dates[1:])) if len(dates) >= 2 else None

    return BpTrend(
        points=points,
        systolic_avg_7d=systolic_avg_7d,
        diastolic_avg_7d=diastolic_avg_7d,
        coverage=BodyTrendCoverage(
            point_count=len(points), days_spanned=days_spanned, largest_gap_days=largest_gap_days
        ),
    )


def log_bp(systolic: int, diastolic: int, pulse: int, date_str: str | None = None, notes: str | None = None) -> LogBpResult:
    """Log a blood pressure reading. pulse is required -- garminconnect's
    set_blood_pressure validates it as an int in [20, 250] with no default,
    so there's no way to write a reading without one despite the feature
    request's optional signature; the Microlife MAM device this is read from
    reports pulse alongside systolic/diastolic anyway.
    """
    client = get_client()
    day = date.fromisoformat(date_str) if date_str else _today()
    local_noon = datetime.combine(day, dtime(12, 0), tzinfo=TZ)
    _call(
        client.set_blood_pressure,
        systolic, diastolic, pulse,
        timestamp=local_noon.isoformat(),
        notes=notes or "",
    )
    return LogBpResult(ok=True, date=day.isoformat(), systolic=systolic, diastolic=diastolic, pulse=pulse)


def delete_bp(date_str: str | None = None) -> DeleteBpResult:
    """Delete any BP reading(s) logged for a date. Defaults to today."""
    client = get_client()
    day = date.fromisoformat(date_str) if date_str else _today()
    raw = _call(client.get_blood_pressure, day.isoformat(), day.isoformat())
    deleted = 0
    for day_summary in (raw or {}).get("measurementSummaries", []):
        for m in day_summary.get("measurements", []):
            version = m.get("version")
            if version is not None:
                _call(client.delete_blood_pressure, version=str(version), cdate=day.isoformat())
                deleted += 1
    return DeleteBpResult(ok=True, date=day.isoformat(), deleted_count=deleted)


def log_hydration(ml: float, date_str: str | None = None) -> LogHydrationResult:
    """Add a hydration entry. Unlike log_weight/log_bp, this is ADDITIVE, not
    a replace -- confirmed from garminconnect's add_hydration_data source:
    it accepts negative values explicitly "for subtraction", meaning Garmin
    Connect itself treats hydration as a running daily total you add entries
    to throughout the day, not a single point-in-time reading. Calling this
    twice logs two separate drinks, it does not overwrite the first.
    """
    client = get_client()
    day = date.fromisoformat(date_str) if date_str else _today()
    result = _call(client.add_hydration_data, value_in_ml=ml, cdate=day.isoformat())
    new_total = (result or {}).get("valueInML")
    return LogHydrationResult(ok=True, date=day.isoformat(), added_ml=ml, new_total_ml=new_total)


def delete_hydration(date_str: str | None = None) -> DeleteHydrationResult:
    """Clear a day's hydration total. There is no delete endpoint for
    hydration (confirmed: garminconnect exposes only add_hydration_data /
    get_hydration_data) -- this reads the current total and adds its exact
    negation, which is the only way to zero it out via the real API.
    """
    client = get_client()
    day = date.fromisoformat(date_str) if date_str else _today()
    current = _call(client.get_hydration_data, day.isoformat())
    current_ml = (current or {}).get("valueInML") or 0
    if current_ml:
        _call(client.add_hydration_data, value_in_ml=-current_ml, cdate=day.isoformat())
    return DeleteHydrationResult(ok=True, date=day.isoformat(), cleared_ml=current_ml)
