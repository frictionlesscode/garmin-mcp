"""Return shapes for garmin-mcp tools. Compact by design -- the consumer is an
LLM context window, not a dashboard. Fields are None (never 0) when Garmin has
no data for that slot.
"""

from typing import TypedDict


class Activity(TypedDict):
    id: str
    date: str
    type: str
    duration_min: float | None
    distance_mi: float | None
    avg_hr: int | None
    max_hr: int | None
    calories: int | None
    training_effect: float | None
    possible_duplicate_of: str | None


class HrZones(TypedDict):
    z1: float | None
    z2: float | None
    z3: float | None
    z4: float | None
    z5: float | None


class Split(TypedDict):
    lap: int
    duration_min: float | None
    distance_mi: float | None
    avg_hr: int | None


class ExerciseSet(TypedDict):
    exercise: str
    reps: int | None
    weight_lb: float | None
    duration_sec: float | None


class ActivityDetail(TypedDict):
    id: str
    date: str
    type: str
    duration_min: float | None
    hr_zones: HrZones
    zone_boundaries_bpm: dict[str, list[int | None]] | None
    zone_config_id: str | None
    splits: list[Split]
    sets: list[ExerciseSet]
    load_lb: float | None
    temperature_f: float | None
    humidity_pct: float | None
    notes: str | None


class SleepNight(TypedDict):
    date: str
    total_hr: float | None
    deep_min: float | None
    light_min: float | None
    rem_min: float | None
    awake_min: float | None
    score: int | None
    resting_hr: int | None
    sleep_start: str | None
    sleep_end: str | None
    respiration_avg: float | None
    respiration_low: float | None
    spo2_avg: float | None
    spo2_low: float | None
    restless_moments: int | None


class DailyStats(TypedDict):
    date: str
    hrv_ms: int | None
    hrv_highest_5min: int | None
    hrv_7d_baseline: float | None
    resting_hr: int | None
    body_battery_high: int | None
    body_battery_low: int | None
    steps: int | None
    active_calories: int | None
    stress_avg: int | None
    stress_rest_pct: float | None
    stress_low_pct: float | None
    stress_medium_pct: float | None
    stress_high_pct: float | None


class BodyTrendPoint(TypedDict):
    date: str
    weight_lb: float


class BodyTrendCoverage(TypedDict):
    point_count: int
    days_spanned: int | None
    largest_gap_days: int | None


class BodyTrend(TypedDict):
    points: list[BodyTrendPoint]
    avg_7d: float | None
    avg_28d: float | None
    trend_lb_per_week: float | None
    trend_suppressed_reason: str | None
    coverage: BodyTrendCoverage


class ActivityTrendPoint(TypedDict):
    date: str
    steps: int | None
    active_calories: int | None
    intensity_minutes: float | None
    floors: float | None
    is_training_day: bool


class ActivityTrend(TypedDict):
    points: list[ActivityTrendPoint]
    steps_avg_7d: float | None
    steps_avg_28d: float | None
    steps_trend_per_week: float | None
    training_day_avg_steps: float | None
    rest_day_avg_steps: float | None
    coverage_days: int


class Vo2MaxPoint(TypedDict):
    date: str
    vo2max: float


class Vo2MaxTrend(TypedDict):
    points: list[Vo2MaxPoint]
    current: float | None
    change_28d: float | None
    measurement_count: int


class ZoneWeek(TypedDict):
    week_start: str
    z1_min: float
    z2_min: float
    z3_min: float
    z4_min: float
    z5_min: float
    total_min: float
    z1_pct: float
    z2_pct: float
    z3_pct: float
    z4_pct: float
    z5_pct: float
    activity_count: int


class ZoneSummary(TypedDict):
    weeks: list[ZoneWeek]
    zone_boundaries_bpm: dict[str, int] | None


class HydrationPoint(TypedDict):
    date: str
    ml: float | None
    goal_ml: float | None
    sweat_loss_ml: float | None
    pct_of_goal: float | None


class HydrationTrend(TypedDict):
    points: list[HydrationPoint]
    avg_7d_ml: float | None
    days_below_goal: int
    coverage_days: int


class LogHydrationResult(TypedDict):
    ok: bool
    date: str
    added_ml: float
    new_total_ml: float | None


class TrainingLoadPoint(TypedDict):
    date: str
    activity_id: str
    activity_type: str
    trimp: float | None
    zone_config_id: str | None


class TrainingLoad(TypedDict):
    method: str
    acute_7d: float | None
    chronic_28d: float | None
    ratio: float | None
    ratio_suppressed_reason: str | None
    points: list[TrainingLoadPoint]
    sessions_missing_hr: int
    coverage_note: str | None


class DeleteHydrationResult(TypedDict):
    ok: bool
    date: str
    cleared_ml: float


class BpReading(TypedDict):
    date: str
    systolic: int
    diastolic: int
    pulse: int | None


class BpTrend(TypedDict):
    points: list[BpReading]
    systolic_avg_7d: float | None
    diastolic_avg_7d: float | None
    coverage: BodyTrendCoverage


class LogBpResult(TypedDict):
    ok: bool
    date: str
    systolic: int
    diastolic: int
    pulse: int


class DeleteBpResult(TypedDict):
    ok: bool
    date: str
    deleted_count: int


class Readiness(TypedDict):
    date: str
    hrv_ms: int | None
    hrv_7d_baseline: float | None
    resting_hr: int | None
    rhr_7d_baseline: float | None
    sleep_score: int | None
    body_battery: int | None
    last_3_activities: list[Activity]
    days_since_rest: int | None


class LogWeightResult(TypedDict):
    ok: bool
    date: str
    weight_lb: float
    garmin_response_status: int


class DeleteWeightResult(TypedDict):
    ok: bool
    date: str
    deleted_count: int
