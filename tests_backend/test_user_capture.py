"""
Unit tests for ``server.user_capture`` - per-user Plex token capture +
the per-server rate-limit gate.

Real Plex calls are mocked: ``connect_to_server`` returns a marker
object, and ``get_home_users`` returns the exact list of
``(username, token, user_server)`` tuples the test wants stored.
``media_db`` is the real per-test sandboxed instance from conftest.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Self-contained import-path setup. Normally tests_backend/conftest.py
# handles this, but the suite scaffolding is intentionally minimal
# right now and may not include a conftest. Insert the repo root so
# ``from server import ...`` always resolves.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from server import media_db, user_capture
from server.user_capture import (
    _next_attempt_seconds,
    _reset_throttle_for_tests,
    _throttle_allows,
    capture_managed_user_tokens,
)


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    """
    Self-contained sandbox: point PLEXMIGRATE_DATA_DIR at a fresh
    per-test temp dir so init_media_db() never touches the operator's
    real ``server_data/media.db``, and wipe the throttle state and
    the lazily-built Fernet/media-db singletons around every test.

    This is the same sandbox the original tests_backend/conftest.py
    provided. Inlined here so the test file is safe to run even when
    no conftest is present.
    """
    monkeypatch.setenv("PLEXMIGRATE_DATA_DIR", str(tmp_path / "server_data"))
    # Reset the lazy Fernet so the new keyfile is generated under tmp_path.
    try:
        import server.secrets as _secrets_mod
        monkeypatch.setattr(_secrets_mod, "_fernet", None, raising=False)
    except Exception:
        pass
    # Close the process-wide media_db connection so init_media_db()
    # opens a fresh one against the new tmp data dir.
    media_db._close_for_tests()
    _reset_throttle_for_tests()
    yield
    media_db._close_for_tests()
    _reset_throttle_for_tests()


# ── Throttle gate ──────────────────────────────────────────────────────────

class TestThrottle:
    def test_first_attempt_is_allowed(self):
        assert _throttle_allows("srv-1") is True

    def test_second_immediate_attempt_is_blocked(self):
        _throttle_allows("srv-1")
        assert _throttle_allows("srv-1") is False

    def test_force_bypasses_the_window(self):
        _throttle_allows("srv-1")
        assert _throttle_allows("srv-1", force=True) is True

    def test_throttle_is_per_server(self):
        _throttle_allows("srv-1")
        # A different server is its own bucket.
        assert _throttle_allows("srv-2") is True

    def test_interval_honours_setting(self, monkeypatch):
        # 3600 per_hour = 1 second interval. Two back-to-back calls
        # would normally both be blocked at the default 4/hr; with
        # 3600/hr the second still blocks because <1s has elapsed.
        monkeypatch.setattr(
            user_capture, "_throttle_min_interval_seconds", lambda: 3600.0,
        )
        _throttle_allows("srv-1")
        assert _next_attempt_seconds("srv-1") > 0


# ── capture_managed_user_tokens: happy + failure paths ──────────────────────

def _stub_capture(monkeypatch, *, home_users):
    """
    Patch the three external dependencies of capture_managed_user_tokens
    with safe fakes:
      * server_registry.get_server_by_id   -> a row with a fake token
      * server_registry.decrypt_server_token -> returns "admin-tok"
      * services.auth.connect_to_server    -> returns a marker object
      * services.auth.get_home_users       -> returns ``home_users``
    """
    from server import server_registry
    from services import auth as auth_mod

    monkeypatch.setattr(
        server_registry, "get_server_by_id",
        lambda sid, include_token=True: {
            "id": sid, "url": "http://plex:32400",
            "token": "ciphertext-placeholder",
        },
    )
    monkeypatch.setattr(server_registry, "decrypt_server_token", lambda row: "admin-tok")
    # Also patch the symbols re-exported through user_capture's late imports.
    monkeypatch.setattr(auth_mod, "connect_to_server", lambda url, tok, lg: object())
    monkeypatch.setattr(auth_mod, "get_home_users", lambda srv, url, lg: list(home_users))


class TestCaptureHappyPath:
    def test_captures_token_for_each_returned_user(self, monkeypatch):
        media_db.init_media_db()
        # Pre-populate managed_users rows so set_managed_user_credential
        # has something to update.
        media_db.upsert_managed_user(server_id="srv-x", username="alice")
        media_db.upsert_managed_user(server_id="srv-x", username="bob")
        _stub_capture(monkeypatch, home_users=[
            ("alice", "tok-alice", object()),
            ("bob", "tok-bob", object()),
        ])
        result = capture_managed_user_tokens("srv-x")
        assert result["captured"] == 2
        assert result["throttled"] is False
        assert result["errors"] == []
        # Round-trip: the encrypted tokens decrypt back to the originals.
        assert media_db.get_managed_user_credential("srv-x", "alice", "auth_token") == "tok-alice"
        assert media_db.get_managed_user_credential("srv-x", "bob", "auth_token") == "tok-bob"

    def test_users_dropped_by_get_home_users_are_not_errors(self, monkeypatch):
        # Three managed users in the DB; only two come back from
        # get_home_users (PIN-protected user with no stored PIN is
        # dropped by the auth flow upstream). The function should NOT
        # report the dropped user as an error - the preflight check
        # surfaces those separately.
        media_db.init_media_db()
        for u in ("alice", "bob", "pin-protected"):
            media_db.upsert_managed_user(server_id="srv-x", username=u)
        _stub_capture(monkeypatch, home_users=[
            ("alice", "tok-a", object()),
            ("bob", "tok-b", object()),
        ])
        result = capture_managed_user_tokens("srv-x")
        assert result["captured"] == 2
        assert result["errors"] == []

    def test_empty_home_users_list_is_clean(self, monkeypatch):
        media_db.init_media_db()
        _stub_capture(monkeypatch, home_users=[])
        result = capture_managed_user_tokens("srv-x")
        assert result == {"captured": 0, "throttled": False, "errors": []}


class TestCaptureFailureModes:
    def test_throttled_attempt_returns_throttled_true(self, monkeypatch):
        media_db.init_media_db()
        _stub_capture(monkeypatch, home_users=[("alice", "tok-a", object())])
        # First attempt goes through; second is throttled.
        first = capture_managed_user_tokens("srv-x")
        assert first["throttled"] is False
        second = capture_managed_user_tokens("srv-x")
        assert second["throttled"] is True
        assert second["captured"] == 0

    def test_force_bypasses_throttle(self, monkeypatch):
        media_db.init_media_db()
        media_db.upsert_managed_user(server_id="srv-x", username="alice")
        _stub_capture(monkeypatch, home_users=[("alice", "tok-a", object())])
        capture_managed_user_tokens("srv-x")  # consume the slot
        forced = capture_managed_user_tokens("srv-x", force=True)
        assert forced["throttled"] is False
        assert forced["captured"] == 1

    def test_unknown_server_returns_clean_error(self, monkeypatch):
        from server import server_registry
        monkeypatch.setattr(
            server_registry, "get_server_by_id",
            lambda sid, include_token=True: None,
        )
        result = capture_managed_user_tokens("no-such-srv")
        assert result["captured"] == 0
        assert any("unknown server_id" in e for e in result["errors"])

    def test_connect_returning_none_is_an_error(self, monkeypatch):
        media_db.init_media_db()
        from server import server_registry
        from services import auth as auth_mod
        monkeypatch.setattr(
            server_registry, "get_server_by_id",
            lambda sid, include_token=True: {
                "id": sid, "url": "http://plex:32400", "token": "ct",
            },
        )
        monkeypatch.setattr(server_registry, "decrypt_server_token", lambda row: "tok")
        monkeypatch.setattr(auth_mod, "connect_to_server", lambda url, tok, lg: None)
        result = capture_managed_user_tokens("srv-x")
        assert result["captured"] == 0
        assert any("connect returned None" in e for e in result["errors"])

    def test_get_home_users_exception_is_an_error(self, monkeypatch):
        media_db.init_media_db()
        from server import server_registry
        from services import auth as auth_mod
        monkeypatch.setattr(
            server_registry, "get_server_by_id",
            lambda sid, include_token=True: {
                "id": sid, "url": "http://plex:32400", "token": "ct",
            },
        )
        monkeypatch.setattr(server_registry, "decrypt_server_token", lambda row: "tok")
        monkeypatch.setattr(auth_mod, "connect_to_server", lambda url, tok, lg: object())
        def _raises(*a, **kw):
            raise RuntimeError("plex.tv 429 rate-limited")
        monkeypatch.setattr(auth_mod, "get_home_users", _raises)
        result = capture_managed_user_tokens("srv-x")
        assert result["captured"] == 0
        assert any("get_home_users failed" in e for e in result["errors"])
