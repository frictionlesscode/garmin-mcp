# garmin-mcp

> **Unofficial and unaffiliated.** This project is not affiliated with, endorsed by, or supported by Garmin. It reaches Garmin Connect through an unofficial API client that can stop working whenever Garmin changes their site. You run it against your own account, at your own risk.

A self-hosted [MCP](https://modelcontextprotocol.io) server that gives Claude read access to your Garmin Connect account — activities, sleep, HRV, body-weight trend, HR zones, blood pressure, hydration — plus a small write path (weight/BP/hydration logging) and an independent training-load calculation for the sessions Garmin's own Firstbeat pipeline can't cover.

It's the data plane only. No coaching logic, no thresholds, no "you should train today" verdicts live here — those belong in a separate Claude Skill (see [garmin-coach](../garmin-coach) if you're using this alongside it). This server's job is to hand back real numbers, cleanly labeled, with nulls where the data genuinely isn't there.

## Benefits

- **Ask Claude about your training in plain language** — "how'd my sleep look this week," "log today's weight," "what's my HRV trend" — without opening the Garmin Connect app.
- **Fixes real Garmin/Firstbeat gaps, doesn't paper over them.** Garmin's Training Effect and Training Load are computed on-device and never recalculated for externally-uploaded activities (Zwift, TrainerRoad, Tonal-to-Garmin pipelines, etc.) — this project ships its own TRIMP-based training-load calculation that covers those sessions too, and every tool that does this kind of substitution says so explicitly in its output rather than silently presenting a number as Garmin's own.
- **No fabricated data.** Missing HRV, missing training effect, too few data points for a trend — these come back as `null` with a stated reason, never a guessed or interpolated number.
- **Self-hosted, single-user.** Runs on your own machine in Docker; only you hold the Garmin login and the server's own token. Nothing about your data passes through a third-party service beyond Garmin's own API and (optionally) Claude.
- **Read is separate from write.** Nothing writes to your Garmin account unless a tool is explicitly a write tool (`log_weight`, `log_bp`, `log_hydration`, and their `delete_*` counterparts) — and those are documented as such.

## Tools

**Reads:** `get_activities`, `get_activity_detail`, `get_activity_trend`, `get_sleep`, `get_daily_stats`, `get_readiness`, `get_body_trend`, `get_vo2max`, `get_zone_summary`, `get_training_load`, `get_bp_trend`, `get_hydration`.

**Writes:** `log_weight`, `log_bp` / `delete_bp`, `log_hydration` / `delete_hydration`.

Every tool's docstring documents where its numbers come from, what's genuinely missing vs. suppressed for being too sparse to trust, and (for `get_training_load`) that the figure is this project's own calculation, not Garmin's.

## Setup

### Prerequisites

- Python 3.11+, Docker, a Garmin Connect account (your own login — MFA is fine, see below).
- Somewhere to run this that can stay online (a home server, NAS, always-on PC). This is not a cloud-hosted service.

### 1. One-time Garmin login

Garmin's API requires an interactive login (MFA-capable) the first time; after that, a token is cached and reused.

```bash
python -m venv .venv
.venv\Scripts\activate      # or: source .venv/bin/activate
pip install -e .
python scripts/login.py ./data/garth
```

This writes a token store to `./data/garth` — mounted into the container in the next step. You should not need to do this again unless the tokens are revoked (password change, ~1 year expiry).

### 2. Configure and run

```bash
cp .env.example .env
```

Fill in:

| Var | What it's for |
|---|---|
| `MCP_BEARER_TOKEN` | The one-time login password for the server's OAuth flow (see "Auth" below). Pick a long random string. |
| `MCP_PUBLIC_URL` | The externally-reachable URL you'll expose this at (see step 4). Required for correct OAuth redirect URLs — `127.0.0.1` won't work here. |
| `SQLITE_PATH` | Leave as default unless you've changed the GarminDB layout. |
| `TZ` | Your local timezone, e.g. `America/New_York`. |

```bash
docker compose up --build -d
curl http://localhost:18080/health   # {"status": "ok", ...}
```

