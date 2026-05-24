// Help > Deployment Map.
//
// Interactive call-chain reference for every job type the engine
// supports. Click a job card on the Overview to drill into its
// step-by-step flow; hover any step for plain-English context via
// InfoTip; click "Show code" on any step to inspect a representative
// Python snippet plus example input / output.
//
// Data source: read-only recon performed against the current code
// tree (post-refactor). file:function citations are best-effort and
// will drift as the codebase evolves; treat the snippets as
// REFERENCE skeletons that capture the contract, not as byte-exact
// reproductions of every line in the repo.
//
// Architecture:
//   <DeploymentMap>            top-level view router
//     <OverviewMap>            landing view: grid of job cards + shared infra
//     <JobFlow>                drill-down for one job (step list + code)
//     <SharedInfra>            drill-down for cross-cutting modules
//
// Code rendering:
//   <CodeBlock code=python />  inline Python with token-level colouring
//   <CodeExample>              wraps a code block plus optional input
//                              and output panels, collapsed by default
//
// Tooltip strategy: InfoTip is used inline (children-mode) at every
// step so the operator / developer can hover for additional context
// without leaving the page.

import { useMemo, useState } from 'react';

// ── Data model ───────────────────────────────────────────────────────────────

type StepRole =
  | 'dispatch'
  | 'preflight'
  | 'engine'
  | 'adapter'
  | 'write-path'
  | 'telemetry'
  | 'cleanup'
  | 'state';

interface FlowStep {
  /** Display order; same as array index. */
  n: number;
  /** Short label for the step list. */
  label: string;
  /** file:function citation (file path only when no specific function). */
  cite: string;
  /** Plain-English explanation for the tooltip. */
  detail: string;
  /** Role colour-code. */
  role: StepRole;
  /** Representative Python snippet for this step. */
  code?: string;
  /** Example input (what the function receives). */
  input?: string;
  /** Example output (what the function returns) OR a side-effect description. */
  output?: string;
}

interface JobFlow {
  id: string;
  title: string;
  oneliner: string;
  /** Short paragraph shown above the step list. */
  preamble: string;
  /** Linear step chain from REST entry to cleanup. */
  steps: FlowStep[];
  /** Cross-cutting modules this job touches; rendered as a side panel. */
  sharedInfra: string[];
  /** Databases or external surfaces written / read. */
  surfaces: string[];
  /** Optional caveats: legacy paths, deferred features, known gaps. */
  notes?: string[];
}

interface SharedInfraSnippet {
  /** Section header shown above the snippet (function name, class, etc.). */
  label: string;
  /** Optional file:function citation when different from the module's file. */
  cite?: string;
  /** Representative Python snippet. */
  code: string;
  /** Example input (call args). */
  input?: string;
  /** Example output (return value or side-effect description). */
  output?: string;
}

interface SharedInfraEntry {
  id: string;
  title: string;
  file: string;
  oneliner: string;
  detail: string;
  /** What jobs consume this module (rendered as backlinks). */
  consumers: string[];
  /** Key functions / classes from this module, with code + I/O examples. */
  snippets?: SharedInfraSnippet[];
}

// ── Python syntax highlighter ────────────────────────────────────────────────
//
// Pure JS tokenizer, no external dependency. Walks the source character
// by character; emits a flat array of (text, kind) tokens. Recognises:
//   keywords / built-in identifiers / None / True / False
//   triple-quoted strings ("""..." or '''...''')
//   single-line strings ("..." or '...'), with backslash escapes
//   f-string prefix (the f is highlighted as a keyword, body as string)
//   # comments to end of line
//   numeric literals
//   @decorators
//   def / class identifier following 'def' or 'class'
//
// Edge cases ignored on purpose: nested f-string expressions, byte
// strings, raw strings with embedded quotes. The snippets are reference
// material; pathological Python isn't worth the highlighter complexity.

type TokenKind =
  | 'plain'
  | 'keyword'
  | 'builtin'
  | 'string'
  | 'comment'
  | 'number'
  | 'decorator'
  | 'fn'
  | 'cls';

interface Token {
  text: string;
  kind: TokenKind;
}

const PY_KEYWORDS = new Set([
  'False', 'None', 'True', 'and', 'as', 'assert', 'async', 'await',
  'break', 'class', 'continue', 'def', 'del', 'elif', 'else', 'except',
  'finally', 'for', 'from', 'global', 'if', 'import', 'in', 'is',
  'lambda', 'nonlocal', 'not', 'or', 'pass', 'raise', 'return', 'try',
  'while', 'with', 'yield', 'self', 'cls', 'match', 'case',
]);

const PY_BUILTINS = new Set([
  'print', 'len', 'range', 'str', 'int', 'float', 'list', 'dict', 'set',
  'tuple', 'bool', 'bytes', 'isinstance', 'issubclass', 'type', 'open',
  'super', 'object', 'enumerate', 'zip', 'map', 'filter', 'sorted',
  'reversed', 'min', 'max', 'sum', 'abs', 'all', 'any', 'getattr',
  'setattr', 'hasattr', 'delattr', 'vars', 'dir', 'iter', 'next',
  'callable', 'id', 'hash', 'repr', 'format', 'chr', 'ord', 'hex', 'oct',
  'bin', 'round', 'divmod', 'pow', 'frozenset', 'slice', 'property',
  'classmethod', 'staticmethod', '__init__', '__main__',
]);

function tokenizePython(source: string): Token[] {
  const tokens: Token[] = [];
  let i = 0;
  const n = source.length;

  const peek = (off = 0) => (i + off < n ? source[i + off] : '');
  const push = (text: string, kind: TokenKind) => {
    if (text.length === 0) return;
    tokens.push({ text, kind });
  };

  while (i < n) {
    const ch = source[i];

    // Comment to end of line.
    if (ch === '#') {
      let end = i;
      while (end < n && source[end] !== '\n') end++;
      push(source.slice(i, end), 'comment');
      i = end;
      continue;
    }

    // Triple-quoted string.
    if ((ch === '"' || ch === "'") && peek(1) === ch && peek(2) === ch) {
      const q = ch + ch + ch;
      let end = i + 3;
      while (end < n) {
        if (source[end] === ch && source[end + 1] === ch && source[end + 2] === ch) {
          end += 3;
          break;
        }
        end++;
      }
      push(source.slice(i, Math.min(end, n)), 'string');
      i = Math.min(end, n);
      continue;
    }

    // String prefix: f / r / b / fr / rf (highlight prefix as keyword).
    if (/[frbFRB]/.test(ch) && (peek(1) === '"' || peek(1) === "'")) {
      const prefix = ch;
      push(prefix, 'keyword');
      i += 1;
      // Fall through to string scan on the quote.
    } else if (
      /[frbFRB]/.test(ch) &&
      /[frbFRB]/.test(peek(1)) &&
      (peek(2) === '"' || peek(2) === "'")
    ) {
      push(source.slice(i, i + 2), 'keyword');
      i += 2;
    }

    // Single / double quoted string with backslash escapes.
    const q = source[i];
    if (q === '"' || q === "'") {
      let end = i + 1;
      while (end < n && source[end] !== q && source[end] !== '\n') {
        if (source[end] === '\\' && end + 1 < n) {
          end += 2;
          continue;
        }
        end++;
      }
      if (end < n && source[end] === q) end++;
      push(source.slice(i, end), 'string');
      i = end;
      continue;
    }

    // Numeric literal (int / float / hex / underscore-grouped).
    if (/[0-9]/.test(ch)) {
      let end = i;
      while (end < n && /[0-9a-fA-FxX_.eE+\-]/.test(source[end])) {
        const c = source[end];
        // Only count + / - as part of the number if it follows e or E.
        if ((c === '+' || c === '-') && !'eE'.includes(source[end - 1] ?? '')) break;
        end++;
      }
      push(source.slice(i, end), 'number');
      i = end;
      continue;
    }

    // Decorator @name.path
    if (ch === '@' && /[A-Za-z_]/.test(peek(1))) {
      let end = i + 1;
      while (end < n && /[A-Za-z0-9_.]/.test(source[end])) end++;
      push(source.slice(i, end), 'decorator');
      i = end;
      continue;
    }

    // Identifier / keyword.
    if (/[A-Za-z_]/.test(ch)) {
      let end = i;
      while (end < n && /[A-Za-z0-9_]/.test(source[end])) end++;
      const word = source.slice(i, end);
      // Detect `def foo` and `class Foo` patterns: peek back for the
      // last non-whitespace token in tokens[].
      const prev = tokens.length > 0 ? tokens[tokens.length - 1] : null;
      const prevPrev = tokens.length > 1 ? tokens[tokens.length - 2] : null;
      // Effective last keyword is the last token whose kind is keyword.
      let lastKw: Token | null = null;
      for (let k = tokens.length - 1; k >= 0; k--) {
        if (tokens[k].kind === 'plain' && tokens[k].text.trim() === '') continue;
        lastKw = tokens[k];
        break;
      }
      if (PY_KEYWORDS.has(word)) {
        push(word, 'keyword');
      } else if (PY_BUILTINS.has(word)) {
        push(word, 'builtin');
      } else if (lastKw && lastKw.kind === 'keyword' && lastKw.text === 'def') {
        push(word, 'fn');
      } else if (lastKw && lastKw.kind === 'keyword' && lastKw.text === 'class') {
        push(word, 'cls');
      } else {
        push(word, 'plain');
      }
      i = end;
      continue;
    }

    // Whitespace + everything else: lump into 'plain'.
    let end = i;
    while (end < n) {
      const c = source[end];
      if (c === '#' || c === '"' || c === "'" || c === '@' || /[A-Za-z0-9_]/.test(c)) break;
      end++;
    }
    if (end === i) end++;
    push(source.slice(i, end), 'plain');
    i = end;
  }

  return tokens;
}

// Colours match a dark-themed Material-style palette.
const TOKEN_STYLE: Record<TokenKind, React.CSSProperties> = {
  plain: { color: '#cdd6f4' },
  keyword: { color: '#c792ea' },
  builtin: { color: '#82aaff' },
  string: { color: '#c3e88d' },
  comment: { color: '#546e7a', fontStyle: 'italic' },
  number: { color: '#f78c6c' },
  decorator: { color: '#ffcb6b' },
  fn: { color: '#82aaff' },
  cls: { color: '#ffcb6b' },
};

interface CodeBlockProps {
  code: string;
}

function CodeBlock({ code }: CodeBlockProps) {
  const tokens = useMemo(() => tokenizePython(code), [code]);
  return (
    <pre
      style={{
        margin: 0,
        padding: 12,
        borderRadius: 6,
        background: '#1e1e2e',
        fontSize: 12,
        lineHeight: 1.55,
        overflowX: 'auto',
        fontFamily:
          "'Cascadia Code', 'Fira Code', 'JetBrains Mono', 'SF Mono', Menlo, Consolas, 'Courier New', monospace",
      }}
    >
      <code>
        {tokens.map((t, k) => (
          <span key={k} style={TOKEN_STYLE[t.kind]}>
            {t.text}
          </span>
        ))}
      </code>
    </pre>
  );
}

interface CodeExampleProps {
  code?: string;
  input?: string;
  output?: string;
}

