"""
Tests for the loopback OAuth2 + PKCE flow.

Two things here are security properties rather than features, and both are
tested as such: the state nonce has to actually reject a mismatched
redirect, and a rotated refresh token has to reach the keyring *before* the
new access token is handed back. The second one has no visible symptom when
it is wrong -- it only shows up as a locked-out account after a crash at the
wrong moment -- so the ordering is asserted directly.

No real sockets talk to the internet and no real keyring is touched: the
token endpoint is an injected transport, and the receiver is driven with a
plain urllib request to its own ephemeral port.
"""

import base64
import hashlib
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from ticker.auth import oauth
from ticker.sources.base import AuthExpired, PermanentError, TransientError

# Built from ordinals so this file cannot itself be saved with the bytes
# it is writing on the wire.
CRLF_TEXT = chr(13) + chr(10)


# -- PKCE -------------------------------------------------------------------

def test_the_verifier_is_within_the_length_rfc_7636_allows():
    for _ in range(20):
        verifier = oauth.generate_verifier()
        assert 43 <= len(verifier) <= 128


def test_the_verifier_is_unpadded_base64url():
    verifier = oauth.generate_verifier()
    assert "=" not in verifier
    assert "+" not in verifier and "/" not in verifier


def test_verifiers_are_not_reused():
    seen = {oauth.generate_verifier() for _ in range(50)}
    assert len(seen) == 50


def test_the_challenge_is_the_s256_digest_of_the_verifier():
    verifier = oauth.generate_verifier()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    assert oauth.challenge_for(verifier) == expected


def test_the_authorization_url_always_declares_s256():
    """The 'plain' method would send the verifier itself, defeating PKCE."""
    url = oauth.build_authorization_url(
        "https://vendor.example/authorize", client_id="abc",
        redirect_uri="http://127.0.0.1:5000/callback", scope="heartrate",
        state="nonce", code_challenge="chal")
    assert "code_challenge_method=S256" in url
    assert "code_challenge=chal" in url
    assert "response_type=code" in url


def test_the_authorization_url_keeps_an_existing_query_string():
    url = oauth.build_authorization_url(
        "https://vendor.example/authorize?tenant=eu", client_id="abc",
        redirect_uri="http://127.0.0.1:5000/callback", scope="s",
        state="n", code_challenge="c")
    assert "tenant=eu" in url
    assert url.count("?") == 1


# -- The loopback receiver --------------------------------------------------

