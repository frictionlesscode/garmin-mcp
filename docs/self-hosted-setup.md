# Self-hosted setup notes

Generic guidance for exposing this server publicly so it can be added as a
Claude custom connector, plus a few operational notes worth knowing before
you run this long-term. Everything below uses placeholder values —
`<your-machine>`, `example.ts.net`, etc. — swap in your own.

## Exposing the server publicly (Tailscale Funnel example)

Claude's connector UI needs a real HTTPS URL to reach your server's `/mcp`
endpoint — `127.0.0.1` or a bare LAN IP won't work. [Tailscale
Funnel](https://tailscale.com/kb/1223/funnel) is one of the simplest ways to
get one without owning a domain or touching DNS: it gives you a stable
`https://<your-machine>.<your-tailnet>.ts.net` URL that proxies straight to a
local port, using a domain Tailscale already manages for you.

1. Install Tailscale on the host and sign in (`tailscale up`), if it isn't
   already.
2. Enable Funnel for your tailnet once, in the [Tailscale admin
   console](https://login.tailscale.com/admin/settings/general) (Funnel
   toggle) — a one-time account-level setting.
3. Point Funnel at the port `compose.yml` publishes (`18080` by default):

   ```bash
   tailscale funnel --bg 18080
   ```

4. Confirm it's live:

   ```bash
   tailscale funnel status
   ```

   This prints your public URL, something like
   `https://<your-machine>.example.ts.net`.

5. Set `MCP_PUBLIC_URL` in `.env` to that URL (e.g.
   `https://<your-machine>.example.ts.net`) and restart the container —
   this value has to be correct because OAuth's issuer/redirect URLs are
   derived from it, not from `127.0.0.1`.
6. Verify from a network that isn't the host itself (phone on cellular, a
   different machine) that `<MCP_PUBLIC_URL>/health` returns a real
   response — confirming it only from the host can hit local TLS quirks that
   don't reflect what an outside client will actually see.

### Other options

Any reverse tunnel or reverse proxy that terminates HTTPS and forwards to
the container's published port works the same way — **Cloudflare Tunnel**
and **ngrok** are common alternatives, and if you already own a domain and
want it on your own hostname rather than a `*.ts.net` one, Cloudflare Tunnel
is probably the more natural choice. Setting either of those up is left as
an exercise — the only thing this project needs from whichever one you pick
is a stable public HTTPS URL to put in `MCP_PUBLIC_URL`.

## Sharing the Funnel with another MCP server

If this host also runs another public-facing server (e.g. a second MCP
server) behind the same Tailscale Funnel hostname, three things matter:

1. **`tailscale funnel reset` wipes the whole shared config, not just this
   server's mapping.** Funnel config lives per-hostname, not per-service. If
   you're troubleshooting garmin-mcp and reach for `funnel reset`, you will
   also silently delete every other service's path mappings on that
   hostname — they'll need re-adding from whatever set them up originally.
2. **garmin-mcp needs to stay on the domain root (`/`).** OAuth
   protected-resource discovery is root-relative: on a 401, the server's
   `WWW-Authenticate` header points at a `.well-known` path under the
   *domain root*, not under whatever path this service happens to be mounted
   at. Whichever service owns `/` gets correct discovery for free; anything
   else sharing the hostname has to be mounted at a subpath with explicit,
   unstripped `.well-known` mappings added to compensate. Moving garmin-mcp
   off `/` breaks its connector the same way, and the failure is easy to
   mistake for something else: Claude just reports an invalid/expired token
   and never shows a login prompt, while the server logs show a bare
   `POST /mcp` → 401 with no `/register` or `/authorize` call before it.
3. **Serve on port 443, not an alternate port.** Claude's backend only
   egresses to MCP connectors on 443; anything else fails silently — the
   request never arrives, so nothing shows up in the server's logs either,
   which looks identical to a server-side bug. `tailscale funnel --bg 18080`
   gets this right automatically (Funnel always terminates on 443), but it's
   easy to break by choosing a setup that maps a different public port
   later. See [this write-up](https://www.brendanlong.com/debugging-claude-ai-mcp-connectors.html)
   for how this class of failure presents.

`/health` returning 200 does not prove the connector works — it stays green
even when OAuth discovery is completely broken. Test the actual discovery
chain (401 → `WWW-Authenticate` pointer → protected-resource metadata →
auth-server metadata → advertised issuer) instead of trusting `/health`.

## Picking a host port

`compose.yml` defaults to publishing `127.0.0.1:18080:8080`. If `18080` is
already taken by something else on your machine, change the host-side
number in `compose.yml` (leave the container-side `8080` alone) and update
whatever you point your tunnel at to match.

## Scheduled sync

If you're using GarminDB for the trend tools (`get_body_trend`,
`get_activity_trend`), schedule `scripts/sync_garmindb.py` to run
periodically — a 15-20 minute interval keeps trend data reasonably current
without hammering Garmin's API. Windows Task Scheduler example:

```bash
schtasks /create /tn "garmin-mcp-sync" /tr "\"<path-to-repo>\.venv\Scripts\python.exe\" \"<path-to-repo>\scripts\sync_garmindb.py\"" /sc daily /st 03:00
```

On Linux/macOS, the cron equivalent works the same way — call the same
script with the same venv's interpreter.

Consider also scheduling a periodic **full** resync
(`sync_garmindb.py --full`), less frequently (e.g. nightly) — an
incremental-only sync (`--latest`) won't pick up entries backfilled for a
past date after the fact. See the build log in the main
[README](../README.md) ("Sync gaps") for why this matters.

## Companion Skill

If you're pairing this with [garmin-coach](https://github.com/frictionlesscode/garmin-coach)
(a Claude Skill that teaches correct interpretation of this server's tool
output), install it via Claude's Skills UI and add this server's MCP
connector with the bearer token from your `.env`.

## Running alongside another Garmin-touching project

If you have more than one project authenticating to the same Garmin
account (e.g. a separate activity-sync tool), decide up front whether they
should share one token store directory or each keep an isolated copy.
Sharing means one login serves both; isolating means each has its own blast
radius if one component is compromised or misbehaves — worth doing if one
of them (like this server) is reachable from the internet and the other
isn't. Both are reasonable; just pick deliberately rather than by accident,
since `garminconnect`'s token file format is shared across recent versions
of the library either way.