function CodeExample({ code, input, output }: CodeExampleProps) {
  const [open, setOpen] = useState(false);
  if (!code && !input && !output) return null;
  return (
    <div style={{ marginLeft: 38, marginTop: 6 }}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        style={{
          background: 'transparent',
          border: 'none',
          color: 'var(--accent, #60a5fa)',
          cursor: 'pointer',
          padding: 0,
          fontSize: 11,
          textDecoration: 'underline',
        }}
      >
        {open ? '▾ Hide code & I/O' : '▸ Show code & I/O'}
      </button>
      {open && (
        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            gap: 8,
            marginTop: 6,
          }}
        >
          {code && (
            <div>
              <div
                style={{
                  fontSize: 10,
                  textTransform: 'uppercase',
                  letterSpacing: 0.5,
                  color: 'var(--text-dim)',
                  marginBottom: 3,
                }}
              >
                Python (representative)
              </div>
              <CodeBlock code={code} />
            </div>
          )}
          {input && (
            <div>
              <div
                style={{
                  fontSize: 10,
                  textTransform: 'uppercase',
                  letterSpacing: 0.5,
                  color: 'var(--text-dim)',
                  marginBottom: 3,
                }}
              >
                Example input
              </div>
              <CodeBlock code={input} />
            </div>
          )}
          {output && (
            <div>
              <div
                style={{
                  fontSize: 10,
                  textTransform: 'uppercase',
                  letterSpacing: 0.5,
                  color: 'var(--text-dim)',
                  marginBottom: 3,
                }}
              >
                Example output
              </div>
              <CodeBlock code={output} />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Job flow data (recon 2026-05-22) ─────────────────────────────────────────

const JOB_FLOWS: JobFlow[] = [
  {
    id: 'snapshot-plex',
    title: 'Snapshot (Plex source)',
    oneliner:
      'Capture watch history, ratings, playlists, and collections from a Plex source into media.db + a per-snapshot .db file.',
    preamble: `Plex-source snapshots use the perf-tuned plexapi engine. Each library is captured in parallel; the run wraps the whole pipeline in run_timer.time_operation for ETA training, and finalises with a snapshot.db materialised from media.db.`,
    steps: [
      {
        n: 1,
        label: 'REST entry',
        cite: 'server/routers/jobs.py::post_job_export',
        detail:
          'Receives SnapshotJobIn from the Run Job form. Validates paths and applies preflight ack if present.',
        role: 'dispatch',
        code: `@router.post("/jobs/export")
def post_job_export(
    body: SnapshotJobIn,
    user: AuthUser = Depends(require_role("operator")),
) -> JobAck:
    """REST entry for snapshot jobs."""
    _apply_preflight_ack(body)
    job_id = queue.submit_snapshot(body.model_dump())
    return JobAck(job_id=job_id)`,
        input: `# POST /api/jobs/export  body:
{
    "source_server_name": "Jade.TV",
    "libraries": ["Movies", "TV"],
    "include_watch_history": True,
    "include_ratings": True,
    "include_playlists": True,
    "include_collections": True,
    "output_dir": "/snapshots",
    "pin_preflight_ack": True,
}`,
        output: `# 202 Accepted
{
    "job_id": "snap_20260522_174201_a91f"
}`,
      },
      {
        n: 2,
        label: 'Queue submit',
        cite: 'server/jobs.py::JobQueue.submit_snapshot',
        detail:
          'Creates a JobRecord and pushes it to the worker queue. Single-worker FIFO; concurrent runs are not supported.',
        role: 'dispatch',
        code: `def submit_snapshot(self, params: dict) -> str:
    """Queue a snapshot job. Returns the job_id."""
    job_id = _new_job_id(prefix="snap")
    rec = JobRecord(
        job_id=job_id,
        mode="snapshot",
        params=params,
        state=STATE_QUEUED,
    )
    self._queue.put(rec)
    self._history[job_id] = rec
    return job_id`,
        input: `params = {
    "source_server_name": "Jade.TV",
    "libraries": ["Movies", "TV"],
    ...
}`,
        output: `"snap_20260522_174201_a91f"
# Side effect: JobRecord enqueued; worker thread picks it up FIFO.`,
      },
      {
        n: 3,
        label: 'Worker pulls',
        cite: 'server/jobs.py::JobQueue._worker_loop',
        detail:
          'Daemon thread pulls the next JobRecord and invokes the per-mode dispatcher. The whole job is wrapped in `with run_timer.time_operation("job:snapshot", scope=SCOPE_RUN)`.',
        role: 'dispatch',
        code: `def _worker_loop(self) -> None:
    """Single-worker FIFO. Wraps every job in run_timer."""
    while not self._shutdown.is_set():
        rec = self._queue.get()
        run_id = run_timer.start_run()
        try:
            with run_timer.time_operation(
                f"job:{rec.mode}", scope=SCOPE_RUN,
            ):
                if rec.mode == "snapshot":
                    self._run_snapshot(rec)
                elif rec.mode == "restore":
                    self._run_restore(rec)
                # ... other modes
        finally:
            entries = run_timer.end_run()
            persist_entries(entries, run_id)
            record_run_history(rec, run_id)`,
      },
      {
        n: 4,
        label: 'Snapshot dispatcher',
        cite: 'server/jobs.py::JobQueue._run_snapshot',
        detail:
          'Resolves source server, merges settings, sets up logging, populates state ContextVars. Branches on service_type: "plex" stays in this path; anything else routes to _run_snapshot_via_adapter.',
        role: 'dispatch',
        code: `def _run_snapshot(self, rec: JobRecord) -> None:
    """Per-snapshot dispatcher."""
    params = self._merge_settings(rec.params)
    src = self._resolve_source_connection(params)
    if src.service_type != "plex":
        return self._run_snapshot_via_adapter(rec, src, params)

    # Plex source path.
    log_dir = self._build_logger(rec, src)
    dump_run_settings("snapshot", log_dir, params, log)
    state._plex_base_url = src.base_url
    state._plex_token = src.token
    state._dashboard = DashboardState()
    server_id = self._resolve_server_id(src.name)
    upsert_server_row(server_id, src.name, src.service_type)

    run_snapshot(src.plex, params["libraries"], ...)`,
      },
      {
        n: 5,
        label: 'Resolve source',
        cite: 'server/jobs.py::_resolve_source_connection',
        detail:
          'Maps source_server_name to a registered server in servers.json, decrypts the Fernet token, opens a PlexServer connection.',
        role: 'preflight',
        code: `def _resolve_source_connection(params: dict) -> ServerConnection:
    """Look up source server in registry; build a connection."""
    name = params["source_server_name"]
    row = server_registry.get_server_by_name(name)
    if not row:
        raise ValueError(f"Unknown source server: {name}")
    token = server_registry.decrypt_server_token(row)
    plex = PlexServer(row["url"], token, timeout=15)
    return ServerConnection(
        name=name,
        service_type=row["service_type"],
        base_url=row["url"],
        token=token,
        plex=plex,
    )`,
        input: `params = {"source_server_name": "Jade.TV", ...}`,
        output: `ServerConnection(
    name="Jade.TV",
    service_type="plex",
    base_url="http://192.168.1.10:32400",
    token="<decrypted Fernet token>",
    plex=<PlexServer object>,
)`,
      },
      {
        n: 6,
        label: 'Stamp run dir',
        cite: 'server/run_context.py::_set_run_timestamp + _build_logger',
        detail:
          'Builds `<server_slug>_<libraries>_<timestamp>/` log dir, opens the per-run logger with the token scrubber installed on every handler.',
        role: 'preflight',
        code: `def _build_logger(rec: JobRecord, src: ServerConnection) -> Path:
    """Create the per-run log dir, attach the token scrubber."""
    slug = safe_server_name(src.name)
    libs_part = "_".join(rec.params["libraries"])[:64]
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    log_dir = LOG_ROOT / f"{slug}_{libs_part}_{ts}"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger, _ = setup_logging(log_dir, verbose=rec.params.get("verbose"))
    return log_dir`,
        output: `Path('/var/log/plexmigrate/Jade-TV_Movies_TV_20260522_174201/')
# Side effects:
#   - runtime.log opened for write
#   - troubleshoot.log opened for write
#   - unresolved.log opened for write
#   - TokenScrubFilter installed on every handler`,
      },
      {
        n: 7,
        label: 'Dump run-settings.log',
        cite: 'services/run_settings_log.py::dump_run_settings',
        detail:
          'Writes a human-readable Markdown of every active setting at job time. Secrets redacted.',
        role: 'telemetry',
        code: `def dump_run_settings(
    job_type: str,
    run_log_dir: Path,
    params: dict,
    logger: logging.Logger,
) -> None:
    """Emit run-settings.log next to runtime.log."""
    md = _build_markdown(job_type, params)
    md = redact_secrets(md)  # plex_token, password, fernet_key
    out = run_log_dir / "run-settings.log"
    out.write_text(md, encoding="utf-8")
    logger.info("wrote %s", out)`,
        output: `# Wrote: <run_log_dir>/run-settings.log
# Contents (Markdown):
#   # Run Settings - <rundir>
#   Captured: 2026-05-22 17:42:01 UTC
#   ## At a glance
#       Source     : Jade.TV
#       Libraries  : Movies, TV
#       Workers    : 8
#   ## Per-library metrics
#       <table>
#   ## Tunables (47 known, 3 overridden)
#       <Overridden subsection>
#       <All tunables table>`,
      },
      {
        n: 8,
        label: 'Populate ContextVars',
        cite: 'services/state.py (multiple sets)',
        detail:
          'Sets _dashboard, _plex_base_url, _plex_token, _plex_owner_name, _run_trigger ContextVars so worker threads spawned via submit_with_context see them.',
        role: 'state',
        code: `# services/state.py — module-level
_dashboard_var: ContextVar[Optional[DashboardState]] = ContextVar(
    "_dashboard", default=None,
)
_plex_base_url_var: ContextVar[Optional[str]] = ContextVar(
    "_plex_base_url", default=None,
)
_plex_token_var: ContextVar[Optional[str]] = ContextVar(
    "_plex_token", default=None,
)

# Setter used by JobQueue at dispatch time
def reset_run_state() -> None:
    _dashboard_var.set(DashboardState())
    _http_lib_var.set(None)
    _http_job_id_var.set(None)`,
      },
      {
        n: 9,
        label: 'Upsert server row',
        cite: 'server/media_db/_core.py::upsert_server_row',
        detail:
          'Ensures the source server has a row in media.db (lookup by registered server_id). Required before per-server data writes.',
        role: 'write-path',
        code: `def upsert_server_row(
    server_id: str,
    name: str,
    service_type: str,
) -> None:
    """Idempotent: insert OR update."""
    with _connect() as cx:
        cx.execute(
            """
            INSERT INTO servers (server_id, name, service_type)
            VALUES (?, ?, ?)
            ON CONFLICT(server_id) DO UPDATE SET
                name = excluded.name,
                service_type = excluded.service_type
            """,
            (server_id, name, service_type),
        )`,
      },
      {
        n: 10,
        label: 'Run snapshot engine',
        cite: 'services/snapshotter.py::run_snapshot',
        detail:
          'Top-level Plex-source orchestrator. Discovers selected libraries, spawns the per-library ThreadPoolExecutor.',
        role: 'engine',
        code: `def run_snapshot(
    server: PlexServer,
    library_names: List[str],
    output_dir: Path,
    *,
    include_watch_history: bool = True,
    include_ratings: bool = True,
    include_playlists: bool = True,
    include_collections: bool = True,
    library_workers: int = 4,
    home_users: Optional[List[str]] = None,
) -> SnapshotResult:
    """Per-server snapshot orchestrator."""
    sections = _resolve_sections(server, library_names)
    state.get_dashboard().add_libraries(library_names)
    with ThreadPoolExecutor(max_workers=library_workers) as pool:
        futures = {
            submit_with_context(
                pool,
                snapshot_library,
                server, sec, output_dir, log, home_users,
                include_watch_history=include_watch_history,
                include_ratings=include_ratings,
                include_playlists=include_playlists,
                include_collections=include_collections,
            ): sec.title for sec in sections
        }
        for fut in as_completed(futures):
            sec_name = futures[fut]
            result = fut.result()
            state.get_dashboard().set_library_done(sec_name)
    return SnapshotResult(libraries=sections)`,
        input: `server = <PlexServer connection>
library_names = ["Movies", "TV"]
output_dir = Path("/snapshots")
include_watch_history = True
include_ratings = True
include_playlists = True
include_collections = True`,
        output: `SnapshotResult(libraries=[<LibrarySection 'Movies'>, <LibrarySection 'TV'>])
# Side effects:
#   - 4 worker threads spawned per library (gather phases)
#   - media.db: items, server_items, watch_events, ratings, playlists, collections rows written
#   - DashboardState updated with per-library progress`,
      },
      {
        n: 11,
        label: 'Per-library gather',
        cite: 'services/snapshotter.py::snapshot_library',
        detail:
          'For each library, fires four concurrent gather tasks: snapshot_watch_history, snapshot_ratings, snapshot_playlists, snapshot_collections.',
        role: 'engine',
        code: `def snapshot_library(
    server: PlexServer,
    sec: LibrarySection,
    output_dir: Path,
    logger: logging.Logger,
    home_users: List[str],
    *,
    include_watch_history: bool,
    include_ratings: bool,
    include_playlists: bool,
    include_collections: bool,
) -> LibrarySnapshot:
    """Per-library gather + media.db ingest."""
    payload = {"library": sec.title, "library_section_id": sec.key}

    with time_operation(
        "snapshot_library", scope=SCOPE_LIBRARY,
        library=sec.title,
    ):
        with ThreadPoolExecutor(max_workers=4) as gather_pool:
            futures = []
            if include_watch_history:
                futures.append(submit_with_context(
                    gather_pool, snapshot_watch_history, server, sec, home_users,
                ))
            if include_ratings:
                futures.append(submit_with_context(
                    gather_pool, snapshot_ratings, server, sec, home_users,
                ))
            if include_playlists:
                futures.append(submit_with_context(
                    gather_pool, snapshot_playlists, server, sec, home_users,
                ))
            if include_collections:
                futures.append(submit_with_context(
                    gather_pool, snapshot_collections, server, sec,
                ))
            for fut in as_completed(futures):
                payload.update(fut.result())

    ingest_snapshot_payload(payload)
    return LibrarySnapshot(name=sec.title, key=sec.key)`,
      },
      {
        n: 12,
        label: 'Ingest payload',
        cite: 'server/media_db/snapshot_ingest.py::ingest_snapshot_payload',
        detail:
          'Writes per-library payload to media.db. The v0.15 library_section_id-on-every-row invariant is enforced here (ValueError on missing/zero).',
        role: 'write-path',
        code: `def ingest_snapshot_payload(payload: dict) -> None:
    """Write per-library payload to media.db.
    Enforces v0.15: library_section_id MUST be present and > 0."""
    sec_id = payload.get("library_section_id")
    if not sec_id or sec_id <= 0:
        raise ValueError(
            f"ingest_snapshot_payload: library_section_id required "
            f"(got {sec_id!r})"
        )

    with _connect() as cx, cx.transaction():
        for item in payload.get("items", []):
            _upsert_item(cx, item)
            _upsert_server_item(cx, item, sec_id)
        for ev in payload.get("watch_events", []):
            _insert_watch_event(cx, ev, sec_id)
        for r in payload.get("ratings", []):
            _upsert_rating(cx, r, sec_id)
        for pl in payload.get("playlists", []):
            _upsert_playlist(cx, pl, sec_id)
        for col in payload.get("collections", []):
            _upsert_collection(cx, col, sec_id)`,
        input: `payload = {
    "library": "Movies",
    "library_section_id": 1,
    "items": [{"title": "Inception", "guids": ["imdb://tt1375666", ...], ...}, ...],
    "watch_events": [...],
    "ratings": [...],
    "playlists": [...],
    "collections": [...],
}`,
        output: `# Side effects: SQL writes to media.db across 6 tables.
# Raises ValueError when library_section_id is missing / 0
# (the v0.15 invariant).`,
      },
      {
        n: 13,
        label: 'Materialise snapshot.db',
        cite: 'server/snapshot_capture.py::capture_snapshot_db',
        detail:
          'Reads filtered media.db rows for this server and writes a portable per-snapshot .db file under output_dir. Schema v15+.',
        role: 'write-path',
        code: `def capture_snapshot_db(
    server_id: str,
    output_dir: Path,
    captured_at: float,
) -> Path:
    """Materialise a portable per-snapshot .db for server_id."""
    out_path = output_dir / _snapshot_filename(server_id, captured_at)
    with media_db._connect() as src_cx:
        src_cx.execute(f"ATTACH DATABASE ? AS snap", (str(out_path),))
        _create_snapshot_schema(src_cx)  # v15+
        _copy_table(src_cx, "items", where="""
            id IN (SELECT item_id FROM server_items WHERE server_id = ?)
        """, args=(server_id,))
        _copy_table(src_cx, "server_items", where="server_id = ?", args=(server_id,))
        _copy_table(src_cx, "watch_events", where="server_id = ?", args=(server_id,))
        _copy_table(src_cx, "ratings", where="server_id = ?", args=(server_id,))
        _copy_table(src_cx, "playlists", where="server_id = ?", args=(server_id,))
        _copy_table(src_cx, "collections", where="server_id = ?", args=(server_id,))
        _copy_table(src_cx, "library_sections", where="server_id = ?", args=(server_id,))
        _write_snapshot_meta(src_cx, server_id, captured_at)
        src_cx.execute("DETACH DATABASE snap")
    return out_path`,
        output: `Path('/snapshots/Jade-TV_20260522_174201.db')
# Contents: SQLite v15-schema snapshot with all rows for server "Jade.TV"`,
      },
      {
        n: 14,
        label: 'Register snapshot',
        cite: 'server/snapshot_registry.py::upsert_snapshot',
        detail:
          'Inserts a row in snapshots.db keyed by the new .db file. Drives the Exports panel + the Restore-from-snapshot picker.',
        role: 'write-path',
        code: `def upsert_snapshot(
    snapshot_id: str,
    file_path: Path,
    server_id: str,
    server_name: str,
    libraries: List[str],
    schema_version: int,
    item_counts: dict,
    captured_at: float,
) -> None:
    """Register a captured snapshot in the registry DB."""
    with _connect() as cx:
        cx.execute("""
            INSERT OR REPLACE INTO snapshots
              (id, file_path, server_id, server_name, libraries_json,
               schema_version, item_counts_json, captured_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (snapshot_id, str(file_path), server_id, server_name,
              json.dumps(libraries), schema_version,
              json.dumps(item_counts), captured_at))`,
      },
      {
        n: 15,
        label: 'Finalize run dir',
        cite: 'server/run_context.py::_finalize_run',
        detail:
          'Renames the run log dir to add PASS or FAIL suffix based on the outcome counters.',
        role: 'cleanup',
        code: `def _finalize_run(run_log_dir: Path, success: bool) -> Path:
    """Rename log dir with PASS / FAIL suffix."""
    suffix = "PASS" if success else "FAIL"
    new_dir = run_log_dir.with_name(f"{run_log_dir.name}_{suffix}")
    run_log_dir.rename(new_dir)
    return new_dir`,
        output: `Path('/var/log/plexmigrate/Jade-TV_Movies_TV_20260522_174201_PASS/')`,
      },
      {
        n: 16,
        label: 'Flush timing buffer',
        cite: 'services/run_timer.py::end_run → server/run_timings_db.py::persist_entries',
        detail:
          'Drains the in-memory TimingEntry buffer to run_timings.db. eta_training.batch_update folds the entries into per-bucket EMA + variance.',
        role: 'telemetry',
        code: `def end_run(persist: bool = True) -> List[TimingEntry]:
    """Drain the per-run buffer; return entries."""
    buf = _current_run_buffer
    if buf is None:
        return []
    entries = buf.snapshot()
    if persist:
        persist_entries(entries, buf.run_id)
        eta_training.get_trainer().batch_update(entries)
    _set_current_run_buffer(None)
    return entries`,
        input: `# Implicit: the module-level _current_run_buffer that was opened
#           in start_run() at the top of the worker loop.`,
        output: `[TimingEntry(label='snapshot_library', library='Movies',
              duration_seconds=12.4, items_processed=2847, ...),
 TimingEntry(label='snapshot_library', library='TV',
              duration_seconds=8.1, items_processed=1109, ...),
 ...]
# Side effects:
#   - run_timings.db: 1 row per TimingEntry
#   - eta_weights table: EMA + variance updated per matched bucket`,
      },
      {
        n: 17,
        label: 'Record run_history row',
        cite: 'server/run_timings_db.py::record_run_history',
        detail:
          'One summary row per completed run: mode, server, libraries, start/end, success/failure, item totals.',
        role: 'telemetry',
        code: `def record_run_history(rec: JobRecord, run_id: str) -> None:
    """One row per completed run; drives Recent Runtimes panel."""
    with _connect() as cx:
        cx.execute("""
            INSERT INTO run_history
              (run_id, mode, server_id, libraries_json,
               started_at, ended_at, success, item_totals_json,
               affected_users_json, trigger, schedule_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_id, rec.mode, rec.server_id,
            json.dumps(rec.params.get("libraries", [])),
            rec.started_at, rec.ended_at,
            rec.state == STATE_COMPLETED,
            json.dumps(rec.item_totals),
            json.dumps(rec.affected_users),
            rec.params.get("_trigger", "manual"),
            rec.params.get("_schedule_name"),
        ))`,
      },
      {
        n: 18,
        label: 'Reset ContextVars',
        cite: 'services/state.py (cleanup paths)',
        detail: 'Nulls _dashboard so the next job starts with a fresh context.',
        role: 'cleanup',
        code: `# services/state.py
def reset_run_state() -> None:
    """Null per-run state so the next job starts clean."""
    _dashboard_var.set(None)
    _restoration_log = None
    _restoration_log_affected_users = []
    _snapshot_server_id = None
    _run_trigger = None
    _run_schedule_name = None`,
      },
    ],
    sharedInfra: ['state', 'dashboard', 'run_timer', 'logging_ops', 'log_scrubber', 'eta_training', 'run_settings_log'],
    surfaces: [
      'server_data/media.db',
      'server_data/snapshots.db (registry)',
      '<output_dir>/<server>_<libs>_<ts>.db',
      'server_data/run_timings.db',
      'plex_logs/<slug>/',
    ],
  },

  // ── Snapshot (Adapter) ────────────────────────────────────────────────────
  {
    id: 'snapshot-adapter',
    title: 'Snapshot (Jellyfin / Emby source)',
    oneliner:
      'Capture from a non-Plex source via the backend-agnostic adapter ABC. Same destination shape as Plex sources.',
    preamble: `Identical entry + dispatch path as the Plex snapshot. At the engine selection branch, _run_snapshot routes service_type != "plex" to the adapter engine. Capture flows through the MediaServerAdapter ABC instead of plexapi directly.`,
    steps: [
      {
        n: 1,
        label: 'Branch on service_type',
        cite: 'server/jobs.py::JobQueue._run_snapshot',
        detail:
          'Detects the source server\'s service_type from servers.json. Anything other than "plex" routes to _run_snapshot_via_adapter.',
        role: 'dispatch',
        code: `def _run_snapshot(self, rec: JobRecord) -> None:
    """Dispatcher: routes to adapter when non-Plex source."""
    src = self._resolve_source_connection(rec.params)
    if src.service_type != "plex":
        return self._run_snapshot_via_adapter(rec, src, rec.params)
    # ... Plex path`,
      },
      {
        n: 2,
        label: 'Adapter dispatcher',
        cite: 'server/jobs.py::JobQueue._run_snapshot_via_adapter',
        detail:
          'Builds a ServerConnection (adapter instance + creds + base_url) from the registry, validates the adapter responds, then enters the adapter engine.',
        role: 'dispatch',
        code: `def _run_snapshot_via_adapter(
    self, rec: JobRecord, src: ServerConnection, params: dict,
) -> None:
    """Per-snapshot dispatcher for adapter backends."""
    adapter = src.adapter  # JellyfinAdapter / EmbyAdapter
    if not adapter.ping():
        raise ConnectionError(f"{src.name} unreachable")
    server_id = self._resolve_server_id(src.name)
    upsert_server_row(server_id, src.name, src.service_type)

    for library_name in params["libraries"]:
        lib_id = adapter.find_library_id(library_name)
        snapshot_library_adapter(
            src, lib_id, library_name,
            include_watch_history=params["include_watch_history"],
            include_ratings=params["include_ratings"],
        )`,
      },
      {
        n: 3,
        label: 'Per-library snapshot',
        cite: 'services/snapshotter_adapter.py::snapshot_library_adapter',
        detail:
          'Per-library orchestrator. Iterates items via adapter.iter_items(library_id, ...). Each ItemSnapshot returned drives one media.db row write.',
        role: 'engine',
        code: `def snapshot_library_adapter(
    conn: ServerConnection,
    library_id: str,
    library_name: str,
    *,
    include_watch_history: bool,
    include_ratings: bool,
) -> None:
    """Per-library adapter-engine snapshot. Backend-agnostic."""
    sec_key = stable_section_key(library_id)
    payload = {
        "library": library_name,
        "library_section_id": sec_key,
        "library_section_type": conn.adapter.library_type(library_id),
        "items": [],
        "watch_events": [],
        "ratings": [],
    }

    for item in conn.adapter.iter_items(library_id):
        engine_item = item_snapshot_to_engine_dict(item)
        payload["items"].append(engine_item)
        if include_watch_history and item.view_count > 0:
            payload["watch_events"].append({
                "item_backend_id": item.backend_item_id,
                "user_backend_id": item.user_backend_id,
                "view_count": item.view_count,
                "view_offset_ms": item.view_offset_ms,
                "last_viewed_at": item.last_viewed_at,
            })
        if include_ratings and item.user_rating > 0:
            payload["ratings"].append({
                "item_backend_id": item.backend_item_id,
                "user_backend_id": item.user_backend_id,
                "rating": item.user_rating,
            })

    ingest_snapshot_payload(payload)`,
        input: `conn = ServerConnection(name="Plex+", service_type="jellyfin", adapter=<JellyfinAdapter>)
library_id = "5b1e3f..."  # Jellyfin GUID
library_name = "Movies"
include_watch_history = True
include_ratings = True`,
      },
      {
        n: 4,
        label: 'Adapter calls',
        cite: 'services/adapters/{jellyfin,emby}.py',
        detail:
          'JellyfinAdapter / EmbyAdapter implement iter_items, get_watch_state, get_ratings, etc. via the shared _http_base.py session. Jellyfin uses MediaBrowser auth header; Emby uses Emby scheme.',
        role: 'adapter',
        code: `class JellyfinAdapter(MediaServerAdapter):
    """Jellyfin implementation of MediaServerAdapter."""

    def iter_items(
        self, library_id: str, *, page_size: int = 200,
    ) -> Iterable[ItemSnapshot]:
        """Stream all items in a library."""
        start = 0
        while True:
            params = {
                "ParentId": library_id,
                "Recursive": "true",
                "Fields": "ProviderIds,UserData",
                "StartIndex": start,
                "Limit": page_size,
            }
            resp = self._session.get(
                f"/Users/{self._user_id}/Items",
                params=params,
                headers=self._auth_header(),
            )
            data = resp.json()
            items = data.get("Items", [])
            if not items:
                return
            for raw in items:
                yield ItemSnapshot(
                    backend_item_id=raw["Id"],
                    title=raw["Name"],
                    guids=_jellyfin_guids(raw["ProviderIds"]),
                    view_count=raw["UserData"]["PlayCount"],
                    last_viewed_at=raw["UserData"].get("LastPlayedDate"),
                    user_rating=raw["UserData"].get("UserRating", 0),
                )
            if len(items) < page_size:
                return
            start += page_size`,
      },
      {
        n: 5,
        label: 'Stable section_key',
        cite: 'services/snapshotter_adapter.py::stable_section_key',
        detail:
          'Hashes the non-numeric library GUID (Jellyfin/Emby) to a 31-bit positive int so it fits the v0.15 section_key NOT NULL invariant.',
        role: 'engine',
        code: `def stable_section_key(library_id: str) -> int:
    """Map a backend library identifier to a stable positive int
    that fits the v0.15 section_key column."""
    if library_id.isdigit():
        # Plex uses small integer section keys; keep as-is.
        return int(library_id)
    # Jellyfin / Emby use GUIDs. Hash to 31-bit (positive int).
    h = hashlib.blake2b(library_id.encode(), digest_size=4).digest()
    return int.from_bytes(h, "big") & 0x7FFFFFFF`,
        input: `library_id = "5b1e3f6c0b194f0a8c2d3e4f5a6b7c8d"`,
        output: `1843726491  # always > 0; same library always hashes to the same int`,
      },
    ],
    sharedInfra: ['state', 'dashboard', 'run_timer', 'logging_ops', 'adapters', 'eta_training'],
    surfaces: ['server_data/media.db', 'server_data/snapshots.db', '<output_dir>/<server>_<libs>_<ts>.db'],
    notes: [
      'Playlists + collections capture on the adapter engine is Phase 2 work; MVP captures watch_history + ratings only.',
      'Per-user fan-out on the adapter engine shipped in Phase 3.',
    ],
  },

  // ── Restore (Plex) ────────────────────────────────────────────────────────
  {
    id: 'restore-plex',
    title: 'Restore (Plex destination)',
    oneliner:
      'Read a snapshot payload and apply it to a Plex destination. Merge (additive) is default; Replace overwrites destination-only members.',
    preamble: `The restore engine lives in services/restorer/ (split into 6 submodules: engine, watch, ratings, playlists, collections, plus __init__). Each metric is restored by its own submodule.`,
    steps: [
      {
        n: 1,
        label: 'Load payload',
        cite: 'server/jobs.py::_load_snapshot_payload',
        detail:
          'Two paths: .db inputs deserialise via snapshot_serializer.py; .json inputs read directly and unwrap. The .json branch is the legacy-strip surface.',
        role: 'preflight',
        code: `def _load_snapshot_payload(input_path: Path) -> dict:
    """Load a snapshot payload from .db or .json."""
    suffix = input_path.suffix.lower()
    if suffix == ".db":
        return payload_from_snapshot_db(input_path)
    elif suffix == ".json":
        # Legacy path; .plexexport.json wrapper-unwrap.
        # Targeted for removal under the legacy strip plan.
        raw = json.loads(input_path.read_text())
        if "libraries" not in raw:
            raw = {"libraries": [raw]}  # wrap flat shape
        return raw
    raise ValueError(f"Unsupported snapshot extension: {suffix}")`,
        input: `input_path = Path("/snapshots/Jade-TV_20260522_174201.db")`,
        output: `{
    "snapshot_meta": {
        "server_id": "abc123",
        "captured_at": 1716397321,
        "schema_version": 15,
    },
    "libraries": [
        {"library": "Movies", "library_section_id": 1,
         "items": [...], "watch_events": [...], ...},
        {"library": "TV", ...},
    ],
}`,
      },
      {
        n: 2,
        label: 'Resolve destination',
        cite: 'server/jobs.py::_resolve_dest_connection',
        detail: 'Connects to the destination Plex via the registered server token.',
        role: 'preflight',
        code: `def _resolve_dest_connection(params: dict) -> ServerConnection:
    """Look up destination server in registry; build connection."""
    name = params["dest_server_names"][0]  # single-dest path
    row = server_registry.get_server_by_name(name)
    token = server_registry.decrypt_server_token(row)
    plex = PlexServer(row["url"], token, timeout=15)
    return ServerConnection(
        name=name, service_type=row["service_type"],
        base_url=row["url"], token=token, plex=plex,
    )`,
      },
      {
        n: 3,
        label: 'Open restoration log',
        cite: 'services/restoration_log.py::open_restoration_log',
        detail:
          'Opens per-run restoration.log inside the run dir. One line per (item, user, metric) outcome (RESTORED/NOOP/SKIPPED/FAILED).',
        role: 'telemetry',
        code: `def open_restoration_log(run_log_dir: Path) -> RestorationLogWriter:
    """Open restoration.log writer. Best-effort: failure → null writer."""
    try:
        path = run_log_dir / "restoration.log"
        fp = path.open("w", encoding="utf-8")
        writer = RestorationLogWriter(fp, started_at=time.time())
        state._restoration_log = writer
        return writer
    except OSError as exc:
        log.warning("restoration.log open failed: %s", exc)
        return _NullWriter()`,
      },
      {
        n: 4,
        label: 'restore_export_file',
        cite: 'services/restorer/__init__.py::restore_export_file',
        detail:
          'Per-file driver. Accepts the wrapped payload from the serializer; dispatches per-library to watch / ratings / playlists / collections.',
        role: 'engine',
        code: `def restore_export_file(
    server: PlexServer,
    input_path: Path,
    *,
    mode: str = "merge",
    preloaded_data: Optional[dict] = None,
) -> RestoreResult:
    """Per-export-file restore driver."""
    payload = preloaded_data or _load_snapshot_payload(input_path)
    libraries = payload.get("libraries", [])

    result = RestoreResult()
    for lib in libraries:
        lib_name = lib["library"]
        section_key = lib["library_section_id"]
        section = _resolve_section(server, lib_name)

        with time_operation(
            "restore_library", scope=SCOPE_LIBRARY, library=lib_name,
        ):
            for user_block in lib.get("users", []):
                user = user_block["name"]
                restore_watch_history(server, section, user_block, mode)
                restore_ratings(server, section, user_block, mode)
                restore_playlists(server, section, user_block, mode, user=user)
                restore_collections(server, section, user_block, mode, user=user)

    return result`,
      },
      {
        n: 5,
        label: 'Watch history',
        cite: 'services/restorer/watch.py::restore_watch_history',
        detail:
          'Per-user, per-item: resolves item on destination via guid_translator, scrobbles via /:/scrobble or markPlayed depending on Merge / Replace mode.',
        role: 'write-path',
        code: `def restore_watch_history(
    server: PlexServer,
    section: LibrarySection,
    user_block: dict,
    mode: str,
) -> None:
    """Apply per-user watch events for one library."""
    user = user_block["name"]
    for ev in user_block.get("watch_events", []):
        target = resolve_item(server, section, ev["guids"])
        if target is None:
            state._restoration_log.failed(
                library=section.title, user=user, metric="watch_history",
                item=ev.get("title"), reason="resolver-miss",
            )
            continue
        # Merge: only bump view_count if snapshot is higher.
        if mode == "merge" and target.viewCount >= ev["view_count"]:
            state._restoration_log.noop(
                library=section.title, user=user, metric="watch_history",
                item=target.title,
            )
            continue
        try:
            _scrobble(server, target, ev["view_count"], user_token=...)
            state._restoration_log.restored(
                library=section.title, user=user, metric="watch_history",
                item=target.title,
            )
        except Exception as exc:
            state._restoration_log.failed(
                library=section.title, user=user, metric="watch_history",
                item=target.title, reason=str(exc),
            )`,
      },
      {
        n: 6,
        label: 'Ratings',
        cite: 'services/restorer/ratings.py::restore_ratings',
        detail:
          'Per-user, per-item: rates via /:/rate. Merge mode never overwrites an existing rating; Replace mode sets the snapshot value unconditionally.',
        role: 'write-path',
        code: `def restore_ratings(
    server: PlexServer,
    section: LibrarySection,
    user_block: dict,
    mode: str,
) -> None:
    """Apply per-user ratings."""
    user = user_block["name"]
    for r in user_block.get("ratings", []):
        target = resolve_item(server, section, r["guids"])
        if target is None:
            state._restoration_log.failed(...)
            continue
        existing = target.userRating
        if mode == "merge" and existing:
            state._restoration_log.noop(...)
            continue
        try:
            target.rate(r["rating"])
            state._restoration_log.restored(...)
        except Exception as exc:
            state._restoration_log.failed(..., reason=str(exc))`,
      },
      {
        n: 7,
        label: 'Playlists',
        cite: 'services/restorer/playlists.py::restore_playlists',
        detail:
          'Per-user: creates the playlist if missing, appends members in Merge; in Replace mode, diffs against snapshot members and removes destination-only entries. Smart playlists are skipped.',
        role: 'write-path',
        code: `def restore_playlists(
    server: PlexServer,
    section: LibrarySection,
    user_block: dict,
    mode: str,
    user: str = "Plex Owner",
) -> None:
    """Restore non-smart playlists."""
    for pl in user_block.get("playlists", []):
        if pl.get("smart"):
            state._restoration_log.skipped(
                library=section.title, user=user, metric="playlists",
                item=pl["title"], reason="smart playlist not supported",
            )
            continue
        existing = _find_playlist(server, pl["title"], user)
        snap_items = [resolve_item(server, section, g) for g in pl["item_guids"]]
        snap_items = [x for x in snap_items if x]
        if existing is None:
            Playlist.create(server, pl["title"], items=snap_items)
            state._restoration_log.restored(...)
        elif mode == "merge":
            new = [x for x in snap_items if x not in existing.items()]
            if new:
                existing.addItems(new)
            else:
                state._restoration_log.noop(...)
        else:  # replace
            _replace_playlist_members(existing, snap_items)`,
      },
      {
        n: 8,
        label: 'Close restoration log',
        cite: 'services/restoration_log.py::close_with_summary',
        detail:
          'Drains the writer; emits the summary block: totals per status, per metric, per library, per user, wall-clock.',
        role: 'cleanup',
        code: `def close_with_summary(writer: RestorationLogWriter) -> None:
    """Append summary block, close handle. Idempotent."""
    if writer._closed:
        return
    elapsed = time.time() - writer.started_at
    summary = _build_summary_block(
        per_status=writer.per_status,
        per_metric=writer.per_metric,
        per_library=writer.per_library,
        per_user=writer.per_user,
        elapsed=elapsed,
    )
    writer._fp.write("\\n" + summary)
    writer._fp.flush()
    writer._fp.close()
    writer._closed = True`,
      },
    ],
    sharedInfra: ['state', 'dashboard', 'run_timer', 'logging_ops', 'restoration_log', 'eta_training'],
    surfaces: [
      'Destination Plex server (via /:/scrobble, /:/rate, playlist/collection mutators)',
      'server_data/run_timings.db',
      'plex_logs/<slug>/restoration.log',
    ],
  },

  // ── Restore (Adapter) ─────────────────────────────────────────────────────
  {
    id: 'restore-adapter',
    title: 'Restore (Jellyfin / Emby destination)',
    oneliner:
      'Backend-agnostic restore engine. Same payload shape as the Plex restorer consumes; writes go through the destination adapter.',
    preamble: `When the destination service_type is "jellyfin" or "emby", _run_restore routes through restorer_adapter.py::restore_payload_adapter. Adapter calls replace the plexapi calls.`,
    steps: [
      {
        n: 1,
        label: 'Cross-platform preflight',
        cite: 'services/restorer_adapter.py::dry_run_resolve_users (enforced)',
        detail:
          'Re-runs the user-resolution dry-run at restore start. If overall_verdict == "blocked", raises ValueError before any write.',
        role: 'preflight',
        code: `def dry_run_resolve_users(
    payload: dict,
    adapter: MediaServerAdapter,
    dest_server_id: str,
    source_server_id: str,
) -> CrossPlatformPreflightReport:
    """Read-only user-resolution check. No writes."""
    resolutions: List[UserResolution] = []
    for lib in payload["libraries"]:
        for user_block in lib.get("users", []):
            src_user = user_block["name"]
            dest_user = _resolve_destination_user(
                src_user, adapter, dest_server_id, source_server_id,
            )
            resolutions.append(UserResolution(
                source_user=src_user,
                proposed_dest_user_id=dest_user.id if dest_user else None,
                proposed_resolution=_classify(dest_user, user_block),
                blocks_submit=(dest_user is None and user_block["is_admin"]),
            ))
    verdict = _aggregate_verdict(resolutions)
    return CrossPlatformPreflightReport(
        resolutions=resolutions, overall_verdict=verdict,
    )`,
      },
      {
        n: 2,
        label: 'Engine entry',
        cite: 'services/restorer_adapter.py::restore_payload_adapter',
        detail:
          'Top-level orchestrator. Iterates the wrapped payload\'s libraries; per library, iterates per-user blocks.',
        role: 'engine',
        code: `def restore_payload_adapter(
    conn: ServerConnection,
    payload: dict,
    *,
    source_server_id: str,
    enforce_preflight: bool = True,
) -> RestoreResult:
    """Backend-agnostic restore engine for Jellyfin / Emby."""
    if enforce_preflight:
        report = dry_run_resolve_users(
            payload, conn.adapter, conn.server_id, source_server_id,
        )
        if report.overall_verdict == "blocked":
            raise ValueError(f"Preflight blocked: {report.reasons}")

    result = RestoreResult()
    for lib in payload["libraries"]:
        section_id = lib["library_section_id"]
        for user_block in lib.get("users", []):
            src_user = user_block["name"]
            dest_user = _resolve_destination_user(
                src_user, conn.adapter, conn.server_id, source_server_id,
            )
            if dest_user is None:
                # Tier-3 skip; recorded in restoration log if open
                continue
            _apply_per_user_block(
                conn.adapter, dest_user, lib, user_block,
            )
    return result`,
      },
      {
        n: 3,
        label: 'Resolve users per row',
        cite: 'services/restorer_adapter.py::_resolve_destination_user',
        detail:
          'Tier 0: user_identity_map lookup; Tier 1: case-insensitive username match; Tier 2: single-admin destination fallback; else SKIPPED.',
        role: 'engine',
        code: `def _resolve_destination_user(
    src_user: str,
    adapter: MediaServerAdapter,
    dest_server_id: str,
    source_server_id: str,
) -> Optional[UserSpec]:
    """Tier 0 → Tier 1 → Tier 2 resolution chain."""
    # Tier 0: identity_map (authoritative)
    mapped = media_db.user_identity_map.get(
        source_server_id, src_user, dest_server_id,
    )
    if mapped:
        return adapter.get_user_by_id(mapped["dest_user_id"])

    # Tier 1: case-insensitive name match
    for u in adapter.list_users():
        if u.name.casefold() == src_user.casefold():
            return u

    # Tier 2: single-admin fallback (owner-role source only)
    if _src_is_owner_role(src_user):
        admins = [u for u in adapter.list_users() if u.is_admin]
        if len(admins) == 1:
            return admins[0]

    # Else: skip with reason
    return None`,
        input: `src_user = "alice"
adapter = <JellyfinAdapter for "Plex+">
dest_server_id = "jf-srv-1"
source_server_id = "plex-srv-1"`,
        output: `# Tier 0 hit:
UserSpec(id="u-7f3a", name="Alice", is_admin=False, ...)

# Or None if no tier matches → restoration log records SKIPPED reason.`,
      },
      {
        n: 4,
        label: 'Adapter writes per metric',
        cite: 'services/adapters/{jellyfin,emby}.py',
        detail:
          'set_watched + set_resume_position for watch history; set_rating (Likes) + set_user_data Rating for the dual rating model; create_playlist + add_items_to_playlist for playlists.',
        role: 'adapter',
        code: `class JellyfinAdapter(MediaServerAdapter):
    def set_watched(
        self, user_id: str, item_id: str, viewed_at: float,
    ) -> WriteResult:
        """Mark item watched for user."""
        resp = self._session.post(
            f"/Users/{user_id}/PlayedItems/{item_id}",
            params={"DatePlayed": _to_iso(viewed_at)},
            headers=self._auth_header(),
        )
        return WriteResult(ok=resp.ok, code=resp.status_code)

    def set_rating(
        self, user_id: str, item_id: str, rating: float, like: bool,
    ) -> WriteResult:
        """D-RATE dual write: Likes flag + numeric Rating."""
        if like:
            self._session.post(
                f"/Users/{user_id}/FavoriteItems/{item_id}",
                headers=self._auth_header(),
            )
        return self._session.post(
            f"/Users/{user_id}/Items/{item_id}/UserData",
            json={"Rating": rating},
            headers=self._auth_header(),
        )`,
      },
    ],
    sharedInfra: ['state', 'dashboard', 'run_timer', 'logging_ops', 'adapters', 'user_resolution'],
    surfaces: ['Destination Jellyfin/Emby server (via REST writes)', 'server_data/run_timings.db'],
    notes: [
      'Inline user creation: server/routers/jobs.py::post_jobs_inline_create_user creates a missing destination user.',
      'Plex destinations refuse adapter.create_user (returns "not_supported").',
    ],
  },

  // ── Direct Transfer ──────────────────────────────────────────────────────
  {
    id: 'direct',
    title: 'Direct Transfer',
    oneliner:
      'Read from source and write to destination in-memory, without an intermediate .db on disk.',
    preamble: `Direct transfer pipes a per-library snapshot from source to destination without materialising a .db file. Per-library serial; intentional to limit dual-server load.`,
    steps: [
      {
        n: 1,
        label: 'Engine entry',
        cite: 'server/direct_transfer.py::run_direct_transfer',
        detail:
          'Per-library serial loop. For each library: gather in-memory from source, apply to destination, free the in-memory dict, move to next library.',
        role: 'engine',
        code: `def run_direct_transfer(
    src: ServerConnection,
    dest: ServerConnection,
    libraries: List[str],
    *,
    mode: str = "merge",
) -> DirectResult:
    """Source → destination, in-memory, per-library serial."""
    result = DirectResult()
    for lib_name in libraries:
        with time_operation(
            "direct_library", scope=SCOPE_LIBRARY, library=lib_name,
        ):
            payload = {"library": lib_name, "library_section_id": ...}
            # Source-side gather (in-memory)
            sec = src.plex.library.section(lib_name)
            payload["watch_events"] = snapshot_watch_history(src.plex, sec)
            payload["ratings"] = snapshot_ratings(src.plex, sec)
            payload["playlists"] = snapshot_playlists(src.plex, sec)
            payload["collections"] = snapshot_collections(src.plex, sec)

            # Destination-side apply (no disk hop)
            if dest.service_type == "plex":
                restore_export_file(
                    dest.plex, None,
                    mode=mode,
                    preloaded_data={"libraries": [payload]},
                )
            else:
                restore_payload_adapter(
                    dest, {"libraries": [payload]},
                    source_server_id=src.server_id,
                )

            del payload  # free for next library
            result.libraries_done.append(lib_name)
    return result`,
      },
    ],
    sharedInfra: ['state', 'dashboard', 'run_timer', 'logging_ops', 'restoration_log'],
    surfaces: ['Source server (read)', 'Destination server (write)', 'server_data/run_timings.db'],
    notes: [
      'Serial across libraries to avoid hammering both servers in parallel.',
      'Per-library resilience: chain to disk fallback if in-memory transfer fails.',
    ],
  },

  // ── Fan-out ──────────────────────────────────────────────────────────────
  {
    id: 'fan-out',
    title: 'Fan-out (multi-destination)',
    oneliner:
      'Drive one job (restore or direct) against multiple destinations in parallel.',
    preamble: `Fan-out is a coordinator, not a separate engine. It wraps run_restore or run_direct_transfer in a ThreadPoolExecutor across destinations. Per-destination DashboardState, log directory, and ContextVar copy.`,
    steps: [
      {
        n: 1,
        label: 'Spawn destination workers',
        cite: 'server/fan_out.py::run_fan_out_restore / run_fan_out_direct',
        detail:
          'ThreadPoolExecutor with one worker per destination (cap: fan_out_destination_workers tunable). Per-destination contextvars.copy_context() propagation.',
        role: 'dispatch',
        code: `def run_fan_out_restore(
    payload: dict,
    dests: List[ServerConnection],
    *,
    mode: str = "merge",
    library_workers: int = 4,
) -> FanOutResult:
    """Spawn one worker per destination; aggregate results."""
    result = FanOutResult(destinations=[
        FanOutDestResult(name=d.name) for d in dests
    ])
    _set_active_result(result)  # WS broadcaster reads this

    max_workers = min(len(dests), get_tunable("fan_out_destination_workers"))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        # NOTE: bare pool.submit here; submit_with_context fix pending
        # (Plan[FIX-ORDER] item 1). Each destination thread starts
        # in a fresh ContextVar copy via copy_context() inside the leg.
        futures = {
            pool.submit(_run_destination_leg, payload, d, mode, library_workers): d
            for d in dests
        }
        for fut in as_completed(futures):
            d = futures[fut]
            try:
                leg_result = fut.result()
                _slot_for(result, d.name).state = "completed"
                _slot_for(result, d.name).leg = leg_result
            except Exception as exc:
                _slot_for(result, d.name).state = "failed"
                _slot_for(result, d.name).error = str(exc)

    time.sleep(_FAN_OUT_GRACE_SECONDS)  # WS final-frame window
    clear_active_result()
    return result`,
      },
      {
        n: 2,
        label: 'Per-destination state',
        cite: 'services/state.py (per-thread ContextVars)',
        detail:
          'Each destination thread sees its own _plex_base_url, _dashboard. Writes to ContextVars inside the destination thread do not leak to siblings (PEP 567 isolation).',
        role: 'state',
        code: `def _run_destination_leg(
    payload: dict, dest: ServerConnection, mode: str, library_workers: int,
) -> RestoreResult:
    """Per-destination leg. Runs in its own ContextVar copy."""
    ctx = contextvars.copy_context()

    def _leg():
        state.reset_run_state()
        state._dashboard = DashboardState()
        state._plex_base_url = dest.base_url
        state._plex_token = dest.token

        if dest.service_type == "plex":
            return restore_export_file(
                dest.plex, None, mode=mode, preloaded_data=payload,
            )
        else:
            return restore_payload_adapter(dest, payload, ...)

    return ctx.run(_leg)`,
      },
    ],
    sharedInfra: ['state', 'dashboard', 'run_timer', 'logging_ops', 'ws'],
    surfaces: [
      'Multiple destination servers (parallel)',
      'server_data/run_timings.db (one row per destination)',
      'plex_logs/<slug>/destination_<n>/',
    ],
  },

  // ── Scheduled ────────────────────────────────────────────────────────────
  {
    id: 'schedule',
    title: 'Scheduled job',
    oneliner:
      'Cron-like trigger; on fire, builds a JobRequest and dispatches via the normal job queue.',
    preamble: `Scheduled jobs are a trigger source, not a separate engine. The Scheduler daemon polls every 30s and submits jobs through the same JobQueue manual submissions use.`,
    steps: [
      {
        n: 1,
        label: 'Tick: check schedules',
        cite: 'server/schedules.py::Scheduler._tick',
        detail:
          'Iterates schedules.json; for each row, compares next_run_at to time.time(). Skips entirely if a job is already running.',
        role: 'dispatch',
        code: `class Scheduler:
    def _tick(self) -> None:
        """One scheduler iteration."""
        if not self._queue.idle():
            return  # don't fire while a job runs
        now = time.time()
        schedules = persistence.load_schedules()
        for sched in schedules:
            if not sched.get("enabled", True):
                continue
            if sched["next_run_at"] > now:
                continue
            self._fire(sched)
            sched["next_run_at"] = _compute_next(sched, now)
            persistence.save_schedules(schedules)

    def _fire(self, sched: dict) -> None:
        """Submit the scheduled job via JobQueue."""
        params = {**sched["params"],
                  "_trigger": "scheduled",
                  "_schedule_name": sched["name"]}
        if sched["mode"] == "snapshot":
            self._queue.submit_snapshot(params)
        elif sched["mode"] == "restore":
            self._queue.submit_restore(params)`,
        input: `# schedules.json example:
[
    {
        "name": "Nightly Movies snapshot",
        "mode": "snapshot",
        "enabled": True,
        "frequency": "daily",
        "hour": 3, "minute": 0,
        "next_run_at": 1716440400.0,
        "params": {
            "source_server_name": "Jade.TV",
            "libraries": ["Movies"],
            "include_watch_history": True,
            ...
        }
    }
]`,
        output: `# Side effects (when fired):
#   1. Job submitted to queue (same path as manual UI submission)
#   2. next_run_at rolled forward to the next 03:00
#   3. schedules.json persisted with the new timestamp`,
      },
    ],
    sharedInfra: ['state', 'dashboard', 'run_timer', 'logging_ops'],
    surfaces: ['server_data/schedules.json', 'Whatever the underlying job mode touches'],
  },

  // ── Playlist Copy ─────────────────────────────────────────────────────────
  {
    id: 'playlist-copy',
    title: 'Playlist Copy (single)',
    oneliner: 'Copy one playlist from one server to another, with cross-server item matching.',
    preamble: `Playlist transfer is its own job mode and engine. The flow resolves the source playlist + items, matches each item to a destination row via GUID translation, and writes through the destination adapter.`,
    steps: [
      {
        n: 1,
        label: 'Resolve users on both servers',
        cite: 'services/playlist_user_auth.py::_find_user',
        detail:
          'Per-server user resolution. Handles owner (Plex Owner sentinel), home users (PIN/token), and managed users.',
        role: 'preflight',
        code: `def _find_user(
    server: ServerConnection, username: str,
) -> UserContext:
    """Find user + auth on a server. Returns (user, token_or_pin)."""
    if username == "Plex Owner":
        return UserContext(name=username, token=server.owner_token, ...)
    # Try home users (PIN-protected sometimes)
    home_users = server.adapter.list_home_users()
    match = next((u for u in home_users if u.name == username), None)
    if match:
        return UserContext(name=username, token=match.token, ...)
    # Try managed users (credential store)
    cred = media_db.managed_users.get_credential(
        server.server_id, username,
    )
    if cred:
        return UserContext(name=username, token=cred["token"], ...)
    raise UserNotFoundError(username)`,
      },
      {
        n: 2,
        label: 'Per-item GUID translation',
        cite: 'services/playlist_item_resolver.py::resolve_item_to_dest',
        detail:
          'For each item: looks up matching destination item via the GUID set (Imdb, Tmdb, Tvdb, MusicBrainz).',
        role: 'engine',
        code: `def resolve_item_to_dest(
    item: ItemRef,
    dest_server: ServerConnection,
    library_id: str,
) -> Optional[ItemRef]:
    """Resolve a source item to a destination row by upstream GUID."""
    # Check server_mirror cache first
    cached = server_mirror.find_by_guids(
        dest_server.server_id, item.guids,
    )
    if cached:
        return cached
    # Live GUID lookup on destination
    for guid in item.guids:
        scheme, value = guid.split("://", 1)
        results = dest_server.adapter.resolve_by_guids({scheme: value})
        if results:
            server_mirror.cache_resolution(
                dest_server.server_id, item, results[0],
            )
            return results[0]
    return None`,
        input: `item = ItemRef(
    title="Inception",
    guids=["imdb://tt1375666", "tmdb://27205"],
)
dest_server = <ServerConnection for "Plex+">
library_id = "5b1e3f..."`,
        output: `ItemRef(
    title="Inception (2010)",
    backend_item_id="328910",  # Jellyfin's internal ID
    guids=["imdb://tt1375666", "tmdb://27205"],
)
# OR None if no GUID match found anywhere on destination.`,
      },
    ],
    sharedInfra: ['state', 'dashboard', 'run_timer', 'adapters', 'playlist_cache_db'],
    surfaces: ['Source + destination servers', 'server_data/server_mirror.db (resolution cache)'],
  },

  // ── Playlist Batch ───────────────────────────────────────────────────────
  {
    id: 'playlist-batch',
    title: 'Playlist Copy (batch)',
    oneliner: 'Multiple playlist copies in one job with parallelism + per-item cancel.',
    preamble: `Same per-item engine as single-copy, wrapped in a ThreadPoolExecutor. Per-source semaphore limits concurrent source-side authentications.`,
    steps: [
      {
        n: 1,
        label: 'Engine entry',
        cite: 'services/playlist_copy.py::copy_playlist_batch',
        detail:
          'Per-source semaphore acquire (services/batch_runner.py::acquire_with_cancel). Then submits each copy to a per-batch ThreadPoolExecutor.',
        role: 'engine',
        code: `def copy_playlist_batch(
    specs: List[CopySpec],
    *,
    batch_workers: int = 4,
    cancel_events: Dict[int, threading.Event],
) -> BatchResult:
    """Parallel playlist copies; per-item cancellable."""
    results: List[CopyOutcome] = [None] * len(specs)
    with ThreadPoolExecutor(max_workers=batch_workers) as pool:
        futures = {}
        for idx, spec in enumerate(specs):
            cancel = cancel_events[idx]
            sem = batch_runner._per_source_semaphore_for(
                spec.source_server_id,
            )
            fut = pool.submit(
                _copy_one_with_cancel, spec, cancel, sem,
            )
            futures[fut] = idx
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                results[idx] = CopyOutcome.success(fut.result())
            except CancelledError:
                results[idx] = CopyOutcome.cancelled()
            except Exception as exc:
                results[idx] = CopyOutcome.failed(str(exc))
    return BatchResult(items=results)`,
      },
    ],
    sharedInfra: ['state', 'dashboard', 'run_timer', 'adapters', 'batch_runner'],
    surfaces: ['Source + destination servers', 'server_data/server_mirror.db'],
  },

  // ── Smart Playlist Migration ─────────────────────────────────────────────
  {
    id: 'smart-playlist',
    title: 'Smart Playlist Migration',
    oneliner:
      'Translate a smart playlist (server-specific filter) to a destination-shaped equivalent. Plex stays smart; Jellyfin/Emby materialise to static.',
    preamble: `Smart playlists use server-specific tag IDs. The migration decodes the source filter to a portable form (tag NAMES), then re-encodes for the destination — keeping it smart on Plex, materialising to a static list of items on Jellyfin/Emby.`,
    steps: [
      {
        n: 1,
        label: 'Decode to portable form',
        cite: 'services/smart_playlist.py::to_portable',
        detail:
          'Converts server-specific tag IDs to tag NAMES (via guid_translator + library-section lookups). Produces a PortableSmartFilter.',
        role: 'engine',
        code: `def to_portable(
    raw: RawSmartFilter, source: ServerConnection, library_id: str,
) -> PortableSmartFilter:
    """Decode server-specific tag IDs to portable tag NAMES."""
    portable_groups = []
    for group in raw.groups:
        portable_clauses = []
        for clause in group.clauses:
            if clause.field in TAG_FIELDS:
                # Resolve numeric tag id → tag name
                tag_name = source.adapter.resolve_tag_id(
                    library_id, clause.field, clause.value,
                )
                portable_clauses.append(SmartClause(
                    field=clause.field,
                    op=clause.op,
                    value=tag_name,  # NAME, not id
                ))
            else:
                portable_clauses.append(clause)  # numeric / string field
        portable_groups.append(SmartGroup(
            op=group.op, clauses=portable_clauses,
        ))
    return PortableSmartFilter(groups=portable_groups)`,
        input: `raw = RawSmartFilter(groups=[SmartGroup(
    op="AND", clauses=[
        SmartClause(field="genre", op="=", value=47),   # Plex tag id
        SmartClause(field="year", op=">=", value=2010),
    ],
)])
source = <ServerConnection for Plex>
library_id = "1"  # Movies`,
        output: `PortableSmartFilter(groups=[SmartGroup(
    op="AND", clauses=[
        SmartClause(field="genre", op="=", value="Sci-Fi"),  # NAME
        SmartClause(field="year", op=">=", value=2010),
    ],
)])`,
      },
      {
        n: 2,
        label: 'Destination branch',
        cite: 'services/smart_playlist.py::materialize_filter OR re-encode for Plex',
        detail:
          'Plex destination: re-encodes via plexapi LibrarySection.create(smart=True, filters=...). Jellyfin/Emby destination: materialize_filter resolves to a static item list.',
        role: 'engine',
        code: `def migrate_smart_playlist(
    raw_filter: RawSmartFilter,
    source: ServerConnection,
    dest: ServerConnection,
    library_id: str,
    title: str,
) -> MigrateOutcome:
    """Top-level smart-playlist migration."""
    portable = to_portable(raw_filter, source, library_id)

    if dest.service_type == "plex":
        # Re-encode via plexapi; resolves names → destination tag ids.
        section = dest.plex.library.section(library_name)
        return section.create(
            title=title, smart=True, filters=_to_plexapi(portable),
        )

    # Jellyfin/Emby: no smart-playlist concept. Materialize the filter
    # to a static item list, then create a regular playlist.
    items = materialize_filter(portable, dest, library_id)
    return dest.adapter.create_playlist(
        title=title, item_ids=[i.backend_item_id for i in items],
        smart=False,
    )`,
      },
    ],
    sharedInfra: ['state', 'dashboard', 'adapters', 'guid_translator', 'smart_playlist'],
    surfaces: ['Source + destination servers'],
    notes: [
      'Jellyfin/Emby have no smart-playlist concept; the engine creates a STATIC playlist there.',
      'Re-encoding for Plex uses plexapi\'s smart-playlist construction.',
    ],
  },

  // ── Preflight ────────────────────────────────────────────────────────────
  {
    id: 'preflight',
    title: 'Cross-Platform Preflight',
    oneliner:
      'Dry-run user resolution + library-type checks + tombstone exclusion BEFORE a cross-backend restore writes.',
    preamble: `Preflight is a separate REST surface called from the UI before submit. The same dry_run_resolve_users function runs again inside the adapter restore engine (enforce_preflight=True) so a bypassed UI check still gets caught.`,
    steps: [
      {
        n: 1,
        label: 'REST entry',
        cite: 'server/routers/jobs.py::post_jobs_cross_platform_preflight',
        detail:
          'Receives the same payload as the job submit. Returns a PreflightResponse with per-destination CrossPlatformPreflightReport.',
        role: 'dispatch',
        code: `@router.post("/jobs/cross-platform-preflight")
def post_jobs_cross_platform_preflight(
    body: RestoreJobIn,
    user: AuthUser = Depends(require_role("operator")),
) -> PreflightResponse:
    """Dry-run preflight before a cross-backend restore."""
    payload = _load_preflight_payload(body)
    reports = {}
    aggregate = "ok"
    for dest_name in body.dest_server_names:
        dest = _resolve_dest_connection({"dest_server_names": [dest_name]})
        report = _run_preflight_for_dest(payload, dest, body.source_server_name)
        reports[dest.server_id] = report
        aggregate = _worse_of(aggregate, report.overall_verdict)
    return PreflightResponse(reports=reports, aggregate_verdict=aggregate)`,
        output: `{
    "reports": {
        "jf-srv-1": {
            "resolutions": [
                {"source_user": "alice",
                 "proposed_dest_user_id": "u-7f3a",
                 "proposed_resolution": "map_to_existing",
                 "blocks_submit": False},
                {"source_user": "bob",
                 "proposed_dest_user_id": None,
                 "proposed_resolution": "create",
                 "blocks_submit": False},
            ],
            "smart_playlists_skipped": [],
            "library_type_notes": [],
            "tombstoned_users_excluded": [],
            "overall_verdict": "ack_required",
        }
    },
    "aggregate_verdict": "ack_required"
}`,
      },
    ],
    sharedInfra: ['adapters', 'user_resolution', 'snapshot_validator', 'backend_translation', 'smart_playlist'],
    surfaces: ['Destination server (read-only adapter calls)'],
    notes: [
      'Inline user creation: POST /api/jobs/inline-create-user creates a missing destination user from within the preflight modal.',
      'Preflight runs again inside the engine (enforce_preflight=True). Blocked verdict raises ValueError before any write.',
    ],
  },
];

// ── Shared infrastructure data ───────────────────────────────────────────────

const SHARED_INFRA: SharedInfraEntry[] = [
  {
    id: 'state',
    title: 'ContextVar-backed engine state',
    file: 'services/state.py',
    oneliner: 'Per-run ContextVars for source/destination identity, dashboard, restoration log.',
    detail:
      '_dashboard_var, _plex_base_url_var, _plex_token_var, _plex_owner_name_var, _restoration_log, _restoration_log_affected_users, _snapshot_server_id, _run_trigger, _run_schedule_name. Fan-out destinations get isolated copies via contextvars.copy_context().',
    consumers: ['all jobs'],
    snippets: [
      {
        label: 'ContextVar declarations',
        code: `# services/state.py — module-level ContextVar declarations.
# Every per-run value lives in a ContextVar so worker threads spawned
# via submit_with_context inherit them, and fan-out destinations get
# isolated copies (PEP 567).

from contextvars import ContextVar
from typing import Optional

_dashboard_var: ContextVar[Optional["DashboardState"]] = ContextVar(
    "_dashboard", default=None,
)
_plex_base_url_var: ContextVar[Optional[str]] = ContextVar(
    "_plex_base_url", default=None,
)
_plex_token_var: ContextVar[Optional[str]] = ContextVar(
    "_plex_token", default=None,
)
_plex_owner_name_var: ContextVar[Optional[str]] = ContextVar(
    "_plex_owner_name", default=None,
)
_http_lib_var: ContextVar[Optional[str]] = ContextVar(
    "_http_lib", default=None,
)
_http_job_id_var: ContextVar[Optional[str]] = ContextVar(
    "_http_job_id", default=None,
)`,
      },
      {
        label: 'reset_run_state — null per-run state between jobs',
        cite: 'services/state.py::reset_run_state',
        code: `def reset_run_state() -> None:
    """Null per-run state so the next job starts clean.
    Called by JobQueue at job end."""
    _dashboard_var.set(None)
    _http_lib_var.set(None)
    _http_job_id_var.set(None)
    # module-level globals (not ContextVars) reset too
    global _restoration_log, _restoration_log_affected_users
    global _snapshot_server_id, _run_trigger, _run_schedule_name
    _restoration_log = None
    _restoration_log_affected_users = []
    _snapshot_server_id = None
    _run_trigger = None
    _run_schedule_name = None`,
      },
      {
        label: 'get_dashboard — engine-facing accessor',
        cite: 'services/state.py::get_dashboard',
        code: `def get_dashboard() -> "DashboardState":
    """Engine-facing accessor. Returns a no-op shim when no dashboard
    is in context (CLI / test mode), so engine code can call
    state.get_dashboard().set_library_phase(...) unconditionally."""
    dash = _dashboard_var.get()
    if dash is None:
        return _NullDashboard()
    return dash`,
        input: `# Anywhere inside an engine call chain:
state.get_dashboard().push_activity("scrobbled", "Movies", "Inception")`,
        output: `# When called inside a job context: real DashboardState updated;
# WS broadcaster picks up the new activity entry on its next tick.
# When called outside (tests / CLI): _NullDashboard absorbs the call.`,
      },
    ],
  },

  {
    id: 'dashboard',
    title: 'DashboardState + submit_with_context',
    file: 'services/dashboard.py',
    oneliner:
      'Per-run state object holding tier_counts, activity_feed, libraries, current_items. submit_with_context propagates ContextVars into worker threads.',
    detail:
      'DashboardState is broadcast via WS every ~250ms. submit_with_context() wraps ThreadPoolExecutor.submit() so worker threads inherit the parent\'s ContextVar values (critical for per-library HTTP attribution).',
    consumers: ['all jobs'],
    snippets: [
      {
        label: 'DashboardState — per-run progress + activity feed',
        cite: 'services/dashboard.py::DashboardState',
        code: `class DashboardState:
    """Per-run state object. Lock-protected for multi-thread reads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.tier_counts: Dict[str, int] = defaultdict(int)
        self.activity_feed: Deque[ActivityEntry] = deque(maxlen=100)
        self.libraries: Dict[str, LibraryProgress] = {}
        self.current_items: Dict[str, ItemContext] = {}

    def push_activity(
        self, action: str, library: str, title: str,
    ) -> None:
        """Append to the activity feed; broadcast on next WS tick."""
        with self._lock:
            self.activity_feed.append(ActivityEntry(
                action=action,
                library=library,
                title=title,
                at=time.time(),
            ))

    def set_library_phase(self, library: str, phase: str) -> None:
        with self._lock:
            self.libraries.setdefault(library, LibraryProgress()).phase = phase

    def add_batch_total(self, metric: str, count: int) -> None:
        with self._lock:
            self.tier_counts[metric] += count

    def snapshot(self) -> dict:
        """Defensive copy under lock for WS broadcast."""
        with self._lock:
            return {
                "tier_counts": dict(self.tier_counts),
                "activity_feed": list(self.activity_feed),
                "libraries": {k: v.copy() for k, v in self.libraries.items()},
                "current_items": dict(self.current_items),
            }`,
      },
      {
        label: 'submit_with_context — ContextVar propagation across threads',
        cite: 'services/dashboard.py::submit_with_context',
        code: `def submit_with_context(
    executor: ThreadPoolExecutor,
    fn: Callable,
    *args,
    **kwargs,
) -> Future:
    """Wrap executor.submit so the worker thread inherits the calling
    thread's ContextVar values. Critical for per-library HTTP
    attribution and per-destination state isolation in fan-out."""
    ctx = contextvars.copy_context()
    return executor.submit(ctx.run, fn, *args, **kwargs)`,
        input: `with ThreadPoolExecutor(max_workers=4) as pool:
    futures = [
        submit_with_context(pool, snapshot_library, server, sec)
        for sec in selected_libraries
    ]`,
        output: `# Each worker thread sees the same _plex_base_url, _dashboard,
# _http_lib_var as the parent — without leaking writes back.
# Without submit_with_context: workers start in a fresh, empty
# ContextVar context and HTTP attribution breaks silently.`,
      },
    ],
  },

  {
    id: 'run_timer',
    title: 'Per-operation timing recorder',
    file: 'services/run_timer.py',
    oneliner: 'time_operation context manager + per-run RunTimingBuffer.',
    detail:
      'time_operation(label, scope=SCOPE_*) wraps any block. On exit, appends a TimingEntry to the current run\'s in-memory buffer. start_run / end_run bracket the whole job.',
    consumers: ['all jobs'],
    snippets: [
      {
        label: 'TimingEntry — one row per timed operation',
        cite: 'services/run_timer.py::TimingEntry',
        code: `@dataclass
class TimingEntry:
    """One row per timed operation. Persisted to run_timings.db.
    eta_training also folds these into per-bucket EMA + variance."""
    run_id: str
    scope: str          # SCOPE_RUN | SCOPE_LIBRARY | SCOPE_USER | SCOPE_BATCH | SCOPE_OPERATION
    label: str          # e.g. "snapshot_library", "restore_watch_history"
    started_at: float
    ended_at: float
    duration_seconds: float
    library: Optional[str] = None
    user_handle: Optional[str] = None
    items_processed: Optional[int] = None
    extra: Dict[str, Any] = field(default_factory=dict)`,
      },
      {
        label: 'time_operation — context-manager wrapper',
        cite: 'services/run_timer.py::time_operation',
        code: `@contextmanager
def time_operation(
    label: str,
    scope: str = SCOPE_OPERATION,
    *,
    library: Optional[str] = None,
    user_handle: Optional[str] = None,
    items_processed: Optional[int] = None,
    **extra,
) -> Iterator[dict]:
    """Context manager that records duration_seconds + metadata.
    Microseconds overhead; safe to wrap per-item loops if needed."""
    started = time.time()
    payload: Dict[str, Any] = {"extra": dict(extra)}
    try:
        yield payload
    finally:
        ended = time.time()
        buf = _current_run_buffer
        if buf is not None:
            buf.append(TimingEntry(
                run_id=buf.run_id,
                scope=scope,
                label=label,
                started_at=started,
                ended_at=ended,
                duration_seconds=ended - started,
                library=library,
                user_handle=user_handle,
                items_processed=payload.get("items_processed", items_processed),
                extra=payload["extra"],
            ))`,
        input: `with time_operation(
    "snapshot_watch_history",
    scope=SCOPE_USER,
    library="Movies",
    user_handle="alice",
) as t:
    events = scrape_watch_history(plex, section, user="alice")
    t["items_processed"] = len(events)`,
        output: `# Side effect: one TimingEntry appended to _current_run_buffer.
# At end_run(): the buffer flushes to run_timings.db + eta_training.`,
      },
    ],
  },

  {
    id: 'eta_training',
    title: 'Adaptive ETA training engine',
    file: 'services/eta_training.py',
    oneliner: 'Per-bucket EMA + variance estimator. Reads run_timings.db at boot; writes touched buckets at job end.',
    detail:
      'BucketKey is 5-dim: server_id, label, library_type, bulk_strategy, size_bucket. Each new TimingEntry updates the matching bucket. Cold-start fallback chain widens by dropping dimensions when the exact bucket has < N samples.',
    consumers: ['snapshot (Plex + adapter)', 'restore (Plex + adapter)', 'direct'],
    snippets: [
      {
        label: 'BucketKey — five-dim identity for a learning bucket',
        cite: 'services/eta_training.py::BucketKey',
        code: `class BucketKey(NamedTuple):
    """Five-dim key for an ETA bucket. Two operations with the same
    label but different sizes train independent estimators so the
    'ratings on a 50k-item library' bucket and the 'ratings on a 300-
    item library' bucket don't pollute each other."""
    server_id: str
    label: str               # e.g. "snapshot_ratings", "restore_watch_history"
    library_type: str        # "movie" | "show" | "music" | ""
    bulk_strategy: str       # "smart" | "naive" | ""
    size_bucket: str         # "S" | "M" | "L" | "XL"`,
      },
      {
        label: 'AdaptiveETA.update — online EMA + variance',
        cite: 'services/eta_training.py::AdaptiveETA.update',
        code: `def update(self, observed: float) -> None:
    """Online update on one observation. Verbatim Perplexity math:
    variance uses the PRIOR ema (before the mean shifts) — otherwise
    spread is under-counted."""
    alpha = self.alpha
    if self.samples == 0:
        # Seed: no prior; just set the mean to the observation.
        self.ema = observed
        self.variance = 0.0
    else:
        delta = observed - self.ema
        self.variance = (1 - alpha) * (self.variance + alpha * delta * delta)
        self.ema = alpha * observed + (1 - alpha) * self.ema
    self.samples += 1
    self.last_observed_at = time.time()`,
        input: `bucket = AdaptiveETA(alpha=0.2)
bucket.update(12.4)   # first observation seeds the EMA
bucket.update(11.8)
bucket.update(14.1)`,
        output: `# bucket.ema     ~= 12.6  (weighted average; recent samples weigh more)
# bucket.variance ~= 0.5  (spread, in seconds-squared)
# bucket.samples = 3`,
      },
      {
        label: 'predict_for_job — pre-run ETA with fallback chain',
        cite: 'services/eta_training.py::predict_for_job',
        code: `def predict_for_job(
    self, server_id: str, jobs: List[JobUnit],
) -> ETAPrediction:
    """Sum per-unit predictions for a planned job. Each unit looks
    up its bucket via the four-tier fallback chain (exact -> drop
    size -> drop library_type -> drop strategy)."""
    total_ema = 0.0
    total_variance = 0.0
    min_samples = math.inf
    for unit in jobs:
        bucket = self._lookup_with_fallback(server_id, unit)
        if bucket is None or bucket.samples < self.MIN_SAMPLES_TO_REPORT:
            continue
        total_ema += bucket.ema
        total_variance += bucket.variance
        min_samples = min(min_samples, bucket.samples)
    std = math.sqrt(total_variance)
    z = self.eta_confidence_z
    return ETAPrediction(
        mean_seconds=total_ema,
        low_seconds=max(0, total_ema - z * std),
        high_seconds=total_ema + z * std,
        confidence=("low" if min_samples < 10 else "medium" if min_samples < 30 else "high"),
    )`,
      },
    ],
  },

  {
    id: 'restoration_log',
    title: 'Per-action restore log',
    file: 'services/restoration_log.py',
    oneliner: 'Emits one line per (item, user, metric) outcome during restore.',
    detail:
      'RestorationLogWriter, opened at restore start, closed with summary block at end. Statuses: RESTORED / NOOP / SKIPPED / FAILED. Summary block totals per status, per metric, per library, per user, wall-clock.',
    consumers: ['restore (Plex)', 'direct'],
    snippets: [
      {
        label: 'RestorationLogWriter — thread-safe line emitter',
        cite: 'services/restoration_log.py::RestorationLogWriter',
        code: `class RestorationLogWriter:
    """One-line-per-outcome writer for restoration.log. Thread-safe;
    closes with a summary block listing totals per status / metric /
    library / user / wall-clock."""

    STATUS_RESTORED = "RESTORED"
    STATUS_NOOP = "NOOP"
    STATUS_SKIPPED = "SKIPPED"
    STATUS_FAILED = "FAILED"

    def __init__(self, fp, started_at: float) -> None:
        self._fp = fp
        self.started_at = started_at
        self._lock = threading.Lock()
        self._closed = False
        # Counters for the summary block.
        self.per_status: Counter = Counter()
        self.per_metric: Counter = Counter()
        self.per_library: Counter = Counter()
        self.per_user: Counter = Counter()

    def _emit(
        self, status: str, library: str, user: str, metric: str,
        item: str, reason: Optional[str] = None,
    ) -> None:
        """Format and write one line; bump counters."""
        ts = datetime.utcnow().isoformat(timespec="seconds")
        title_clean = (item or "").replace("\\n", " ")[:200]
        reason_part = f" reason='{reason}'" if reason else ""
        line = (
            f"{ts} {status:<8} {metric:<14} "
            f"library='{library}' user='{user}' "
            f"item='{title_clean}'{reason_part}\\n"
        )
        with self._lock:
            if self._closed:
                return
            self._fp.write(line)
            self.per_status[status] += 1
            self.per_metric[(metric, status)] += 1
            self.per_library[(library, status)] += 1
            self.per_user[(user, status)] += 1

    def restored(self, library, user, metric, item):
        self._emit(self.STATUS_RESTORED, library, user, metric, item)

    def noop(self, library, user, metric, item):
        self._emit(self.STATUS_NOOP, library, user, metric, item)

    def skipped(self, library, user, metric, item, reason):
        self._emit(self.STATUS_SKIPPED, library, user, metric, item, reason)

    def failed(self, library, user, metric, item, reason):
        self._emit(self.STATUS_FAILED, library, user, metric, item, reason)`,
        input: `writer.restored(
    library="Movies", user="alice",
    metric="watch_history", item="Inception",
)`,
        output: `# Written to restoration.log:
# 2026-05-22T17:42:01 RESTORED watch_history library='Movies' user='alice' item='Inception'`,
      },
    ],
  },

  {
    id: 'logging_ops',
    title: 'Per-run logger setup',
    file: 'services/logging_ops.py',
    oneliner: 'Opens runtime.log, troubleshoot.log, unresolved.log with the token scrubber installed on every handler.',
    detail:
      'setup_logging(log_dir, verbose) returns (logger, run_log_dir). Installs TokenScrubFilter as a filter on every handler so X-Plex-Token is redacted at the logging framework level.',
    consumers: ['all jobs'],
    snippets: [
      {
        label: 'setup_logging — per-run logger + scrubber install',
        cite: 'services/logging_ops.py::setup_logging',
        code: `def setup_logging(
    log_dir: Path,
    verbose: bool = False,
) -> Tuple[logging.Logger, Path]:
    """Open per-run log files; install the token scrubber on every
    handler so X-Plex-Token is never written to disk in plaintext."""
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("plexmigrate")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    runtime_h = RotatingFileHandler(
        log_dir / "runtime.log",
        maxBytes=20 * 1024 * 1024, backupCount=3,
    )
    runtime_h.setFormatter(_fmt())
    runtime_h.addFilter(TokenScrubFilter())  # scrubber installed

    troubleshoot_h = RotatingFileHandler(
        log_dir / "troubleshoot.log",
        maxBytes=10 * 1024 * 1024, backupCount=3,
    )
    troubleshoot_h.setLevel(logging.WARNING)
    troubleshoot_h.setFormatter(_fmt())
    troubleshoot_h.addFilter(TokenScrubFilter())

    logger.addHandler(runtime_h)
    logger.addHandler(troubleshoot_h)
    return logger, log_dir`,
        output: `(<Logger plexmigrate (DEBUG)>, Path('/var/log/plexmigrate/<rundir>/'))
# Side effects:
#   - runtime.log opened (20MB rolling, 3 backups)
#   - troubleshoot.log opened (10MB rolling, WARNING+)
#   - TokenScrubFilter attached to both`,
      },
    ],
  },

  {
    id: 'log_scrubber',
    title: 'Token / credential scrubber',
    file: 'server/log_scrubber.py',
    oneliner: 'Regex-based redaction of credential patterns from log output.',
    detail:
      'Matches X-Plex-Token=..., Authorization: Bearer ..., token=..., password=..., Fernet ciphertext blobs. Installed as a logging.Filter; runs on every log record before any handler sees it.',
    consumers: ['all jobs'],
    snippets: [
      {
        label: 'TokenScrubFilter — logging.Filter that mutates messages',
        cite: 'server/log_scrubber.py::TokenScrubFilter',
        code: `# Patterns are checked in order; first match wins per line. The
# replacement keeps surrounding context intact so log lines stay
# readable while the secret itself becomes <redacted>.
_PATTERNS: List[Tuple[Pattern, str]] = [
    (re.compile(r"X-Plex-Token=[A-Za-z0-9_-]+"), "X-Plex-Token=<redacted>"),
    (re.compile(r"X-Emby-Token: [A-Za-z0-9_-]+"), "X-Emby-Token: <redacted>"),
    (re.compile(r"Authorization: Bearer [A-Za-z0-9._-]+"),
        "Authorization: Bearer <redacted>"),
    (re.compile(r'"token"\\s*:\\s*"[^"]+"'), '"token": "<redacted>"'),
    (re.compile(r"password=[^\\s&]+"), "password=<redacted>"),
    # Fernet ciphertext (base64url 100+ chars, ends with = padding)
    (re.compile(r"gAAAAA[A-Za-z0-9_-]{100,}={0,2}"), "<redacted-fernet>"),
]


class TokenScrubFilter(logging.Filter):
    """Mutates LogRecord.msg + .args in place. Runs before any
    formatter, before any handler sees the record. Cheap; runs on
    every record but only when the regex matches."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
            for pattern, replacement in _PATTERNS:
                text = pattern.sub(replacement, text)
            record.msg = text
            record.args = None  # already substituted
        except Exception:
            pass  # never let logging scrubbing kill a real log line
        return True`,
        input: `logger.info(
    "GET %s?X-Plex-Token=secret-deadbeef",
    "http://plex/library/sections/1/all",
)`,
        output: `# Written to runtime.log:
# 2026-05-22 17:42:01 INFO  GET http://plex/library/sections/1/all?X-Plex-Token=<redacted>`,
      },
    ],
  },

  {
    id: 'run_settings_log',
    title: 'Per-run settings dump',
    file: 'services/run_settings_log.py',
    oneliner: 'Writes run-settings.log: human-readable Markdown of every active setting at job start.',
    detail:
      'Sections: At a Glance / Per-library Metrics / Tunables / Persistent Settings / Job Params. Secrets redacted. Aimed at troubleshooting.',
    consumers: ['all jobs'],
    snippets: [
      {
        label: 'dump_run_settings — entry point',
        cite: 'services/run_settings_log.py::dump_run_settings',
        code: `def dump_run_settings(
    job_type: str,
    run_log_dir: Path,
    params: dict,
    logger: logging.Logger,
) -> Optional[Path]:
    """Best-effort: write run-settings.log; failure never blocks the
    job. Sections include At a glance, Per-library metrics, Tunables
    (with Overridden subsection), Persistent Settings, Job Params."""
    try:
        sections = [
            _section_at_a_glance(job_type, params),
            _section_per_library_metrics(params.get("library_metrics", {})),
            _section_tunables(tunables.all_values(), tunables.defaults()),
            _section_persistent_settings(persistence.load_settings()),
            _section_job_params(job_type, params),
        ]
        md = "\\n\\n---\\n\\n".join(sections)
        md = _redact_secrets(md)  # plex_token, password, fernet_key, ...
        out = run_log_dir / "run-settings.log"
        out.write_text(md, encoding="utf-8")
        logger.info("wrote %s", out)
        return out
    except Exception:
        logger.exception("run-settings dump failed; continuing")
        return None`,
        output: `# Wrote /var/log/.../run-settings.log (Markdown). Body looks like:
#
#   # Run Settings - <rundir>
#   Captured: 2026-05-22T17:42:01Z
#
#   ## At a glance
#       Job        : snapshot
#       Source     : Jade.TV
#       Libraries  : Movies, TV
#       Workers    : 8
#
#   ## Tunables (47 known, 3 overridden)
#       ### Overridden
#           ...
#       ### All tunables
#           | Key | Value | Default | Override? |
#
#   ## Job Params
#       ...`,
      },
    ],
  },

  {
    id: 'adapters',
    title: 'MediaServerAdapter ABC + backend implementations',
    file: 'services/adapters/',
    oneliner: 'Backend-agnostic interface. PlexAdapter, JellyfinAdapter, EmbyAdapter.',
    detail:
      'The ABC was derived from real call sites (not clean-sheet design). Each adapter implements ping, server_identity, list_libraries, list_users, iter_items, set_watched, set_resume_position, set_rating, create_playlist, add_items_to_playlist, create_user (J/E only).',
    consumers: ['all snapshots', 'all restores', 'all playlist jobs', 'preflight'],
    snippets: [
      {
        label: 'MediaServerAdapter — the ABC',
        cite: 'services/adapters/__init__.py::MediaServerAdapter',
        code: `class MediaServerAdapter(ABC):
    """Backend-agnostic interface. PlexAdapter, JellyfinAdapter,
    EmbyAdapter implement this. The ABC was derived from real engine
    call sites: anything the engine doesn't call today isn't on the ABC."""

    # Discovery
    @abstractmethod
    def ping(self) -> bool: ...
    @abstractmethod
    def server_identity(self) -> ServerIdentity: ...
    @abstractmethod
    def list_libraries(self) -> List[LibraryRef]: ...
    @abstractmethod
    def list_users(self) -> List[UserSpec]: ...

    # Read
    @abstractmethod
    def iter_items(
        self, library_id: str, *, page_size: int = 200,
    ) -> Iterable[ItemSnapshot]: ...
    @abstractmethod
    def resolve_by_guids(
        self, guids: Dict[str, str],
    ) -> List[ItemRef]: ...

    # Write
    @abstractmethod
    def set_watched(
        self, user_id: str, item_id: str, viewed_at: float,
    ) -> WriteResult: ...
    @abstractmethod
    def set_rating(
        self, user_id: str, item_id: str, rating: float, like: bool,
    ) -> WriteResult: ...
    @abstractmethod
    def create_playlist(
        self, user_id: str, title: str, item_ids: List[str],
    ) -> WriteResult: ...
    @abstractmethod
    def add_items_to_playlist(
        self, user_id: str, playlist_id: str, item_ids: List[str],
    ) -> WriteResult: ...

    # User management (J/E only; Plex returns NotSupportedError)
    @abstractmethod
    def create_user(
        self, username: str, password: str, policy: UserPolicy,
    ) -> UserSpec: ...`,
      },
      {
        label: 'EmbyAdapter — Authorization header (the J/E divergence)',
        cite: 'services/adapters/emby.py::EmbyAdapter._auth_header',
        code: `def _auth_header(self) -> dict:
    """Emby's structured auth header. The only material divergence
    from Jellyfin: scheme name is 'Emby' instead of 'MediaBrowser'.
    The shared _HttpMediaAdapter mixin parameterises this so the
    rest of the request-construction code is shared between J/E."""
    return {
        "Authorization": (
            f'Emby UserId="{self._user_id}", '
            f'Token="{self._token}", '
            f'Client="PlexBackUp", '
            f'DeviceId="{self._device_id}", '
            f'Version="{self._version}"'
        ),
    }`,
        output: `{
    "Authorization":
        'Emby UserId="7f3a...", Token="<emby-token>", '
        'Client="PlexBackUp", DeviceId="pm-host-1", Version="0.13.1"',
}
# Note: Emby also accepts X-Emby-Token as a legacy alternative;
# the structured form is the documented preferred path.`,
      },
    ],
  },

  {
    id: 'user_resolution',
    title: 'Cross-server user resolution',
    file: 'services/user_resolution.py + services/restorer_adapter.py::_resolve_destination_user',
    oneliner: 'Tier 0 identity_map → Tier 1 exact name match → Tier 2 single-admin fallback.',
    detail:
      'Tier 0: media.db user_identity_map (authoritative; written by inline-create-user + auto-derived during runs). Tier 1: case-insensitive username string match. Tier 2: when destination has exactly one admin user, the owner-role source user falls back to it.',
    consumers: ['restore (adapter)', 'direct (cross-backend)', 'preflight', 'playlist copy'],
    snippets: [
      {
        label: '_resolve_destination_user — Tier 0 → 1 → 2 chain',
        cite: 'services/restorer_adapter.py::_resolve_destination_user',
        code: `def _resolve_destination_user(
    src_user: str,
    src_role: str,        # "admin" | "owner" | "managed" | ...
    adapter: MediaServerAdapter,
    dest_server_id: str,
    source_server_id: str,
) -> Optional[UserSpec]:
    """Tier 0 → Tier 1 → Tier 2 chain. Returns None (SKIPPED) when
    no tier matches; caller writes a SKIPPED line with the reason."""

    # Tier 0: media.db user_identity_map (authoritative)
    mapped = media_db.user_identity_map.get(
        source_server_id, src_user, dest_server_id,
    )
    if mapped is not None:
        user = adapter.get_user_by_id(mapped["dest_user_id"])
        if user is not None:
            return user
        # Mapped to a deleted user. Fall through to tier 1.

    # Tier 1: case-insensitive name match
    dest_users = adapter.list_users()
    for u in dest_users:
        if u.name.casefold() == src_user.casefold():
            # Auto-write the identity map so next run hits Tier 0.
            media_db.user_identity_map.upsert(
                source_server_id, src_user, dest_server_id, u.id,
                source="auto:name_match",
            )
            return u

    # Tier 2: single-admin destination fallback (owner-role source only)
    if src_role in ("owner", "admin"):
        admins = [u for u in dest_users if u.is_admin]
        if len(admins) == 1:
            return admins[0]

    # No match. Caller treats this as a SKIPPED outcome.
    return None`,
        input: `src_user = "alice"
src_role = "admin"
adapter = <JellyfinAdapter for "Plex+">
dest_server_id = "jf-srv-1"
source_server_id = "plex-srv-1"`,
        output: `# Tier 0 hit (existing identity_map entry):
UserSpec(id="u-7f3a", name="Alice", is_admin=False, ...)

# Tier 1 hit (fresh name match; identity_map auto-populated):
UserSpec(id="u-1234", name="alice", is_admin=False, ...)
# Side effect: user_identity_map upserted for next-run Tier 0 hit.

# Tier 2 hit (owner falls back to single dest admin):
UserSpec(id="u-admin", name="root", is_admin=True, ...)

# No tier matched → None (caller records SKIPPED).`,
      },
    ],
  },

  {
    id: 'guid_translator',
    title: 'Cross-server GUID translation',
    file: 'services/guid_translator.py',
    oneliner: 'Matches items across servers by upstream metadata IDs (Imdb, Tmdb, Tvdb, MusicBrainz).',
    detail:
      'Resolves a source item\'s GUID set against a destination via the items table. Falls back to fuzzy title match when GUIDs miss.',
    consumers: ['restore', 'direct', 'playlist copy', 'smart playlist'],
    snippets: [
      {
        label: 'resolve_item — GUID set + fuzzy fallback',
        cite: 'services/guid_translator.py::resolve_item',
        code: `def resolve_item(
    server: ServerLike,
    section: LibrarySection,
    guids: List[str],
    *,
    fallback_title: Optional[str] = None,
    fallback_year: Optional[int] = None,
) -> Optional[ItemRef]:
    """Resolve a source item to a destination row.
    Order: (1) media.db cache lookup -> (2) live GUID query -> (3)
    fuzzy title+year match (only when fallback_title supplied)."""

    # (1) Cache: media.db items table indexed by upstream GUID.
    for guid in guids:
        cached = media_db.items.find_by_guid(guid, section.server_id)
        if cached:
            return cached

    # (2) Live GUID lookup on destination.
    for guid in guids:
        scheme, value = guid.split("://", 1)
        hits = server.adapter.resolve_by_guids({scheme: value})
        if hits:
            media_db.items.upsert(hits[0])  # populate cache
            return hits[0]

    # (3) Fuzzy fallback: only when caller opts in. Imperfect.
    if fallback_title:
        for candidate in section.search(title=fallback_title):
            if fallback_year and candidate.year != fallback_year:
                continue
            if _titles_match(candidate.title, fallback_title):
                return candidate

    return None`,
        input: `guids = ["imdb://tt1375666", "tmdb://27205"]
fallback_title = "Inception"
fallback_year = 2010`,
        output: `# Tier 1 hit (cached): ItemRef(server_id=..., backend_item_id="328910", ...)
# Tier 2 hit (live): same as above + cached for next run.
# Tier 3 hit (fuzzy): same ItemRef but no GUIDs (less reliable).
# No match: None (caller logs FAILED with reason "resolver-miss").`,
      },
    ],
  },

  {
    id: 'batch_runner',
    title: 'Per-source semaphore for batch jobs',
    file: 'services/batch_runner.py',
    oneliner: 'Limits concurrent source-side authentications for playlist batch jobs.',
    detail:
      'acquire_with_cancel(source_server_id) acquires a per-source semaphore (size tunable). Prevents the playlist-batch from saturating one source server\'s auth endpoint.',
    consumers: ['playlist copy (batch)'],
    snippets: [
      {
        label: 'acquire_with_cancel — semaphore + cancel-event wait',
        cite: 'services/batch_runner.py::acquire_with_cancel',
        code: `_per_source_semaphores: Dict[str, threading.Semaphore] = {}
_per_source_lock = threading.Lock()


def _per_source_semaphore_for(source_server_id: str) -> threading.Semaphore:
    """Lazy-initialised per-source semaphore sized by tunable."""
    with _per_source_lock:
        sem = _per_source_semaphores.get(source_server_id)
        if sem is None:
            limit = tunables.playlist_mgmt_batch_per_source_workers()
            sem = threading.Semaphore(limit)
            _per_source_semaphores[source_server_id] = sem
        return sem


@contextmanager
def acquire_with_cancel(
    sem: threading.Semaphore,
    cancel_event: threading.Event,
    *,
    poll_seconds: float = 0.1,
) -> Iterator[None]:
    """Wait for the semaphore; honour cancellation if it fires while
    we're queued. Releases the slot on context exit."""
    while not cancel_event.is_set():
        if sem.acquire(timeout=poll_seconds):
            break
    else:
        raise CancelledError("cancelled while waiting for semaphore")
    try:
        yield
    finally:
        sem.release()`,
      },
    ],
  },

  {
    id: 'snapshot_validator',
    title: 'Snapshot integrity validation',
    file: 'services/snapshot_validator.py',
    oneliner: 'Pre-restore validation of snapshot.db shape (schema version, section_key invariants).',
    detail:
      'validate_snapshot(path) returns SnapshotValidationReport with issues per severity. Gated by is_before_restore_enabled() tunable.',
    consumers: ['restore', 'preflight'],
    snippets: [
      {
        label: 'validate_snapshot — pre-restore integrity sweep',
        cite: 'services/snapshot_validator.py::validate_snapshot',
        code: `def validate_snapshot(path: Path) -> SnapshotValidationReport:
    """Pre-restore validation. Returns issues per severity; the engine
    refuses to restore when overall_severity == FATAL."""
    report = SnapshotValidationReport(path=path)
    with sqlite3.connect(path) as cx:
        # 1. Schema version (refuse below SNAPSHOT_SCHEMA_VERSION).
        ver = cx.execute(
            "SELECT schema_version FROM snapshot_meta"
        ).fetchone()[0]
        if ver < SNAPSHOT_SCHEMA_VERSION:
            report.add(SEVERITY_FATAL, "schema_version_too_old",
                       f"snapshot schema_version={ver} < required={SNAPSHOT_SCHEMA_VERSION}")
            return report

        # 2. v0.15 invariant: every per-server row has section_key > 0.
        for table in ("server_items", "watch_events", "ratings",
                      "playlists", "collections"):
            zeros = cx.execute(
                f"SELECT COUNT(*) FROM {table} WHERE section_key <= 0"
            ).fetchone()[0]
            if zeros > 0:
                report.add(SEVERITY_ERROR, "zero_section_key",
                           f"{table} has {zeros} row(s) with section_key=0")

        # 3. library_sections rows referenced by every per-server row.
        for table in ("server_items", "watch_events", "ratings",
                      "playlists", "collections"):
            orphans = cx.execute(f"""
                SELECT COUNT(*) FROM {table} t
                LEFT JOIN library_sections ls
                  ON ls.section_key = t.section_key
                WHERE ls.section_key IS NULL
            """).fetchone()[0]
            if orphans > 0:
                report.add(SEVERITY_WARNING, "orphan_section_key",
                           f"{table} has {orphans} row(s) with no library_sections match")

    return report`,
        input: `path = Path("/snapshots/Jade-TV_20260522_174201.db")`,
        output: `SnapshotValidationReport(
    path=...,
    issues=[
        # if any
        ValidationIssue(severity=SEVERITY_WARNING, code="orphan_section_key",
                        message="watch_events has 3 row(s) with no library_sections match"),
    ],
    overall_severity=SEVERITY_WARNING,  # FATAL / ERROR / WARNING / OK
)`,
      },
    ],
  },

  {
    id: 'playlist_cache_db',
    title: 'Per-user playlist roster cache',
    file: 'server/playlist_cache_db.py + services/playlist_cache_*.py',
    oneliner: 'TTL-bounded per-user playlist list cache to avoid an N+1 fetch storm.',
    detail:
      'Listing playlists for N users on Plex requires N login + N list calls. The cache stores the list per-user with a TTL.',
    consumers: ['playlist copy', 'playlist mgmt UI'],
    snippets: [
      {
        label: 'get_or_fetch — TTL-bounded read-through',
        cite: 'services/playlist_cache_api.py::get_or_fetch_user_playlists',
        code: `def get_or_fetch_user_playlists(
    server: ServerConnection,
    username: str,
    *,
    ttl_seconds: int = 600,
    force_refresh: bool = False,
) -> List[PlaylistSummary]:
    """Read-through cache. Returns cached playlists if fresh; else
    fetches live via the adapter and updates the cache."""
    if not force_refresh:
        row = playlist_cache_db.get(server.server_id, username)
        if row and (time.time() - row["fetched_at"]) < ttl_seconds:
            return _deserialise(row["playlists_json"])

    # Cache miss or stale; fetch live.
    user_token = playlist_user_auth.resolve(server, username)
    user_conn = server.with_user(user_token)
    playlists = user_conn.adapter.list_user_playlists()

    playlist_cache_db.upsert(
        server.server_id, username,
        playlists_json=_serialise(playlists),
        fetched_at=time.time(),
    )
    return playlists`,
        input: `server = <ServerConnection for "Jade.TV">
username = "alice"
ttl_seconds = 600   # default 10 min
force_refresh = False`,
        output: `# Cache hit (fresh): 0 network calls.
# Cache miss / stale: 1 login + 1 list call against source server;
#                     cache row upserted; result returned.
[
    PlaylistSummary(id="pl-1", title="Workout", item_count=42, ...),
    PlaylistSummary(id="pl-2", title="Friday Movie Night", item_count=8, ...),
    ...
]`,
      },
    ],
  },

  {
    id: 'ws',
    title: 'WebSocket broadcaster',
    file: 'server/ws.py',
    oneliner: 'Pushes dashboard snapshots to connected clients ~4 Hz; enriched with fan-out result for ~8s after job completion.',
    detail:
      'WSManager broadcasts the JobQueue.current() snapshot (job + state + dashboard frame). Reads fan_out.get_active_result() to enrich the payload for fan-out jobs.',
    consumers: ['all jobs (visibility)', 'fan-out'],
    snippets: [
      {
        label: 'WSManager._broadcast_loop — ~4 Hz frame push',
        cite: 'server/ws.py::WSManager._broadcast_loop',
        code: `class WSManager:
    """Coalesces JobQueue.current() snapshots into a WS frame and
    broadcasts to every connected client every ~250ms."""

    BROADCAST_INTERVAL = 0.25   # 4 Hz

    async def _broadcast_loop(self) -> None:
        while not self._shutdown.is_set():
            await asyncio.sleep(self.BROADCAST_INTERVAL)
            if not self._clients:
                continue
            frame = self._build_frame()
            payload = orjson.dumps(frame)
            await asyncio.gather(*[
                self._send_safely(ws, payload) for ws in list(self._clients)
            ])

    def _build_frame(self) -> dict:
        """Per-tick snapshot: current job + dashboard + fan-out result."""
        rec = self._queue.current()
        dash = state.get_dashboard().snapshot() if rec else {}
        fan_out_result = fan_out.get_active_result()
        return {
            "type": "frame",
            "at": time.time(),
            "job": rec.to_dict() if rec else None,
            "dashboard": dash,
            "fan_out": fan_out_result.to_dict() if fan_out_result else None,
        }`,
        output: `# Every 250ms, every connected WS client receives:
{
    "type": "frame",
    "at": 1716397321.412,
    "job": {
        "job_id": "snap_20260522_174201_a91f",
        "mode": "snapshot",
        "state": "running",
        "started_at": 1716397320.0,
        ...
    },
    "dashboard": {
        "tier_counts": {"watch_count": 1247, "rating_count": 89},
        "activity_feed": [
            {"action": "scrobbled", "library": "Movies",
             "title": "Inception", "at": 1716397321.0},
            ...
        ],
        "libraries": {"Movies": {"phase": "gathering", "progress": 0.42}},
    },
    "fan_out": null
}`,
      },
    ],
  },
];

// ── Role colours ─────────────────────────────────────────────────────────────

const ROLE_COLOURS: Record<StepRole, { bg: string; fg: string; label: string }> = {
  dispatch: { bg: 'rgba(59, 130, 246, 0.15)', fg: '#3b82f6', label: 'Dispatch' },
  preflight: { bg: 'rgba(245, 158, 11, 0.15)', fg: '#f59e0b', label: 'Preflight' },
  engine: { bg: 'rgba(16, 185, 129, 0.15)', fg: '#10b981', label: 'Engine' },
  adapter: { bg: 'rgba(168, 85, 247, 0.15)', fg: '#a855f7', label: 'Adapter' },
  'write-path': { bg: 'rgba(249, 115, 22, 0.15)', fg: '#f97316', label: 'Write path' },
  telemetry: { bg: 'rgba(148, 163, 184, 0.15)', fg: '#94a3b8', label: 'Telemetry' },
  cleanup: { bg: 'rgba(100, 116, 139, 0.15)', fg: '#64748b', label: 'Cleanup' },
  state: { bg: 'rgba(20, 184, 166, 0.15)', fg: '#14b8a6', label: 'State' },
};

// ── Components ───────────────────────────────────────────────────────────────

// ── StepHint: labeled "?" pill + properly-styled popover ────────────────────
//
// Replaces an inline InfoTip on each step header. The default InfoTip
// renders a tiny circular (?) that visually orphans next to the role
// tag. StepHint is a pill button with both the icon AND a "Learn more"
// label, so the affordance is unambiguous; the popover is a proper
// rounded card with an arrow indicator pointing at the trigger.
//
// Hover OR focus opens the popover; mouseleave / blur closes it. Click
// toggles (sticky open) so a keyboard user or someone reading the body
// can click to pin it. Escape closes when focused. The popover is
// position: absolute, anchored to the trigger's left edge with a small
// gap; max-width keeps it from overflowing narrow viewports.

interface StepHintProps {
  children: React.ReactNode;
}

function StepHint({ children }: StepHintProps) {
  const [open, setOpen] = useState(false);
  const [pinned, setPinned] = useState(false);
  const isOpen = open || pinned;

  return (
    <span
      style={{ position: 'relative', display: 'inline-block' }}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
    >
      <button
        type="button"
        aria-label="Show step detail"
        aria-expanded={isOpen}
        onClick={() => setPinned((p) => !p)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onKeyDown={(e) => {
          if (e.key === 'Escape') {
            setPinned(false);
            setOpen(false);
            (e.currentTarget as HTMLButtonElement).blur();
          }
        }}
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: 6,
          padding: '3px 10px 3px 4px',
          borderRadius: 12,
          border: isOpen
            ? '1px solid rgba(96, 165, 250, 0.6)'
            : '1px solid rgba(96, 165, 250, 0.35)',
          background: isOpen
            ? 'rgba(96, 165, 250, 0.18)'
            : 'rgba(96, 165, 250, 0.08)',
          color: '#60a5fa',
          fontSize: 11,
          fontWeight: 500,
          cursor: 'help',
          transition: 'background 120ms ease, border-color 120ms ease',
        }}
      >
        <span
          aria-hidden
          style={{
            display: 'inline-flex',
            width: 16,
            height: 16,
            borderRadius: 8,
            background: 'rgba(96, 165, 250, 0.3)',
            color: '#bfdbfe',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: 11,
            fontWeight: 700,
            fontFamily:
              "'Cascadia Code', 'Fira Code', 'JetBrains Mono', monospace",
            lineHeight: 1,
          }}
        >
          ?
        </span>
        {pinned ? 'Hide detail' : 'Learn more'}
      </button>
      {isOpen && (
        <div
          role="tooltip"
          style={{
            position: 'absolute',
            top: 'calc(100% + 10px)',
            left: 0,
            zIndex: 50,
            width: 380,
            maxWidth: 'min(380px, 90vw)',
            padding: '12px 14px',
            background: '#1e1e2e',
            border: '1px solid rgba(96, 165, 250, 0.4)',
            borderRadius: 8,
            boxShadow:
              '0 10px 24px rgba(0, 0, 0, 0.45), 0 2px 6px rgba(0, 0, 0, 0.3)',
            fontSize: 13,
            lineHeight: 1.55,
            color: '#cdd6f4',
            textAlign: 'left',
            cursor: 'auto',
          }}
        >
          {/* Arrow pointing up at the trigger */}
          <span
            aria-hidden
            style={{
              position: 'absolute',
              top: -6,
              left: 16,
              width: 10,
              height: 10,
              background: '#1e1e2e',
              borderTop: '1px solid rgba(96, 165, 250, 0.4)',
              borderLeft: '1px solid rgba(96, 165, 250, 0.4)',
              transform: 'rotate(45deg)',
            }}
          />
          {children}
          {pinned && (
            <div
              style={{
                marginTop: 10,
                paddingTop: 8,
                borderTop: '1px solid rgba(148, 163, 184, 0.15)',
                fontSize: 11,
                color: 'var(--text-dim, #94a3b8)',
              }}
            >
              Click "Hide detail" or press Esc to close.
            </div>
          )}
        </div>
      )}
    </span>
  );
}

function RoleTag({ role }: { role: StepRole }) {
  const c = ROLE_COLOURS[role];
  return (
    <span
      style={{
        display: 'inline-block',
        padding: '2px 8px',
        borderRadius: 4,
        background: c.bg,
        color: c.fg,
        fontSize: 11,
        fontWeight: 600,
        letterSpacing: 0.3,
        textTransform: 'uppercase',
      }}
    >
      {c.label}
    </span>
  );
}

function Legend() {
  return (
    <div className="panel" style={{ marginBottom: 16 }}>
      <strong style={{ fontSize: 13 }}>Step role colours</strong>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 8 }}>
        {Object.keys(ROLE_COLOURS).map((r) => (
          <RoleTag key={r} role={r as StepRole} />
        ))}
      </div>
      <p style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 8, marginBottom: 0 }}>
        Each step is tagged with the role it plays in the call chain.
        Hover the "? Learn more" pill on any step for plain-English
        context, or click it to pin the popover open. Click "Show code &amp; I/O"
        below any step for a representative Python snippet plus example
        input / output.
      </p>
    </div>
  );
}

