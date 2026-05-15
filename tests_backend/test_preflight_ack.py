"""
Unit tests for the PR-12 preflight-acknowledgement plumbing:

  * ``server.app._apply_preflight_ack`` - re-maps the public-facing
    ``pin_preflight_acknowledged`` / ``pin_preflight_at_risk`` body
    fields onto the underscore-prefixed synthetic params the engine
    consumes on the JobRecord.
  * ``server.jobs._log_pin_preflight_ack`` - writes the per-run audit
    line when an acknowledged job actually starts running.

Both are pure helpers, so the tests don't touch live Plex, the
FastAPI app, or the job worker thread.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    """Belt-and-suspenders: even though these tests don't touch the
    DB, sandbox the data dir so any incidental ``load_settings()`` or
    media.db open during import doesn't hit the operator's real tree."""
    monkeypatch.setenv("PLEXMIGRATE_DATA_DIR", str(tmp_path / "server_data"))


# ── _apply_preflight_ack: the body-field re-mapping ────────────────────────

class TestApplyPreflightAck:
    def test_ack_true_remaps_to_underscore_prefix(self):
        from server.app import _apply_preflight_ack
        params = {
            "pin_preflight_acknowledged": True,
            "pin_preflight_at_risk": ["alice", "bob"],
            "source_server_name": "Plex",
        }
        _apply_preflight_ack(params)
        # Public fields stripped.
        assert "pin_preflight_acknowledged" not in params
        assert "pin_preflight_at_risk" not in params
        # Underscore-prefixed synthetics stamped.
        assert params["_pin_preflight_acknowledged"] is True
        assert params["_pin_preflight_at_risk"] == ["alice", "bob"]
        # Unrelated fields untouched.
        assert params["source_server_name"] == "Plex"

    def test_ack_false_drops_both_fields_silently(self):
        from server.app import _apply_preflight_ack
        params = {
            "pin_preflight_acknowledged": False,
            "pin_preflight_at_risk": ["should-not-appear"],
        }
        _apply_preflight_ack(params)
        # No underscore-prefixed flags created.
        assert params == {}

    def test_ack_absent_is_a_no_op(self):
        from server.app import _apply_preflight_ack
        params = {"source_server_name": "Plex"}
        _apply_preflight_ack(params)
        assert params == {"source_server_name": "Plex"}

    def test_non_list_at_risk_normalises_to_empty_list(self):
        from server.app import _apply_preflight_ack
        params = {
            "pin_preflight_acknowledged": True,
            "pin_preflight_at_risk": "not a list",  # bad client input
        }
        _apply_preflight_ack(params)
        assert params["_pin_preflight_at_risk"] == []

    def test_non_string_items_in_at_risk_are_filtered(self):
        from server.app import _apply_preflight_ack
        params = {
            "pin_preflight_acknowledged": True,
            "pin_preflight_at_risk": ["alice", None, 42, "bob"],
        }
        _apply_preflight_ack(params)
        assert params["_pin_preflight_at_risk"] == ["alice", "bob"]


# ── _log_pin_preflight_ack: the audit-log emitter ──────────────────────────

class TestLogPinPreflightAck:
    def _rec(self, **params):
        from server.jobs import JobRecord
        return JobRecord(job_id="j1", mode="snapshot", params=params)

    def test_silent_when_flag_absent(self, caplog):
        from server.jobs import _log_pin_preflight_ack
        rec = self._rec()
        with caplog.at_level(logging.WARNING):
            _log_pin_preflight_ack(rec, logging.getLogger("test"))
        assert caplog.records == []

    def test_warns_when_flag_set_with_at_risk_list(self, caplog):
        from server.jobs import _log_pin_preflight_ack
        rec = self._rec(
            _pin_preflight_acknowledged=True,
            _pin_preflight_at_risk=["alice", "bob"],
        )
        with caplog.at_level(logging.WARNING, logger="test"):
            _log_pin_preflight_ack(rec, logging.getLogger("test"))
        # One warning that names both at-risk users.
        msgs = [r.getMessage() for r in caplog.records]
        assert any("alice" in m and "bob" in m for m in msgs)

    def test_warns_with_placeholder_when_at_risk_missing(self, caplog):
        from server.jobs import _log_pin_preflight_ack
        rec = self._rec(_pin_preflight_acknowledged=True)
        with caplog.at_level(logging.WARNING, logger="test"):
            _log_pin_preflight_ack(rec, logging.getLogger("test"))
        assert any("none specified" in r.getMessage() for r in caplog.records)


# ── Model integration: the Pydantic input models accept the new fields ─────

class TestModelIntegration:
    def test_snapshot_job_in_accepts_ack_fields(self):
        from server.models import SnapshotJobIn
        m = SnapshotJobIn(
            source_server_name="Plex",
            pin_preflight_acknowledged=True,
            pin_preflight_at_risk=["alice"],
        )
        d = m.model_dump(exclude_none=True)
        assert d["pin_preflight_acknowledged"] is True
        assert d["pin_preflight_at_risk"] == ["alice"]

    def test_restore_job_in_accepts_ack_fields(self):
        from server.models import RestoreJobIn
        m = RestoreJobIn(pin_preflight_acknowledged=True)
        d = m.model_dump(exclude_none=True)
        assert d["pin_preflight_acknowledged"] is True

    def test_direct_transfer_in_accepts_ack_fields(self):
        from server.models import DirectTransferIn
        m = DirectTransferIn(
            source_server_name="Src",
            dest_server_name="Dst",
            pin_preflight_acknowledged=True,
        )
        d = m.model_dump(exclude_none=True)
        assert d["pin_preflight_acknowledged"] is True

    def test_field_defaults_keep_old_clients_working(self):
        # A client that doesn't know the new fields still constructs
        # the model fine, and the dump excludes the False/None values
        # so nothing leaks to the params dict.
        from server.models import SnapshotJobIn
        m = SnapshotJobIn(source_server_name="Plex")
        d = m.model_dump(exclude_none=True)
        # ``False`` defaults can't be hidden by exclude_none, so the
        # ack field will be present but False. The endpoint helper
        # treats False as a no-op and strips it.
        assert d.get("pin_preflight_acknowledged", False) is False
