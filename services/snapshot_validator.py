"""
Snapshot integrity validator (Feature 2 phases 2.1 + 2.2).

Performs read-only structural assertions against a per-server snapshot
``.db`` file. Two consumption points (per end user decision D5):

* **Post-capture:** ``server.snapshot_capture`` invokes the validator
  after a fresh snapshot.db has been written but before the capture
  is considered successful. Failure aborts the job with an
  actionable error message.
* **Pre-restore:** ``services.restore.plex_native`` invokes the validator before
  any restore primitives fire against the destination. Failure aborts
  the restore so a malformed snapshot doesn't leak partial data into
  Plex.

Both consumption points read settings flags to decide whether to run:

* ``validate_snapshot_after_capture`` (default ON)
* ``validate_snapshot_before_restore`` (default OFF)

Safety contract: the validator NEVER opens the snapshot file in write
mode. It uses ``sqlite3`` URI mode=ro so a bug in this module cannot
modify the artefact under inspection. It also makes a temp-file copy
of the file before reading; that mirrors the "copy then assert then
clean up" pattern the dev tool's structural test mode uses and means
the validator can run on a snapshot file that another process is
still finalising.

Severities:

* ``error``: corruption or invariant violation that makes the
  snapshot unsafe to restore. The consumer aborts.
* ``warning``: a non-fatal deviation worth surfacing in the log
  (e.g. a row with section_key that has no matching library_sections
  parent, but the parent is recoverable from snapshot_meta).

The assertions in this module are deliberately a REIMPLEMENTATION
of the structural checks in
``tests_backend/test_library_section_identity.py``. The tests stay
untouched. Keeping them as separate code surfaces means a bug fix
in the validator does not silently weaken the unit tests, and vice
versa. The cost is a small amount of duplicated SQL; the benefit is
that each consumer can evolve on its own cadence.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


log = logging.getLogger("plexmigrate.services.snapshot_validator")


# Severities and minimum required schema version for a snapshot to be
# accepted by the restore path. The latter mirrors
# server.snapshot_capture.SNAPSHOT_SCHEMA_VERSION; the validator reads
# the constant from there at call time rather than freezing a number
# here so a future schema bump auto-tightens this check.
SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"


@dataclass
class ValidationIssue:
    severity: str
    code: str
    message: str
    where: Optional[str] = None


@dataclass
class ValidationReport:
    ok: bool
    schema_version: int
    issues: List[ValidationIssue] = field(default_factory=list)
    summary: Dict[str, int] = field(default_factory=dict)
    source_path: str = ""

    @property
    def errors(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == SEVERITY_ERROR]

    @property
    def warnings(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == SEVERITY_WARNING]

    def format_summary_line(self) -> str:
        """One-line human-readable summary. The capture / restore
        runners log this so end users see the result without opening
        the structured report."""
        e = len(self.errors)
        w = len(self.warnings)
        if self.ok and e == 0 and w == 0:
            return (
                f"Snapshot integrity OK "
                f"(schema_version={self.schema_version}, "
                f"libraries={self.summary.get('library_sections', 0)})"
            )
        return (
            f"Snapshot integrity {'OK' if self.ok else 'FAILED'}: "
            f"{e} error(s), {w} warning(s), "
            f"schema_version={self.schema_version}"
        )


# ── Public entry point ──────────────────────────────────────────────────────

def validate_snapshot(
    snapshot_db_path: Path,
    *,
    expected_schema_version: Optional[int] = None,
) -> ValidationReport:
    """
    Run the structural validation suite against ``snapshot_db_path``
    and return a :class:`ValidationReport`. The file is NEVER opened
    in write mode; a temp copy is made and the copy is opened
    read-only.

    ``expected_schema_version`` overrides the default minimum (which
    comes from ``server.snapshot_capture.SNAPSHOT_SCHEMA_VERSION``).
    Tests pass an explicit value to assert against historical
    versions; production callers leave it None.
    """
    report = ValidationReport(ok=False, schema_version=0,
                              source_path=str(snapshot_db_path))

    if not snapshot_db_path.is_file():
        report.issues.append(ValidationIssue(
            severity=SEVERITY_ERROR,
            code="missing_file",
            message=f"snapshot .db not found at {snapshot_db_path}",
        ))
        return report

    # Copy first; assert against the copy. The temp file lives in the
    # OS-provided temp dir and is cleaned up via NamedTemporaryFile's
    # context manager (delete=False so we can close + reopen via SQLite
    # URI; the finally block does the unlink).
    with tempfile.NamedTemporaryFile(
        suffix=".snapshot-validation.db", delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)
    try:
        try:
            shutil.copy2(str(snapshot_db_path), str(tmp_path))
        except OSError as exc:
            report.issues.append(ValidationIssue(
                severity=SEVERITY_ERROR,
                code="copy_failed",
                message=f"could not copy snapshot to temp: {exc}",
            ))
            return report

        try:
            conn = sqlite3.connect(
                f"file:{tmp_path}?mode=ro", uri=True, timeout=10.0,
            )
            conn.row_factory = sqlite3.Row
        except sqlite3.DatabaseError as exc:
            report.issues.append(ValidationIssue(
                severity=SEVERITY_ERROR,
                code="open_failed",
                message=f"sqlite3 refused the file: {exc}",
            ))
            return report

        try:
            _validate(report, conn, expected_schema_version)
        finally:
            try:
                conn.close()
            except Exception:
                pass
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            log.debug("could not unlink temp copy %s", tmp_path, exc_info=True)

    report.ok = not report.errors
    return report


# ── Internal: assertion suite ───────────────────────────────────────────────

def _validate(
    report: ValidationReport,
    conn: sqlite3.Connection,
    expected_schema_version: Optional[int],
) -> None:
    """Run every structural check. Each check appends issues to
    ``report.issues`` rather than raising so a single broken
    assertion never short-circuits the rest of the suite (end users
    benefit from seeing every problem at once)."""

    # Resolve the required schema version. Done inside the function so
    # tests can drive validate_snapshot with an explicit override
    # without monkey-patching the source module.
    if expected_schema_version is not None:
        required = int(expected_schema_version)
    else:
        try:
            from server.snapshot_capture import SNAPSHOT_SCHEMA_VERSION
            required = int(SNAPSHOT_SCHEMA_VERSION)
        except Exception:
            log.debug(
                "Could not import SNAPSHOT_SCHEMA_VERSION; defaulting to 15",
                exc_info=True,
            )
            required = 15

    _check_schema_version(report, conn, required)
    _check_library_sections_present(report, conn)
    _check_per_table_columns(report, conn)
    _check_no_zero_section_keys(report, conn)
    _check_section_key_references(report, conn)
    _populate_summary(report, conn)


def _check_schema_version(
    report: ValidationReport,
    conn: sqlite3.Connection,
    required: int,
) -> None:
    """The snapshot file must declare a schema version at or above
    the build's minimum. Older files have a different on-disk shape
    and should not be consumed by the current restore path."""
    try:
        row = conn.execute(
            "SELECT schema_version FROM snapshot_meta LIMIT 1"
        ).fetchone()
        version = int(row["schema_version"] or 0) if row else 0
    except sqlite3.OperationalError:
        version = 0

    report.schema_version = version
    if version < required:
        report.issues.append(ValidationIssue(
            severity=SEVERITY_ERROR,
            code="schema_version_too_old",
            message=(
                f"snapshot_meta.schema_version={version}, required>={required}. "
                "Re-capture the snapshot with the current build."
            ),
            where="snapshot_meta",
        ))


def _check_library_sections_present(
    report: ValidationReport,
    conn: sqlite3.Connection,
) -> None:
    """The library_sections table is the v0.15 integrity anchor.
    Every per-server row references it via section_key; the table
    must exist and be non-empty."""
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='library_sections'"
        ).fetchone()
    except sqlite3.OperationalError as exc:
        report.issues.append(ValidationIssue(
            severity=SEVERITY_ERROR,
            code="sqlite_master_read_failed",
            message=f"could not read sqlite_master: {exc}",
        ))
        return

    if row is None:
        report.issues.append(ValidationIssue(
            severity=SEVERITY_ERROR,
            code="library_sections_missing",
            message=(
                "library_sections table is missing. The snapshot was "
                "written by a pre-v0.15 build, or the file is corrupt."
            ),
            where="library_sections",
        ))
        return

    try:
        cnt = int(conn.execute(
            "SELECT COUNT(*) AS n FROM library_sections"
        ).fetchone()["n"] or 0)
    except sqlite3.OperationalError as exc:
        report.issues.append(ValidationIssue(
            severity=SEVERITY_ERROR,
            code="library_sections_read_failed",
            message=f"could not count library_sections rows: {exc}",
        ))
        return

    if cnt == 0:
        report.issues.append(ValidationIssue(
            severity=SEVERITY_ERROR,
            code="library_sections_empty",
            message=(
                "library_sections has zero rows. A valid snapshot must "
                "record at least one library section."
            ),
            where="library_sections",
        ))


def _check_per_table_columns(
    report: ValidationReport,
    conn: sqlite3.Connection,
) -> None:
    """Every per-server table must carry the section_key column. The
    v0.15 ingest helpers raise on missing values, but a snapshot
    written by an older build would have the columns missing entirely
    and the validator should call that out clearly."""
    from server._sql_identifier import safe_identifier
    _ALLOWED = frozenset({
        "server_items", "watch_events", "ratings", "playlists", "collections",
    })
    for table in _ALLOWED:
        tbl = safe_identifier(table, _ALLOWED)
        try:
            cols = {
                r["name"]
                for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()
            }
        except sqlite3.OperationalError as exc:
            report.issues.append(ValidationIssue(
                severity=SEVERITY_ERROR,
                code="table_introspection_failed",
                message=f"PRAGMA table_info({table}) failed: {exc}",
                where=table,
            ))
            continue
        if "section_key" not in cols:
            report.issues.append(ValidationIssue(
                severity=SEVERITY_ERROR,
                code="section_key_column_missing",
                message=(
                    f"{table}.section_key column is missing. The snapshot "
                    "was written by a pre-v0.15 build."
                ),
                where=table,
            ))


def _check_no_zero_section_keys(
    report: ValidationReport,
    conn: sqlite3.Connection,
) -> None:
    """No row in any per-server table may carry section_key=0 (the
    'unknown' sentinel). A zero indicates either a build bypassed the
    Python-level validation, or rows from a pre-v0.15 era survived
    a migration. Either way it's a corrupt invariant."""
    for table in ("server_items", "watch_events", "ratings",
                  "playlists", "collections"):
        try:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE section_key = 0"
            ).fetchone()
            zero_count = int(row["n"] or 0) if row else 0
        except sqlite3.OperationalError:
            # Column or table missing; the earlier checks already
            # raised an error for this case.
            continue
        if zero_count > 0:
            report.issues.append(ValidationIssue(
                severity=SEVERITY_ERROR,
                code="zero_section_key",
                message=(
                    f"{table} has {zero_count} row(s) with section_key=0. "
                    "These rows lack library identity and would route to "
                    "the wrong destination on restore."
                ),
                where=table,
            ))