`/health` is unauthenticated; every other endpoint needs a real OAuth access token (see below), not the raw `MCP_BEARER_TOKEN`.

### 3. Historical data (GarminDB)

Some tools (`get_body_trend`, step-based trends) read from a local SQLite database synced by [GarminDB](https://github.com/tcgoetz/GarminDB) rather than the live API, since a 30/90-day trend query against Garmin's live endpoints is both slow and rate-limit-risky.

```bash
python -m venv .garmindb-venv
.garmindb-venv\Scripts\pip install garmindb
python scripts/sync_garmindb.py
```

Schedule it to run nightly (Windows Task Scheduler shown; cron works the same way on Linux/macOS):

```bash
schtasks /create /tn "garmin-mcp-sync" /tr "\"<path-to>\.venv\Scripts\python.exe\" \"<path-to>\scripts\sync_garmindb.py\"" /sc daily /st 03:00
```

Tools that depend on this will say so clearly (`TrendDataUnavailableError`) if you skip this step.

### 4. Expose it and connect Claude

Claude's connector UI requires OAuth 2.1 with Dynamic Client Registration — it won't accept a static API key directly. This server implements that (see [SPEC.md](SPEC.md) for the design), gated behind the `MCP_BEARER_TOKEN` you set above, which acts as a one-time login password rather than a per-request header.

1. Expose port `18080` publicly over HTTPS — a reverse tunnel (Tailscale Funnel, Cloudflare Tunnel, ngrok) is the simplest path if you don't already have a public domain pointed at this machine. Point `MCP_PUBLIC_URL` in `.env` at that URL and restart the container.
2. In Claude, add a custom connector pointing at `<MCP_PUBLIC_URL>/mcp`.
3. When prompted to sign in, enter your `MCP_BEARER_TOKEN` as the password. Claude then holds a short-lived OAuth token it refreshes automatically — the bearer token itself is never sent again after this step.

### Auth notes

- 10 failed login attempts within 60 seconds locks that client out for 5 minutes.
- The container should only ever be published to `127.0.0.1` (see `compose.yml`) — let your tunnel/reverse-proxy be the actual public-facing edge, not the container directly.

## FAQ

**Does this send my Garmin data anywhere besides Claude?**
No. The server talks to Garmin's API and to whatever local SQLite DB GarminDB syncs; the only outbound consumer is whatever MCP client you connect (Claude, or `curl`/MCP Inspector for testing).

**Why does `get_training_load` not match what I see in Garmin Connect?**
Garmin's Training Effect/Training Load numbers come from Firstbeat, and Firstbeat only runs *on the recording device* — it never reprocesses an externally-uploaded FIT file. If any of your activities come from something other than a Garmin watch (Tonal, Zwift, TrainerRoad, manual upload), Garmin's own field is `null` for those and always will be. `get_training_load` computes its own TRIMP-style figure instead, applied uniformly across every session with heart-rate data, and labels itself explicitly as an independent calculation via its `method` field — it is not trying to reproduce Garmin's number.

**Why is some field `null` when I know the data exists in Garmin Connect?**
Two common reasons: (1) the value genuinely isn't populated yet for that day (Garmin doesn't backfill HRV/VO2max retroactively), or (2) there's too little data in the requested window to compute a trustworthy trend, in which case the tool returns `null` with a `*_reason`/`*_note` field explaining why, rather than a number fitted to sparse data.

**Does this give Claude any ability to act on my behalf beyond reading/logging data?**
No. The only write tools are weight, blood pressure, and hydration logging (plus their deletes) — nothing else. There's no path from this server to any other Garmin account action (no deleting activities, no changing device settings, no messaging).

**Can I run this without exposing it to the internet?**
Yes — skip step 4 above and connect a local MCP client (MCP Inspector, or Claude Code running on the same machine) directly at `http://127.0.0.1:18080/mcp`. You lose mobile access from Claude's app, but nothing else requires public exposure.

**Is this an official Garmin project?**
No. It's built on the unofficial [`garminconnect`](https://github.com/cyberjunky/python-garminconnect) Python client, which reverse-engineers Garmin Connect's own web API. It can break if Garmin changes that API; that's an accepted tradeoff of not having an official public API to build against.

