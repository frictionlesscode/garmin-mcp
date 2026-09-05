# Security policy

## Reporting a vulnerability

Please **don't** open a public issue for anything security-sensitive. Use GitHub's private
vulnerability reporting instead — the repository's **Security** tab → **Report a
vulnerability** — which opens a private advisory visible only to you and the maintainer.

For low-risk hardening suggestions, a normal issue is fine.

There is no bounty and no SLA; this is a personal project maintained in spare time. Reports
will still be read and acted on as quickly as is practical.

## What this server holds

Everything sensitive lives in `.env` or the `./data` directory. Both are git-ignored — no
secret is committed to this repo, and none should ever be.

| Item | Location | Impact if it leaks |
|---|---|---|
| Garmin Connect session tokens | `./data/garth/` | Full access to the Garmin account until the tokens are revoked (change the Garmin password). |
| OAuth client + token state | `./data/oauth_state.json` | A valid refresh token here is equivalent to being logged in to this server. Delete the file to invalidate every issued token and registered client. |
| `MCP_BEARER_TOKEN` | `.env` | Used once, at login, to obtain an OAuth access token. Rotate it and restart the container. |

Recommended: `chmod 600 .env`, `chmod 700 data`, keep the container published to `127.0.0.1`
only (as `compose.yml` does), and let a tunnel or reverse proxy be the public-facing edge.

## Scope

In scope: authentication bypass, credential or token exposure, flaws in the OAuth flow, or
anything that lets a request reach account data or a write endpoint without a valid access
token.

Out of scope: issues that require already having the host's `.env` or `./data`; rate limits
on the upstream Garmin API; and the behavior of the unofficial
[`garminconnect`](https://github.com/cyberjunky/python-garminconnect) client this builds on.

## No warranty

This software is provided "as is", without warranty of any kind — see [LICENSE](LICENSE).
