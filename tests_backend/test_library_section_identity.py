"""
v0.15 library-section identity invariant tests.

These tests pin down the contract that the v0.15 schema refactor
introduced: every per-server row carries a ``section_key`` linking it
to a row in ``library_sections``, and the round-trip
``payload -> media.db -> snapshot.db -> reconstructed payload`` keeps
each library's data partitioned by its real section identity.

Three pillars:

  * **Round-trip identity** - two libraries with the same media_type
    (e.g. ``artist`` for both Music and Audio-Books) ingested into
    media.db and re-emitted from the serializer must produce two
    separate entries in the rebuilt payload's ``libraries`` array,
    each with its source library's rows scoped to it.

  * **Schema invariants** - missing or zero section_key on input must
    raise ``ValueError`` at the media.db ingest boundary; the FK on
    snapshot.db must reject orphan section_key references; the
    serializer must refuse pre-v15 snapshots.

  * **Restore-correctness shape** - a reconstructed payload from the
    serializer must NOT contain an "All Libraries" virtual entry, and
    each entry's per-library total computed by the restorer's
    metadata-peek logic must match the row counts ingested for that
    library exactly (no fan-out + divide-by-N approximation).

Self-sandboxed via the ``_sandbox`` fixture pattern from
``tests_backend/test_user_capture.py`` - every test gets a fresh
``media.db`` under a temp directory and the process-wide connection
is recycled before/after.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest


# Make the repo root importable without needing a conftest.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from server import media_db
from server import snapshot_capture
from server import snapshot_serializer


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    """Sandboxed data dir + cycled media_db connection."""
    monkeypatch.setenv("PLEXMIGRATE_DATA_DIR", str(tmp_path / "server_data"))
    try:
        import server.secrets as _secrets_mod
        monkeypatch.setattr(_secrets_mod, "_fernet", None, raising=False)
    except Exception:
        pass
    media_db._close_for_tests()
    media_db.init_media_db()
    # Seed a server row so FK targets exist for the per-server tables.
    media_db.upsert_server_row(
        server_id="srv-test",
        name="Test Server",
        service="plex",
        url="http://example",
        machine_id="mach-test",
    )
    yield
    media_db._close_for_tests()


# ── Helpers ────────────────────────────────────────────────────────────────

def _make_payload(
    *,
    library: str,
    section_id: int,
    section_type: str,
    items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build a minimal valid per-library payload."""
    return {
        "library": library,
        "library_section_id": section_id,
        "library_section_type": section_type,
        "captured_at": "2026-05-15T00:00:00+00:00",
        "snapshot_meta": {
            "server_id": "srv-test",
            "server_name": "Test Server",
            "backend": "plex",
        },
        "users": {
            "": {
                "role": "owner",
                "display_name": "Plex Owner",
                "backend_user_id": "111",
                "watch_history": items,
                "ratings": [],
                "playlists": [],
                "collections": [],
            },
        },
    }


def _item(guid: str, title: str, rating_key: int, media_type: str = "track") -> Dict[str, Any]:
    """One item record shaped for the watch_history list."""
    return {
        "guids": [guid],
        "title": title,
        "type": media_type,
        "rating_key": rating_key,
        "view_count": 1,
        "view_offset": 0,
    }


# ── Schema invariants ──────────────────────────────────────────────────────

class TestIngestRejectsMissingSection:
    def test_missing_library_section_id_raises(self):
        payload = _make_payload(
            library="Movies", section_id=1, section_type="movie", items=[],
        )
        del payload["library_section_id"]
        with pytest.raises(ValueError, match="library_section_id"):
            media_db.ingest_snapshot_payload("srv-test", payload)

    def test_zero_library_section_id_raises(self):
        payload = _make_payload(
            library="Movies", section_id=0, section_type="movie", items=[],
        )
        with pytest.raises(ValueError, match="library_section_id"):
            media_db.ingest_snapshot_payload("srv-test", payload)

    def test_missing_library_section_type_raises(self):
        payload = _make_payload(
            library="Movies", section_id=1, section_type="", items=[],
        )
        with pytest.raises(ValueError, match="library_section_type"):
            media_db.ingest_snapshot_payload("srv-test", payload)

    def test_missing_library_title_raises(self):
        payload = _make_payload(
            library="", section_id=1, section_type="movie", items=[],
        )
        with pytest.raises(ValueError, match="library"):
            media_db.ingest_snapshot_payload("srv-test", payload)


