"""OpenID Connect relying party and localhost gateway trust boundary."""

import hmac
import ssl
from logging import getLogger
from time import time

from authlib.integrations.starlette_client import OAuth
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.datastructures import Headers
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import HTTPConnection

COOKIE = "__Host-f1ri-session"
FLOW = "__Host-f1ri-oidc"
LOGGER = getLogger("f1ri.private")
UNPROTECTED = {"/auth/sign-in", "/auth/login", "/auth/callback", "/auth/signed-out", "/healthz"}
CACHE = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"}
SIGNIN = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>Private research access</title><style>body{color:#eff2f4;background:#101417;font:16px system-ui;margin:0;
min-height:100vh;display:grid;place-items:center}main{box-sizing:border-box;width:min(92%,470px);padding:32px;
background:#171e24;border:1px solid #3b454b;border-radius:12px}p{color:#b6c4ce;line-height:1.5}
a{display:inline-block;background:#ff865c;color:#181310;padding:11px 16px;border-radius:6px;font-weight:700;
text-decoration:none}a:focus-visible{outline:2px solid #fff;outline-offset:4px}</style>
<main><small>F1 RACE INTELLIGENCE</small><h1>{title}</h1><p>{message}</p><a href="/auth/login">
Sign in with your invited account</a></main></html>"""


def _page(title, message, status=200):
    return HTMLResponse(SIGNIN.replace("{title}", title).replace("{message}", message), status_code=status, headers=CACHE)


def fail(code, message, status):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status, headers=CACHE)


class Boundary:
    def __init__(self, app, config, store):
        self.app, self.config, self.store = app, config, store
        self.edge = config.edge_secret.get_secret_value().encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        path, method = scope["path"], scope["method"]

        async def respond(response):
            return await response(scope, receive, send)

        if not hmac.compare_digest(headers.get("x-f1ri-edge", "").encode(), self.edge):
            return await respond(fail("EDGE_REQUIRED", "Use the authenticated application gateway.", 403))
        if method in {"POST", "PUT", "DELETE", "PATCH", "OPTIONS"} and headers.get("origin") != self.config.origin:
            LOGGER.info("request_denied reason=origin status=403")
            return await respond(fail("ORIGIN_FORBIDDEN", "Use the private application origin.", 403))
        if path not in UNPROTECTED:
            raw = HTTPConnection(scope).cookies.get(COOKIE)
            principal = self.store.session(raw)
            if not principal:
                if path == "/auth/check":
                    return await respond(RedirectResponse("/auth/sign-in", status_code=303, headers=CACHE))
                LOGGER.info("request_denied reason=session status=401")
                return await respond(fail("AUTH_REQUIRED", "Sign in to continue this private research session.", 401))
            scope["f1ri.principal"] = dict(principal)

        async def safe_send(message):
            if message["type"] == "http.response.start":
                from starlette.datastructures import MutableHeaders

                response_headers = MutableHeaders(scope=message)
                response_headers.update(CACHE)
                if path.startswith("/api/"):
                    LOGGER.info("api_response status=%d", message["status"])
            await send(message)

        await self.app(scope, receive, safe_send)


def owner(request):
    return request.scope["f1ri.principal"]["id"]


def install(app, config, store):
    """Use an issuer-validated OIDC code exchange; store only local opaque session hashes."""
    tls = ssl.create_default_context(cafile=str(config.issuer_ca)) if config.issuer_ca else True
    oauth = OAuth()
    issuer = oauth.register("identity",
        server_metadata_url=config.oidc_issuer+"/.well-known/openid-configuration",
        client_id=config.oidc_client_id, client_secret=config.oidc_client_secret.get_secret_value(),
        client_kwargs={"scope": "openid", "code_challenge_method": "S256", "verify": tls, "timeout": 8.0,
                       "trust_env": False},
    )
    callback = config.origin + "/auth/callback"

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ready"}

    @app.get("/auth/sign-in", include_in_schema=False)
    def sign_in():
        return _page("Private research access", "Sign in to open the race strategy workspace. Access is by invitation.")

    @app.get("/auth/login", include_in_schema=False)
    async def login(request: Request):
        try:
            response = await issuer.authorize_redirect(request, callback, prompt="login")
            response.headers.update(CACHE)
            return response
        except Exception:  # noqa: BLE001 -- Authentication failures must not echo provider context.
            LOGGER.warning("oidc_login_failed")
            return _page("Sign-in unavailable", "The identity provider could not be reached. Try again.", 503)

    @app.get("/auth/callback", include_in_schema=False)
    async def finish(request: Request):
        try:
            token = await issuer.authorize_access_token(request)
            claims = token.get("userinfo") or {}
            if claims.get("iss") != config.oidc_issuer or not isinstance(claims.get("sub"), str):
                return fail("OIDC_CALLBACK_INVALID", "The sign-in response was rejected. Start sign-in again.", 400)
            account = store.principal(config.oidc_issuer, claims["sub"])
            if not account:
                LOGGER.info("oidc_callback_denied reason=invitation")
                return _page("Access denied", "This identity is outside the private invitation list.", 403)
            raw = store.new_session(account["id"], config.session_seconds, replace=request.cookies.get(COOKIE))
        except Exception:  # noqa: BLE001 -- Provider errors can contain sensitive callback information.
            LOGGER.info("oidc_callback_denied reason=validation")
            return fail("OIDC_CALLBACK_INVALID", "The sign-in response was rejected. Start sign-in again.", 400)
        request.session.clear()
        response = RedirectResponse("/", status_code=303, headers=CACHE)
        response.set_cookie(COOKIE, raw, max_age=config.session_seconds, path="/", secure=True,
                            httponly=True, samesite="lax")
        LOGGER.info("session_started")
        return response

    @app.get("/auth/check", include_in_schema=False)
    def check():
        return Response(status_code=204, headers=CACHE)

    @app.get("/auth/session", include_in_schema=False)
    def session(request: Request):
        principal = request.scope["f1ri.principal"]
        return {"private": True, "label": principal["label"],
                "expires_in_s": max(0, principal["expires"]-int(time()))}

    @app.post("/auth/logout", include_in_schema=False)
    def logout(request: Request):
        store.logout(request.cookies.get(COOKIE))
        response = JSONResponse({"signed_out": True}, headers=CACHE)
        response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
        request.session.clear()
        LOGGER.info("session_ended")
        return response

    @app.get("/auth/signed-out", include_in_schema=False)
    def signed_out():
        return _page("Signed out", "This device's workspace session has ended.")

    app.add_middleware(SessionMiddleware, secret_key=config.flow_secret.get_secret_value(), session_cookie=FLOW,
                       max_age=300, path="/", same_site="lax", https_only=True)
    app.add_middleware(Boundary, config=config, store=store)
