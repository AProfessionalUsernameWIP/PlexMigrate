"""
Developer-tool test runner harness (Feature 3 phases 3.2 + 3.3).

This module is the dispatcher between the end user-facing test runner
endpoint (``POST /api/dev/run-tests``) and the on-disk test suites
(``tests_backend/``, ``tests_structural/``, ``tests_live/``).

Three modes:

* ``synthetic``: runs every test under ``tests_backend/``. Default
  mode. Uses pytest with the existing tests; safe to run at any time.
* ``structural``: runs every test under ``tests_structural/``. Each
  test makes a copy of a real artifact in tmp and asserts against
  the copy; originals are never opened in write mode.
* ``live``: runs every test under ``tests_live/``. Each test issues
  read-only operations against a configured live Plex server. The
  UI must confirm before this mode fires, and the harness records a
  loud warning in the log so an accidental fire is visible.

Outputs per run:

* A ``.log`` file with the captured pytest stdout/stderr wrapped in
  a structured header + footer (see :func:`_format_log_header` /
  :func:`_format_log_footer`).
* A sidecar ``.summary.json`` carrying the parsed totals, failed
  test list, xfail / xpass counts, and a list of tests that passed
  via ``pytest.raises``.

Both files live under
``$PLEXMIGRATE_DATA_DIR/logs/test_runs/`` so they survive container
restarts. The frontend reads ``.summary.json`` for the list view and
streams the ``.log`` on the end user's "View raw log" click.

The pytest filter (single-test mode) accepts a free-form ``-k``
pattern. The harness sanitises the pattern to an allowlist of
characters (alphanumerics + dot + colon + underscore + space + the
keywords ``and`` / ``or`` / ``not``) before passing it to pytest, so
shell metacharacters can never escape into the subprocess command
line.
"""

from __future__ import annotations

import ast
import json
import logging
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


log = logging.getLogger("plexmigrate.server.dev_test_harness")


# Test mode -> directory under the repo root. The directory must
# exist when the mode is dispatched; the harness raises ValueError
# otherwise to surface a deployment-shape mismatch instead of letting
# pytest's "no tests collected" land as a silent zero-failure run.
MODES: Dict[str, str] = {
    "synthetic": "tests_backend",
    "structural": "tests_structural",
    "live": "tests_live",
}


def repo_root() -> Path:
    """Return the absolute path to the repository root. The harness
    is in ``server/`` so the root is the parent of this module's
    directory."""
    return Path(__file__).resolve().parent.parent


def logs_dir() -> Path:
    """Return the directory for per-run log artefacts. Lives under
    the end user's data dir so container restarts don't lose history.
    Created on first call."""
    from server.persistence import get_data_dir
    p = get_data_dir() / "logs" / "test_runs"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ── Filter sanitisation ─────────────────────────────────────────────────────

# The pytest ``-k`` flag accepts a Python-expression-like selector
# string. End users may pass things like
# ``TestSmartMode and music`` or ``test_round_trip_identity``. We
# allowlist a conservative character set so a maliciously crafted
# selector cannot escape into the subprocess shell, even though we
# pass the args list (no shell=True) and never interpolate the
# selector into a string. Defence in depth.
_FILTER_ALLOWED = re.compile(r"^[A-Za-z0-9_.:\-\[\] ]+$")


def sanitise_filter(raw: Optional[str]) -> Optional[str]:
    """
    Return a sanitised pytest ``-k`` filter, or ``None`` if no
    filter was supplied / the filter is empty after stripping.

    Raises :class:`ValueError` when the filter contains disallowed
    characters. The caller turns that into a 400-level error for the
    end user with the offending character noted.
    """
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    if not _FILTER_ALLOWED.match(cleaned):
        # Find one disallowed character to surface in the error so
        # the end user knows what to fix.
        for ch in cleaned:
            if not re.match(_FILTER_ALLOWED, ch):
                raise ValueError(
                    f"pytest filter contains disallowed character {ch!r}. "
                    "Allowed: alphanumerics, underscore, dot, colon, "
                    "hyphen, brackets, space."
                )
        # Unreachable: regex matched fail above implies at least one
        # disallowed char. Fallback message just in case.
        raise ValueError("pytest filter contains a disallowed character.")
    return cleaned


# ── Result types ────────────────────────────────────────────────────────────

