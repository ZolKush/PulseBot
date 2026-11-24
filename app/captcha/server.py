"""Small HTTP/1 listener behind Nginx, owned by the polling application."""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlsplit

import h11
import httpx

from .verification import VerificationError, verify

_LOG = logging.getLogger(__name__)
_BODY_LIMIT = 24 * 1024
_ASSETS = Path(__file__).with_name("static")
_CSP = (
    "default-src 'none'; script-src 'self' https://telegram.org https://challenges.cloudflare.com; "
    "frame-src https://challenges.cloudflare.com; style-src 'self' 'unsafe-inline'; "
    "connect-src 'self' https://challenges.cloudflare.com; base-uri 'none'; form-action 'none'; "
    "frame-ancestors https://web.telegram.org https://*.web.telegram.org"
)
Response = tuple[int, bytes, bytes]


async def _read_request(reader: asyncio.StreamReader, protocol: h11.Connection) -> tuple[h11.Request, bytes]:
    request = None
    body = bytearray()
    while True:
        event = protocol.next_event()
        if event is h11.NEED_DATA:
            protocol.receive_data(await reader.read(4096))
        elif isinstance(event, h11.Request):
            request = event
            if len(request.target) > 1024:
                raise VerificationError(414)
        elif isinstance(event, h11.Data):
            if len(body) + len(event.data) > _BODY_LIMIT:
                raise VerificationError(413)
            body.extend(event.data)
        elif isinstance(event, h11.EndOfMessage) and request is not None:
            return request, bytes(body)
        else:
            raise VerificationError(400)


class CaptchaServer:
    def __init__(
        self,
        *,
        url: str,
        site_key: str,
        secret_key: str,
        bot_token: str,
        on_verified: Callable[[int], Awaitable[None]],
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._origin = url.rstrip("/")
        self._hostname = urlsplit(url).hostname or ""
        self._bot_token = bot_token
        self._secret = secret_key
        self._on_verified = on_verified
        self._http = httpx.AsyncClient(timeout=6, trust_env=False, transport=transport)
        self._listener: asyncio.AbstractServer | None = None
        self._active: set[asyncio.Task] = set()
        self._recent: deque[float] = deque()
        page = (_ASSETS / "index.html").read_text(encoding="utf-8")
        self._assets = {
            b"/": (b"text/html; charset=utf-8", page.replace("{{SITE_KEY}}", html.escape(site_key)).encode()),
            b"/app.css": (b"text/css; charset=utf-8", (_ASSETS / "app.css").read_bytes()),
            b"/app.js": (b"text/javascript; charset=utf-8", (_ASSETS / "app.js").read_bytes()),
        }

    async def start(self, host: str, port: int) -> None:
        try:
            self._listener = await asyncio.start_server(self._accept, host, port, limit=16384)
        except BaseException:
            await self._http.aclose()
            raise

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._listener is None or len(self._active) >= 64:
            writer.close()
            return
        task = asyncio.create_task(self._serve(reader, writer))
        self._active.add(task)
        task.add_done_callback(self._active.discard)

    async def close(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        tasks = tuple(self._active)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=3)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if listener is not None:
            await listener.wait_closed()
        await self._http.aclose()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        protocol = h11.Connection(h11.SERVER, max_incomplete_event_size=8192)
        request = None
        try:
            try:
                request, body = await asyncio.wait_for(_read_request(reader, protocol), timeout=8)
                status, mime, content = await self._route(request, body)
            except VerificationError as exc:
                status, mime, content = exc.status, b"application/json", b"{}"
            except (h11.RemoteProtocolError, asyncio.TimeoutError, ConnectionError):
                status, mime, content = 400, b"application/json", b"{}"
            except Exception:
                # Request and exception text can contain signed Telegram data.
                _LOG.error("Captcha request failed")
                status, mime, content = 503, b"application/json", b"{}"
            headers = [
                (b"content-type", mime),
                (b"content-length", str(len(content)).encode()),
                (b"connection", b"close"),
                (b"cache-control", b"no-store"),
                (b"referrer-policy", b"strict-origin"),
                (b"x-content-type-options", b"nosniff"),
                (b"content-security-policy", _CSP.encode()),
            ]
            writer.write(protocol.send(h11.Response(status_code=status, headers=headers)))
            if request is None or request.method != b"HEAD":
                writer.write(protocol.send(h11.Data(data=content)))
            writer.write(protocol.send(h11.EndOfMessage()))
            await asyncio.wait_for(writer.drain(), timeout=2)
        except (h11.ProtocolError, ConnectionError, asyncio.TimeoutError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def _route(self, request: h11.Request, body: bytes) -> Response:
        path = request.target.partition(b"?")[0]
        if request.method == b"GET" and path in self._assets:
            mime, content = self._assets[path]
            return 200, mime, content
        if request.target != b"/verify" or request.method != b"POST":
            raise VerificationError(404)
        headers = dict(request.headers)
        if headers.get(b"origin") != self._origin.encode("ascii"):
            raise VerificationError(403)
        if headers.get(b"content-type", b"").partition(b";")[0].lower() != b"application/json":
            raise VerificationError(415)
        now = time.monotonic()
        while self._recent and self._recent[0] <= now - 1:
            self._recent.popleft()
        if len(self._recent) >= 32:
            raise VerificationError(429)
        self._recent.append(now)
        user_id = await verify(
            body, bot_token=self._bot_token, secret=self._secret, hostname=self._hostname, client=self._http
        )
        try:
            await asyncio.wait_for(self._on_verified(user_id), timeout=8)
        except Exception:
            _LOG.warning("Could not deliver registration prompt after captcha verification")
            return 200, b"application/json", b'{"continued":false}'
        return 200, b"application/json", b'{"continued":true}'
