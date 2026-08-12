"""M1 gate: prove the Garmin read/write path works end to end.

Usage:  python scripts/smoke.py [tokenstore_dir] [--write]

Using persisted tokens only (no password/MFA prompt):
  1. Fetches yesterday's sleep summary.
  2. Fetches the last 7 days of activities.
  3. With --write: writes one test weigh-in (real data in your Garmin
     account) and deletes it again immediately. Omitted by default so
     running this script doesn't silently mutate your account.

Prints what it did so the result can be checked against Garmin Connect.
"""

import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from garminconnect import Garmin

TZ = ZoneInfo("America/New_York")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    do_write = "--write" in sys.argv[1:]

    tokenstore = args[0] if args else os.environ.get("GARMIN_TOKEN_DIR")
    if not tokenstore:
        print(
            "usage: python scripts/smoke.py <tokenstore_dir> [--write]\n"
            "(or set GARMIN_TOKEN_DIR in the environment)",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        client = Garmin()
        client.login(tokenstore=tokenstore)
    except Exception as e:  # noqa: BLE001
        print(f"Login failed: {e}", file=sys.stderr)
        print("Tokens may be expired. Re-run scripts/login.py.", file=sys.stderr)
        sys.exit(3)

    today = datetime.now(TZ).date()
    yesterday = today - timedelta(days=1)
    week_ago = today - timedelta(days=6)

    print(f"== Sleep for {yesterday} ==")
    sleep = client.get_sleep_data(yesterday.isoformat())
    dto = (sleep or {}).get("dailySleepDTO") or {}
    print(f"  sleepTimeSeconds: {dto.get('sleepTimeSeconds')}")
    print(f"  deepSleepSeconds: {dto.get('deepSleepSeconds')}")
    print(f"  lightSleepSeconds: {dto.get('lightSleepSeconds')}")
    print(f"  remSleepSeconds: {dto.get('remSleepSeconds')}")
    print(f"  awakeSleepSeconds: {dto.get('awakeSleepSeconds')}")

    print(f"\n== Activities {week_ago} .. {today} ==")
    activities = client.get_activities_by_date(week_ago.isoformat(), today.isoformat())
    print(f"  count: {len(activities)}")
    for a in activities:
        print(f"  - {a.get('startTimeLocal')}  {a.get('activityType', {}).get('typeKey')}  "
              f"{a.get('activityName')}")

    if do_write:
        print("\n== Writing test weigh-in ==")
        test_weight_lb = 180.0
        result = client.add_weigh_in(weight=test_weight_lb, unitKey="lbs")
        print(f"  wrote {test_weight_lb} lbs -> {result}")

        pk = None
        if isinstance(result, dict):
            pk = result.get("samplePk") or result.get("weightPk") or result.get("pk")
        if pk is None:
            # Garmin's weigh-in "weight" field is grams.
            for entry in client.get_daily_weigh_ins(today.isoformat()).get("dateWeightList", []):
                weight_grams = entry.get("weight")
                if weight_grams and abs(weight_grams / 453.59237 - test_weight_lb) < 1:
                    pk = entry.get("samplePk")
                    break

        if pk is not None:
            client.delete_weigh_in(weight_pk=str(pk), cdate=today.isoformat())
            print(f"  deleted test weigh-in (samplePk={pk})")
        else:
            print(
                "  WARNING: could not find the test weigh-in's samplePk to delete it.\n"
                "  Check Garmin Connect and remove the 180.0 lb entry for today by hand.",
                file=sys.stderr,
            )
    else:
        print("\n(skipping weigh-in write -- pass --write to exercise it)")

    print("\nM1 smoke test complete. Verify the data above against Garmin Connect.")


if __name__ == "__main__":
    main()