@dataclass
class FailedTest:
    nodeid: str
    duration_seconds: float
    error_excerpt: str


@dataclass
class XfailTest:
    nodeid: str
    reason: str


@dataclass
class RunResult:
    run_id: str
    mode: str
    started_at: float
    ended_at: float
    duration_seconds: float
    test_target: str
    filter: Optional[str]
    exit_code: int
    operator: Optional[str]
    totals: Dict[str, int] = field(default_factory=dict)
    failed_tests: List[FailedTest] = field(default_factory=list)
    xfail_tests: List[XfailTest] = field(default_factory=list)
    raises_tests: List[str] = field(default_factory=list)
    log_path: str = ""

    def to_summary_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["failed_tests"] = [asdict(t) for t in self.failed_tests]
        d["xfail_tests"] = [asdict(t) for t in self.xfail_tests]
        return d


# ── AST scan: tests that pass via pytest.raises ─────────────────────────────

def find_raises_tests(test_dir: Path) -> List[str]:
    """
    Walk every ``.py`` under ``test_dir`` and return the set of test
    nodeids (``<file>::<Class>::<func>``) whose body contains a
    ``pytest.raises(...)`` call. AST-based rather than regex to avoid
    false positives from comments and string literals.

    The returned nodeids match pytest's discovery shape so the dev
    tool's summary can intersect them with the actually-collected
    pytest results.
    """
    out: List[str] = []
    if not test_dir.is_dir():
        return out
    for py in test_dir.rglob("test_*.py"):
        try:
            source = py.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py))
        except (SyntaxError, OSError, UnicodeDecodeError):
            # Skip unparseable files; the dev tool's surface is
            # advisory, not gating. The actual pytest run will
            # produce the authoritative error for any file it can't
            # collect.
            continue
        _walk_for_raises(tree, py, out)
    return out