interface OverviewMapProps {
  onPick: (id: string) => void;
}

function OverviewMap({ onPick }: OverviewMapProps) {
  return (
    <>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Deployment map</h2>
        <span
          className="help"
          style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12 }}
        >
          Interactive call-chain reference for every job type the engine
          supports. Pick a job to drill in; hover any step for context;
          click "Show code &amp; I/O" for representative Python plus example
          input / output. Pair with the source code, not in place of it.
          line numbers drift.
        </span>
      </div>

      <Legend />

      <div className="panel">
        <h3 style={{ marginTop: 0 }}>Job types</h3>
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))',
            gap: 12,
            marginTop: 8,
          }}
        >
          {JOB_FLOWS.map((j) => (
            <button
              key={j.id}
              type="button"
              onClick={() => onPick(j.id)}
              style={{
                textAlign: 'left',
                padding: 14,
                borderRadius: 8,
                border: '1px solid var(--panel-border, rgba(148,163,184,0.3))',
                background: 'var(--panel-bg-strong, rgba(15,23,42,0.4))',
                cursor: 'pointer',
                display: 'flex',
                flexDirection: 'column',
                gap: 6,
              }}
            >
              <strong style={{ fontSize: 14 }}>{j.title}</strong>
              <span style={{ fontSize: 12, color: 'var(--text-dim)', lineHeight: 1.45 }}>
                {j.oneliner}
              </span>
              <span style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 4 }}>
                {j.steps.length} steps · touches {j.sharedInfra.length} shared modules
              </span>
            </button>
          ))}
        </div>
      </div>

      <div className="panel">
        <h3 style={{ marginTop: 0 }}>Shared infrastructure</h3>
        <p style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 0 }}>
          Modules every job (or most) consume. Click any entry to see
          which jobs touch it.
        </p>
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))',
            gap: 10,
            marginTop: 8,
          }}
        >
          {SHARED_INFRA.map((s) => (
            <button
              key={s.id}
              type="button"
              onClick={() => onPick(`shared:${s.id}`)}
              style={{
                textAlign: 'left',
                padding: 12,
                borderRadius: 6,
                border: '1px solid var(--panel-border, rgba(148,163,184,0.2))',
                background: 'transparent',
                cursor: 'pointer',
                display: 'flex',
                flexDirection: 'column',
                gap: 4,
              }}
            >
              <strong style={{ fontSize: 13 }}>{s.title}</strong>
              <code style={{ fontSize: 11, color: 'var(--text-dim)' }}>{s.file}</code>
              <span style={{ fontSize: 12, lineHeight: 1.4 }}>{s.oneliner}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="panel" style={{ fontSize: 12, color: 'var(--text-dim)' }}>
        <strong>About the data:</strong> this map is built from a
        read-only recon performed on 2026-05-22. file:function
        citations drift as the code evolves. Code snippets are
        REPRESENTATIVE skeletons that capture the contract; treat the
        source code as authoritative when they disagree.
      </div>
    </>
  );
}

interface JobFlowViewProps {
  jobId: string;
  onBack: () => void;
  onPick: (id: string) => void;
}

function JobFlowView({ jobId, onBack, onPick }: JobFlowViewProps) {
  const job = JOB_FLOWS.find((j) => j.id === jobId);
  if (!job) {
    return (
      <div className="panel">
        <p>Unknown job: {jobId}</p>
        <button onClick={onBack}>Back to overview</button>
      </div>
    );
  }

  return (
    <>
      <div className="panel">
        <button
          type="button"
          onClick={onBack}
          style={{
            background: 'transparent',
            border: 'none',
            color: 'var(--accent, #60a5fa)',
            cursor: 'pointer',
            padding: 0,
            fontSize: 13,
            marginBottom: 8,
          }}
        >
          ← Back to overview
        </button>
        <h2 style={{ marginTop: 0 }}>{job.title}</h2>
        <p style={{ fontSize: 13, color: 'var(--text-dim)', marginTop: 0 }}>
          {job.oneliner}
        </p>
        <p style={{ fontSize: 14, lineHeight: 1.55 }}>{job.preamble}</p>
      </div>

      <div className="panel">
        <h3 style={{ marginTop: 0 }}>Call chain</h3>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {job.steps.map((s) => (
            <div
              key={s.n}
              style={{
                padding: 12,
                borderRadius: 6,
                border: '1px solid var(--panel-border, rgba(148,163,184,0.2))',
                background: 'var(--panel-bg-strong, rgba(15,23,42,0.3))',
                display: 'flex',
                flexDirection: 'column',
                gap: 6,
              }}
            >
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 10,
                  flexWrap: 'wrap',
                }}
              >
                <span
                  style={{
                    minWidth: 28,
                    height: 28,
                    borderRadius: 14,
                    background: 'rgba(96,165,250,0.2)',
                    color: '#60a5fa',
                    display: 'inline-flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    fontSize: 12,
                    fontWeight: 600,
                  }}
                >
                  {s.n}
                </span>
                <strong style={{ fontSize: 14 }}>{s.label}</strong>
                <RoleTag role={s.role} />
                <StepHint>{s.detail}</StepHint>
              </div>
              <code
                style={{
                  fontSize: 12,
                  color: 'var(--text-dim)',
                  marginLeft: 38,
                  wordBreak: 'break-all',
                }}
              >
                {s.cite}
              </code>
              <CodeExample code={s.code} input={s.input} output={s.output} />
            </div>
          ))}
        </div>
      </div>

      <div className="panel">
        <h3 style={{ marginTop: 0 }}>Shared infrastructure consumed</h3>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
          {job.sharedInfra.map((id) => {
            const s = SHARED_INFRA.find((x) => x.id === id);
            if (!s) {
              return (
                <span
                  key={id}
                  style={{
                    padding: '4px 10px',
                    background: 'rgba(148,163,184,0.15)',
                    borderRadius: 4,
                    fontSize: 12,
                  }}
                >
                  {id}
                </span>
              );
            }
            return (
              <button
                key={id}
                type="button"
                onClick={() => onPick(`shared:${id}`)}
                style={{
                  padding: '4px 10px',
                  background: 'rgba(96,165,250,0.15)',
                  border: '1px solid rgba(96,165,250,0.3)',
                  borderRadius: 4,
                  fontSize: 12,
                  color: '#60a5fa',
                  cursor: 'pointer',
                }}
              >
                {s.title}
              </button>
            );
          })}
        </div>
      </div>

      <div className="panel">
        <h3 style={{ marginTop: 0 }}>Databases / surfaces touched</h3>
        <ul style={{ marginTop: 0, paddingLeft: 20, fontSize: 13, lineHeight: 1.6 }}>
          {job.surfaces.map((s) => (
            <li key={s}>
              <code style={{ fontSize: 12 }}>{s}</code>
            </li>
          ))}
        </ul>
      </div>

      {job.notes && job.notes.length > 0 && (
        <div className="panel">
          <h3 style={{ marginTop: 0 }}>Notes</h3>
          <ul style={{ marginTop: 0, paddingLeft: 20, fontSize: 13, lineHeight: 1.6 }}>
            {job.notes.map((n, i) => (
              <li key={i}>{n}</li>
            ))}
          </ul>
        </div>
      )}
    </>
  );
}

