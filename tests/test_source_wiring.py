"""
Tests that every connector is reachable from the two registries.

A connector that exists but is in neither registry is not a feature: nothing
can configure it and nothing will sync it, and no test of the connector
itself notices. That is the whole subject of this file.

The end-to-end case drives the Fitbit OAuth flow with a stub browser and a
stub token endpoint, from `setup add fitbit` through to the sync layer
building a connector that answers with the stored token. Nothing here
touches the network or the real keyring.
"""

import json
import threading
import urllib.parse
import urllib.request

import pytest

from ticker.auth import oauth, secrets, setup
from ticker.db import migrate
from ticker.ingest import sync
from ticker.sources import fitbit


@pytest.fixture
def keyring(monkeypatch):
    vault = {}

    class FakeKeyring:
        def get_password(self, service, name):
            return vault.get((service, name))

        def set_password(self, service, name, secret):
            vault[(service, name)] = secret

        def delete_password(self, service, name):
            vault.pop((service, name), None)

    monkeypatch.setattr(secrets, "_backend", lambda: FakeKeyring())
    return vault


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "ticker.sqlite3"
    migrate.migrate(path, backup=False)
    connection = migrate.open_db(path)
    yield connection
    connection.close()


# -- The registries agree with each other -----------------------------------

def test_every_configurable_pull_vendor_has_a_connector():
    """Otherwise `setup add` succeeds and sync then skips the source."""
    configurable = {vendor for vendor, (kind, _name, _style)
                    in setup.VENDORS.items() if kind == "pull"}
    assert configurable <= set(sync.BUILDERS)


def test_every_connector_can_be_configured():
    """The other direction: a builder nothing can create a source row for."""
    assert set(sync.BUILDERS) <= set(setup.VENDORS)


def test_fitbit_is_registered_in_both():
    assert "fitbit" in setup.VENDORS
    assert "fitbit" in sync.BUILDERS


def test_each_vendor_declares_a_known_auth_style():
    # 'password' is Garmin's: signed in with once, and never stored.
    for vendor, (_kind, _name, style) in setup.VENDORS.items():
        assert style in {"token", "oauth", "password"}, vendor


def test_the_builders_produce_pull_sources():
    from ticker.sources import base
    for vendor, builder in sync.BUILDERS.items():
        built = builder(auth_ref="{}:x".format(vendor), on_payload=None,
                        on_session=None)
        assert isinstance(built, base.PullSource), vendor


# -- Fitbit needs configuration Oura does not -------------------------------

def test_the_fitbit_builder_passes_the_configured_timezone(monkeypatch):
    """Without it the connector defaults to UTC and misplaces everything."""
    from ticker import config as tconfig
    monkeypatch.setattr(tconfig, "FITBIT_PROFILE_TZ", "America/New_York")
    built = sync.BUILDERS["fitbit"](auth_ref="fitbit:x", on_payload=None,
                                    on_session=None)
    assert built.profile_tz == "America/New_York"


def test_the_fitbit_builder_passes_the_client_id(monkeypatch):
    from ticker import config as tconfig
    monkeypatch.setattr(tconfig, "FITBIT_CLIENT_ID", "cid-123")
    built = sync.BUILDERS["fitbit"](auth_ref="fitbit:x", on_payload=None,
                                    on_session=None)
    assert built.client_id == "cid-123"


def test_the_default_profile_timezone_is_a_resolvable_iana_name():
    """tzname() looks like an answer and is not one on Windows."""
    from zoneinfo import ZoneInfo

    from ticker import config as tconfig
    ZoneInfo(tconfig.FITBIT_PROFILE_TZ)


# -- The OAuth setup path, end to end ---------------------------------------

def _stub_token_endpoint(captured):
    def transport(url, data, headers):
        captured.update(dict(pair.split("=", 1)
                             for pair in data.decode().split("&")))
        return (200, {}, json.dumps({
            "access_token": "at-1", "refresh_token": "rt-1",
            "expires_in": 28800, "user_id": "ABC"}).encode())
    return transport


