"""HTTP client for the authoritative Forever Dungeon server (stdlib urllib only).

Errors are classified so the app can decide what is safe:
- NetworkError: the outcome is uncertain (request may or may not have committed).
- RateLimited / ServerUnavailable: retry later with the SAME envelope.
- Unauthorized: token revoked/invalid.
- Conflict: 409, carries `current_revision` when the server provides it.
- MalformedResponse: 2xx with an unparseable body (uncertain for actions).
"""
from __future__ import annotations

import http.client
import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import __version__

USER_AGENT = f"foreverdungeon-handheld/{__version__}"
CONNECT_TIMEOUT = 6.0


def _connect(host, port, timeout, source_address=None):
    """IPv4-first connect with a short per-address timeout.

    Some boards (the Orange Pi this was tested on) resolve AAAA records but have
    no working IPv6 route; plain urllib then hangs for the full timeout.
    """
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    infos.sort(key=lambda i: 0 if i[0] == socket.AF_INET else 1)
    last = OSError(f"could not resolve {host}")
    for family, stype, proto, _, addr in infos:
        sock = socket.socket(family, stype, proto)
        try:
            ct = CONNECT_TIMEOUT if timeout is None or timeout is socket._GLOBAL_DEFAULT_TIMEOUT else min(CONNECT_TIMEOUT, timeout)
            sock.settimeout(ct)
            if source_address:
                sock.bind(source_address)
            sock.connect(addr)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            return sock
        except OSError as exc:
            last = exc
            sock.close()
    raise last


class _HTTPConn(http.client.HTTPConnection):
    def connect(self):
        self.sock = _connect(self.host, self.port, self.timeout, self.source_address)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


class _HTTPSConn(http.client.HTTPSConnection):
    def connect(self):
        sock = _connect(self.host, self.port, self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_HTTPConn, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self):
        super().__init__(context=ssl.create_default_context())

    def https_open(self, req):
        return self.do_open(_HTTPSConn, req, context=self._context)


_OPENER = urllib.request.build_opener(_HTTPHandler, _HTTPSHandler)


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None, detail=None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.detail = detail
        self.retry_after = retry_after


class NetworkError(ApiError):
    pass


class MalformedResponse(ApiError):
    pass


class Unauthorized(ApiError):
    pass


class Conflict(ApiError):
    @property
    def current_revision(self):
        return self.detail.get("current_revision") if isinstance(self.detail, dict) else None

    @property
    def in_progress(self) -> bool:
        return isinstance(self.detail, str) and "already being processed" in self.detail


class RateLimited(ApiError):
    pass


class ServerUnavailable(ApiError):
    pass


class Expired(ApiError):
    pass


def _detail_message(detail) -> str:
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)
    if isinstance(detail, list):  # FastAPI validation errors
        def one(d):
            if not isinstance(d, dict):
                return str(d)
            field = d.get("loc", [None])[-1] if isinstance(d.get("loc"), list) else None
            msg = str(d.get("msg", d)).removeprefix("Value error, ")
            return f"{field}: {msg}" if field and field != "body" else msg
        return "; ".join(one(d) for d in detail)
    return str(detail) if detail is not None else ""


def new_envelope(character_id: str, command: str, expected_revision: int | None, expanded: bool = False) -> dict:
    """Build the full action envelope. The idempotency key is generated exactly once, here."""
    env = {"character_id": character_id, "command": command, "idempotency_key": str(uuid.uuid4()),
           "expected_revision": expected_revision, "expanded": bool(expanded)}
    if not isinstance(expected_revision, int) or expected_revision < 1:
        env["expected_revision"] = None
    return env


