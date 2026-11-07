"""Protects the local pages from other websites open in the same browser.

The app has no password because it only answers on this computer. That alone is not enough:
a web page you visit could still make your browser send requests to 127.0.0.1, either by
tricking the address (DNS rebinding) or by posting a form (CSRF). So:

- the `Host` header must be this computer (localhost / 127.0.0.1 / [::1]),
- every request that changes something must come from a page of this same app (`Origin` or
  `Referer` equal to the app's own address),
- every response carries headers that stop other sites from framing the app or loading
  scripts into it. Map pictures from OpenStreetMap are the only outside content allowed.
"""

from urllib.parse import urlsplit

from starlette.types import ASGIApp, Message, Receive, Scope, Send

LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "[::1]", "::1"}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

CSP = "; ".join([
    "default-src 'self'",
    "img-src 'self' data: https://tile.openstreetmap.org",
    "style-src 'self' 'unsafe-inline'",  # the map library positions things with inline styles
    "script-src 'self'",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
])
SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),  # OpenStreetMap wants a Referer
    (b"permissions-policy", b"geolocation=(), camera=(), microphone=()"),
]


def _hostname(host_header: str) -> str:
    host_header = host_header.strip().lower()
    if host_header.startswith("["):  # [::1]:8765
        return host_header.split("]")[0] + "]"
    return host_header.split(":")[0]


class LocalOnlyGuard:
    def __init__(self, app: ASGIApp, extra_hosts: list[str] | None = None):
        self.app = app
        self.allowed = LOCAL_HOSTNAMES | {h.lower() for h in (extra_hosts or [])}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        host = headers.get("host", "")
        if _hostname(host) not in self.allowed:
            await self._refuse(send, 421, "Ova adresa nije dopuštena.")
            return
        if scope["method"] in UNSAFE_METHODS and not self._same_origin(headers, host):
            await self._refuse(send, 403, "Zahtjev nije stigao s stranice ove aplikacije.")
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing = {k.lower() for k, _ in message["headers"]}
                message["headers"] = list(message["headers"]) + [
                    h for h in SECURITY_HEADERS if h[0] not in existing
                ]
            await send(message)

        await self.app(scope, receive, send_with_headers)

    @staticmethod
    def _same_origin(headers: dict[str, str], host: str) -> bool:
        source = headers.get("origin") or headers.get("referer")
        if not source:
            return False  # browsers always send one of them when a page posts a form
        return urlsplit(source).netloc.lower() == host.strip().lower()

    @staticmethod
    async def _refuse(send: Send, status: int, text: str) -> None:
        body = text.encode()
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(body)).encode()),
            *SECURITY_HEADERS,
        ]})
        await send({"type": "http.response.body", "body": body})
