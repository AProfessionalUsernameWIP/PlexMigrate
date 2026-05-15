"""
Unit tests for ``server.preflight.compute_pin_preflight``.

Self-sandboxed: each test runs against a fresh per-test temp data dir
so the real ``server_data/`` tree is never touched. The server
registry is faked via a small in-memory shim so tests don't need to
go through ``probe_unsaved`` / live Plex calls.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# Make the repo root importable without needing a conftest.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from server import media_db
from server.preflight import compute_pin_preflight


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    """Sandboxed data dir + cycled media_db connection. See
    ``tests_backend/test_user_capture.py`` for the same pattern."""
    monkeypatch.setenv("PLEXMIGRATE_DATA_DIR", str(tmp_path / "server_data"))
    try:
        import server.secrets as _secrets_mod
        monkeypatch.setattr(_secrets_mod, "_fernet", None, raising=False)
    except Exception:
        pass
    media_db._close_for_tests()
    yield
    media_db._close_for_tests()


@pytest.fixture
def fake_registry(monkeypatch):
    """
    Replace ``server_registry.get_server_by_name`` with an in-memory
    map. Tests register `(friendly_name, server_id)` pairs via
    ``fake_registry.add(...)``.
    """
    name_to_id: Dict[str, str] = {}

    def _get_by_name(name, *, include_token=True):
        sid = name_to_id.get(name)
        if sid is None:
            return None
        return {"id": sid, "name": name, "url": "http://example", "token": ""}

    from server import server_registry
    monkeypatch.setattr(server_registry, "get_server_by_name", _get_by_name)

    class _Reg:
        def add(self, name: str, server_id: str) -> None:
            name_to_id[name] = server_id

    return _Reg()


def _seed_user(server_id: str, username: str, *, kind: str = "managed",
               token: Optional[str] = None, pin: Optional[str] = None) -> None:
    """Convenience: insert a managed_users row and (optionally) its credentials."""
    media_db.init_media_db()
    media_db.upsert_managed_user(server_id=server_id, username=username, kind=kind)
    if token is not None:
        media_db.set_managed_user_credential(
            server_id=server_id, username=username,
            kind="auth_token", plaintext=token,
        )
    if pin is not None:
        media_db.set_managed_user_credential(
            server_id=server_id, username=username,
            kind="plex_home_pin", plaintext=pin,
        )


# ── Mode-aware short-circuits ───────────────────────────────────────────────

class TestModeAwareness:
    def test_restore_mode_is_a_no_op(self):
        # File-mediated restore needs no per-user auth. Always returns
        # checked=False with an empty list, even if the source has
        # at-risk users.
        out = compute_pin_preflight(
            mode="restore",
            source_server_name="Jade.TV",
            dest_server_names=None,
            user_filter=None,
        )
        assert out["checked"] is False
        assert out["at_risk_users"] == []

    def test_unknown_server_returns_unchecked(self, fake_registry):
        # No matching server in the registry -> no users to inspect.
        out = compute_pin_preflight(
            mode="snapshot",
            source_server_name="Ghost",
            dest_server_names=None,
            user_filter=None,
        )
        assert out["checked"] is False
        assert out["at_risk_users"] == []

    def test_missing_source_name_returns_unchecked(self):
        out = compute_pin_preflight(
            mode="snapshot",
            source_server_name=None,
            dest_server_names=None,
            user_filter=None,
        )
        assert out["checked"] is False
        assert out["at_risk_users"] == []


# ── Snapshot-mode user scoping ──────────────────────────────────────────────

class TestSnapshot:
    def test_user_with_no_credentials_is_at_risk(self, fake_registry):
        fake_registry.add("Jade.TV", "srv-1")
        _seed_user("srv-1", "alice")  # no token, no pin
        out = compute_pin_preflight(
            mode="snapshot",
            source_server_name="Jade.TV",
            dest_server_names=None,
            user_filter=None,
        )
        assert out["checked"] is True
        assert out["at_risk_users"] == ["alice"]
        assert out["servers_checked"] == ["Jade.TV"]

    def test_user_with_stored_token_is_safe(self, fake_registry):
        fake_registry.add("Jade.TV", "srv-1")
        _seed_user("srv-1", "alice", token="tok-a")
        out = compute_pin_preflight(
            mode="snapshot", source_server_name="Jade.TV",
            dest_server_names=None, user_filter=None,
        )
        assert out["at_risk_users"] == []

    def test_user_with_stored_pin_is_safe(self, fake_registry):
        fake_registry.add("Jade.TV", "srv-1")
        _seed_user("srv-1", "alice", pin="1234")
        out = compute_pin_preflight(
            mode="snapshot", source_server_name="Jade.TV",
            dest_server_names=None, user_filter=None,
        )
        assert out["at_risk_users"] == []

    def test_owner_is_never_flagged(self, fake_registry):
        # Even without a stored token or PIN, the owner is covered by
        # the admin token.
        fake_registry.add("Jade.TV", "srv-1")
        _seed_user("srv-1", "owner@example.com", kind="owner")
        out = compute_pin_preflight(
            mode="snapshot", source_server_name="Jade.TV",
            dest_server_names=None, user_filter=None,
        )
        assert out["at_risk_users"] == []

    def test_user_filter_narrows_scope(self, fake_registry):
        fake_registry.add("Jade.TV", "srv-1")
        _seed_user("srv-1", "alice")  # at risk
        _seed_user("srv-1", "bob")    # at risk too
        # Filter scopes to alice only - bob shouldn't surface.
        out = compute_pin_preflight(
            mode="snapshot", source_server_name="Jade.TV",
            dest_server_names=None, user_filter=["alice"],
        )
        assert out["at_risk_users"] == ["alice"]

    def test_result_is_sorted_and_deduped(self, fake_registry):
        fake_registry.add("Jade.TV", "srv-1")
        _seed_user("srv-1", "charlie")
        _seed_user("srv-1", "alice")
        _seed_user("srv-1", "bob")
        out = compute_pin_preflight(
            mode="snapshot", source_server_name="Jade.TV",
            dest_server_names=None, user_filter=None,
        )
        assert out["at_risk_users"] == ["alice", "bob", "charlie"]


# ── Direct-mode crosses source + destinations ───────────────────────────────

class TestDirect:
    def test_direct_inspects_source_and_each_destination(self, fake_registry):
        fake_registry.add("Source", "srv-src")
        fake_registry.add("DestA", "srv-A")
        fake_registry.add("DestB", "srv-B")
        # alice is missing creds on source, bob is missing on DestA,
        # carol is fully credentialed everywhere.
        _seed_user("srv-src", "alice")
        _seed_user("srv-src", "carol", token="t")
        _seed_user("srv-A", "bob")
        _seed_user("srv-A", "carol", token="t")
        _seed_user("srv-B", "carol", token="t")
        out = compute_pin_preflight(
            mode="direct",
            source_server_name="Source",
            dest_server_names=["DestA", "DestB"],
            user_filter=None,
        )
        assert out["checked"] is True
        assert out["at_risk_users"] == ["alice", "bob"]
        assert set(out["servers_checked"]) == {"Source", "DestA", "DestB"}

    def test_same_username_at_risk_on_multiple_servers_dedupes(self, fake_registry):
        fake_registry.add("Source", "srv-src")
        fake_registry.add("Dest", "srv-d")
        _seed_user("srv-src", "shared")  # at risk on source
        _seed_user("srv-d", "shared")    # at risk on dest
        out = compute_pin_preflight(
            mode="direct",
            source_server_name="Source",
            dest_server_names=["Dest"],
            user_filter=None,
        )
        assert out["at_risk_users"] == ["shared"]
