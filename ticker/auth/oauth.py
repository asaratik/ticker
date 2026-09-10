"""
OAuth2 authorization-code flow with PKCE, over a loopback redirect.

The flow redirects to
`http://127.0.0.1:<ephemeral>/callback`, bound to loopback only, with PKCE
and a `state` nonce, and shut the listener down the moment the code arrives.

Why loopback rather than a custom URI scheme or a hosted callback: a desktop
app has nowhere secret to put a client secret, so it is a *public* client.
Public clients cannot prove who they are, which is what PKCE replaces -- the
authorization code is useless to anyone who intercepts it without the
verifier that only this process ever held.

The listener binds 127.0.0.1 explicitly, never 0.0.0.0. On a laptop on a
cafe network those differ by exactly one thing: whether a stranger can reach
the port that is, for a few seconds, holding an authorization code.

Nothing here touches SQLite and nothing here logs a token. Tokens go to the
keyring via `secrets`; `sources.auth_ref` stores only the entry name.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets as pysecrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Dict, Optional, Tuple

from ticker.auth import secrets
from ticker.sources.base import AuthExpired, PermanentError, TransientError

log = logging.getLogger(__name__)

USER_AGENT = "ticker/2 (+https://github.com/ticker)"
TIMEOUT_SEC = 30.0

# How long to wait for the user to finish in their browser. Long enough to
# find a password manager and get through an MFA prompt; short enough that a
# forgotten tab doesn't leave a socket open all afternoon.
CALLBACK_TIMEOUT_SEC = 300.0

# RFC 7636 allows a 43-128 character verifier. 64 random bytes lands at 86
# base64url characters, comfortably inside that and well past the entropy
# needed for a strong PKCE verifier.
VERIFIER_BYTES = 64

# Refresh a little before the server thinks the token dies, so a slow
# request doesn't start valid and finish expired.
EXPIRY_SKEW_SEC = 60


def _b64url(raw: bytes) -> str:
    """base64url with the padding stripped, which is what RFC 7636 wants."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def generate_verifier() -> str:
    """A fresh PKCE code verifier. Never reused, never logged."""
    return _b64url(pysecrets.token_bytes(VERIFIER_BYTES))


def challenge_for(verifier: str) -> str:
    """The S256 challenge for a verifier.

    S256 only -- the "plain" method sends the verifier itself to the
    authorization server, which gives up the entire point of PKCE.
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return _b64url(digest)


def generate_state() -> str:
    """A nonce tying the redirect back to the request this process made."""
    return _b64url(pysecrets.token_bytes(16))


@dataclass
class TokenSet:
    """What a token endpoint hands back, plus when it stops working.

    `expires_at` is absolute and UTC rather than the `expires_in` seconds the
    server sends, because a duration is only meaningful next to the instant
    it was measured from, and that instant is gone by the time this is read
    back out of the keyring.
    """

    access_token: str
    refresh_token: Optional[str] = None
    expires_at: Optional[datetime] = None
    scope: str = ""
    token_type: str = "Bearer"
    extra: Dict[str, str] = field(default_factory=dict)

    def expired(self, now: Optional[datetime] = None) -> bool:
        """True if this should be refreshed before the next request.

        A token with no stated expiry is treated as live: some vendors issue
        non-expiring tokens, and refreshing one on every call would burn rate
        limit for nothing.
        """
        if self.expires_at is None:
            return False
        now = now or datetime.now(timezone.utc)
        return now >= self.expires_at - timedelta(seconds=EXPIRY_SKEW_SEC)

    def to_json(self) -> str:
        return json.dumps({
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "scope": self.scope,
            "token_type": self.token_type,
            "extra": self.extra,
        })

    @classmethod
    def from_json(cls, blob: str) -> "TokenSet":
        data = json.loads(blob)
        raw_expiry = data.get("expires_at")
        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=datetime.fromisoformat(raw_expiry) if raw_expiry else None,
            scope=data.get("scope", ""),
            token_type=data.get("token_type", "Bearer"),
            extra=data.get("extra", {}),
        )

    @classmethod
    def from_response(cls, payload: dict,
                      now: Optional[datetime] = None) -> "TokenSet":
        """Build from a token endpoint's JSON body.

        `expires_in` is optional and occasionally arrives as a string; both
        shapes are accepted rather than failing an otherwise good exchange.
        """
        now = now or datetime.now(timezone.utc)
        expires_at = None
        raw = payload.get("expires_in")
        if raw is not None:
            try:
                expires_at = now + timedelta(seconds=int(raw))
            except (TypeError, ValueError):
                log.warning("token response had an unreadable expires_in")
        known = {"access_token", "refresh_token", "expires_in", "scope",
                 "token_type"}
        return cls(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token"),
            expires_at=expires_at,
            scope=payload.get("scope", ""),
            token_type=payload.get("token_type", "Bearer"),
            extra={k: str(v) for k, v in payload.items() if k not in known},
        )


def load_tokens(ref: str) -> Optional[TokenSet]:
    """Read a stored TokenSet, or None if there isn't a usable one."""
    blob = secrets.get_secret(ref)
    if not blob:
        return None
    try:
        return TokenSet.from_json(blob)
    except (ValueError, KeyError) as exc:
        # A corrupt entry is worth saying out loud -- it looks exactly like
        # "never authenticated" from the caller's side otherwise.
        log.warning("stored credentials for %r are unreadable: %s", ref, exc)
        return None


