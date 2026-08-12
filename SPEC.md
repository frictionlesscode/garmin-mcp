# garmin-mcp — Build Spec

A remote MCP server exposing Garmin Connect data and writes to Claude, reachable from mobile.

---

## Objective

Claude (via the mobile app) acts as a training and nutrition coach. It reads Garmin activity/sleep/HRV data and writes weight back. The server is the data plane only — no coaching logic lives here.

## Locked decisions — do not re-litigate

| Decision | Choice |
|---|---|
| Language | Python 3.11+ |
| Garmin client | `garminconnect` (cyberjunky), which uses `garth` for OAuth |
| MCP framework | FastMCP, **Streamable HTTP transport** (not SSE — SSE is being deprecated) |
| Local store | SQLite |
| Historical data | GarminDB, nightly cron |
| Packaging | Docker, bound to `127.0.0.1:8080` only |
| Public exposure | Cloudflare Tunnel (configured outside this repo) |
| Auth | In-server, on the `authorization` header |
| Host | Owner's always-on PC |

## Non-goals

- No waist/circumference storage. Garmin doesn't support it; the owner keeps it in conversation.
- No nutrition/macro logging in v1.
- No coaching logic, thresholds, or programming rules. Those live in a separate Claude Skill.
- No web UI.
- No multi-user support. Single user, single Garmin account.

---

## Critical instructions

**Verify library APIs against the installed package, not from memory.** `python-garminconnect` changes method names and signatures between releases. Before writing any call, inspect the installed version (`python -c "import garminconnect; help(garminconnect.Garmin)"` or read the source in site-packages). Do not guess method names.

**The owner must perform the initial Garmin login interactively** — it requires MFA. Build a one-shot `scripts/login.py` that prompts, authenticates, and writes the garth token store. Do not attempt to automate MFA.

**Never commit secrets.** The garth token directory, `.env`, and the SQLite file are all gitignored. No credentials in code, no credentials in Docker images.

**Build to the milestone gates below.** Stop at each gate and report. Do not build milestones 2–5 before milestone 1 is verified working, because milestone 1 is where all the real risk is.

---

## Milestones

### M1 — Prove the Garmin path (do this first, alone)

A standalone script, no MCP, no Docker.

1. `scripts/login.py` — interactive garth login with MFA, persists token store to a configurable path.
2. `scripts/smoke.py` — using the persisted tokens: fetch yesterday's sleep summary, fetch the last 7 days of activities, and write one test weigh-in.

**Gate:** the test weigh-in appears in Garmin Connect and the reads return real data. Report the exact library version and method names that worked.

### M2 — MCP server, read tools only

FastMCP server over Streamable HTTP. Implement the read tools below. No auth yet, no Docker yet.

**Gate:** all read tools pass against MCP Inspector locally.

### M3 — Auth + Docker

- Bearer token middleware validating the `authorization` header against a value from env. Constant-time comparison. Reject with 401 on missing/bad token.
- Dockerfile + compose. Container binds `127.0.0.1:8080` only.
- Health endpoint at `/health`, unauthenticated, returning version and Garmin token expiry.

**Gate:** container runs; authenticated request succeeds; unauthenticated request returns 401.

### M4 — Write tools

Add `log_weight`. Idempotency: if a weigh-in already exists for that date, update rather than duplicate.

**Gate:** weight logged through the tool appears in Garmin Connect.

### M5 — GarminDB + trend tools

Nightly cron syncing GarminDB into SQLite. Trend tools read SQL, not the live API.

**Gate:** `get_body_trend(90)` returns in under a second.

---

## Tool contracts

All weights in **pounds** at the tool boundary; convert to kg internally (Garmin's API expects kg). All dates ISO `YYYY-MM-DD`. All times in `America/New_York`.

Every tool returns a compact JSON object. Prefer few fields with high signal over dumping raw Garmin responses — the consumer is an LLM context window, not a dashboard.

### Reads

```
get_activities(days: int = 7, activity_type: str | None = None)
  -> [{id, date, type, duration_min, distance_mi, avg_hr, max_hr, calories, training_effect}]

get_activity_detail(activity_id: str)
  -> {id, date, type, duration_min, hr_zones: {z1..z5 minutes}, splits: [...], notes}

get_sleep(days: int = 7)
  -> [{date, total_hr, deep_min, light_min, rem_min, awake_min, score, resting_hr}]

get_daily_stats(days: int = 7)
  -> [{date, hrv_ms, resting_hr, body_battery_high, body_battery_low, steps, active_calories, stress_avg}]

get_body_trend(days: int = 30)
  -> {points: [{date, weight_lb}], avg_7d, avg_28d, trend_lb_per_week}

get_readiness()
  -> {date, hrv_ms, hrv_7d_baseline, resting_hr, rhr_7d_baseline, sleep_score,
      body_battery, last_3_activities: [...], days_since_rest}
```

`get_readiness` is a composite convenience call so the Skill can answer "how should I train today" in one round trip. It reports data only — no recommendation, no readiness score of our own invention.

### Writes

```
log_weight(weight_lb: float, date: str | None = None)
  -> {ok, date, weight_lb, garmin_response_status}
```

Default date is today. Validate range 80–500 lb and reject outside it.

---

## Repo structure

```
garmin-mcp/
├── src/garmin_mcp/
│   ├── server.py        # FastMCP app, tool registration
│   ├── auth.py          # bearer middleware
│   ├── garmin.py        # thin wrapper over garminconnect
│   ├── store.py         # SQLite access
│   └── models.py        # return shapes
├── scripts/
│   ├── login.py
│   ├── smoke.py
│   └── sync_garmindb.py
├── tests/
├── Dockerfile
├── compose.yml
├── .env.example
└── README.md
```

## Config (env)

```
GARMIN_TOKEN_DIR=/data/garth
MCP_BEARER_TOKEN=
SQLITE_PATH=/data/garmin.db
TZ=America/New_York
LOG_LEVEL=INFO
```

## Error handling

Garmin's unofficial API fails in ways that matter:

- **401 / expired tokens** — return a clear, actionable error telling the user to re-run `scripts/login.py`. Don't retry into a lockout.
- **429 rate limiting** — exponential backoff, and surface it rather than silently returning empty data.
- **Empty vs. missing** — a day with no sleep data must return `null`, never a zero. Zeros silently corrupt trend math.

Never return a fabricated or interpolated value. If data is missing, say so.

## Testing

Unit tests with the Garmin client mocked. One integration test, marked and skipped by default, that runs against the real account read-only.