def _stub_browser(url):
    """Answer the redirect the way Fitbit's authorization page would."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    redirect = query["redirect_uri"][0]
    state = query["state"][0]

    def go():
        try:
            urllib.request.urlopen(
                "{}?code=the-code&state={}".format(redirect, state),
                timeout=10).read()
        except Exception:
            pass

    threading.Thread(target=go, daemon=True).start()


def test_adding_fitbit_stores_tokens_and_enables_the_source(
        conn, keyring, monkeypatch):
    from ticker import config as tconfig
    monkeypatch.setattr(tconfig, "FITBIT_CLIENT_ID", "test-client")

    captured = {}
    real = fitbit.complete_authorization
    monkeypatch.setattr(
        fitbit, "complete_authorization",
        lambda r, s, v, c, ref: real(r, s, v, c, ref,
                                     transport=_stub_token_endpoint(captured)))

    source_id = setup.add_oauth(conn, "fitbit", "Watch",
                                open_browser=_stub_browser)

    kind, vendor, name, ref, enabled = conn.execute(
        "SELECT kind, vendor, display_name, auth_ref, enabled FROM sources "
        "WHERE id = ?", (source_id,)).fetchone()
    assert (kind, vendor, name, enabled) == ("pull", "fitbit", "Watch", 1)

    stored = oauth.load_tokens(ref)
    assert stored.access_token == "at-1"
    assert stored.refresh_token == "rt-1"
    # PKCE actually took part, rather than the flow merely resembling it.
    assert captured["code_verifier"]


def test_the_client_id_is_not_stored_as_a_secret(conn, keyring, monkeypatch):
    """It identifies a public client; only tokens belong in the keyring."""
    from ticker import config as tconfig
    monkeypatch.setattr(tconfig, "FITBIT_CLIENT_ID", "test-client")
    real = fitbit.complete_authorization
    monkeypatch.setattr(
        fitbit, "complete_authorization",
        lambda r, s, v, c, ref: real(r, s, v, c, ref,
                                     transport=_stub_token_endpoint({})))

    setup.add_oauth(conn, "fitbit", "Watch", open_browser=_stub_browser)

    assert not any("test-client" in str(value) for value in keyring.values())


def test_a_configured_source_is_built_by_the_sync_layer(
        conn, keyring, monkeypatch):
    """The join the two registries exist for."""
    from ticker import config as tconfig
    monkeypatch.setattr(tconfig, "FITBIT_CLIENT_ID", "test-client")
    real = fitbit.complete_authorization
    monkeypatch.setattr(
        fitbit, "complete_authorization",
        lambda r, s, v, c, ref: real(r, s, v, c, ref,
                                     transport=_stub_token_endpoint({})))
    setup.add_oauth(conn, "fitbit", "Watch", open_browser=_stub_browser)

    built = sync.build_sources(conn, writer=None)

    assert len(built) == 1
    _source_id, connector = built[0]
    assert connector.vendor == "fitbit"
    assert connector.access_token() == "at-1"


def test_adding_fitbit_without_a_client_id_explains_itself(conn, keyring,
                                                           monkeypatch):
    from ticker import config as tconfig
    monkeypatch.setattr(tconfig, "FITBIT_CLIENT_ID", "")
    with pytest.raises(ValueError) as excinfo:
        setup.add_oauth(conn, "fitbit", "Watch", open_browser=_stub_browser)
    assert "TICKER_FITBIT_CLIENT_ID" in str(excinfo.value)


def test_a_vendor_with_no_oauth_flow_is_refused(conn, keyring):
    with pytest.raises(ValueError):
        setup.add_oauth(conn, "oura", "Ring", open_browser=_stub_browser)


def test_the_cli_lists_both_vendors(capsys):
    with pytest.raises(SystemExit):
        setup.main(["add", "--help"])
    printed = capsys.readouterr().out
    assert "fitbit" in printed and "oura" in printed