def save_tokens(ref: str, tokens: TokenSet) -> None:
    """Persist a TokenSet. Raises KeyringUnavailable if it can't be stored."""
    secrets.set_secret(ref, tokens.to_json())


class CallbackError(RuntimeError):
    """The redirect came back as a failure, or never came back at all."""


_SUCCESS_PAGE = (
    b"<!doctype html><meta charset=utf-8><title>Ticker</title>"
    b"<body style='font-family:system-ui;padding:3rem'>"
    b"<h1>Connected.</h1><p>You can close this tab and go back to Ticker.</p>"
)
_FAILURE_PAGE = (
    b"<!doctype html><meta charset=utf-8><title>Ticker</title>"
    b"<body style='font-family:system-ui;padding:3rem'>"
    b"<h1>Authorization failed.</h1><p>Go back to Ticker for the details.</p>"
)


class _CallbackHandler(BaseHTTPRequestHandler):
    """Handles exactly one useful GET and records what it saw.

    Browsers request more than the callback -- /favicon.ico most reliably --
    so anything that is not the callback path gets a 404 and is otherwise
    ignored rather than being mistaken for the redirect.
    """

    def do_GET(self):  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != self.server.callback_path:
            self.send_response(404)
            self.end_headers()
            return

        params = urllib.parse.parse_qs(parsed.query)
        self.server.result = {k: v[0] for k, v in params.items()}
        ok = "code" in self.server.result and "error" not in self.server.result
        body = _SUCCESS_PAGE if ok else _FAILURE_PAGE
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.server.done.set()

    def log_message(self, fmt, *args):
        """Silence the default stderr access log.

        The request line contains the authorization code, so it must not
        appear in logs.
        """


class LoopbackReceiver:
    """A one-shot HTTP listener on 127.0.0.1 that catches the redirect.

    Used as a context manager so the socket is closed on every path out,
    including the user closing the browser and the timeout expiring:

        with LoopbackReceiver() as receiver:
            webbrowser.open(build_authorization_url(..., receiver.redirect_uri))
            code = receiver.wait(state)
    """

    def __init__(self, callback_path: str = "/callback",
                 host: str = "127.0.0.1", port: int = 0):
        # Port 0 asks the OS for an ephemeral port. Binding a fixed one would
        # collide with whatever else is running and, worse, make the redirect
        # URI guessable.
        self._server = HTTPServer((host, port), _CallbackHandler)
        self._server.callback_path = callback_path
        self._server.result = None
        self._server.done = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.callback_path = callback_path

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def redirect_uri(self) -> str:
        return "http://127.0.0.1:{}{}".format(self.port, self.callback_path)

    def start(self) -> "LoopbackReceiver":
        """Begin answering requests.

        Separate from __init__ because the constructor binds the socket --
        which is what fixes the port, and therefore the redirect URI that
        goes into the authorization URL -- while this starts serving. A
        caller that builds the URL before serving (begin_authorization does)
        needs the port early and the accept loop only once the browser is
        about to be sent. Calling it twice is a no-op.

        Nothing answers the redirect until this runs: the socket is listening,
        so the browser connects and then waits forever for a response.
        """
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                name="oauth-callback", daemon=True)
            self._thread.start()
        return self

    def __enter__(self) -> "LoopbackReceiver":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.close()

    def wait(self, expected_state: str,
             timeout: float = CALLBACK_TIMEOUT_SEC) -> str:
        """Block until the redirect arrives; return the authorization code.

        Raises CallbackError if the user denied it, the state doesn't match,
        or nobody came back before the timeout.
        """
        if not self._server.done.wait(timeout):
            raise CallbackError(
                "no redirect after {:.0f}s -- authorization was never "
                "completed in the browser".format(timeout))
        result = self._server.result or {}

        # Check state before anything else in the response is believed: a
        # mismatch means this redirect belongs to a different request, and
        # its code should not be exchanged.
        got_state = result.get("state", "")
        if not hmac.compare_digest(got_state, expected_state):
            raise CallbackError(
                "state mismatch -- the redirect did not come from the "
                "request this process started")

        if "error" in result:
            detail = result.get("error_description") or result["error"]
            raise CallbackError("authorization denied: {}".format(detail))
        code = result.get("code")
        if not code:
            raise CallbackError("redirect carried no authorization code")
        return code

    def close(self) -> None:
        """Stop serving and release the port. Safe to call twice.

        shutdown() is only called if the accept loop was actually started:
        it blocks until serve_forever acknowledges, so calling it on a
        receiver that was constructed and never started hangs forever. That
        matters because close() runs in the `finally` of the flow, which is
        exactly the path taken when something went wrong early.
        """
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=5)
            self._thread = None
        self._server.server_close()