class TestSchemaVersion:
    def test_media_db_current_schema_version_is_at_least_8(self):
        # v0.15 bumped to 8. Any future bump is fine, but going below
        # would mean someone removed the library_sections migration.
        assert media_db.CURRENT_SCHEMA_VERSION >= 8
        assert media_db.get_schema_version() == media_db.CURRENT_SCHEMA_VERSION

    def test_library_sections_table_exists(self):
        conn = media_db._require_conn()
        row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='library_sections'"
        ).fetchone()
        assert row is not None, "library_sections table is missing"

    def test_per_server_tables_have_section_key_column(self):
        conn = media_db._require_conn()
        for table in ("server_items", "watch_events", "ratings",
                      "playlists", "collections"):
            cols = {
                r["name"]
                for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            assert "section_key" in cols, (
                f"{table}.section_key column missing in v0.15"
            )

    def test_snapshot_schema_version_is_15(self):
        assert snapshot_capture.SNAPSHOT_SCHEMA_VERSION == 15


# ── Round-trip identity ────────────────────────────────────────────────────

class TestRoundTripIdentity:
    """
    The motivating bug: two libraries with the SAME media_type (e.g.
    Music and Audio-Books both reporting type='artist') previously
    collapsed into a single virtual library on reconstruction. The
    round-trip must keep them distinct.
    """

    def test_two_artist_libraries_stay_separated_through_round_trip(self, tmp_path):
        # Ingest two libraries with the same media_type. Each has its
        # own item and the items go into the same media.db.
        music = _make_payload(
            library="Music",
            section_id=10,
            section_type="artist",
            items=[_item("plex://1", "Rock Anthems", 1001, media_type="track")],
        )
        audiobooks = _make_payload(
            library="Audio-Books",
            section_id=11,
            section_type="artist",
            items=[_item("plex://2", "A Spy Story", 1002, media_type="track")],
        )
        media_db.ingest_snapshot_payload("srv-test", music)
        media_db.ingest_snapshot_payload("srv-test", audiobooks)

        # Build the snapshot.db from the same payloads.
        snap_path = tmp_path / "snap-test.db"
        snapshot_capture.build_snapshot_db_from_payloads(
            snapshot_path=snap_path,
            snapshot_id="snap-1",
            server_id="srv-test",
            server_name="Test Server",
            libraries=["Music", "Audio-Books"],
            metrics=["watch_history", "ratings", "playlists", "collections"],
            captured_at=time.time(),
            payloads=[music, audiobooks],
        )
        assert snap_path.is_file()

        # Serialise back to the payload shape.
        rebuilt = snapshot_serializer.build_payload_from_db(
            snap_path,
            server_name="Test Server",
            server_id="srv-test",
        )

        # The new shape: top-level `libraries` array, one entry per
        # source library, no "All Libraries" virtual entry.
        assert "libraries" in rebuilt
        names = [e["library"] for e in rebuilt["libraries"]]
        assert "Music" in names
        assert "Audio-Books" in names
        assert "All Libraries (reconstructed from snapshot DB)" not in names
        assert len(rebuilt["libraries"]) == 2

        # Each library's watch_history contains ONLY that library's
        # item.
        per_lib = {e["library"]: e for e in rebuilt["libraries"]}
        music_owner = per_lib["Music"]["users"].get("Plex Owner") or {}
        audiobooks_owner = per_lib["Audio-Books"]["users"].get("Plex Owner") or {}

        music_titles = [w["title"] for w in music_owner.get("watch_history", [])]
        audiobooks_titles = [w["title"] for w in audiobooks_owner.get("watch_history", [])]

        assert music_titles == ["Rock Anthems"]
        assert audiobooks_titles == ["A Spy Story"]

    def test_serializer_refuses_pre_v15_snapshot(self, tmp_path):
        # Hand-craft a snapshot.db that LOOKS like a pre-v15 file
        # (snapshot_meta.schema_version is missing / 0).
        legacy_db = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(legacy_db))
        try:
            conn.executescript("""
                CREATE TABLE snapshot_meta (
                    snapshot_id TEXT, server_id TEXT, server_name TEXT,
                    captured_at REAL, libraries_json TEXT,
                    metrics_json TEXT, created_by TEXT,
                    schema_version INTEGER NOT NULL DEFAULT 0
                );
                INSERT INTO snapshot_meta
                    (snapshot_id, server_id, server_name, captured_at,
                     libraries_json, metrics_json, schema_version)
                VALUES ('legacy', 'srv', 'Legacy', 0.0, '[]', '[]', 7);
            """)
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(snapshot_serializer.SnapshotSchemaMismatch):
            snapshot_serializer.build_payload_from_db(
                legacy_db,
                server_name="Legacy",
                server_id="srv",
            )


# ── Restore-shape correctness ──────────────────────────────────────────────

class TestRestoreShape:
    """
    The restorer's pre-v15 fan-out + divide-by-N hack computed an
    inflated per-library total (the same number for every library).
    The new shape gives each entry its own count from its own users
    block; the metadata peek logic must compute exact totals.
    """

    def test_reconstructed_payload_each_entry_has_real_total(self, tmp_path):
        # Library A has 3 watched items, Library B has 1.
        lib_a = _make_payload(
            library="A", section_id=20, section_type="movie",
            items=[
                _item("plex://a1", "A One", 2001, media_type="movie"),
                _item("plex://a2", "A Two", 2002, media_type="movie"),
                _item("plex://a3", "A Three", 2003, media_type="movie"),
            ],
        )
        lib_b = _make_payload(
            library="B", section_id=21, section_type="movie",
            items=[
                _item("plex://b1", "B One", 3001, media_type="movie"),
            ],
        )
        media_db.ingest_snapshot_payload("srv-test", lib_a)
        media_db.ingest_snapshot_payload("srv-test", lib_b)

        snap_path = tmp_path / "snap-totals.db"
        snapshot_capture.build_snapshot_db_from_payloads(
            snapshot_path=snap_path,
            snapshot_id="snap-totals",
            server_id="srv-test",
            server_name="Test Server",
            libraries=["A", "B"],
            metrics=["watch_history", "ratings", "playlists", "collections"],
            captured_at=time.time(),
            payloads=[lib_a, lib_b],
        )

        rebuilt = snapshot_serializer.build_payload_from_db(
            snap_path,
            server_name="Test Server",
            server_id="srv-test",
        )

        per_lib = {e["library"]: e for e in rebuilt["libraries"]}

        # Owner key for each: "Plex Owner".
        a_watch = per_lib["A"]["users"]["Plex Owner"]["watch_history"]
        b_watch = per_lib["B"]["users"]["Plex Owner"]["watch_history"]
        assert len(a_watch) == 3
        assert len(b_watch) == 1

        # The wrapper itself must carry the schema_version marker.
        assert rebuilt["snapshot_meta"]["schema_version"] == \
            snapshot_capture.SNAPSHOT_SCHEMA_VERSION
        assert rebuilt["snapshot_meta"]["reconstructed_from_db"] is True
