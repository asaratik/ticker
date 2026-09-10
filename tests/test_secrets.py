"""
Tests for keyring-backed secret storage and pull source configuration.

None of these touch the real keyring: a test suite that writes to the user's
Keychain or Credential Manager is one that leaves things behind. A fake
backend is installed in place of the module instead.
"""

import sys
import types

import pytest

from ticker.auth import secrets, setup
from ticker.db import store


class FakeKeyring:
    """Stands in for the keyring module."""

    def __init__(self, failing=False, backend_is_fail=False):
        self.store = {}
        self.failing = failing
        self.backend_is_fail = backend_is_fail

    def get_password(self, service, name):
        if self.failing:
            raise RuntimeError("keychain is locked")
        return self.store.get((service, name))

    def set_password(self, service, name, secret):
        if self.failing:
            raise RuntimeError("keychain is locked")
        self.store[(service, name)] = secret

    def delete_password(self, service, name):
        if (service, name) not in self.store:
            raise KeyError(name)
        del self.store[(service, name)]


@pytest.fixture
def keyring(monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(secrets, "_backend", lambda: fake)
    return fake


@pytest.fixture
def no_keyring(monkeypatch):
    monkeypatch.setattr(secrets, "_backend", lambda: None)


# -- storage -------------------------------------------------------------

def test_a_secret_round_trips(keyring):
    secrets.set_secret("oura:Ring", "token-123")
    assert secrets.get_secret("oura:Ring") == "token-123"


def test_an_absent_secret_reads_as_none(keyring):
    assert secrets.get_secret("oura:Nothing") is None


def test_secrets_are_namespaced_by_entry_name(keyring):
    secrets.set_secret("oura:Ring", "a")
    secrets.set_secret("oura:Other ring", "b")
    assert secrets.get_secret("oura:Ring") == "a"
    assert secrets.get_secret("oura:Other ring") == "b"


def test_deleting_a_secret_removes_it(keyring):
    secrets.set_secret("oura:Ring", "x")
    assert secrets.delete_secret("oura:Ring") is True
    assert secrets.get_secret("oura:Ring") is None


def test_deleting_something_absent_is_not_an_error(keyring):
    assert secrets.delete_secret("oura:Nothing") is False


def test_a_locked_keychain_reads_as_none_rather_than_raising(monkeypatch):
    """A source that can't read its token is unhealthy, not fatal."""
    monkeypatch.setattr(secrets, "_backend", lambda: FakeKeyring(failing=True))
    assert secrets.get_secret("oura:Ring") is None


def test_storing_without_a_backend_raises(no_keyring):
    """Silently not storing a token the user just typed would leave them
    thinking they were set up when they weren't."""
    with pytest.raises(secrets.KeyringUnavailable):
        secrets.set_secret("oura:Ring", "x")


def test_reading_without_a_backend_is_quiet(no_keyring):
    assert secrets.get_secret("oura:Ring") is None
    assert secrets.available() is False


def test_a_missing_keyring_package_is_not_an_import_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", None)
    assert secrets._backend() is None


def test_keyrings_own_fail_backend_counts_as_unavailable(monkeypatch):
    """keyring installs a backend that raises only when used, which would
    turn 'no keyring here' into a 3am sync failure instead of a startup one."""
    fail_module = types.ModuleType("keyring.backends.fail")

    class Keyring:
        pass

    fail_module.Keyring = Keyring
    backends = types.ModuleType("keyring.backends")
    backends.fail = fail_module
    keyring_module = types.ModuleType("keyring")
    keyring_module.get_keyring = lambda: Keyring()
    keyring_module.backends = backends

    monkeypatch.setitem(sys.modules, "keyring", keyring_module)
    monkeypatch.setitem(sys.modules, "keyring.backends", backends)
    monkeypatch.setitem(sys.modules, "keyring.backends.fail", fail_module)
    assert secrets._backend() is None


def test_auth_ref_is_derived_from_the_source_natural_key():
    assert secrets.auth_ref("oura", "Ring") == "oura:Ring"
    # Two accounts of the same vendor must not share an entry.
    assert secrets.auth_ref("oura", "Ring") != secrets.auth_ref("oura", "Spare")


# -- setup ---------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "setup.sqlite3")
    yield connection
    connection.close()


def test_adding_a_source_stores_the_token_and_registers_it(conn, keyring):
    source_id = setup.add(conn, "oura", "Ring", token="token-123")

    assert secrets.get_secret("oura:Ring") == "token-123"
    kind, vendor, auth_ref, enabled = conn.execute(
        "SELECT kind, vendor, auth_ref, enabled FROM sources WHERE id = ?",
        (source_id,)).fetchone()
    assert (kind, vendor, enabled) == ("pull", "oura", 1)
    assert auth_ref == "oura:Ring"


def test_the_database_never_holds_the_secret_itself(conn, keyring):
    """Section 8: sources.auth_ref stores the keyring entry name only."""
    setup.add(conn, "oura", "Ring", token="super-secret-value")
    dump = "".join(
        str(row) for row in conn.execute("SELECT * FROM sources").fetchall())
    assert "super-secret-value" not in dump


def test_adding_twice_updates_rather_than_duplicates(conn, keyring):
    first = setup.add(conn, "oura", "Ring", token="one")
    second = setup.add(conn, "oura", "Ring", token="two")
    assert first == second
    assert secrets.get_secret("oura:Ring") == "two"
    assert conn.execute(
        "SELECT COUNT(*) FROM sources WHERE vendor = 'oura'").fetchone() == (1,)


def test_an_empty_token_is_rejected(conn, keyring):
    with pytest.raises(ValueError):
        setup.add(conn, "oura", "Ring", token="   ")
    # Nothing registered: the check happens before anything is written.
    # (The migration's own 'ble' row is always there, hence the vendor
    # filter rather than a bare count.)
    assert conn.execute(
        "SELECT COUNT(*) FROM sources WHERE vendor = 'oura'").fetchone() == (0,)
    assert secrets.get_secret("oura:Ring") is None


def test_removing_a_source_keeps_its_data(conn, keyring):
    """Deleting the row would cascade and take the observations with it,
    which is not what 'remove this connector' should mean."""
    source_id = setup.add(conn, "oura", "Ring", token="x")
    metric_id = conn.execute(
        "SELECT id FROM metrics WHERE name = 'spo2_pct'").fetchone()[0]
    conn.execute(
        "INSERT INTO observations (source_id, metric_id, ts, value, ingested_at) "
        "VALUES (?,?,'2026-03-01T00:00:00.000+00:00',97,'x')",
        (source_id, metric_id))

    assert setup.remove(conn, "oura", "Ring") is True
    assert secrets.get_secret("oura:Ring") is None
    assert conn.execute("SELECT enabled FROM sources WHERE id = ?",
                        (source_id,)).fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone() == (1,)


def test_removing_something_absent_reports_it(conn, keyring):
    assert setup.remove(conn, "oura", "Nothing") is False


def test_the_listing_shows_configured_sources(conn, keyring, capsys):
    setup.add(conn, "oura", "Ring", token="x")
    assert setup._dispatch(conn, _args(command="list")) == 0
    out = capsys.readouterr().out
    assert "oura" in out and "Ring" in out


def test_add_refuses_when_there_is_no_keyring(conn, no_keyring, capsys):
    assert setup._dispatch(
        conn, _args(command="add", vendor="oura", name="Ring")) == 1
    assert "keyring" in capsys.readouterr().err.lower()


def _args(**kwargs):
    namespace = type("Args", (), {})()
    for key, value in kwargs.items():
        setattr(namespace, key, value)
    return namespace