def _walk_for_raises(tree: ast.AST, path: Path, out: List[str]) -> None:
    """Recursively walk an AST and record every test function that
    contains a ``pytest.raises(...)`` call. Test classes are walked
    so methods get the class qualifier in their nodeid."""
    rel = _relative_test_path(path)
    for class_node in [n for n in ast.iter_child_nodes(tree)
                       if isinstance(n, ast.ClassDef)]:
        if not class_node.name.startswith("Test"):
            continue
        for method in [m for m in class_node.body
                       if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            if not method.name.startswith("test_"):
                continue
            if _contains_pytest_raises(method):
                out.append(f"{rel}::{class_node.name}::{method.name}")
    # Top-level test functions (no enclosing class).
    for fn in [n for n in ast.iter_child_nodes(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        if not fn.name.startswith("test_"):
            continue
        if _contains_pytest_raises(fn):
            out.append(f"{rel}::{fn.name}")


def _relative_test_path(path: Path) -> str:
    """Return the path relative to the repo root, with forward
    slashes (pytest's nodeid uses forward slashes on every OS)."""
    try:
        rel = path.resolve().relative_to(repo_root())
    except ValueError:
        rel = path
    return str(rel).replace("\\", "/")


def _contains_pytest_raises(fn: ast.AST) -> bool:
    """True when a function body contains a ``pytest.raises(...)``
    or ``with pytest.raises(...)`` call. Catches both forms because
    pytest.raises is most commonly used as a context manager but the
    bare-call form (returning an excinfo object) is also valid
    pytest API."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "pytest"
                    and func.attr == "raises"):
                return True
    return False


# ── Subprocess runner ───────────────────────────────────────────────────────

def run_tests(
    *,
    mode: str,
    pytest_filter: Optional[str] = None,
    operator: Optional[str] = None,
) -> RunResult:
    """
    Run the selected test suite as a subprocess and return a parsed
    :class:`RunResult`. Writes the ``.log`` + ``.summary.json``
    artefacts to :func:`logs_dir`.

    Raises :class:`ValueError` when:

    * ``mode`` is not in :data:`MODES`.
    * The mode's target directory does not exist.
    * The filter contains disallowed characters (delegated to
      :func:`sanitise_filter`).
    """
    if mode not in MODES:
        raise ValueError(
            f"Unknown test mode {mode!r}. Valid: {sorted(MODES)}."
        )
    target_dir = repo_root() / MODES[mode]
    if not target_dir.is_dir():
        raise ValueError(
            f"Test directory {target_dir} does not exist for mode "
            f"{mode!r}. Deploy the corresponding tests directory or "
            "pick a different mode."
        )

    cleaned_filter = sanitise_filter(pytest_filter)
    run_id = _generate_run_id()
    log_dir = logs_dir()
    log_path = log_dir / f"test_run_{mode}_{run_id}.log"
    summary_path = log_dir / f"test_run_{mode}_{run_id}.summary.json"
    junit_path = log_dir / f"test_run_{mode}_{run_id}.junit.xml"

    # Build the pytest command. ``-rN`` suppresses the pytest summary
    # of skips/xfails/etc. so our junit parsing is the source of
    # truth; we still want the per-test output in the captured log
    # for the end user to read.
    cmd = [
        sys.executable, "-m", "pytest",
        "-v",
        f"--junitxml={junit_path}",
        str(target_dir),
    ]
    if cleaned_filter:
        cmd.extend(["-k", cleaned_filter])

    if mode == "live":
        log.warning(
            "Dev tool is firing LIVE INTEGRATION TESTS (mode=live, "
            "operator=%r, filter=%r). These run against real Plex "
            "servers; ensure the operator confirmed the warning.",
            operator, cleaned_filter,
        )

    started_at = time.time()
    started_perf = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_root()),
            capture_output=True,
            text=True,
            timeout=600,  # 10-minute ceiling; long enough for live
        )
        timed_out = False
        exit_code = proc.returncode
        captured_stdout = proc.stdout
        captured_stderr = proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = 124  # convention for "timed out"
        captured_stdout = exc.stdout.decode("utf-8", errors="replace") if exc.stdout else ""
        captured_stderr = (
            (exc.stderr.decode("utf-8", errors="replace") if exc.stderr else "")
            + "\n[Process exceeded the 10-minute ceiling and was killed.]\n"
        )

    ended_perf = time.perf_counter()
    ended_at = time.time()
    duration = ended_perf - started_perf

    # AST scan for tests with pytest.raises; intersected with the
    # collected test list from junitxml so we only report tests that
    # actually ran.
    raises_candidates = find_raises_tests(target_dir)

    totals, failed, xfail_list = _parse_junit(junit_path)
    collected = set(t.nodeid for t in failed) | set(t.nodeid for t in xfail_list)
    # The junit parser also returns the full collected nodeid list
    # in totals; for now we intersect manually with raises candidates
    # by matching nodeids.
    raises_tests_in_run = [n for n in raises_candidates if n in totals.get("_collected_nodeids", set())]
    # _collected_nodeids is an internal field used only for this
    # intersection; remove before returning.
    totals.pop("_collected_nodeids", None)

    result = RunResult(
        run_id=run_id,
        mode=mode,
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=duration,
        test_target=str(target_dir.relative_to(repo_root())).replace("\\", "/"),
        filter=cleaned_filter,
        exit_code=exit_code,
        operator=operator,
        totals=totals,
        failed_tests=failed,
        xfail_tests=xfail_list,
        raises_tests=raises_tests_in_run,
        log_path=str(log_path.relative_to(get_data_dir_for_logs())),
    )

    # Write the log file + sidecar summary.
    _write_log_file(
        log_path,
        result=result,
        stdout=captured_stdout,
        stderr=captured_stderr,
        timed_out=timed_out,
    )
    _write_summary_json(summary_path, result)

    # Clean up the junit XML; the parsed data is preserved in the
    # summary JSON and the raw pytest output is in the log.
    try:
        junit_path.unlink()
    except OSError:
        log.debug("could not unlink junit file %s", junit_path, exc_info=True)

    return result


def get_data_dir_for_logs() -> Path:
    """Helper: returns the data dir root for relativising log paths
    in the summary JSON. Kept as a thin wrapper so a future swap to
    a different layout has one call site to update."""
    from server.persistence import get_data_dir
    return get_data_dir()


def _generate_run_id() -> str:
    """ISO-ish timestamp with a 4-digit jitter so two runs in the
    same second don't collide. The file naming uses this directly,
    so the result must be filename-safe on every OS."""
    return time.strftime("%Y-%m-%d_%H-%M-%S")


# ── junitxml parsing ────────────────────────────────────────────────────────

def _parse_junit(junit_path: Path) -> tuple:
    """
    Parse pytest's --junitxml output into ``(totals, failed, xfail)``.

    ``totals`` is a dict with keys ``collected``, ``passed_normal``,
    ``passed_xfail``, ``failed``, ``errors``, ``skipped``,
    ``unexpected_pass`` (xpass) plus an internal
    ``_collected_nodeids`` set used for the AST-scan intersection.
    The internal field is removed by :func:`run_tests` before the
    result is returned.
    """
    totals = {
        "collected": 0,
        "passed_normal": 0,
        "passed_xfail": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "unexpected_pass": 0,
        "_collected_nodeids": set(),
    }
    failed: List[FailedTest] = []
    xfails: List[XfailTest] = []

    if not junit_path.is_file():
        return totals, failed, xfails

    try:
        tree = ET.parse(str(junit_path))
    except ET.ParseError:
        log.debug("junitxml parse failed; returning empty totals",
                  exc_info=True)
        return totals, failed, xfails

    root = tree.getroot()
    # pytest's junitxml shape: <testsuites><testsuite><testcase>...
    # Iterate <testcase> regardless of nesting depth.
    for tc in root.iter("testcase"):
        classname = tc.attrib.get("classname", "")
        name = tc.attrib.get("name", "")
        nodeid = _nodeid_from_junit(classname, name)
        try:
            duration = float(tc.attrib.get("time", "0") or 0.0)
        except ValueError:
            duration = 0.0

        totals["collected"] += 1
        totals["_collected_nodeids"].add(nodeid)

        # Classify by child elements: failure, error, skipped, or none.
        failure = tc.find("failure")
        error = tc.find("error")
        skipped = tc.find("skipped")

        if failure is not None:
            totals["failed"] += 1
            failed.append(FailedTest(
                nodeid=nodeid,
                duration_seconds=duration,
                error_excerpt=(failure.text or "").strip()[:500],
            ))
        elif error is not None:
            totals["errors"] += 1
            failed.append(FailedTest(
                nodeid=nodeid,
                duration_seconds=duration,
                error_excerpt=(error.text or "").strip()[:500],
            ))
        elif skipped is not None:
            # pytest encodes xfail as <skipped type="pytest.xfail">
            # and xpass as a regular pass with a properties marker.
            stype = skipped.attrib.get("type", "")
            if "xfail" in stype.lower():
                totals["passed_xfail"] += 1
                xfails.append(XfailTest(
                    nodeid=nodeid,
                    reason=(skipped.text or skipped.attrib.get("message", "")).strip()[:300],
                ))
            else:
                totals["skipped"] += 1
        else:
            # No child = passed normally. Check for xpass via the
            # <properties> child (pytest reports unexpected passes
            # this way when strict_xfail isn't set; if strict it
            # surfaces as failure, already counted above).
            xpass = False
            for prop in tc.findall("properties/property"):
                if prop.attrib.get("name", "").lower() in ("xpassed", "xpass"):
                    xpass = True
                    break
            if xpass:
                totals["unexpected_pass"] += 1
            else:
                totals["passed_normal"] += 1

    return totals, failed, xfails


def _nodeid_from_junit(classname: str, name: str) -> str:
    """
    Recover a pytest-style nodeid from junitxml's classname + name.

    pytest emits ``classname="tests_backend.test_foo.TestBar"`` for a
    method ``test_baz`` in class ``TestBar`` of file
    ``tests_backend/test_foo.py``. The nodeid format is
    ``tests_backend/test_foo.py::TestBar::test_baz``.
    """
    if not classname:
        return name
    # Split into path components and class. The class component
    # starts at the first uppercase part (Python convention says
    # test classes start with a capital).
    parts = classname.split(".")
    # Find the last component that looks like a module (lowercase),
    # then everything after is the class chain.
    module_end = len(parts)
    for i, part in enumerate(parts):
        if part and part[0].isupper():
            module_end = i
            break
    module_path = "/".join(parts[:module_end]) + ".py"
    class_chain = "::".join(parts[module_end:])
    if class_chain:
        return f"{module_path}::{class_chain}::{name}"
    return f"{module_path}::{name}"


# ── Log file formatter ──────────────────────────────────────────────────────

def _write_log_file(
    log_path: Path,
    *,
    result: RunResult,
    stdout: str,
    stderr: str,
    timed_out: bool,
) -> None:
    """Write the human-readable .log file with structured header +
    full captured output + structured footer. See D9 in the plan."""
    body_parts: List[str] = []
    body_parts.append(_format_log_header(result, timed_out=timed_out))
    body_parts.append("\n=== Captured stdout ===\n")
    body_parts.append(stdout or "(empty)\n")
    if stderr.strip():
        body_parts.append("\n=== Captured stderr ===\n")
        body_parts.append(stderr)
    body_parts.append("\n")
    body_parts.append(_format_log_footer(result))
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("".join(body_parts), encoding="utf-8")
    except OSError:
        log.exception("Could not write dev test runner log to %s", log_path)


def _format_log_header(result: RunResult, *, timed_out: bool) -> str:
    return (
        "=== Test Run ===\n"
        f"Run ID:        {result.run_id}\n"
        f"Mode:          {result.mode}\n"
        f"Started:       {time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(result.started_at))}Z\n"
        f"Ended:         {time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(result.ended_at))}Z\n"
        f"Duration:      {result.duration_seconds:.3f}s\n"
        f"Test target:   {result.test_target}\n"
        f"Filter (-k):   {result.filter or '(none)'}\n"
        f"Operator:      {result.operator or '(unknown)'}\n"
        f"Exit code:     {result.exit_code}\n"
        f"Timed out:     {'yes' if timed_out else 'no'}\n"
    )


def _format_log_footer(result: RunResult) -> str:
    t = result.totals
    lines: List[str] = ["=== Summary ===\n"]
    lines.append(f"Total:               {t.get('collected', 0)}\n")
    lines.append(f"Passed (normal):     {t.get('passed_normal', 0)}\n")
    lines.append(f"Passed (xfail):      {t.get('passed_xfail', 0)}\n")
    lines.append(f"Unexpected pass:     {t.get('unexpected_pass', 0)}\n")
    lines.append(f"Failed:              {t.get('failed', 0)}\n")
    lines.append(f"Errors:              {t.get('errors', 0)}\n")
    lines.append(f"Skipped:             {t.get('skipped', 0)}\n")
    if result.failed_tests:
        lines.append("\n=== Failed tests ===\n")
        for ft in result.failed_tests:
            lines.append(f"  - {ft.nodeid}\n")
            lines.append(f"      Duration: {ft.duration_seconds:.3f}s\n")
            if ft.error_excerpt:
                # Indent the excerpt so the block reads as nested.
                indented = "\n".join(
                    "      " + line for line in ft.error_excerpt.splitlines()
                )
                lines.append(f"      Excerpt:\n{indented}\n")
    if result.xfail_tests:
        lines.append("\n=== Tests that passed by expected failure (xfail) ===\n")
        for xt in result.xfail_tests:
            lines.append(f"  - {xt.nodeid}\n")
            if xt.reason:
                lines.append(f"      Reason: {xt.reason}\n")
    if result.raises_tests:
        lines.append(
            "\n=== Tests that contain pytest.raises "
            "(passed by catching an exception) ===\n"
        )
        for nodeid in result.raises_tests:
            lines.append(f"  - {nodeid}\n")
    return "".join(lines)


def _write_summary_json(summary_path: Path, result: RunResult) -> None:
    try:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(
            json.dumps(result.to_summary_dict(), indent=2),
            encoding="utf-8",
        )
    except OSError:
        log.exception("Could not write dev test runner summary to %s",
                      summary_path)


# ── Run listing for the dev tool UI ─────────────────────────────────────────

def list_recent_runs(*, limit: int = 25) -> List[Dict[str, Any]]:
    """Return the parsed summary JSON for the most recent ``limit``
    runs, newest first by file mtime. Used by the Developer tab to
    populate the run-history list without parsing the raw .log."""
    out: List[Dict[str, Any]] = []
    try:
        ld = logs_dir()
    except Exception:
        log.exception("logs_dir() lookup failed; returning empty list")
        return out
    candidates = sorted(
        ld.glob("test_run_*.summary.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:max(1, min(int(limit), 200))]
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["_summary_filename"] = path.name
            out.append(data)
        except (OSError, json.JSONDecodeError):
            log.debug("Skipping unreadable summary %s", path, exc_info=True)
    return out