def _check_section_key_references(
    report: ValidationReport,
    conn: sqlite3.Connection,
) -> None:
    """Every section_key referenced by a per-server row must have a
    matching row in library_sections. Snapshot.db enforces this with
    FK ON during capture, but a file copied between hosts or hand-
    edited could still drift. A failure here is a warning rather
    than an error because the rows are LIKELY recoverable (the
    restore can still proceed for sections that DO have parents)."""
    for table in ("server_items", "watch_events", "ratings",
                  "playlists", "collections"):
        try:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS n FROM {table}
                WHERE section_key NOT IN (
                    SELECT section_key FROM library_sections
                )
                """
            ).fetchone()
            orphan = int(row["n"] or 0) if row else 0
        except sqlite3.OperationalError:
            continue
        if orphan > 0:
            report.issues.append(ValidationIssue(
                severity=SEVERITY_WARNING,
                code="orphan_section_key",
                message=(
                    f"{table} has {orphan} row(s) referencing a section_key "
                    "with no matching library_sections parent. Those rows "
                    "will be skipped on restore."
                ),
                where=table,
            ))


def _populate_summary(
    report: ValidationReport,
    conn: sqlite3.Connection,
) -> None:
    """Stash per-table row counts in ``report.summary`` so end users
    have a single-glance view of what the snapshot contains, even
    when no issues fire."""
    for table in ("library_sections", "server_items", "items",
                  "watch_events", "ratings", "playlists", "collections"):
        try:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM {table}"
            ).fetchone()
            report.summary[table] = int(row["n"] or 0) if row else 0
        except sqlite3.OperationalError:
            report.summary[table] = 0


# ── Settings resolution helpers ─────────────────────────────────────────────

def is_after_capture_enabled() -> bool:
    """Read ``settings.validate_snapshot_after_capture`` with a True
    fallback (the documented default per D5). Capture path consults
    this before invoking the validator."""
    return _read_bool_setting("validate_snapshot_after_capture", default=True)


def is_before_restore_enabled() -> bool:
    """Read ``settings.validate_snapshot_before_restore`` with a
    False fallback. Restore path consults this before invoking the
    validator."""
    return _read_bool_setting("validate_snapshot_before_restore", default=False)


def _read_bool_setting(key: str, *, default: bool) -> bool:
    try:
        from server.persistence import load_settings
        settings = load_settings() or {}
        raw = settings.get(key)
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        log.debug("settings.%s lookup failed", key, exc_info=True)
    return default