interface SharedInfraViewProps {
  infraId: string;
  onBack: () => void;
  onPick: (id: string) => void;
}

function SharedInfraView({ infraId, onBack, onPick }: SharedInfraViewProps) {
  const s = SHARED_INFRA.find((x) => x.id === infraId);
  if (!s) {
    return (
      <div className="panel">
        <p>Unknown infrastructure: {infraId}</p>
        <button onClick={onBack}>Back to overview</button>
      </div>
    );
  }

  const consumingJobs = JOB_FLOWS.filter((j) => j.sharedInfra.includes(infraId));

  return (
    <>
      <div className="panel">
        <button
          type="button"
          onClick={onBack}
          style={{
            background: 'transparent',
            border: 'none',
            color: 'var(--accent, #60a5fa)',
            cursor: 'pointer',
            padding: 0,
            fontSize: 13,
            marginBottom: 8,
          }}
        >
          ← Back to overview
        </button>
        <h2 style={{ marginTop: 0 }}>{s.title}</h2>
        <code style={{ fontSize: 13, color: 'var(--text-dim)' }}>{s.file}</code>
        <p style={{ fontSize: 14, lineHeight: 1.55, marginTop: 12 }}>{s.detail}</p>
      </div>

      <div className="panel">
        <h3 style={{ marginTop: 0 }}>Consumed by</h3>
        {consumingJobs.length === 0 ? (
          <p style={{ fontSize: 13, color: 'var(--text-dim)' }}>
            {s.consumers.join(', ')}
          </p>
        ) : (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
            {consumingJobs.map((j) => (
              <button
                key={j.id}
                type="button"
                onClick={() => onPick(j.id)}
                style={{
                  padding: '4px 10px',
                  background: 'rgba(16,185,129,0.15)',
                  border: '1px solid rgba(16,185,129,0.3)',
                  borderRadius: 4,
                  fontSize: 12,
                  color: '#10b981',
                  cursor: 'pointer',
                }}
              >
                {j.title}
              </button>
            ))}
          </div>
        )}
      </div>

      {s.snippets && s.snippets.length > 0 && (
        <div className="panel">
          <h3 style={{ marginTop: 0 }}>Key functions</h3>
          <p style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 0 }}>
            Representative snippets for the most important entry points
            in this module. Click "Show code &amp; I/O" beneath any
            entry to expand the Python plus example input / output.
          </p>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginTop: 8 }}>
            {s.snippets.map((sn, i) => (
              <div
                key={i}
                style={{
                  padding: 12,
                  borderRadius: 6,
                  border: '1px solid var(--panel-border, rgba(148,163,184,0.2))',
                  background: 'var(--panel-bg-strong, rgba(15,23,42,0.3))',
                }}
              >
                <strong style={{ fontSize: 13 }}>{sn.label}</strong>
                {sn.cite && (
                  <code
                    style={{
                      display: 'block',
                      marginTop: 4,
                      fontSize: 11,
                      color: 'var(--text-dim)',
                      wordBreak: 'break-all',
                    }}
                  >
                    {sn.cite}
                  </code>
                )}
                <div style={{ marginLeft: -38, marginTop: 4 }}>
                  <CodeExample code={sn.code} input={sn.input} output={sn.output} />
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </>
  );
}

// ── Top-level DeploymentMap component ────────────────────────────────────────

export function DeploymentMapPage() {
  const [view, setView] = useState<string>('overview');

  const handlePick = (id: string) => setView(id);
  const handleBack = () => setView('overview');

  const content = useMemo(() => {
    if (view === 'overview') {
      return <OverviewMap onPick={handlePick} />;
    }
    if (view.startsWith('shared:')) {
      const infraId = view.slice('shared:'.length);
      return (
        <SharedInfraView
          infraId={infraId}
          onBack={handleBack}
          onPick={handlePick}
        />
      );
    }
    return <JobFlowView jobId={view} onBack={handleBack} onPick={handlePick} />;
  }, [view]);

  return content;
}