def build_authorization_url(authorize_url: str, client_id: str,
                            redirect_uri: str, scope: str, state: str,
                            code_challenge: str,
                            extra: Optional[Dict[str, str]] = None) -> str:
    """The URL to open in the user's browser to start the flow."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    params.update(extra or {})
    joiner = "&" if "?" in authorize_url else "?"
    return authorize_url + joiner + urllib.parse.urlencode(params)


Transport = Callable[[str, bytes, Dict[str, str]],
                     Tuple[int, Dict[str, str], bytes]]


def _urllib_transport(url: str, body: bytes, headers: Dict[str, str]
                      ) -> Tuple[int, Dict[str, str], bytes]:
    """POST a form and return (status, headers, body).

    Injectable so the flows can be tested without a network peer, the same
    way the Oura connector does it.
    """
    request = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
            return (response.status, dict(response.headers), response.read())
    except urllib.error.HTTPError as exc:
        return (exc.code, dict(exc.headers or {}), exc.read())
    except urllib.error.URLError as exc:
        raise TransientError("could not reach {}: {}".format(url, exc.reason))


def _describe(payload: bytes) -> str:
    """A short, safe description of an error body.

    Bodies are truncated and only the documented error fields are pulled out,
    because a token endpoint's error response can echo back parts of the
    request -- and this string ends up in logs.
    """
    try:
        data = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return payload[:200].decode("utf-8", "replace")
    if isinstance(data, dict):
        for key in ("error_description", "error", "message"):
            if data.get(key):
                return str(data[key])[:200]
    return "no detail"


def _post_token_request(token_url: str, form: Dict[str, str],
                        client_id: str, client_secret: Optional[str],
                        transport: Optional[Transport]) -> TokenSet:
    """Shared body of the code exchange and the refresh."""
    transport = transport or _urllib_transport
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if client_secret:
        # A confidential client authenticates the token request itself.
        # Public clients (the desktop default) send client_id in the form and
        # rely on PKCE instead.
        raw = "{}:{}".format(client_id, client_secret).encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")

    body = urllib.parse.urlencode(form).encode("utf-8")
    status, _headers, payload = transport(token_url, body, headers)

    if status >= 400:
        detail = _describe(payload)
        if status in (400, 401):
            # 400 invalid_grant is the usual shape of a dead or already-used
            # refresh token, which is a re-authorize, not a retry.
            raise AuthExpired(
                "token endpoint rejected the request: {}".format(detail))
        if status >= 500:
            raise TransientError(
                "token endpoint failed ({}): {}".format(status, detail))
        raise PermanentError(
            "token endpoint returned {}: {}".format(status, detail))

    try:
        data = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise PermanentError("token endpoint returned non-JSON: {}".format(exc))
    if "access_token" not in data:
        raise PermanentError(
            "token response carried no access_token: {}".format(_describe(payload)))
    return TokenSet.from_response(data)


def exchange_code(token_url: str, client_id: str, code: str,
                  code_verifier: str, redirect_uri: str,
                  client_secret: Optional[str] = None,
                  transport: Optional[Transport] = None) -> TokenSet:
    """Trade an authorization code for tokens.

    `redirect_uri` must be byte-identical to the one used in the
    authorization request -- servers compare it exactly, and the ephemeral
    port makes it different on every run.
    """
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    return _post_token_request(token_url, form, client_id, client_secret,
                               transport)


def refresh_tokens(token_url: str, client_id: str, tokens: TokenSet,
                   persist: Callable[[TokenSet], None],
                   client_secret: Optional[str] = None,
                   transport: Optional[Transport] = None) -> TokenSet:
    """Exchange a refresh token for a new TokenSet, persisting it first.

    The ordering matters because refresh tokens rotate, so the response
    usually carries a *new* refresh token and
    invalidates the old one. Persist before returning -- if this process dies
    between the response and the write, the stored refresh token is already
    dead and the account is locked out until the user re-authorizes by hand.

    Vendors that don't rotate omit `refresh_token` from the response; the
    existing one is carried forward so it isn't lost on the round trip.
    """
    if not tokens.refresh_token:
        raise AuthExpired("no refresh token stored; re-authorization required")

    form = {
        "grant_type": "refresh_token",
        "refresh_token": tokens.refresh_token,
        "client_id": client_id,
    }
    refreshed = _post_token_request(token_url, form, client_id, client_secret,
                                    transport)
    if not refreshed.refresh_token:
        refreshed.refresh_token = tokens.refresh_token

    persist(refreshed)
    return refreshed
