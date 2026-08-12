"""Minimal OAuth 2.1 authorization server so Claude's remote-connector UI can
talk to garmin-mcp.

Why this exists: the spec called for a simple static bearer token on the
`authorization` header, and that's genuinely enough security-wise. But
Claude's "Add custom connector" flow (like most MCP clients, per the MCP
authorization spec) expects the server to support OAuth 2.1 with Dynamic
Client Registration -- it tries to POST /register and walk an authorize/token
flow, not just accept a static header. Without this, Claude's client fails
at the registration step before ever sending a request we could check a
bearer header on.

This is built on FastMCP's InMemoryOAuthProvider, which already implements
DCR, PKCE, and token issuance/refresh correctly -- reusing it means the
security-critical bookkeeping (code/token generation, expiry, redirect_uri
validation) is the SDK's tested code, not ours. The one thing
InMemoryOAuthProvider is missing -- it says so itself, "for testing purposes"
-- is a gate on the authorize step. Dynamic Client Registration is meant to
be open (any client can self-register, that's the point of DCR), so the only
place a real security boundary can live is the authorize step. Without a
gate there, anyone could complete the OAuth dance and get a working access
token without ever knowing MCP_BEARER_TOKEN. SingleUserOAuthProvider adds
exactly that: a one-field login form requiring the existing secret, using
the SDK's own AuthorizationRequest validation (redirect_uri / scope checks)
so that security-sensitive parsing isn't reimplemented from scratch.

Once logged in, Claude holds a short-lived OAuth access token (auto-refreshed
via the refresh token) instead of sending MCP_BEARER_TOKEN on every request --
an improvement over the plain static-bearer model this replaces: the secret
itself is now only ever transmitted once, at login.
"""

import html
import hmac
import os

from fastmcp.server.auth.auth import ClientRegistrationOptions
from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from fastmcp.utilities.ui import create_secure_html_response
from mcp.server.auth.handlers.authorize import AuthorizationRequest
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError
from mcp.shared.auth import InvalidRedirectUriError, InvalidScopeError
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from garmin_mcp.auth import client_key, limiter


def _login_form_html(auth_request: AuthorizationRequest, error: str | None) -> str:
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    hidden_fields = "".join(
        f'<input type="hidden" name="{name}" value="{html.escape(str(value))}">'
        for name, value in [
            ("client_id", auth_request.client_id),
            ("redirect_uri", auth_request.redirect_uri),
            ("response_type", auth_request.response_type),
            ("code_challenge", auth_request.code_challenge),
            ("code_challenge_method", auth_request.code_challenge_method),
            ("state", auth_request.state or ""),
            ("scope", auth_request.scope or ""),
            ("resource", auth_request.resource or ""),
        ]
        if value
    )
    return f"""
    <div class="container" style="max-width:400px;margin:80px auto;font-family:sans-serif;">
        <h1>garmin-mcp</h1>
        <p>Enter the access token to authorize this connection.</p>
        {error_html}
        <form method="POST">
            {hidden_fields}
            <input type="password" name="token" placeholder="Access token" autofocus
                   style="width:100%;padding:8px;font-size:16px;box-sizing:border-box;">
            <button type="submit" style="width:100%;padding:8px;margin-top:12px;font-size:16px;">
                Authorize
            </button>
        </form>
    </div>
    <style>.error{{color:#b91c1c;}}</style>
    """


def _lockout_html() -> str:
    return """
    <div style="max-width:400px;margin:80px auto;font-family:sans-serif;">
        <h1>Too many attempts</h1>
        <p>Try again in a few minutes.</p>
    </div>
    """


class SingleUserOAuthProvider(InMemoryOAuthProvider):
    """InMemoryOAuthProvider with a real login gate on /authorize.

    Dynamic Client Registration is required, not optional configuration --
    Claude's connector UI needs it to complete the handshake at all -- so
    it's enabled here rather than left for the caller to remember.
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("client_registration_options", ClientRegistrationOptions(enabled=True))
        super().__init__(**kwargs)

    def get_routes(self, mcp_path: str | None = None) -> list:
        routes = super().get_routes(mcp_path)
        return [r for r in routes if not (isinstance(r, Route) and r.path == "/authorize")] + [
            Route("/authorize", endpoint=self._handle_authorize, methods=["GET", "POST"]),
        ]

    async def _handle_authorize(self, request: Request) -> Response:
        params = request.query_params if request.method == "GET" else await request.form()

        try:
            auth_request = AuthorizationRequest.model_validate(params)
        except ValidationError as e:
            return create_secure_html_response(
                f"<h1>Invalid request</h1><p>{html.escape(str(e))}</p>", status_code=400
            )

        client = await self.get_client(auth_request.client_id)
        if not client:
            return create_secure_html_response(
                f"<h1>Unknown client</h1><p>{html.escape(auth_request.client_id)}</p>", status_code=400
            )

        try:
            redirect_uri = client.validate_redirect_uri(auth_request.redirect_uri)
        except InvalidRedirectUriError as e:
            return create_secure_html_response(
                f"<h1>Invalid redirect_uri</h1><p>{html.escape(e.message)}</p>", status_code=400
            )

        try:
            scopes = client.validate_scope(auth_request.scope)
        except InvalidScopeError as e:
            return create_secure_html_response(
                f"<h1>Invalid scope</h1><p>{html.escape(e.message)}</p>", status_code=400
            )

        key = client_key(request)
        if limiter.is_locked_out(key):
            return create_secure_html_response(_lockout_html(), status_code=429)

        if request.method == "GET":
            return create_secure_html_response(_login_form_html(auth_request, error=None))

        submitted = str(params.get("token", ""))
        expected = os.environ.get("MCP_BEARER_TOKEN", "")
        if not expected or not hmac.compare_digest(submitted, expected):
            limiter.record_failure(key)
            return create_secure_html_response(
                _login_form_html(auth_request, error="Incorrect token."), status_code=401
            )
        limiter.record_success(key)

        auth_params = AuthorizationParams(
            state=auth_request.state,
            scopes=scopes,
            code_challenge=auth_request.code_challenge,
            redirect_uri=redirect_uri,
            redirect_uri_provided_explicitly=auth_request.redirect_uri is not None,
            resource=auth_request.resource,
        )

        try:
            target = await self.authorize(client, auth_params)
        except AuthorizeError as e:
            return create_secure_html_response(
                f"<h1>Authorization failed</h1><p>{html.escape(e.error_description or e.error)}</p>",
                status_code=400,
            )

        return RedirectResponse(url=target, status_code=302, headers={"Cache-Control": "no-store"})