class ApiClient:
    def __init__(self, base_url: str, timeout: float = 25.0, token: str | None = None, opener=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token = token
        self._open = opener or _OPENER.open

    def _request(self, method: str, path: str, body: dict | None = None, auth: bool = False):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("X-FD-Client", "handheld")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if auth:
            if not self.token:
                raise Unauthorized("No device token", 401)
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with self._open(req, timeout=self.timeout) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read()
            except Exception:
                raw = b""
            retry_after = None
            try:
                retry_after = float(exc.headers.get("Retry-After")) if exc.headers.get("Retry-After") else None
            except (TypeError, ValueError):
                retry_after = None
            try:
                detail = json.loads(raw.decode("utf-8")).get("detail")
            except Exception:
                detail = raw[:200].decode("utf-8", "replace") if raw else None
            msg = _detail_message(detail) or f"HTTP {status}"
            if status == 401:
                raise Unauthorized(msg, status, detail) from None
            if status == 409:
                raise Conflict(msg, status, detail) from None
            if status == 410:
                raise Expired(msg, status, detail) from None
            if status == 429:
                raise RateLimited(msg, status, detail, retry_after) from None
            if status >= 500:
                raise ServerUnavailable(msg, status, detail, retry_after) from None
            raise ApiError(msg, status, detail) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            raise NetworkError(f"Network error: {getattr(exc, 'reason', exc)}") from None
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            raise MalformedResponse("Server sent malformed JSON", status) from None
        if not isinstance(parsed, dict):
            raise MalformedResponse("Server sent an unexpected response", status)
        return status, parsed

    # ---- pairing --------------------------------------------------------
    def request_code(self, device_name: str) -> dict:
        _, data = self._request("POST", "/api/devices/code", {"device_name": device_name})
        for key in ("device_code", "user_code", "verification_url"):
            if not isinstance(data.get(key), str) or not data[key]:
                raise MalformedResponse(f"Pairing response missing {key}")
        data.setdefault("expires_in", 600)
        data.setdefault("poll_interval", 5)
        return data

    def poll_token(self, device_code: str) -> dict:
        """Returns {"status":"authorization_pending"} or the approved token payload. Raises Expired on 410."""
        status, data = self._request("POST", "/api/devices/token", {"device_code": device_code})
        if status == 202 or data.get("status") == "authorization_pending":
            return {"status": "authorization_pending"}
        if data.get("status") == "approved" and data.get("access_token") and data.get("character_id"):
            return data
        raise MalformedResponse("Unexpected token response")

    # ---- play -------------------------------------------------------------
    def get_state(self, character_id: str) -> dict:
        _, data = self._request("GET", f"/api/game/{urllib.parse.quote(character_id, safe='')}/state", auth=True)
        if not isinstance(data.get("character"), dict) or not isinstance(data.get("location"), dict):
            raise MalformedResponse("State response missing character/location")
        return data

    def send_action(self, envelope: dict) -> dict:
        body = {k: envelope[k] for k in ("command", "idempotency_key", "expected_revision", "expanded")}
        cid = urllib.parse.quote(envelope["character_id"], safe="")
        _, data = self._request("POST", f"/api/game/{cid}/actions", body, auth=True)
        if "narration" not in data:
            raise MalformedResponse("Action response missing narration")
        return data

    # ---- trusted-device accounts (provision key replaces Turnstile) -----------
    @staticmethod
    def _account_result(data: dict) -> dict:
        if data.get("status") == "choose_character" and isinstance(data.get("characters"), list):
            return data
        if data.get("status") == "approved" and data.get("access_token") and data.get("character_id"):
            return data
        raise MalformedResponse("Unexpected account response")

    def device_signup(self, *, email: str, username: str, password: str, provision_key: str,
                      device_name: str, character_name: str | None = None) -> dict:
        body = {"email": email, "username": username, "password": password,
                "provision_key": provision_key, "device_name": device_name}
        if character_name:
            body["character_name"] = character_name
        _, data = self._request("POST", "/api/devices/signup", body)
        return self._account_result(data)

    def device_login(self, *, login: str, password: str, provision_key: str, device_name: str,
                     character_id: str | None = None) -> dict:
        body = {"login": login, "password": password, "provision_key": provision_key, "device_name": device_name}
        if character_id:
            body["character_id"] = character_id
        _, data = self._request("POST", "/api/devices/login", body)
        return self._account_result(data)
