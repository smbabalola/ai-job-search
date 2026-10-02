"""Security headers and a strict, per-request nonce CSP (Bundle 7 spec §20.7, T4).

Templates put ``nonce="{{ request.state.csp_nonce }}"`` on every inline
script; inline event handlers and style attributes are not allowed at all."""
from __future__ import annotations

import base64
import secrets

FIXED_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
)
HSTS = (b"strict-transport-security", b"max-age=31536000; includeSubDomains")


def content_security_policy(nonce: str) -> str:
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'self'"
    )


class SecurityHeadersMiddleware:
    def __init__(self, app, *, hosted: bool) -> None:
        self.app = app
        self.hosted = hosted

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        nonce = base64.b64encode(secrets.token_bytes(16)).decode()
        scope.setdefault("state", {})["csp_nonce"] = nonce
        managed = {name for name, _ in FIXED_HEADERS} | {b"content-security-policy", HSTS[0]}

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = [h for h in message.get("headers", []) if h[0].lower() not in managed]
                headers.extend(FIXED_HEADERS)
                headers.append((b"content-security-policy", content_security_policy(nonce).encode()))
                if self.hosted:
                    headers.append(HSTS)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
