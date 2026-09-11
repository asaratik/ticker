"""
Tests for large secrets: split across keyring entries, read back whole.

Windows' credential store refuses more than 2,560 bytes in one entry, and
Garmin's session tokens are bigger. A fake backend stands in for the OS
keyring and enforces the same limit, so a regression to storing it whole
fails here the way it would on a real Windows machine.
"""

import pytest

from ticker.auth import secrets


class SmallKeyring:
    LIMIT = 2560

    def __init__(self):
        self.entries = {}

    def get_password(self, service, ref):
        return self.entries.get((service, ref))

    def set_password(self, service, ref, value):
        if len(value.encode("utf-16-le")) > self.LIMIT:
            raise ValueError("The stub received bad data")   # what Windows says
        self.entries[(service, ref)] = value

    def delete_password(self, service, ref):
        if (service, ref) not in self.entries:
            raise KeyError(ref)
        del self.entries[(service, ref)]


@pytest.fixture
def keyring(monkeypatch):
    backend = SmallKeyring()
    monkeypatch.setattr(secrets, "_backend", lambda: backend)
    return backend


def test_a_large_secret_round_trips(keyring):
    token = "x" * 5000
    secrets.set_large_secret("garmin:G", token)
    assert secrets.get_large_secret("garmin:G") == token
    assert len(keyring.entries) == 6                 # an index and five parts


def test_a_shorter_secret_leaves_no_stale_parts(keyring):
    secrets.set_large_secret("garmin:G", "x" * 5000)
    secrets.set_large_secret("garmin:G", "short")
    assert secrets.get_large_secret("garmin:G") == "short"
    assert len(keyring.entries) == 2


def test_a_secret_stored_whole_reads_back_the_same_way(keyring):
    secrets.set_secret("oura:Ring", "token")
    assert secrets.get_large_secret("oura:Ring") == "token"


def test_deleting_removes_every_part(keyring):
    secrets.set_large_secret("garmin:G", "x" * 5000)
    assert secrets.delete_large_secret("garmin:G")
    assert keyring.entries == {}
    assert secrets.get_large_secret("garmin:G") is None


def test_a_missing_part_reads_as_no_secret_rather_than_a_corrupt_one(keyring):
    secrets.set_large_secret("garmin:G", "x" * 5000)
    del keyring.entries[(secrets.SERVICE, "garmin:G#2")]
    assert secrets.get_large_secret("garmin:G") is None