## Self-hosted setup

[docs/self-hosted-setup.md](docs/self-hosted-setup.md) covers exposing this
server publicly with a step-by-step Tailscale Funnel example, plus notes on
picking a host port, scheduling the GarminDB sync, and running this
alongside another project that touches the same Garmin account. Cloudflare
Tunnel and other reverse-tunnel options work the same way in principle and
are left as an exercise — the doc only assumes you end up with a stable
public HTTPS URL to put in `MCP_PUBLIC_URL`.

## Repo structure

```
garmin-mcp/
├── src/garmin_mcp/
│   ├── server.py        # FastMCP app, tool registration
│   ├── oauth.py          # OAuth 2.1 / DCR provider, login form, rate limiting
│   ├── garmin.py         # thin wrapper over garminconnect
│   ├── store.py          # SQLite (GarminDB) access
│   └── models.py         # return shapes
├── scripts/
│   ├── login.py           # one-time interactive Garmin login
│   ├── smoke.py            # manual verification script
│   └── sync_garmindb.py    # nightly GarminDB sync
├── docs/
│   └── self-hosted-setup.md  # this deployment's real config
├── Dockerfile
├── compose.yml
├── .env.example
├── SPEC.md               # full build spec / design decisions
└── README.md
```

See [SPEC.md](SPEC.md) for the original design spec and locked decisions, and the build log below for what was actually verified against the live API (which occasionally disagreed with the spec's assumptions) as this was built out.

---

## Build log

Kept for anyone extending this: what was verified against the real API/library (not assumed), what broke, and why specific design choices were made. Read top to bottom for the build history in order.

**M1 — Garmin path proven.** `garminconnect==0.3.2` confirmed working against a real account:

- `Garmin()` + `client.login(tokenstore=dir)` — token-only login, no MFA prompt when a token store already exists.
- `Garmin(email=, password=, prompt_mfa=lambda: ...)` + `client.login(tokenstore=dir)` — interactive first-time login (`scripts/login.py`).
- `client.get_sleep_data(cdate)` — returns a `dailySleepDTO` with `sleepTimeSeconds` / `deepSleepSeconds` / `lightSleepSeconds` / `remSleepSeconds` / `awakeSleepSeconds`.
- `client.get_activities_by_date(startdate, enddate)` — returns a list of activity dicts.
- `client.add_weigh_in(weight, unitKey="lbs")` + `client.delete_weigh_in(weight_pk, cdate)` — write path, verified: wrote a real 180.0 lb test entry and deleted it again cleanly.

Note: this `garminconnect` version does its own token persistence (a single `garmin_tokens.json` in the directory passed as `tokenstore`) rather than garth's default two-file dump, and `garth` is not a separate installed dependency.

**M2 — MCP server, read tools only.** FastMCP 3.4.6 over Streamable HTTP, all 6 read tools verified against MCP Inspector CLI with real data from the account:

- `get_activities`, `get_activity_detail`, `get_sleep`, `get_daily_stats`, `get_body_trend`, `get_readiness`.
- No auth middleware yet (M3) and no Docker yet (M3) — this runs as a plain local process.

Field mappings worth knowing (found by inspecting live responses, not guessed):

- `get_stats(cdate)` is a goldmine for daily stats: `restingHeartRate`, `lastSevenDaysAvgRestingHeartRate` (used as `rhr_7d_baseline`), `bodyBatteryHighestValue`/`LowestValue`/`MostRecentValue`, `totalSteps`, `activeKilocalories`, `averageStressLevel` all come from this one call.
- HRV comes from `get_hrv_data(cdate)['hrvSummary']['lastNightAvg']`; `weeklyAvg`/`baseline` are `null` while Garmin considers the device "onboarding" — surfaced as `null`, never fabricated.
- Sleep score is `get_sleep_data(cdate)['dailySleepDTO']['sleepScores']['overall']['value']`; resting HR for that night is the sibling top-level `restingHeartRate` key (not inside `dailySleepDTO`).
- Body composition weight is in **grams**, not kg (`/ 453.59237` for lb).
- `garminconnect` raises `GarminConnectAuthenticationError` / `GarminConnectTooManyRequestsError` / `GarminConnectConnectionError` — `garmin.py`'s `_call()` wraps every client call with bounded backoff on 429/connection errors and a clear, non-retrying error on auth failure.

**M3 — Auth + Docker.** Verified end to end inside a running container:

- `auth.py`'s `BearerAuthMiddleware` (Starlette `BaseHTTPMiddleware`) checked the `authorization` header on every request except `/health`, using `hmac.compare_digest` against `MCP_BEARER_TOKEN`. Missing token, wrong scheme, or wrong value all got a 401. An unset `MCP_BEARER_TOKEN` failed closed (denied everything) rather than accepting any token. **Superseded by OAuth below** — see that section for why a plain static header wasn't enough once this needed to work as a Claude connector.
- `/health` (unauthenticated, `mcp.custom_route`) returns `{status, version, garmin_token_expires_at}`. The token expiry is read straight off the persisted `garmin_tokens.json` and its JWT `exp` claim — no network call, no login, so health stays fast and doesn't depend on Garmin being reachable.
- `Dockerfile` (python:3.12-slim, installs via `pyproject.toml`) and `compose.yml`. The app listens on `0.0.0.0:8080` *inside* the container (required for Docker's port-publish to reach it); `compose.yml` publishes it to the host as `127.0.0.1:18080:8080` — reachable from the host's loopback only, which is what a Cloudflare Tunnel running on the same host needs. Host port `18080` was picked to sit clear of the port ranges other local services on the host already use.
- Tested by building the image, running it with the reused token store mounted read-only, and confirming: `/health` → 200 with real expiry; `tools/list` with no header → 401; `tools/call` with the correct bearer token → real Garmin data.

**Rate limiting (added once this went publicly reachable).** `FailedAttemptLimiter` in `auth.py`: 10 failed auth attempts within 60s locks that client out for 5 minutes -- including subsequent attempts with the *correct* token, so a leaked/guessed token can't just be retried past a temporary block. Caught a real bug while adding this: Docker's bridge networking means `request.client.host` is always the Docker gateway IP (`172.20.0.1`) for every request regardless of true origin, which would make all real clients share one lockout bucket -- a naive implementation would have meant one noisy client (or one round of testing) locking out everyone. Fixed by trusting `X-Forwarded-For` instead, which is safe specifically *because* the container is published as `127.0.0.1:18080` (loopback-only) -- only a locally-running trusted process (Tailscale Funnel, Cloudflare Tunnel, our own tests) can reach that port at all, so a public attacker can't bypass it to spoof the header themselves. Verified with two distinct forged `X-Forwarded-For` clients: one gets locked out after 10 bad attempts, the other keeps working normally with the correct token.

**OAuth (replaced the plain bearer middleware).** Claude's "Add custom connector" UI doesn't accept a static header — per the MCP authorization spec, it expects the server to support OAuth 2.1 with Dynamic Client Registration (it `POST`s `/register` and walks an authorize/token flow before ever sending a request with a bearer header on it). Confirmed this the hard way: registering `garmin-mcp` in Claude failed with *"Couldn't register with garmin-mcp's sign-in service"* — the client had no fallback to a plain API-key field, only "add an OAuth Client ID."

- `oauth.py`'s `SingleUserOAuthProvider` subclasses FastMCP's `InMemoryOAuthProvider`, which already implements DCR, PKCE, and token issuance/refresh correctly — reusing it means the security-critical bookkeeping (code/token generation and expiry, redirect_uri validation via the SDK's own `AuthorizationRequest`/`client.validate_redirect_uri`) is the SDK's tested code, not hand-rolled. `InMemoryOAuthProvider` is explicitly documented as "for testing purposes" because Dynamic Client Registration is meant to be open (any client can self-register — that's the point of DCR), so its `authorize()` step auto-approves *any* registered client with no credential check at all. Used as-is on a public server, that would mean anyone could complete the OAuth dance and get a working access token without ever knowing `MCP_BEARER_TOKEN` — DCR itself isn't a security boundary, so the boundary has to be here instead.
- The fix: `SingleUserOAuthProvider` overrides `get_routes()` to swap in a custom `/authorize` handler that renders a one-field login form (the existing `MCP_BEARER_TOKEN`, now used as a login password rather than a per-request header) before calling the inherited `authorize()`. Wrong token → re-renders the form with an error and feeds the same `FailedAttemptLimiter` from the rate-limiting work above (now guarding the login form instead of the old per-request check). Correct token → normal OAuth code issuance, unchanged from `InMemoryOAuthProvider`.
- Net effect: `MCP_BEARER_TOKEN` is now transmitted once, at login, instead of on every request — after that, Claude holds a short-lived (1hr) OAuth access token that auto-refreshes. Confirmed the old static secret alone no longer works directly against `/mcp` (`401 invalid_token`) — only a real OAuth-issued access token does.
- `MCP_PUBLIC_URL` (new env var) tells the provider its own externally-reachable URL, since OAuth issuer/redirect URLs must be correct absolute URLs, not `127.0.0.1`.
- Verified the complete flow manually end to end: `/register` (DCR) → `GET /authorize` (form renders, all params preserved as hidden fields) → wrong token (401, re-rendered form, rate-limited after 10 attempts) → correct token (302 with code) → `POST /token` (real access + refresh token) → tool call with the access token (real Garmin data) → confirmed the old static token alone is rejected. Then redeployed for real and confirmed `/.well-known/oauth-authorization-server` serves correct external URLs from an independent network, matching the pattern used to verify the public tunnel itself.

**OAuth state didn't survive a restart (found live, 2026-08-12).** `InMemoryOAuthProvider` holds every registered client and issued access/refresh token as plain in-process dicts -- confirmed after a host reboot: the container came back up healthy (`/health` fine, Tailscale Funnel fine), but Claude's existing session got a flat `401 invalid_token` on every request, because the process it was issued against no longer existed. `SingleUserOAuthProvider` now persists that same state to `/data/oauth_state.json` (the volume that already holds the Garmin token store, so no new mount needed) -- loaded on startup, saved after every mutating call (`register_client`, `authorize`, `exchange_authorization_code`, `exchange_refresh_token`, `revoke_token`), with expired entries dropped on load rather than carried forward. Verified with a register → authorize → exchange → fresh-instance-same-file round trip: client registration and both tokens all reload correctly. This file is bearer-credential-equivalent (a valid refresh token in it means "logged in," no further check) -- same sensitivity as `garmin_tokens.json` next to it, covered by the same `/data` gitignore. One remaining gap: a client that was mid-login (registered but hadn't completed `/authorize` yet) at the moment of a restart just needs to retry the login form -- not persisted differently, not worth the complexity for a single-user server.

**M4 — Write tools.** `log_weight(weight_lb, date=None)` added, defaulting to today, rejecting outside 80-500 lb.

- Idempotent per date: deletes any existing weigh-in(s) for that date via `delete_weigh_in` before adding the new one via `add_weigh_in`, so repeated calls replace rather than duplicate.
- Verified through the running MCP tool (not just the library): wrote 190.5 lb to a test date, then 191.0 lb to the same date, confirmed via `get_daily_weigh_ins` that exactly one entry remained (191.0 — the second write, no duplicate), then deleted it to leave the account clean.
- `unitKey="lbs"` is passed straight through to `add_weigh_in` — this `garminconnect` version accepts lbs natively (see `VALID_WEIGHT_UNITS = {"kg", "lbs"}`), so no manual kg conversion was needed despite the spec's assumption that Garmin's API only takes kg.

**M5 — GarminDB + trend tools.** `get_body_trend` now reads a local SQLite database instead of the live Garmin API.

- **Isolated venv (`.garmindb-venv/`, gitignored).** `garmindb` (PyPI `GarminDb`, 3.8.0) depends on `garminconnect==0.3.3` -- a different release than the server's pinned `0.3.2`. Installing it into the server's `.venv` silently upgraded `garminconnect` and would have changed the exact API behavior M1-M4 were verified against. It's installed into its own venv instead, invoked only via `subprocess` from `scripts/sync_garmindb.py`; the server never imports it.
- **Shared token store, no password on disk.** `garmindb`'s own `GarminConnectAuthAdapter` tries a cached DI token file before falling back to a username/password from `GarminConnectConfig.json`. Its config_dir is pointed at `data/garth` -- the *same* directory the server uses for `GARMIN_TOKEN_DIR` -- so it logs in with the existing token and `credentials.password` in the config stays empty. Both `garminconnect` versions read/write the same `di_token`/`di_refresh_token`/`di_client_id` JSON shape, so this works even though the two components use different library versions.
- **Lesson learned the expensive way**: GarminDB's weight download fetches **one JSON file per day** in the requested range, throttled to ~1 request/second. An initial config with `weight_start_date` at 2020 meant a ~2,400-day backfill (~43 minutes) for a gate that only needs 90 days -- caught partway through (723/2413 days) and killed. Reconfigured `*_start_date` to 180 days back (~3 minutes for a full sync) before rerunning. Nightly runs use `--latest`, which only pulls new days, so this cost is one-time.
- **Weight unit, verified not assumed**: the synced `weight` table stores pounds, not kg -- confirmed by comparing a synced row against the live API's value for the same date.
- `store.py` opens `SQLITE_PATH` (GarminDB's own `garmin.db`, not a separate database we maintain) read-only and queries the `weight` table directly. Measured at **~0.0008s** for `get_body_trend(90)` end to end -- the 1-second gate cleared by three orders of magnitude.
- If `SQLITE_PATH` doesn't exist yet (before the first sync), `get_body_trend` raises a clear `TrendDataUnavailableError` telling you to run `scripts/sync_garmindb.py`, rather than silently falling back to the live API or fabricating data.

**Post-launch fixes, found auditing real usage (2026-08-10).** Two real GarminDB gaps surfaced once actual write traffic hit the account:

- **Sync gaps, two independent causes.** `--latest` mode only ever looks forward from the newest date already local, so a weigh-in logged for a *past* date (backfilling) is invisible to it forever, not just delayed. Separately, GarminDB caches each day's downloaded JSON and skips re-fetching it by default -- so a day checked *before* a backfilled entry existed keeps serving that stale empty cache even across full resyncs, unless `--overwrite` forces a re-fetch. `scripts/sync_garmindb.py --full` now passes `--overwrite`; a second scheduled task (`garmin-mcp-sync-full`, daily) runs it alongside the existing 20-minute `--latest` task. `get_body_trend` also now returns a `coverage` block and suppresses `trend_lb_per_week` with a stated reason when data's too sparse to trust, rather than ever reporting a misleadingly precise number.
- **HRV import is broken in this GarminDB version** -- confirmed live: files download and parse fine, but zero rows ever land in the local `hrv` table, no error logged. Because the table stays permanently empty, `--latest` silently fell back to a full 180-day HRV re-scan on **every single 20-minute sync**. Disabled `hrv` in `GarminConnectConfig.json`'s `enabled_stats` entirely. Safe: no tool reads HRV from the local DB -- `get_daily_stats`/`get_readiness` pull `hrv_ms`/`hrv_7d_baseline` from the *live* API, only `weight` is DB-backed.

**Round 2 (2026-08-11): six new tools, ranked by "what would change a coaching decision."** `get_vo2max`, `get_activity_trend`, `get_zone_summary`, `get_training_load` (read-only), and `log_bp`/`get_bp_trend`/`delete_bp` + `log_hydration`/`get_hydration`/`delete_hydration` (full read+write). All verified against real API calls and real data, not the feature-request doc's assumptions -- several didn't match:

- **VO2max**: this account has zero measurements across its entire history -- tool is correct, just empty until Garmin computes one. Found via a documented community bug report ([python-garminconnect#74](https://github.com/cyberjunky/python-garminconnect/issues/74)) that Garmin's endpoint returns the *most recently known* value regardless of which date is queried between real measurements -- `get_vo2max` dedupes on the response's own `calendarDate` rather than the requested date to handle this correctly.
- **Blood pressure**: writing one test reading revealed Garmin auto-classifies every BP entry into a hypertension stage (`"STAGE_1_HIGH"` etc.) -- deliberately stripped from `get_bp_trend`'s output; would have been exactly the kind of clinical interpretation this server isn't supposed to emit. Also: `pulse` turned out to be a required field in `garminconnect`'s validation (int 20-250, no default), not optional as originally assumed.
- **Hydration**: `add_hydration_data` is additive, not replace -- `log_hydration` keeps that semantic since hydration is naturally a running daily total, unlike weight/BP. No delete endpoint exists at all; `delete_hydration` reads the current total and subtracts it exactly. Separately, GarminDB's synced `hydration_goal` is a flat placeholder (`100`) on every day checked, nowhere close to the real goal the live API reports -- `goal_ml`/`pct_of_goal` are nulled out below an implausibility threshold rather than shipping a wrong percentage.
- **Zone summary**: no zone-config endpoint exists in this library (checked profile/settings/device-settings methods) -- `zone_boundaries_bpm` is read from a real activity's own HR-zone response instead, and reports actual bpm cutoffs rather than guessing whether they're Garmin-default or a custom (e.g. Karvonen) model.
- **Training load**: confirmed live that externally-pushed activities (e.g. from Tonal) return Garmin/Firstbeat's `activityTrainingLoad: null` -- Firstbeat only runs on-device and never reprocesses externally-uploaded FIT files, so nothing on the push side would fix it. Rewrote `get_training_load` to compute its own TRIMP-style figure instead -- duration x HR-intensity, using the same zone-relative HR model `get_zone_summary` already established (no real HRmax exists anywhere in Garmin's API to build a textbook %HRR from) -- applied uniformly to every session with `avg_hr`. `method` always labels it as an independent calculation, not Garmin's number; `sessions_missing_hr`/`coverage_note` cover excluded sessions; `ratio_suppressed_reason` fires on thin windows or when the acute/chronic comparison would silently span a `zone_config_id` change.
- **Steps/NEAT trend**: DB-backed from GarminDB's `garmin_summary.db` `days_summary` rollup table. Coverage depends on how long GarminDB has been syncing for your account.

**The 20-minute `--latest` sync kept getting slower, traced to another rolling-reprocess pattern (2026-08-21).** Same class of bug as the HRV one above (a "latest" sync silently redoing more work than it needs to every run), different cause. Measured a real run directly rather than guessing: 2m47s total, with **84 of those 167 seconds spent re-parsing full FIT detail (sets, laps, records) for the last 25 activities** -- not just genuinely new ones since the last sync. `download_latest_activities: 25` in `GarminConnectConfig.json` is GarminDB's own re-fetch window (by design, to catch late corrections), but it reprocesses that whole window *every single run*, and Tonal-pushed strength activities carry far more per-set detail than a typical run/bike FIT file -- so the more of them sit inside that rolling 25-activity window, the slower every run gets, compounding as more get pushed. (The "Analyzing Data" phase, which *does* scale with total history, was confirmed fast -- under 2 seconds -- so that wasn't the cause.)

Fixed by lowering `download_latest_activities` to `1`: confirmed via `garmindb_cli.py` source that `latest_activity_count()` (the `--latest`/20-minute job) and `all_activity_count()` (`download_all_activities`, the nightly `--full --overwrite` job) are separate config keys -- this change only affects the 20-minute job, the nightly full-refresh safety net that would catch anything a too-narrow window missed is untouched. Re-measured after the change: activity FIT processing dropped from 84s (23 files) to 25s (21 files); total run time from 2m47s to 1m45s. `download_latest_activities` lives in `data/garth/GarminConnectConfig.json`, which is gitignored (host-specific runtime config, same as the Garmin token store next to it) -- this note is the only record of the change and its reasoning.

A second bottleneck is now dominant in the remaining runtime and wasn't part of this fix: processing monitoring FIT data (`FileType.monitoring_b`/`hrv_status`, ~350-360 small files per run) took ~59 of the 105 seconds in the post-fix run. Worth a closer look later if sync time matters further -- not investigated here since it's unrelated to the activity-reprocessing pattern this note is about.