def _get(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.read()


def test_the_receiver_binds_loopback_only():
    """0.0.0.0 would expose the authorization code to the local network."""
    with oauth.LoopbackReceiver() as receiver:
        assert receiver.redirect_uri.startswith("http://127.0.0.1:")
        assert receiver._server.server_address[0] == "127.0.0.1"


def test_the_receiver_takes_an_ephemeral_port():
    with oauth.LoopbackReceiver() as first:
        with oauth.LoopbackReceiver() as second:
            assert first.port != second.port
            assert first.port > 0


def test_a_matching_redirect_yields_the_code():
    with oauth.LoopbackReceiver() as receiver:
        got = {}

        def wait():
            got["code"] = receiver.wait("the-state", timeout=5)

        waiter = threading.Thread(target=wait)
        waiter.start()
        status, body = _get(receiver.redirect_uri + "?code=xyz&state=the-state")
        waiter.join(timeout=5)

    assert got["code"] == "xyz"
    assert status == 200
    assert b"close this tab" in body


def test_a_mismatched_state_is_refused():
    """A redirect from a different request must not have its code exchanged."""
    with oauth.LoopbackReceiver() as receiver:
        errors = {}

        def wait():
            try:
                receiver.wait("expected-state", timeout=5)
            except oauth.CallbackError as exc:
                errors["exc"] = exc

        waiter = threading.Thread(target=wait)
        waiter.start()
        _get(receiver.redirect_uri + "?code=xyz&state=attacker-state")
        waiter.join(timeout=5)

    assert "state mismatch" in str(errors["exc"])


def test_a_missing_state_is_refused():
    with oauth.LoopbackReceiver() as receiver:
        errors = {}

        def wait():
            try:
                receiver.wait("expected-state", timeout=5)
            except oauth.CallbackError as exc:
                errors["exc"] = exc

        waiter = threading.Thread(target=wait)
        waiter.start()
        _get(receiver.redirect_uri + "?code=xyz")
        waiter.join(timeout=5)

    assert "state mismatch" in str(errors["exc"])


def test_a_denied_authorization_reports_the_vendors_reason():
    with oauth.LoopbackReceiver() as receiver:
        errors = {}

        def wait():
            try:
                receiver.wait("s", timeout=5)
            except oauth.CallbackError as exc:
                errors["exc"] = exc

        waiter = threading.Thread(target=wait)
        waiter.start()
        request = urllib.request.Request(
            receiver.redirect_uri
            + "?error=access_denied&error_description=User+said+no&state=s")
        try:
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError:
            pass  # the failure page is served with 400
        waiter.join(timeout=5)

    assert "User said no" in str(errors["exc"])


def test_a_favicon_request_is_not_mistaken_for_the_redirect():
    """Browsers reliably ask for /favicon.ico; it carries no code."""
    with oauth.LoopbackReceiver() as receiver:
        base = "http://127.0.0.1:{}".format(receiver.port)
        try:
            urllib.request.urlopen(base + "/favicon.ico", timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        assert not receiver._server.done.is_set()


def test_waiting_past_the_timeout_is_an_error_not_a_hang():
    with oauth.LoopbackReceiver() as receiver:
        with pytest.raises(oauth.CallbackError) as excinfo:
            receiver.wait("s", timeout=0.2)
    assert "never" in str(excinfo.value)


def test_closing_twice_does_not_raise():
    receiver = oauth.LoopbackReceiver()
    receiver.__enter__()
    receiver.close()
    receiver.close()


def test_closing_a_receiver_that_never_started_does_not_hang():
    """shutdown() blocks until serve_forever acknowledges.

    On a receiver that was constructed and never started, nothing ever
    acknowledges, so this hangs forever -- on the `finally` path, which is
    the one taken when the flow failed early and most needs to clean up.
    """
    import threading

    receiver = oauth.LoopbackReceiver()
    done = threading.Event()

    def close():
        receiver.close()
        done.set()

    threading.Thread(target=close, daemon=True).start()
    assert done.wait(10), "close() blocked on a never-started receiver"


def test_a_receiver_that_was_never_started_never_answers():
    """The bug the start()/__init__ split exists to prevent.

    The socket is bound by the constructor, so a client connects happily and
    then waits for a response that no accept loop will ever produce.
    """
    import socket

    receiver = oauth.LoopbackReceiver()
    try:
        client = socket.create_connection(("127.0.0.1", receiver.port),
                                          timeout=5)
        request = ("GET /callback?code=a&state=b HTTP/1.0"
                   + CRLF_TEXT + CRLF_TEXT)
        client.sendall(request.encode("ascii"))
        client.settimeout(2)
        with pytest.raises((socket.timeout, TimeoutError)):
            client.recv(1024)
        client.close()
    finally:
        receiver.close()


# -- Token exchange ---------------------------------------------------------

def _transport(status=200, payload=None, captured=None):
    """A stand-in token endpoint that records what it was sent."""
    body = json.dumps(payload if payload is not None else {
        "access_token": "at-1", "refresh_token": "rt-1",
        "expires_in": 3600, "scope": "heartrate", "token_type": "Bearer",
    }).encode("utf-8")

    def transport(url, data, headers):
        if captured is not None:
            captured["url"] = url
            captured["headers"] = headers
            captured["form"] = dict(
                pair.split("=", 1) for pair in data.decode().split("&"))
        return (status, {}, body)

    return transport


def test_exchanging_a_code_sends_the_verifier_not_the_challenge():
    captured = {}
    tokens = oauth.exchange_code(
        "https://vendor.example/token", client_id="cid", code="the-code",
        code_verifier="the-verifier",
        redirect_uri="http://127.0.0.1:5000/callback",
        transport=_transport(captured=captured))

    assert captured["form"]["code_verifier"] == "the-verifier"
    assert captured["form"]["grant_type"] == "authorization_code"
    assert tokens.access_token == "at-1"
    assert tokens.refresh_token == "rt-1"


def test_a_public_client_sends_no_authorization_header():
    captured = {}
    oauth.exchange_code(
        "https://vendor.example/token", client_id="cid", code="c",
        code_verifier="v", redirect_uri="http://127.0.0.1:1/callback",
        transport=_transport(captured=captured))
    assert "Authorization" not in captured["headers"]


def test_a_client_secret_becomes_basic_auth():
    captured = {}
    oauth.exchange_code(
        "https://vendor.example/token", client_id="cid", code="c",
        code_verifier="v", redirect_uri="http://127.0.0.1:1/callback",
        client_secret="shh", transport=_transport(captured=captured))
    expected = base64.b64encode(b"cid:shh").decode()
    assert captured["headers"]["Authorization"] == "Basic " + expected


def test_expires_in_becomes_an_absolute_instant():
    before = datetime.now(timezone.utc)
    tokens = oauth.exchange_code(
        "https://vendor.example/token", client_id="c", code="c",
        code_verifier="v", redirect_uri="http://127.0.0.1:1/callback",
        transport=_transport())
    assert tokens.expires_at is not None
    assert tokens.expires_at >= before + timedelta(seconds=3599)


def test_a_rejected_grant_is_auth_expired_not_a_retry():
    """400 invalid_grant means re-authorize; retrying would spin forever."""
    with pytest.raises(AuthExpired):
        oauth.exchange_code(
            "https://vendor.example/token", client_id="c", code="stale",
            code_verifier="v", redirect_uri="http://127.0.0.1:1/callback",
            transport=_transport(status=400, payload={
                "error": "invalid_grant",
                "error_description": "Authorization code expired"}))


def test_a_server_error_is_transient():
    with pytest.raises(TransientError):
        oauth.exchange_code(
            "https://vendor.example/token", client_id="c", code="c",
            code_verifier="v", redirect_uri="http://127.0.0.1:1/callback",
            transport=_transport(status=503, payload={"error": "oops"}))


def test_a_response_without_an_access_token_is_permanent():
    with pytest.raises(PermanentError):
        oauth.exchange_code(
            "https://vendor.example/token", client_id="c", code="c",
            code_verifier="v", redirect_uri="http://127.0.0.1:1/callback",
            transport=_transport(payload={"token_type": "Bearer"}))


# -- Refresh, and the ordering that matters ---------------------------------

def test_a_rotated_refresh_token_is_persisted_before_it_is_returned():
    """Section 8: persist first, or a crash mid-refresh locks the account out.

    The persist callback records the moment it ran; the assertion is that it
    ran at all *and* that what it stored is the new refresh token, not the
    one that the vendor has just invalidated.
    """
    stored = []
    old = oauth.TokenSet(access_token="at-0", refresh_token="rt-0")

    refreshed = oauth.refresh_tokens(
        "https://vendor.example/token", client_id="cid", tokens=old,
        persist=stored.append,
        transport=_transport(payload={
            "access_token": "at-2", "refresh_token": "rt-2",
            "expires_in": 3600}))

    assert len(stored) == 1, "the new tokens were never persisted"
    assert stored[0].refresh_token == "rt-2"
    assert stored[0] is refreshed


def test_a_failed_refresh_persists_nothing():
    """A dead refresh token must not overwrite the stored one with garbage."""
    stored = []
    old = oauth.TokenSet(access_token="at-0", refresh_token="rt-0")

    with pytest.raises(AuthExpired):
        oauth.refresh_tokens(
            "https://vendor.example/token", client_id="cid", tokens=old,
            persist=stored.append,
            transport=_transport(status=400,
                                 payload={"error": "invalid_grant"}))

    assert stored == []


def test_a_vendor_that_does_not_rotate_keeps_the_existing_refresh_token():
    stored = []
    old = oauth.TokenSet(access_token="at-0", refresh_token="rt-0")

    refreshed = oauth.refresh_tokens(
        "https://vendor.example/token", client_id="cid", tokens=old,
        persist=stored.append,
        transport=_transport(payload={"access_token": "at-2",
                                      "expires_in": 3600}))

    assert refreshed.refresh_token == "rt-0"
    assert stored[0].refresh_token == "rt-0"


def test_refreshing_without_a_refresh_token_asks_for_reauthorization():
    with pytest.raises(AuthExpired):
        oauth.refresh_tokens(
            "https://vendor.example/token", client_id="cid",
            tokens=oauth.TokenSet(access_token="at-0"),
            persist=lambda _tokens: None, transport=_transport())


# -- TokenSet ---------------------------------------------------------------

def test_a_token_set_round_trips_through_json():
    original = oauth.TokenSet(
        access_token="at", refresh_token="rt",
        expires_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        scope="heartrate sleep", extra={"user_id": "ABC"})
    restored = oauth.TokenSet.from_json(original.to_json())
    assert restored == original


def test_expiry_is_judged_a_little_early():
    """A token that dies mid-request is a failure the skew exists to avoid."""
    almost = datetime.now(timezone.utc) + timedelta(
        seconds=oauth.EXPIRY_SKEW_SEC - 5)
    assert oauth.TokenSet(access_token="at", expires_at=almost).expired()

    later = datetime.now(timezone.utc) + timedelta(
        seconds=oauth.EXPIRY_SKEW_SEC + 60)
    assert not oauth.TokenSet(access_token="at", expires_at=later).expired()


def test_a_token_without_a_stated_expiry_is_treated_as_live():
    assert not oauth.TokenSet(access_token="at").expired()


def test_an_unreadable_expires_in_does_not_fail_the_exchange():
    tokens = oauth.TokenSet.from_response({"access_token": "at",
                                           "expires_in": "soon"})
    assert tokens.access_token == "at"
    assert tokens.expires_at is None


def test_unknown_response_fields_are_kept():
    """Fitbit returns user_id here, and the connector needs it."""
    tokens = oauth.TokenSet.from_response({"access_token": "at",
                                           "user_id": "ABC123"})
    assert tokens.extra["user_id"] == "ABC123"


def test_corrupt_stored_credentials_read_as_absent(monkeypatch):
    monkeypatch.setattr(oauth.secrets, "get_secret", lambda ref: "{not json")
    assert oauth.load_tokens("fitbit:me") is None


def test_absent_credentials_read_as_none(monkeypatch):
    monkeypatch.setattr(oauth.secrets, "get_secret", lambda ref: None)
    assert oauth.load_tokens("fitbit:me") is None


def test_stored_credentials_round_trip(monkeypatch):
    saved = {}
    monkeypatch.setattr(oauth.secrets, "set_secret",
                        lambda ref, blob: saved.__setitem__(ref, blob))
    monkeypatch.setattr(oauth.secrets, "get_secret", saved.get)

    tokens = oauth.TokenSet(access_token="at", refresh_token="rt")
    oauth.save_tokens("fitbit:me", tokens)
    assert oauth.load_tokens("fitbit:me") == tokens


def test_the_error_describer_truncates_and_never_returns_the_whole_body():
    detail = oauth._describe(b"x" * 5000)
    assert len(detail) <= 200
