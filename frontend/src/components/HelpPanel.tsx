// Settings → Help.
//
// A flat reference page for the longer explanations that used to live
// inline as verbose help text under each panel's heading. Those
// explanations now appear (in shorter form) inside InfoTip popovers
// next to the control they describe; this panel collects them all in
// one place for read-through.
//
// Sub-pages:
//   * Reference         - the original control-by-control writeup.
//   * Activity Statuses - the 11 activity-feed labels, their colors,
//                         when each fires, and what they mean.
//
// Visible to every role that can see the Settings tab. The content
// is purely informational; no permission gating per section.

import { useEffect, useState } from 'react';
import { api } from '../api';

type HelpPage = 'reference' | 'activity_and_phases' | 'api_usage' | 'db_schema' | 'run_logs' | 'troubleshooting';


export function HelpPanel() {
  const [page, setPage] = useState<HelpPage>('reference');
  return (
    <>
      <nav className="tabs sub-tabs">
        <button
          className={page === 'reference' ? 'active' : ''}
          onClick={() => setPage('reference')}
        >
          Reference
        </button>
        <button
          className={page === 'activity_and_phases' ? 'active' : ''}
          onClick={() => setPage('activity_and_phases')}
        >
          Activity &amp; Phases
        </button>
        <button
          className={page === 'api_usage' ? 'active' : ''}
          onClick={() => setPage('api_usage')}
        >
          API Usage
        </button>
        <button
          className={page === 'db_schema' ? 'active' : ''}
          onClick={() => setPage('db_schema')}
        >
          DB Schema
        </button>
        <button
          className={page === 'run_logs' ? 'active' : ''}
          onClick={() => setPage('run_logs')}
        >
          Run Logs
        </button>
        <button
          className={page === 'troubleshooting' ? 'active' : ''}
          onClick={() => setPage('troubleshooting')}
        >
          Troubleshooting
        </button>
      </nav>
      {page === 'reference' && <ReferencePage />}
      {page === 'activity_and_phases' && <ActivityStatusesPage />}
      {page === 'api_usage' && <ApiUsagePage />}
      {page === 'db_schema' && <DbSchemaPage />}
      {page === 'run_logs' && <RunLogsPage />}
      {page === 'troubleshooting' && <TroubleshootingPage />}
    </>
  );
}


// ── Reference (original Help content) ────────────────────────────────────────

function ReferencePage() {
  return (
    <>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Reference</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12 }}>
          Longer explanations for the controls scattered through the app.
          Each section here mirrors a tooltip you'll also see inline next
          to the (?) icon at that control.
        </span>
      </div>

      <div className="panel">
        <h2>Account → Display name</h2>
        <p>
          Shown in the UI instead of your username. Cosmetic only; login
          still uses your real username. Leave blank to fall back to the
          username.
        </p>
      </div>

      <div className="panel">
        <h2>Account → Session</h2>
        <p>
          <strong>Last sign-in</strong> is the timestamp of your most
          recent login. <strong>Session duration</strong> ticks up from
          that point; a page reload doesn't reset it because the timer is
          tied to the JWT, which lives 24 hours.
        </p>
      </div>

      <div className="panel">
        <h2>Account → Clock display</h2>
        <p>
          Controls only the clock shown in the topbar. Schedules and
          logs always use the server's clock regardless of what's chosen
          here, and the preference is stored per-browser.
        </p>
        <ul>
          <li>
            <strong>Server time</strong> - the backend's
            <code> TZ</code>. Same value the engine uses for schedules
            and log timestamps.
          </li>
          <li>
            <strong>My device's local time</strong> - the browser's
            clock + timezone. Useful when you're on the road and the
            server is in a different zone.
          </li>
          <li>
            <strong>Custom time</strong> - show the topbar at an
            arbitrary offset from server time. Type the time it should
            read right now, click Apply, and the offset persists across
            reloads.
          </li>
        </ul>
      </div>

      <div className="panel">
        <h2>Account → Switch view mode</h2>
        <p>
          Lets admin / root_admin preview the UI as a lesser role. Your
          underlying session keeps your real role's access; the override
          is a visual filter only, and a page reload restores the full
          view.
        </p>
        <p>
          A password is required only when raising the visible role.
          Dropping down is free; restoring full access or choosing a
          higher role both require the password.
        </p>
      </div>

      <div className="panel">
        <h2>Account → Account Management</h2>
        <p>
          Inner pages:
        </p>
        <ul>
          <li>
            <strong>Database Admin Account</strong> - the
            <code> db_admin</code> credential row in <code>auth.db</code>.
            Independent of any login account; authorises destructive
            writes in <em>Servers → User Management</em>. Each change
            requires the current db_admin password as the per-call gate.
          </li>
          <li>
            <strong>User Accounts</strong> - explorer for every
            login user on this install. Click a row to manage that user's
            role, display name, password, or to delete them. Admins
            cannot modify the root_admin row.
          </li>
        </ul>
      </div>

      <div className="panel">
        <h2>Roles</h2>
        <ul>
          <li><strong>Viewer</strong> - Dashboard + Servers (read-only) + Account.</li>
          <li><strong>Operator</strong> - Viewer + Start jobs + Logs + Exports + Schedules (read).</li>
          <li><strong>Manager</strong> - Operator + Stop jobs + Edit schedules + Sync.</li>
          <li><strong>Admin</strong> - full access; cannot modify the root_admin user row.</li>
          <li><strong>Root admin</strong> - full access including the root_admin row itself.</li>
          <li><strong>db_admin</strong> - non-login credential. Gates destructive User Management writes.</li>
        </ul>
      </div>
    </>
  );
}


// ── Activity Statuses ────────────────────────────────────────────────────────
//
// Documents the 11 status labels the dashboard's activity feed emits.
// The labels + colors mirror dashboard.py's _ACTION_LABELS / _ACTION_COLORS
// so a glance at the live feed lines up with a glance at this page.

interface StatusRow {
  label: string;
  color: string;
  when: string;
  modes: string;
  example?: string;
}

// Currently Processing phase reference. One row per phase string the
// engine sets via _current_item() / set_current_item(phase=...).
// Colours mirror DashboardPanel.tsx :: PHASE_TAG (which maps each
// phase to a CSS class) so the swatch here matches what the
// Currently Processing panel renders during a live run.
interface PhaseRow {
  label: string;
  color: string;
  when: string;
  modes: string;
  example?: string;
}

const PHASE_ROWS: PhaseRow[] = [
  {
    label: 'capturing',
    // matches --color-capturing in styles.css
    color: '#a78bfa',
    modes: 'snapshot',
    when: 'The engine is serialising one item from the live Plex API into the in-memory snapshot payload. Fires for each item walked during the watch-history, ratings, playlist-member, and collection-member gathers. Violet so a glance at Currently Processing tells you a snapshot is in flight.',
    example: 'Music · track · The Long Way Down · capturing',
  },
  {
    label: 'fetching',
    color: '#58a6ff',
    modes: 'snapshot',
    when: "Calling pl.items() on one playlist to enumerate its members. This is the slow per-playlist round-trip, the dominant cost of the pre-flight Playlist Cache Warm phase. Visible during the warm-up before per-library work begins.",
    example: 'My Server · playlist · My Playlist · fetching',
  },
  {
    label: 'indexing',
    color: '#58a6ff',
    modes: 'restore, direct',
    when: "Building a library section's filepath→item scan_cache + suffix index. Fires once per section the first time a worker needs Tier 2 (filepath) resolution. The scan-cache build can take 30+ seconds on a large music library, this is what that wait looks like in the panel.",
    example: 'Music · scan_cache · Building filepath index for \'Music\' · indexing',
  },
  {
    label: 'resolving',
    color: '#58a6ff',
    modes: 'restore, direct',
    when: 'The resolver is searching for one stored item on the destination server. Walks all four tiers in order (DB cache → API GUID → filepath/suffix → fuzzy title) until one matches or all fail. Fires once per stored watch-history record and once per stored rating before its write phase begins.',
    example: 'Movies · movie · The Matrix (1999) · resolving',
  },
  {
    label: 'scrobbling',
    color: '#3fb950',
    modes: 'restore, direct',
    when: "Watch-history write. The engine resolved an item and is POSTing /:/scrobble to the destination to advance its viewCount. One scrobble per stored play above the destination's current count, capped per item by VIEWCOUNT_INCREMENT_CAP (=50).",
    example: 'Movies · movie · The Matrix (1999) · scrobbling',
  },
  {
    label: 'rating',
    color: '#d29922',
    modes: 'restore, direct',
    when: 'Rating write. The engine resolved a rated item and is POSTing /:/rate to set the star rating. Additive-only: if the destination is already rated, the worker logs SKIPPED in the Activity Feed and the phase ends without a write.',
    example: 'Music · track · Daft Punk - One More Time · rating',
  },
  {
    label: 'merging',
    color: '#58a6ff',
    modes: 'restore, direct',
    when: "Playlist or collection create-or-append. Fires per container during the Phase-2 merge step of import_playlists / import_collections. The container's members were already resolved in Phase 1; this is the create() / addItems() network round-trip itself.",
    example: 'Music · playlist · My Playlist · merging',
  },
];

// Thread Pool category reference. One row per category-key the
// engine pins via ``services.dashboard._thread_category`` plus the
// scan-cache / snapshot entries set by ``set_current_item`` directly.
// Labels mirror DashboardPanel.tsx :: THREAD_LABELS so the help text
// uses the same string the dashboard pill renders.
interface ThreadRow {
  label: string;          // human-readable category label
  color: string;          // tint used in the help-page chip
  modes: string;
  what: string;           // one-line "what these workers are doing"
  when: string;           // longer "when these workers spin up"
  notes?: string;
}

const THREAD_ROWS: ThreadRow[] = [
  {
    label: 'Watched',
    color: '#3fb950',
    modes: 'snapshot, restore, direct',
    what: 'Reading or writing view counts and resume positions for movies, episodes, and other non-music items.',
    when: "During a snapshot, one worker per watched item the engine is serialising. During a restore, one worker per matched item the engine is /:/scrobble-ing. Per-library: each library's watch-history phase has its own pool of these.",
    notes: "Plex tracks view count separately from play count - this pool covers the video side. Music has its own pool (Play Count) so audio and video work don't share a throughput budget.",
  },
  {
    label: 'Play Count',
    color: '#3fb950',
    modes: 'snapshot, restore, direct',
    what: 'Same role as Watched, but scoped to music tracks. Reading or writing play counts on artist-type libraries.',
    when: "Spun up when the section type is 'artist'. The engine picks Watched vs Play Count based on section type at the top of the phase.",
  },
  {
    label: 'Playlists',
    color: '#58a6ff',
    modes: 'snapshot, restore, direct',
    what: 'Reading playlist contents or creating / appending to playlists on the destination.',
    when: "Three places: the pre-flight playlist-cache warm at the start of a snapshot (one worker per source token), the per-playlist serialisation inside a library, and the Phase 2 create-or-append step on the destination during a restore.",
    notes: "Often the largest pool in a snapshot run because Plex's pl.items() round-trip is the slow part of capture.",
  },
  {
    label: 'Collections',
    color: '#58a6ff',
    modes: 'snapshot, restore, direct',
    what: 'Reading collection members or creating / appending collections on the destination.',
    when: "During the per-library collection gather (snapshot) or the Phase 2 create-or-append step (restore). Always per-library; collections don't have a server-wide pre-flight warm.",
  },
  {
    label: 'Ratings',
    color: '#d29922',
    modes: 'snapshot, restore, direct',
    what: 'Reading user star ratings or POSTing /:/rate writes to the destination.',
    when: "During the per-library ratings gather (snapshot) or per-item rating restore (restore / direct). Smaller pool than the others because ratings are typically a small percentage of total items.",
  },
  {
    label: 'Scan Cache',
    color: '#58a6ff',
    modes: 'restore, direct',
    what: "Building one library's file-path lookup table. Powers the resolver's Tier 2 (filepath match) and Tier 2.5 (suffix match).",
    when: 'Once per library, at the top of the restore phase before any per-item resolution fires. Lives for the duration of the build (30+ seconds on a large music library is normal).',
    notes: "If you see a Scan Cache worker stuck for more than 5 minutes, the destination Plex is probably not responding to ``section.searchTracks()`` / ``searchEpisodes()`` - check the destination's HTTP latency in the Networking panel.",
  },
  {
    label: 'Home User',
    color: '#cbd5e1',
    modes: 'snapshot, restore, direct',
    what: 'Fetching or applying data for one Plex Home managed user.',
    when: "One worker per managed user, running in parallel with the server-owner phase. Watch history, ratings, playlists, and collections for that user all flow through this pool.",
    notes: "Managed user threads acquire their own per-user Plex token at the top of the worker so they read what THAT user sees on the source server, not what the admin token can see.",
  },
  {
    label: 'Capturing snapshot',
    color: '#a78bfa',
    modes: 'snapshot',
    what: "The top-level per-library snapshot worker. Reads and serialises one library's data into the in-memory payload.",
    when: 'Spun up once per library in a snapshot run; runs in parallel up to MAX_WORKERS. Each worker drives the four phase sub-workers (Watched, Playlists, Collections, Ratings) for its library.',
    notes: 'Violet pill colour to visually flag snapshot-only work, matching the Currently Processing panel\'s "capturing" phase colour.',
  },
];

const STATUS_ROWS: StatusRow[] = [
  {
    label: 'STARTED',
    color: '#cbd5e1',
    modes: 'snapshot, restore, direct',
    when: 'A library begins its processing. Fires once at the start of that library, before any per-data-type phase work. The "library" cell carries the library name; the "title" cell says "Snapshot started" or "Restore started".',
    example: '03:03:43 STARTED Music Snapshot started',
  },
  {
    label: 'PHASE',
    color: '#60a5fa',
    modes: 'snapshot, restore, direct',
    when: 'A pipeline progress update. Used both for pre-flight steps (cache warm, home-user auth, schema migrations) and for per-data-type milestones inside a library (Watch History → N items, Playlists → N items, etc.). The "library" cell is "-" for pre-flight lines and the library name for per-library lines.',
    example: '03:05:20 PHASE Music Watch History → 6681 items',
  },
  {
    label: 'DONE',
    color: '#22c55e',
    modes: 'snapshot, restore, direct',
    when: "A library's processing finished successfully. Fires once per library, after all four data-type gathers (snapshot) or restores complete. Bold green - look for one DONE per library at the end of a successful run.",
    example: '03:12:01 DONE Music Snapshot complete',
  },
  {
    label: 'ERROR',
    color: '#ef4444',
    modes: 'snapshot, restore, direct',
    when: 'A library failed entirely. Bubbled up from the library worker\'s exception handler when an unrecoverable error stopped processing for that library. The other libraries in the same job keep running; the run as a whole continues. The traceback lands in the run\'s errors.log.',
    example: '03:08:14 ERROR Audio-Books Snapshot failed',
  },
  {
    label: 'MERGED',
    color: '#22c55e',
    modes: 'restore only',
    when: 'An existing item on the target server was successfully updated with export data (a watch count, a rating, a new member in an existing playlist, etc.). Per-item event. You\'ll see one per matched item during the restore phases.',
    example: '04:21:33 MERGED Movies The Matrix (1999)',
  },
  {
    label: 'CREATED',
    color: '#22c55e',
    modes: 'restore only',
    when: 'A new destination object was created from scratch - typically a playlist or collection that didn\'t exist on the target server. Per-item.',
    example: '04:22:01 CREATED Music My Playlist',
  },
  {
    label: 'APPENDED',
    color: '#06b6d4',
    modes: 'restore only',
    when: 'Items were added to an existing destination playlist or collection without rebuilding it. The original contents are preserved; the export\'s members are merged in. Per-item event for each newly-added member.',
    example: '04:22:14 APPENDED Music Pink Floyd - Echoes',
  },
  {
    label: 'RATED',
    color: '#facc15',
    modes: 'restore only',
    when: 'A star rating was applied to a target item. Yellow because ratings are the rarest data type to restore - small per-item operation. The restorer is additive-only: if the target already has a rating, the line shows SKIPPED instead.',
    example: '04:22:55 RATED Music Daft Punk - One More Time',
  },
  {
    label: 'SKIPPED',
    color: '#94a3b8',
    modes: 'restore only',
    when: 'Per-item - the engine deliberately did not write to this item. Common reasons: smart playlist that must be recreated manually on the target, target item already at the desired state (already rated, view count already higher, etc.). Not an error; expected output during a clean restore.',
    example: '04:23:11 SKIPPED Music The Beatles - Hey Jude',
  },
  {
    label: 'FAILED',
    color: '#ef4444',
    modes: 'restore only',
    when: 'Per-item - resolution succeeded (the item was matched on the target) but the API call to apply the change returned an error. Distinct from ERROR which is library-wide. The full reason lands in the per-library fail_*.log.',
    example: '04:23:42 FAILED Movies Inception (2010)',
  },
  {
    label: 'UNRESOLVED',
    color: '#dc2626',
    modes: 'restore only',
    when: "Per-item - the resolver couldn't find a matching item on the target server through any of its four tiers (GUID lookup, exact path match, suffix path match, fuzzy title). The item is recorded in troubleshoot.log with a categorised reason so the operator can chase the gap.",
    example: '04:24:08 UNRESOLVED TV Some Obscure Show - S01E02',
  },
];


function ActivityStatusesPage() {
  // Nested sub-tab inside the "label reference" panel. Same UX as
  // Account Management → (Database Admin Account | User Accounts):
  // the panel header + the table both swap when the operator clicks
  // between Activity Statuses and Processing Phases.
  const [labelView, setLabelView] = useState<'activity' | 'phase' | 'thread'>('activity');
  return (
    <>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Activity Statuses</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12 }}>
          The 11 labels the Dashboard's activity feed emits, in the same colors
          they render with. Each row below explains when the label fires, in
          which job modes (snapshot / restore / direct), and what to do about it
          if anything. The full source of truth lives in
          <code> services/dashboard.py</code> -
          <code> _ACTION_LABELS</code> +
          <code> _ACTION_COLORS</code>.
        </span>
      </div>

      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Reading the feed</h2>
        <p>
          Each activity-feed line carries four fields the dashboard renders
          left-to-right:
        </p>
        <ul>
          <li><strong>Time</strong> - <code>HH:MM:SS</code>, the server's clock when the event was pushed.</li>
          <li><strong>Status label</strong> - one of the 11 below, colored.</li>
          <li><strong>Library</strong> - the library name, or <code>-</code> for events that don't belong to a specific library (cache warms, home-user auth, end-of-run notices).</li>
          <li><strong>Title / detail</strong> - the human-readable specifics. Per-library phase lines look like <code>Watch History → 6681 items</code>; per-item lines carry the item title.</li>
        </ul>
        <p style={{ marginTop: 12 }}>
          Per-item lines (MERGED, CREATED, APPENDED, RATED, SKIPPED, FAILED,
          UNRESOLVED) only fire during restore jobs. Snapshot mode produces
          STARTED / PHASE / DONE / ERROR only, plus the four PHASE summary lines
          (Watch History, Playlists, Collections, Ratings) per library.
        </p>
      </div>

      <div className="panel">
        <nav className="tabs sub-tabs" style={{ marginBottom: 12 }}>
          <button
            className={labelView === 'activity' ? 'active' : ''}
            onClick={() => setLabelView('activity')}
          >
            Activity Statuses
          </button>
          <button
            className={labelView === 'phase' ? 'active' : ''}
            onClick={() => setLabelView('phase')}
          >
            Processing Phases
          </button>
          <button
            className={labelView === 'thread' ? 'active' : ''}
            onClick={() => setLabelView('thread')}
          >
            Thread Pool
          </button>
        </nav>
        {labelView === 'activity' ? (
          <>
            <h2 style={{ marginTop: 0 }}>Every label, in feed order</h2>
            <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 12 }}>
              The Activity Feed (top of the Dashboard) logs <em>completed
              events</em>: a library started, a phase summary landed, an
              item merged. One row = one past event.
            </span>
            <table className="list" style={{ width: '100%' }}>
              <thead>
                <tr>
                  <th style={{ width: 130 }}>Label</th>
                  <th style={{ width: 160 }}>Modes</th>
                  <th>When it fires</th>
                </tr>
              </thead>
              <tbody>
                {STATUS_ROWS.map((r) => (
                  <tr key={r.label}>
                    <td>
                      <span
                        style={{
                          display: 'inline-block',
                          padding: '2px 8px',
                          borderRadius: 4,
                          fontWeight: 600,
                          fontSize: 11,
                          background: r.color + '22',
                          color: r.color,
                          border: `1px solid ${r.color}55`,
                        }}
                      >
                        {r.label}
                      </span>
                    </td>
                    <td style={{ fontSize: 12, color: 'var(--text-dim)' }}>{r.modes}</td>
                    <td>
                      <div style={{ fontSize: 13 }}>{r.when}</div>
                      {r.example && (
                        <div className="mono" style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 4 }}>
                          e.g. {r.example}
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        ) : labelView === 'phase' ? (
          <>
            <h2 style={{ marginTop: 0 }}>Every phase, in the Currently Processing panel</h2>
            <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 12 }}>
              The Currently Processing panel shows <em>live, in-flight
              work</em>: one row per worker thread, updated as it moves
              between phases. Phase strings come from
              <code> services/dashboard.py :: set_current_item</code>;
              colours are mapped via <code>PHASE_TAG</code> in the
              Dashboard component.
            </span>
            <table className="list" style={{ width: '100%' }}>
              <thead>
                <tr>
                  <th style={{ width: 130 }}>Phase</th>
                  <th style={{ width: 160 }}>Modes</th>
                  <th>What's happening</th>
                </tr>
              </thead>
              <tbody>
                {PHASE_ROWS.map((r) => (
                  <tr key={r.label}>
                    <td>
                      <span
                        style={{
                          display: 'inline-block',
                          padding: '2px 8px',
                          borderRadius: 4,
                          fontWeight: 600,
                          fontSize: 11,
                          background: r.color + '22',
                          color: r.color,
                          border: `1px solid ${r.color}55`,
                        }}
                      >
                        {r.label}
                      </span>
                    </td>
                    <td style={{ fontSize: 12, color: 'var(--text-dim)' }}>{r.modes}</td>
                    <td>
                      <div style={{ fontSize: 13 }}>{r.when}</div>
                      {r.example && (
                        <div className="mono" style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 4 }}>
                          e.g. {r.example}
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        ) : (
          <>
            <h2 style={{ marginTop: 0 }}>Every worker pool, in the Thread Pool panel</h2>
            <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 12 }}>
              The Thread Pool panel groups every active worker thread
              by what category of work it's doing. The pill on the
              dashboard shows <em>label × count</em> plus an inline
              description; hover the pill for the same description as
              a native tooltip. Categories come from
              <code> services.dashboard._thread_category</code> and
              the labels render through
              <code> DashboardPanel.tsx :: THREAD_LABELS</code>.
            </span>
            <table className="list" style={{ width: '100%' }}>
              <thead>
                <tr>
                  <th style={{ width: 160 }}>Pool label</th>
                  <th style={{ width: 160 }}>Modes</th>
                  <th>What the workers are doing</th>
                </tr>
              </thead>
              <tbody>
                {THREAD_ROWS.map((r) => (
                  <tr key={r.label}>
                    <td>
                      <span
                        style={{
                          display: 'inline-block',
                          padding: '2px 8px',
                          borderRadius: 4,
                          fontWeight: 600,
                          fontSize: 11,
                          background: r.color + '22',
                          color: r.color,
                          border: `1px solid ${r.color}55`,
                        }}
                      >
                        {r.label}
                      </span>
                    </td>
                    <td style={{ fontSize: 12, color: 'var(--text-dim)' }}>{r.modes}</td>
                    <td>
                      <div style={{ fontSize: 13 }}>{r.what}</div>
                      <div style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 4 }}>
                        <strong>When it spins up:</strong> {r.when}
                      </div>
                      {r.notes && (
                        <div style={{
                          fontSize: 11, color: 'var(--text-dim)', marginTop: 6,
                          paddingLeft: 8, borderLeft: '2px solid var(--color-phase, #58a6ff)',
                          fontStyle: 'italic',
                        }}>
                          {r.notes}
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>

      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Common patterns</h2>
        {labelView === 'activity' ? (
          <>
            <p><strong>A healthy snapshot for one library reads</strong>:</p>
            <pre style={{ background: 'var(--panel-alt, #1b2233)', padding: 10, fontSize: 12, lineHeight: 1.5, overflowX: 'auto' }}>
{`PHASE   -      Warming playlist cache for 'My Server' (0 playlists)…
STARTED Music  Snapshot started
PHASE   Music  Playlists → 2 items
PHASE   Music  Collections → 0 items
PHASE   Music  Ratings → 758 items
PHASE   Music  Watch History → 6681 items
DONE    Music  Snapshot complete`}
            </pre>
            <p style={{ marginTop: 8, fontSize: 12, color: 'var(--text-dim)' }}>
              The four PHASE lines inside a library run in parallel (4-thread pool inside
              <code> snapshot_library</code>) so their timestamps don't follow the order
              they're listed in the code - they finish in whatever order Plex returns.
              Collections will commonly be <strong>0 items</strong> for Music libraries -
              Plex collections are usually a movie/TV concept.
            </p>

            <p style={{ marginTop: 16 }}><strong>A healthy restore for one library reads</strong> (truncated to one per type):</p>
            <pre style={{ background: 'var(--panel-alt, #1b2233)', padding: 10, fontSize: 12, lineHeight: 1.5, overflowX: 'auto' }}>
{`STARTED  Movies  Restore started
PHASE    Movies  → Watched
MERGED   Movies  The Matrix (1999)
SKIPPED  Movies  Inception (2010)  [already at view count 3]
PHASE    Movies  → Ratings
RATED    Movies  Blade Runner (1982)
PHASE    Movies  → Playlists
APPENDED Movies  Pink Floyd - Echoes
CREATED  Movies  My Playlist
PHASE    Movies  → Collections
UNRESOLVED Movies  Some Obscure Show - S01E02
FAILED   Movies  Inception (2010)  [target rejected with HTTP 500]
DONE     Movies  Restore complete`}
            </pre>
            <p style={{ marginTop: 8, fontSize: 12, color: 'var(--text-dim)' }}>
              PHASE lines mark transitions between data types. Per-item events fall
              under whichever PHASE is currently active. UNRESOLVED + FAILED both
              surface in <code>troubleshoot.log</code> and the per-library
              <code> fail_*.log</code>; check those for the categorised reason.
            </p>
          </>
        ) : labelView === 'phase' ? (
          <>
            <p style={{ marginTop: 0, fontSize: 12, color: 'var(--text-dim)' }}>
              Each row in the live panel reads <code>library · type · title · phase</code>.
              The examples below show the typical population of the panel at
              different points in a run. Read top-to-bottom as "this is what
              you'd see if you looked at the panel right now."
            </p>
            <p style={{ marginTop: 16 }}><strong>During a snapshot's playlist cache warm</strong>:</p>
            <pre style={{ background: 'var(--panel-alt, #1b2233)', padding: 10, fontSize: 12, lineHeight: 1.5, overflowX: 'auto' }}>
{`My Server · playlist · My Playlist         · fetching
My Server · playlist · Road Trip           · fetching
My Server · playlist · Chill Mix           · fetching
My Server · playlist · Top Hits            · fetching`}
            </pre>
            <p style={{ marginTop: 8, fontSize: 12, color: 'var(--text-dim)' }}>
              Owner + every home user's token gets one parallel
              <code> build_playlist_cache</code> worker, so on a server with
              the owner + five managed users you'll see up to six rows here.
              Each row swaps title as that worker advances through its
              playlist list. This is the slow part of a snapshot's pre-flight.
            </p>

            <p style={{ marginTop: 16 }}><strong>During a snapshot's per-library gather</strong>:</p>
            <pre style={{ background: 'var(--panel-alt, #1b2233)', padding: 10, fontSize: 12, lineHeight: 1.5, overflowX: 'auto' }}>
{`Music    · track      · The Long Way Down       · capturing   (watch_history)
Music    · track       · Echoes                  · capturing   (ratings)
Music    · playlist    · My Playlist             · capturing   (playlists)
Music    · collection  · Greatest Hits           · capturing   (collections)`}
            </pre>
            <p style={{ marginTop: 8, fontSize: 12, color: 'var(--text-dim)' }}>
              The four gathers run in parallel inside <code>snapshot_library</code>,
              so during a snapshot you'll typically see 4 violet
              <code className="tag capturing" style={{ marginLeft: 4 }}>capturing</code>
              rows per active library. Violet is the snapshot-only colour - if you
              see it during an restore you're looking at the wrong job.
            </p>

            <p style={{ marginTop: 16 }}><strong>During an restore or direct transfer</strong>:</p>
            <pre style={{ background: 'var(--panel-alt, #1b2233)', padding: 10, fontSize: 12, lineHeight: 1.5, overflowX: 'auto' }}>
{`Music    · scan_cache  · Building filepath index for 'Music' · indexing
Movies   · movie        · The Matrix (1999)                   · resolving
Movies   · movie        · Inception (2010)                    · scrobbling
Music    · track        · Daft Punk - One More Time           · rating
Music    · playlist     · My Playlist                         · merging`}
            </pre>
            <p style={{ marginTop: 8, fontSize: 12, color: 'var(--text-dim)' }}>
              <code className="tag phase" style={{ marginRight: 2 }}>resolving</code>
              and write-side phases
              (<code className="tag merged" style={{ marginLeft: 2, marginRight: 2 }}>scrobbling</code>
              for watch history,
              <code className="tag rated" style={{ marginLeft: 2, marginRight: 2 }}>rating</code>
              for stars,
              <code className="tag appended" style={{ marginLeft: 2, marginRight: 2 }}>merging</code>
              for playlists / collections) alternate per worker - one row may
              flip between them every few hundred milliseconds.
              <code className="tag phase" style={{ marginLeft: 4, marginRight: 4 }}>indexing</code>
              appears exactly once per library section the first time a Tier-2
              filepath fallback is needed, and stays on screen for tens of
              seconds on big music libraries.
            </p>
            <p style={{ marginTop: 12, fontSize: 12, color: 'var(--text-dim)' }}>
              An empty Currently Processing panel during a running job is not a
              hang. It just means every worker is in between phases right now.
              The Activity Feed will keep ticking; refresh your eyes to a
              different panel if you're worried.
            </p>
          </>
        ) : (
          <>
            <p style={{ marginTop: 0, fontSize: 12, color: 'var(--text-dim)' }}>
              The dashboard's Thread Pool panel shows one pill per active
              category with the form <code>Label × count - description</code>.
              The examples below show the typical pill set at different
              points in a run.
            </p>

            <p style={{ marginTop: 16 }}><strong>During a snapshot run</strong>:</p>
            <pre style={{ background: 'var(--panel-alt, #1b2233)', padding: 10, fontSize: 12, lineHeight: 1.5, overflowX: 'auto' }}>
{`Active workers: 14   Status: Running

Capturing snapshot × 2   reading and serialising each library
Playlists           × 6   per-token playlist enumeration
Watched             × 4   walking watched items
Ratings             × 1   reading user star ratings
Home User           × 1   per-managed-user gather`}
            </pre>
            <p style={{ marginTop: 8, fontSize: 12, color: 'var(--text-dim)' }}>
              The <strong>Capturing snapshot</strong> count tracks how many
              libraries are in flight at once. <strong>Playlists</strong>
              dominates the pool during a fresh snapshot because the
              pre-flight playlist-cache warm runs one worker per source
              token (owner + each home user).
            </p>

            <p style={{ marginTop: 16 }}><strong>During a restore / restore run</strong>:</p>
            <pre style={{ background: 'var(--panel-alt, #1b2233)', padding: 10, fontSize: 12, lineHeight: 1.5, overflowX: 'auto' }}>
{`Active workers: 18   Status: Running

Scan Cache  × 1    building the destination filepath index
Watched     × 8    matching watch-history items against the destination
Playlists   × 6    creating or appending playlists on the destination
Ratings     × 2    writing star ratings
Home User   × 1    restoring one managed user's data`}
            </pre>
            <p style={{ marginTop: 8, fontSize: 12, color: 'var(--text-dim)' }}>
              <strong>Scan Cache</strong> appears exactly once per library at
              the top of the restore phase. Sub-workers don't start writing
              until the scan cache is ready, so the Watched / Playlists
              counts may spike right after the Scan Cache pill disappears.
            </p>

            <p style={{ marginTop: 12, fontSize: 12, color: 'var(--text-dim)' }}>
              Hover any pill on the dashboard to see the same description
              as a tooltip. The pill cursor flips to the <code>help</code>
              variant when hovered so the tooltip affordance is
              discoverable without reading the help page first.
            </p>
          </>
        )}
      </div>
    </>
  );
}


// ── Behind the Scenes ────────────────────────────────────────────────────────
//
// Audit page documenting every Plex-side call the codebase makes. Each
// entry carries a documentation link so an operator can verify what the
// call actually does on the Plex side. No tokens, URLs, or other
// install-specific values are surfaced - only the public method /
// endpoint names that anyone can look up regardless of which Plex
// install they're running.

const _PLEXAPI = 'https://python-plexapi.readthedocs.io/en/latest/';
const _COMMUNITY = 'https://plexapi.dev/';

interface ApiCall {
  name: string;       // python-plexapi method name OR HTTP endpoint path
  href: string;       // documentation URL
  source: 'plexapi' | 'community';
  what: string;       // plain-English "what it does on the Plex side"
  why: string;        // why we use it / where it fires in our codebase
  note?: string;      // optional design-decision callout
}

interface ApiGroup {
  title: string;
  intro: string;
  calls: ApiCall[];
}

const API_GROUPS: ApiGroup[] = [
  {
    title: 'Connecting & identifying servers',
    intro:
      "Every job needs an authenticated connection to a Plex server. We open it once per run, then hold the connection for the rest of the run rather than reconnecting per request, because Plex itself rate-limits new connections more aggressively than re-used ones.",
    calls: [
      {
        name: 'PlexServer(url, token, timeout=120)',
        href: _PLEXAPI + 'modules/server.html#plexapi.server.PlexServer',
        source: 'plexapi',
        what: 'Opens an authenticated session to one Plex server using the URL and the operator-supplied auth token.',
        why: 'Every snapshot, restore, and direct-transfer job starts here. The 120-second timeout is deliberate: large Plex servers can take 30+ seconds to first-respond on the very first request of a run while the server warms its caches.',
      },
      {
        name: 'server.friendlyName  /  server.version',
        href: _PLEXAPI + 'modules/server.html#plexapi.server.PlexServer',
        source: 'plexapi',
        what: 'Reads the human-readable name and version the server reports about itself.',
        why: "Used to label every run log and snapshot file with which server they came from. The version helps when a future Plex update changes a behaviour we depend on, since the run log records which version produced the file.",
      },
      {
        name: 'server.machineIdentifier',
        href: _PLEXAPI + 'modules/server.html#plexapi.server.PlexServer',
        source: 'plexapi',
        what: 'A stable, unique identifier the Plex server generates for itself.',
        why: 'We key the registry on this instead of the friendly name, so renaming a server in Plex does not look like a brand-new server to us. Also drives the cross-server resolver cache.',
        note: 'Plex never changes a server\'s machineIdentifier unless the operator wipes Plex\'s preferences directory. It is the only Plex-side identifier safe to use as a foreign key.',
      },
    ],
  },
  {
    title: 'Reading users on a server',
    intro:
      "Plex servers can have a Plex-Home admin plus several managed (home) users. Each user can have their own watch history, ratings, playlists, and collections. We enumerate them up front so the snapshot covers every user that exists on the server.",
    calls: [
      {
        name: 'server.systemAccounts()',
        href: _PLEXAPI + 'modules/server.html#plexapi.server.PlexServer.systemAccounts',
        source: 'plexapi',
        what: 'Returns the list of accounts known to this Plex server (admin + every managed user), each with a local numeric user-id.',
        why: 'We map each home user\'s name to that local user-id so we can ask "which playlists were actually CREATED by this user" rather than "which playlists can this user SEE."',
        note: 'Without this, Plex\'s playlist response under a user\'s token includes every playlist that has been shared TO them, and counting them once per recipient would inflate a typical run\'s playlist work by 5x.',
      },
      {
        name: 'server.myPlexAccount() · account.users()',
        href: _PLEXAPI + 'modules/myplex.html#plexapi.myplex.MyPlexAccount.users',
        source: 'plexapi',
        what: 'Reaches out to plex.tv (not the local server) to list the Plex-Home managed users on the admin account.',
        why: 'A snapshot run gathers each managed user\'s personal data in parallel. This call gives us the list of who to gather.',
      },
    ],
  },
  {
    title: 'Browsing the library',
    intro:
      "Plex organises content into sections (Movies, TV, Music, Audio-Books). Each section type uses a slightly different walk because the right \"leaf\" object is different: Movies has Movie leaves, TV has Episode leaves, Music has Track leaves.",
    calls: [
      {
        name: 'server.library.sections()',
        href: _PLEXAPI + 'modules/library.html#plexapi.library.Library.sections',
        source: 'plexapi',
        what: 'Returns every library on the server.',
        why: 'Powers the library picker in the Run Job form and is the entry point of every snapshot run.',
      },
      {
        name: 'section.all()',
        href: _PLEXAPI + 'modules/library.html#plexapi.library.LibrarySection.all',
        source: 'plexapi',
        what: 'Walks every top-level entry in a section: Movies for a movie library, Shows for a TV library, Artists for a music library.',
        why: 'Used by the library-walk maintenance job to confirm which items are still present (Rule 2 / last-seen timestamps).',
      },
      {
        name: 'section.searchTracks() · section.searchEpisodes()',
        href: _PLEXAPI + 'modules/library.html#plexapi.library.MusicSection.searchTracks',
        source: 'plexapi',
        what: 'Walks Music sections at the Track level and TV sections at the Episode level, the actual playable leaves, not the parent Artist / Show objects.',
        why: "We need leaves because watch history and filepath matching are keyed at the playable file. section.all() on a Music library returns Artists, which don't carry a viewCount or a file path, useless for our purposes.",
      },
      {
        name: 'section.search(viewCount__gt=0)',
        href: _PLEXAPI + 'modules/library.html#plexapi.library.LibrarySection.search',
        source: 'plexapi',
        what: "Filtered walk that asks Plex's server-side search to return only items that have been watched at least once.",
        why: "Massive speed win on large libraries. Walking everything and filtering client-side means downloading metadata for 50,000 items just to keep the 800 you care about; Plex's filter does it server-side and returns just the 800.",
      },
      {
        name: 'section.search(userRating__gt=0)',
        href: _PLEXAPI + 'modules/library.html#plexapi.library.LibrarySection.search',
        source: 'plexapi',
        what: 'Filtered walk, only items the user has rated with at least one star.',
        why: 'Same speed reason as above. Rated items are almost always a small minority; filtering at the source means we skip ~99% of the library.',
      },
      {
        name: 'section.collections()',
        href: _PLEXAPI + 'modules/library.html#plexapi.library.LibrarySection.collections',
        source: 'plexapi',
        what: 'Returns every collection in this library section.',
        why: "Snapshot pipeline reads this once per (library, user) pair. For managed users we then subtract the admin's collection set so a server-wide collection visible to everyone isn't captured five times.",
        note: 'Collection visibility in Plex is per-user but Plex returns the WHOLE list under any user\'s token. The deduplication is what makes the per-user gather correct. See the "skip_rating_keys" guard in services/snapshotter.py.',
      },
    ],
  },
  {
    title: 'Finding the same item on a different server',
    intro:
      "A snapshot captured on Server A needs to find the same items on Server B during an restore or direct transfer. Plex doesn't promise that the same movie has the same ratingKey on two installs, so we use four progressively-looser matching strategies, called the tier system. Each tier costs more than the one before it; we stop as soon as one works.",
    calls: [
      {
        name: 'server.fetchItem(ratingKey)',
        href: _PLEXAPI + 'modules/server.html#plexapi.server.PlexServer.fetchItem',
        source: 'plexapi',
        what: 'Direct lookup by per-server ratingKey. The fastest possible item fetch: one HTTP call, no search.',
        why: 'Tier 0 of the resolver. After the first successful match we cache "this GUID on this server has this ratingKey" in our own database. On the next run we go straight to fetchItem and skip the slow GUID search entirely.',
        note: 'This is the optimisation that makes re-runs roughly an order of magnitude faster than first runs on the same server pair.',
      },
      {
        name: 'server.library.getByGuid(guid)',
        href: _PLEXAPI + 'modules/library.html#plexapi.library.Library.getByGuid',
        source: 'plexapi',
        what: 'Asks Plex to find an item by its global GUID (imdb://, tmdb://, tvdb://, musicbrainz://, plex://). Walks every library section until one matches.',
        why: 'Tier 1 of the resolver, the most reliable cross-server identity check. plex:// and musicbrainz:// GUIDs mean the same item on every Plex install, so a match here is essentially guaranteed correct.',
      },
      {
        name: 'section.search(filters={"media.part.file": filepath})',
        href: _PLEXAPI + 'modules/library.html#plexapi.library.LibrarySection.search',
        source: 'plexapi',
        what: 'Asks Plex to find an item whose underlying media file is at a specific path on disk.',
        why: 'Tier 2 of the resolver. Useful when both servers share the same media storage but have different ratingKeys (e.g. one server was rebuilt from scratch). The pre-built scan_cache is what makes the suffix-match variant feasible. Without it we\'d be running this query per item.',
      },
      {
        name: 'section.search(title=..., libtype=...)',
        href: _PLEXAPI + 'modules/library.html#plexapi.library.LibrarySection.search',
        source: 'plexapi',
        what: 'Plain title search, scoped to a specific media type (track / episode / movie / show).',
        why: 'Tier 3 of the resolver, last-resort fallback. Can produce wrong matches when two items share a title, so it is OFF by default for direct transfers and any hit gets a yellow warning banner on the Dashboard.',
        note: 'The Transfer Resolution settings let you toggle Tier 2 and Tier 3 independently. Snapshot and restore paths always run all four tiers, since those use the JSON archive as the source of truth, so a wrong fuzzy match is worth less risk.',
      },
    ],
  },
  {
    title: 'Playlists',
    intro:
      "Playlists in Plex are ordered references to other items. They don't contain the media themselves, just pointers. Snapshot captures the pointers; restore resolves them on the destination and rebuilds the playlist.",
    calls: [
      {
        name: 'server.playlists()',
        href: _PLEXAPI + 'modules/server.html#plexapi.server.PlexServer.playlists',
        source: 'plexapi',
        what: 'Lists every playlist visible to the token being used. Includes both playlists this user created AND playlists shared TO them.',
        why: 'We call this once per token (admin + each home user) at the start of a snapshot. The cache is then reused across every library. Without that, a four-library snapshot on a six-user server would call it 24 times instead of six.',
        note: 'The cache is why the snapshot run shows a "Warming playlist cache for X" line at the top. That\'s when this fires for each connection in parallel.',
      },
      {
        name: 'playlist.items()',
        href: _PLEXAPI + 'modules/playlist.html#plexapi.playlist.Playlist.items',
        source: 'plexapi',
        what: 'Reads one playlist\'s members. One HTTP round-trip per playlist.',
        why: "This is the slow part of snapshot capture for playlist-heavy servers. Each call is small but cumulative: 200 playlists × 100ms each = 20 seconds. We parallelise across connections and surface each fetch as a 'fetching' row in the Currently Processing panel so the operator sees progress.",
      },
      {
        name: 'Playlist.create(server, name, items=...)',
        href: _PLEXAPI + 'modules/playlist.html#plexapi.playlist.Playlist.create',
        source: 'plexapi',
        what: 'Creates a new playlist on the destination server with the resolved member items.',
        why: 'Used by the restorer when the playlist name does not already exist on the destination. We chunk the items list at 100 members per call, because Plex builds a long query string from the list and over ~200 members the URL exceeds web server limits.',
        note: 'Smart playlists are NOT created here. Their filter URL contains the source server\'s library section IDs, which never line up with the destination. Smart playlists are logged for manual recreation instead of being silently broken.',
      },
      {
        name: 'playlist.addItems(items)',
        href: _PLEXAPI + 'modules/playlist.html#plexapi.playlist.Playlist.addItems',
        source: 'plexapi',
        what: 'Appends items to an existing playlist.',
        why: "Used by the restorer when the playlist already exists on the destination. We compare members by ratingKey and only add the ones the destination is missing, never overwrite, never reorder, never remove. Same 100-item chunking applies for the same URL-length reason.",
      },
    ],
  },
  {
    title: 'Collections',
    intro:
      "Collections are similar to playlists but unordered, a group of items that belong together (a TV showrunner's complete works, a genre bundle). Plex returns the same collection list under every user's token, so deduplication matters.",
    calls: [
      {
        name: 'collection.items()',
        href: _PLEXAPI + 'modules/collection.html#plexapi.collection.Collection.items',
        source: 'plexapi',
        what: 'Reads one collection\'s members.',
        why: 'Used during snapshot capture. For per-user gathers we skip this entirely when the collection is in the admin\'s rating-key set. That means it is server-wide and we already captured it once.',
      },
      {
        name: 'Collection.create(server, name, section, items=...)',
        href: _PLEXAPI + 'modules/collection.html#plexapi.collection.Collection.create',
        source: 'plexapi',
        what: 'Creates a new collection in a specific library section.',
        why: "Used by the restorer when the collection does not exist on the destination. Same 100-item chunking as playlists for the same Plex-URL-length reason.",
      },
      {
        name: 'collection.addItems(items)',
        href: _PLEXAPI + 'modules/collection.html#plexapi.collection.Collection.addItems',
        source: 'plexapi',
        what: 'Adds members to an existing collection.',
        why: 'Additive merge, same rules as playlist append. Never removes, never reorders.',
      },
    ],
  },
  {
    title: 'Writing back to Plex (the four direct HTTP endpoints)',
    intro:
      'Three operations bypass python-plexapi and call the underlying HTTP endpoints directly: viewCount updates, in-progress play positions, and star ratings. python-plexapi wraps these but the wrappers re-fetch the item after each call to confirm the new state, and we issue thousands of these per restore, so the round-trip overhead matters.',
    calls: [
      {
        name: 'PUT /:/scrobble?key={ratingKey}',
        href: _COMMUNITY,
        source: 'community',
        what: 'Increments a single item\'s viewCount by one. Equivalent to "marking as watched."',
        why: 'Used to advance the destination\'s viewCount up to the stored value from the snapshot. Capped per item at 50 scrobbles per run so a corrupt snapshot count of 9999 cannot hammer the server.',
        note: 'Plex has no API to SET a viewCount directly, only to increment it. That is why the restorer fires N scrobble calls per item where N is (stored count − target count).',
      },
      {
        name: 'PUT /:/progress?key={ratingKey}&time={ms}',
        href: _COMMUNITY,
        source: 'community',
        what: 'Sets a play position offset in milliseconds. "Resume from here" data.',
        why: 'When the snapshot records a partially-watched item, we restore the same resume point on the destination so the user picks up where they left off. Only fires when the destination\'s current offset is zero. Never overwrites a more recent resume point.',
      },
      {
        name: 'PUT /:/rate?key={ratingKey}&rating={value}',
        href: _COMMUNITY,
        source: 'community',
        what: 'Sets a star rating on an item.',
        why: 'Used to restore star ratings. Additive policy: if the destination item already has a rating, we never overwrite it: the operator deliberately set the destination value and we treat it as more recent than the snapshot.',
      },
    ],
  },
  {
    title: 'Library refresh (after writes)',
    intro:
      "Plex keeps an internal index of every section. Writes to the index (creating a playlist, adding a collection member) are picked up live, but very large operations can leave Plex's cached counts mid-update. We do NOT trigger refreshes from this app. Plex's automatic indexing handles it, and a forced refresh during a multi-thousand-item restore could cause the destination's UI to lag for minutes.",
    calls: [],
  },
];

function ApiUsagePage() {
  // Nested sub-tab: which service's API surface to render. Plex is
  // the only one shipped; Emby and Jellyfin are placeholders so the
  // structure is visible while those integrations are still on the
  // roadmap.
  const [service, setService] = useState<'plex' | 'emby' | 'jellyfin'>('plex');
  return (
    <>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>API Usage</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12 }}>
          Every call the app makes against a media-server's API, what
          it does on that server, and why we use it. Method names
          link to official documentation where the vendor publishes
          one. Pick a service below.
        </span>
        <nav className="tabs sub-tabs" style={{ marginTop: 12 }}>
          <button
            className={service === 'plex' ? 'active' : ''}
            onClick={() => setService('plex')}
          >
            Plex
          </button>
          <button
            className={service === 'emby' ? 'active' : ''}
            onClick={() => setService('emby')}
          >
            Emby (WIP)
          </button>
          <button
            className={service === 'jellyfin' ? 'active' : ''}
            onClick={() => setService('jellyfin')}
          >
            Jellyfin (WIP)
          </button>
        </nav>
      </div>

      {service === 'plex' && <PlexApiSection />}
      {service === 'emby' && <ServiceWipPlaceholder name="Emby" />}
      {service === 'jellyfin' && <ServiceWipPlaceholder name="Jellyfin" />}
    </>
  );
}

function ServiceWipPlaceholder({ name }: { name: string }) {
  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>{name}: Work in progress</h2>
      <p style={{ fontSize: 13, color: 'var(--text-dim)' }}>
        {name} integration is on the roadmap. When it ships, this tab
        will document every {name}-side call the app makes the same
        way the Plex tab does today: method or endpoint names,
        descriptions of what they do on the {name} side, and the
        reasoning behind each call's place in the snapshot / restore
        / direct-transfer pipelines.
      </p>
      <p style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 12 }}>
        No {name} calls are made by the app today. Job forms reject
        unregistered {name} servers, and the registry refuses to
        save servers whose service field is anything other than
        <code> plex</code>.
      </p>
    </div>
  );
}

function PlexApiSection() {
  return (
    <>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Plex</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12 }}>
          Method names link to the official
          <strong> python-plexapi</strong> documentation.
          The three direct HTTP endpoints
          (<code>/:/scrobble</code>, <code>/:/progress</code>,
          <code> /:/rate</code>) link to a community reference because
          Plex does not publish an official API doc for those.
        </span>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginTop: 8 }}>
          The app makes no calls outside this list. No tokens, URLs,
          or other install-specific values appear here, only the
          public method and endpoint names anyone can verify against
          a fresh Plex install.
        </span>
      </div>

      {API_GROUPS.map((group) => (
        <div key={group.title} className="panel">
          <h2 style={{ marginTop: 0 }}>{group.title}</h2>
          <p style={{ fontSize: 13, color: 'var(--text-dim)', marginTop: 0 }}>
            {group.intro}
          </p>
          {group.calls.length === 0 ? (
            <p style={{ fontSize: 12, color: 'var(--text-dim)', fontStyle: 'italic' }}>
              No calls in this category. That is intentional. See the intro
              above for why.
            </p>
          ) : (
            <table className="list" style={{ width: '100%' }}>
              <thead>
                <tr>
                  <th style={{ width: '40%' }}>Call</th>
                  <th>What it does · Why we use it</th>
                </tr>
              </thead>
              <tbody>
                {group.calls.map((c) => (
                  <tr key={c.name}>
                    <td>
                      <a
                        href={c.href}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="mono"
                        style={{
                          fontSize: 12,
                          textDecoration: 'none',
                          color: 'var(--color-phase, #58a6ff)',
                        }}
                      >
                        {c.name}
                      </a>
                      <div style={{ fontSize: 10, color: 'var(--text-dim)', marginTop: 4 }}>
                        {c.source === 'plexapi'
                          ? 'python-plexapi (official)'
                          : 'community-maintained reference'}
                      </div>
                    </td>
                    <td>
                      <div style={{ fontSize: 13 }}>{c.what}</div>
                      <div style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 6 }}>
                        {c.why}
                      </div>
                      {c.note && (
                        <div
                          style={{
                            fontSize: 11,
                            color: 'var(--text-dim)',
                            marginTop: 6,
                            paddingLeft: 8,
                            borderLeft: '2px solid var(--color-phase, #58a6ff)',
                            fontStyle: 'italic',
                          }}
                        >
                          <strong>Design note: </strong>{c.note}
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ))}
    </>
  );
}


// ── DB Schema ────────────────────────────────────────────────────────────────
//
// Educational page covering every SQLite database the app owns: how
// each one is laid out, why we picked that layout, the read and write
// surfaces, and the lifecycle. Authored long-form on purpose so a
// reader can build a mental model of the data side of the app from
// scratch.
//
// Three views, switchable via a sub-tab strip:
//
//   * Quick View     - key columns only, scannable at a glance
//   * In-depth       - every column, design rationale, function lists
//   * Source Links   - pointers to the source files for verification

interface DbColumn {
  name: string;
  type: string;          // SQLite type (TEXT / INTEGER / REAL)
  note: string;          // what this column carries
}

interface DbTableDoc {
  name: string;
  purpose: string;
  key_columns: string[];
  columns: DbColumn[];
  design_note?: string;
}

interface FunctionRef {
  name: string;
  module: string;
  description: string;
}

interface DbDoc {
  filename: string;             // friendly name
  filepath: string;             // canonical path
  intro_short: string;          // 1-2 sentences for Quick View
  intro_long: string;           // multi-paragraph for In-depth
  tables: DbTableDoc[];
  write_path: FunctionRef[];
  read_path: FunctionRef[];
  lifecycle: string;
  source_schema: string;        // "server/media_db.py :: _MIGRATIONS"
  source_init: string;
}

const DB_DOCS: DbDoc[] = [
  // ── media.db ────────────────────────────────────────────────────
  {
    filename: 'media.db',
    filepath: 'server_data/media.db',
    intro_short:
      "The cumulative store. Every snapshot job writes a side-effect copy of its live-fetch payload here so future runs can use it as a fast resolver cache. Keyed by GUID at the item level, by (item, server) at the per-server level.",
    intro_long:
      "media.db is the longest-lived SQLite file the app owns. It accumulates state from every snapshot, restore, and direct-transfer run, so on the second run of the same source server the resolver can skip the slow GUID-lookup round-trip and go straight to the cached ratingKey. The schema is split into three concerns: a GUID-keyed item pool that's shared across every server the app has ever talked to, per-(item, server) join tables that pin each item's local identity on each server, and per-server activity tables (watch events, ratings, playlist members, collection members) that record what each user on each server has done with each item.\n\nThe split matters because Plex's `ratingKey` is a per-server identifier: the same movie has different ratingKeys on Server A and Server B, but the same `imdb://` GUID on both. We key the items table by GUID so cross-server matching is structural rather than discovered each run.",
    tables: [
      {
        name: 'schema_version',
        purpose: "Tracks which migrations have been applied so we don't run them twice.",
        key_columns: ['version', 'applied_at'],
        columns: [
          { name: 'version', type: 'INTEGER PRIMARY KEY', note: 'The migration number (1, 2, 3, ...).' },
          { name: 'applied_at', type: 'REAL', note: 'Unix timestamp when the migration ran.' },
        ],
      },
      {
        name: 'servers',
        purpose: 'One row per registered Plex server. Mirrors a subset of the JSON registry so SQL joins can reference servers natively.',
        key_columns: ['id', 'name', 'service', 'machine_id'],
        columns: [
          { name: 'id', type: 'TEXT PRIMARY KEY', note: 'Internal UUID assigned when the server is registered.' },
          { name: 'name', type: 'TEXT NOT NULL', note: 'Friendly name shown in the UI.' },
          { name: 'service', type: "TEXT NOT NULL DEFAULT 'plex'", note: 'Always "plex" today. Future Emby / Jellyfin integrations will reuse this column.' },
          { name: 'url', type: 'TEXT NOT NULL', note: "The server's base URL." },
          { name: 'machine_id', type: 'TEXT', note: "Plex's stable machineIdentifier. Lets us detect a renamed server vs a brand-new one." },
          { name: 'added_at', type: 'REAL NOT NULL', note: 'When the server was first registered.' },
        ],
        design_note:
          "We deliberately duplicate this row from servers.json into media.db rather than just storing the id. SQLite can't reach outside the .db file for a JOIN, so without the in-DB row we'd be running registry lookups in tight loops. The id stays canonical; everything else is a denormalised copy refreshed by upsert_server_row whenever the registry row changes.",
      },
      {
        name: 'items',
        purpose: 'The GUID-keyed pool. Every Plex item the app has ever seen, identified by its global GUIDs (IMDb / TMDB / TVDB / MusicBrainz / Plex).',
        key_columns: ['id', 'imdb_id', 'tmdb_id', 'tvdb_id', 'musicbrainz_id', 'plex_guid', 'title', 'last_seen_at'],
        columns: [
          { name: 'id', type: 'INTEGER PRIMARY KEY AUTOINCREMENT', note: "Internal id. Referenced by every per-(item, server) table." },
          { name: 'imdb_id', type: 'TEXT', note: 'The payload part of an `imdb://...` GUID (e.g. tt0133093).' },
          { name: 'tmdb_id', type: 'TEXT', note: 'TMDB id payload.' },
          { name: 'tvdb_id', type: 'TEXT', note: 'TVDB id payload.' },
          { name: 'musicbrainz_id', type: 'TEXT', note: 'MusicBrainz id payload. Music libraries use this almost exclusively.' },
          { name: 'plex_guid', type: 'TEXT', note: "Plex's own internal GUID. Stable across installs once Plex has matched the item." },
          { name: 'title', type: 'TEXT NOT NULL', note: 'Item title as Plex reports it.' },
          { name: 'media_type', type: 'TEXT NOT NULL', note: 'movie / episode / track / show / album / artist.' },
          { name: 'year', type: 'INTEGER', note: 'Release year when known.' },
          { name: 'filepath_suffix', type: 'TEXT', note: 'The trailing path components Plex reports for this item, used by the Tier 2 resolver fallback.' },
          { name: 'created_at', type: 'REAL', note: 'Unix timestamp the row was first inserted.' },
          { name: 'updated_at', type: 'REAL', note: 'Last touch (any field). Refreshed by upsert_item on every sighting.' },
          { name: 'last_seen_at', type: 'REAL', note: 'When the background library walk last confirmed this item present on at least one server. Drives the Prune Missing Items action.' },
        ],
        design_note:
          "Identity is by GUID, not by ratingKey. Plex assigns a fresh ratingKey when an item is re-added to a library; if we keyed by ratingKey, every re-add would look like a brand new item. By keying on GUID we follow the item's actual identity across server moves, library rebuilds, and Plex match changes. The lookup_item_by_guids function walks the columns in priority order (IMDb -> TMDB -> TVDB -> MusicBrainz -> Plex) and returns the first match.",
      },
      {
        name: 'server_items',
        purpose: 'The (item.id, server.id, ratingKey) join. Lets the resolver skip the slow GUID search on the second visit to the same item on the same server.',
        key_columns: ['item_id', 'server_id', 'rating_key', 'last_seen_at'],
        columns: [
          { name: 'item_id', type: 'INTEGER NOT NULL', note: 'FK into items.id.' },
          { name: 'server_id', type: 'TEXT NOT NULL', note: 'FK into servers.id.' },
          { name: 'rating_key', type: 'INTEGER NOT NULL', note: "Plex's local item id on this server. The value passed to fetchItem()." },
          { name: 'updated_at', type: 'REAL NOT NULL', note: 'When we last confirmed the mapping by either ingesting a payload or fetching the item.' },
          { name: 'last_seen_at', type: 'REAL', note: 'When the library walk last confirmed the item present on this server.' },
        ],
        design_note:
          "This table is what makes Tier 0 of the resolver work. Without it, every cross-server lookup would walk the GUID columns; with it, we go from items.id straight to the ratingKey on the target server in one indexed lookup. Two UNIQUE constraints: (item_id, server_id) prevents duplicate rows, (server_id, rating_key) lets us invert the lookup when we have the ratingKey and need the items.id.",
      },
      {
        name: 'watch_events',
        purpose: "Per-(item, server, user) watch state. One row per item per user per server.",
        key_columns: ['item_id', 'server_id', 'user_handle', 'view_count', 'last_viewed_at'],
        columns: [
          { name: 'id', type: 'INTEGER PRIMARY KEY AUTOINCREMENT', note: 'Internal id.' },
          { name: 'item_id', type: 'INTEGER NOT NULL', note: 'FK into items.id.' },
          { name: 'server_id', type: 'TEXT NOT NULL', note: 'Which server saw the watch.' },
          { name: 'user_handle', type: "TEXT NOT NULL DEFAULT ''", note: 'Owner or managed-user identifier. Empty string = server owner.' },
          { name: 'view_count', type: 'INTEGER NOT NULL DEFAULT 0', note: 'Total times the user has played this item.' },
          { name: 'view_offset', type: 'INTEGER NOT NULL DEFAULT 0', note: 'Last resume position in milliseconds.' },
          { name: 'last_viewed_at', type: 'REAL', note: 'Unix timestamp of the last play.' },
          { name: 'updated_at', type: 'REAL NOT NULL', note: 'When we last wrote this row.' },
        ],
      },
      {
        name: 'ratings',
        purpose: 'Per-(item, server, user) star ratings.',
        key_columns: ['item_id', 'server_id', 'user_handle', 'rating'],
        columns: [
          { name: 'id', type: 'INTEGER PRIMARY KEY AUTOINCREMENT', note: 'Internal id.' },
          { name: 'item_id', type: 'INTEGER NOT NULL', note: 'FK into items.id.' },
          { name: 'server_id', type: 'TEXT NOT NULL', note: 'Which server holds the rating.' },
          { name: 'user_handle', type: "TEXT NOT NULL DEFAULT ''", note: 'Owner or managed-user identifier.' },
          { name: 'rating', type: 'REAL NOT NULL', note: 'Plex rating value (0.0 to 10.0). UI divides by 2 to display 0-5 stars.' },
          { name: 'updated_at', type: 'REAL NOT NULL', note: 'When this rating was last seen / written.' },
        ],
      },
      {
        name: 'playlists',
        purpose: 'Per-(server, user) playlist rows. Membership is denormalised into item_ids_json so we can read the whole list in one query.',
        key_columns: ['server_id', 'user_handle', 'name', 'item_ids_json', 'is_smart'],
        columns: [
          { name: 'id', type: 'INTEGER PRIMARY KEY AUTOINCREMENT', note: 'Internal id.' },
          { name: 'server_id', type: 'TEXT NOT NULL', note: 'Which server owns the playlist.' },
          { name: 'user_handle', type: "TEXT NOT NULL DEFAULT ''", note: 'Empty string = server-wide playlist; username = user-private playlist.' },
          { name: 'name', type: 'TEXT NOT NULL', note: 'Playlist title.' },
          { name: 'description', type: 'TEXT', note: 'Optional summary text.' },
          { name: 'is_smart', type: 'INTEGER NOT NULL DEFAULT 0', note: '1 if this is a smart playlist (filter-based, no static members).' },
          { name: 'smart_filter_json', type: 'TEXT', note: 'The smart-playlist filter URL when is_smart=1. Server-local and not portable.' },
          { name: 'item_ids_json', type: 'TEXT', note: 'JSON array of items.id values. Ordered. Empty for smart playlists.' },
          { name: 'updated_at', type: 'REAL NOT NULL', note: 'When this row was last written.' },
        ],
        design_note:
          "We denormalise membership into a JSON array on purpose. Playlists are read as whole lists, never queried by single member, so a normalised playlist_items join table would force a JOIN on every read with no upside. UNIQUE(server_id, user_handle, name) lets a user have a private playlist with the same name as a server-wide one without collision.",
      },
      {
        name: 'collections',
        purpose: 'Same shape as playlists but for collections. Unordered.',
        key_columns: ['server_id', 'user_handle', 'name', 'item_ids_json'],
        columns: [
          { name: 'id', type: 'INTEGER PRIMARY KEY AUTOINCREMENT', note: 'Internal id.' },
          { name: 'server_id', type: 'TEXT NOT NULL', note: 'Which server owns the collection.' },
          { name: 'user_handle', type: "TEXT NOT NULL DEFAULT ''", note: 'Empty string = server-wide; username = user-private (rare for collections).' },
          { name: 'name', type: 'TEXT NOT NULL', note: 'Collection title.' },
          { name: 'item_ids_json', type: 'TEXT', note: 'JSON array of items.id values.' },
          { name: 'updated_at', type: 'REAL NOT NULL', note: 'When this row was last written.' },
        ],
      },
      {
        name: 'managed_users',
        purpose: 'Per-server managed (home) user records. Carries encrypted credentials for the User Management panel.',
        key_columns: ['server_id', 'username', 'kind', 'has_token (derived)', 'tombstoned'],
        columns: [
          { name: 'id', type: 'INTEGER PRIMARY KEY AUTOINCREMENT', note: 'Internal id.' },
          { name: 'server_id', type: 'TEXT NOT NULL', note: 'Which server this user belongs to.' },
          { name: 'username', type: 'TEXT NOT NULL', note: 'The user identifier (email for owner, handle for managed users).' },
          { name: 'display_name', type: 'TEXT', note: 'Operator-chosen friendly name.' },
          { name: 'service_type', type: "TEXT NOT NULL DEFAULT 'plex'", note: 'plex / emby / jellyfin (only plex today).' },
          { name: 'kind', type: 'TEXT', note: "owner or managed." },
          { name: 'auth_token_enc', type: 'TEXT', note: "Fernet-encrypted auth token. Decrypted via server/secrets.py only when needed for an API call." },
          { name: 'plex_home_pin_enc', type: 'TEXT', note: "Encrypted Plex Home PIN." },
          { name: 'service_password_enc', type: 'TEXT', note: "Encrypted service password (for Emby / Jellyfin when those integrations land)." },
          { name: 'tombstoned', type: 'INTEGER NOT NULL DEFAULT 0', note: 'Per-server hide flag. 1 = hidden, credentials preserved.' },
          { name: 'last_seen', type: 'REAL', note: "When the user was last observed by a live API sync." },
          { name: 'created_at', type: 'REAL NOT NULL', note: 'When the row was first inserted.' },
          { name: 'updated_at', type: 'REAL NOT NULL', note: 'When the row was last written.' },
        ],
        design_note:
          "Fernet ciphertext lives inline in the .db rather than in a separate secrets file. The decryption key lives in server_data/.keyfile, separate from media.db. A export of media.db without .keyfile is unreadable for credential cells.",
      },
      {
        name: 'global_tombstones',
        purpose: 'Username-keyed hide list that crosses every registered server. The User Management panel\'s "hide globally" action writes here.',
        key_columns: ['username', 'tombstoned_at'],
        columns: [
          { name: 'username', type: 'TEXT PRIMARY KEY', note: 'A globally-tombstoned username.' },
          { name: 'tombstoned_at', type: 'REAL NOT NULL', note: 'When the global hide was applied.' },
        ],
      },
      {
        name: 'library_walks',
        purpose: 'Provenance log for the background library-walk maintenance job.',
        key_columns: ['server_id', 'started_at', 'status', 'items_seen'],
        columns: [
          { name: 'id', type: 'INTEGER PRIMARY KEY AUTOINCREMENT', note: 'Walk id.' },
          { name: 'server_id', type: 'TEXT NOT NULL', note: 'Which server was walked.' },
          { name: 'started_at', type: 'REAL NOT NULL', note: 'When the walk started.' },
          { name: 'finished_at', type: 'REAL', note: 'When the walk finished. NULL while running.' },
          { name: 'status', type: 'TEXT NOT NULL', note: 'running / completed / failed / cancelled.' },
          { name: 'items_seen', type: 'INTEGER NOT NULL DEFAULT 0', note: 'Items the walk ticked last_seen_at on.' },
          { name: 'libraries_seen', type: 'INTEGER NOT NULL DEFAULT 0', note: 'Library sections walked successfully.' },
          { name: 'error_message', type: 'TEXT', note: 'Failure detail when status != completed.' },
        ],
      },
    ],
    write_path: [
      { name: 'upsert_item', module: 'server/media_db.py', description: 'Insert or update one item by GUID. Called for every record the snapshot pipeline serialises.' },
      { name: 'record_server_item', module: 'server/media_db.py', description: 'Bind a (server, item, ratingKey) triple. Powers the resolver Tier-0 cache.' },
      { name: 'record_watch_event', module: 'server/media_db.py', description: 'Upsert a single (item, server, user) watch row.' },
      { name: 'upsert_rating', module: 'server/media_db.py', description: 'Upsert a single (item, server, user) rating.' },
      { name: 'upsert_playlist', module: 'server/media_db.py', description: 'Upsert a playlist row, replacing item_ids_json.' },
      { name: 'upsert_collection', module: 'server/media_db.py', description: 'Same as upsert_playlist for collections.' },
      { name: 'upsert_server_row', module: 'server/media_db.py', description: 'Sync one row from servers.json into the servers table.' },
      { name: 'ingest_snapshot_payload', module: 'server/media_db.py', description: 'The high-level entry point: walks an export_data dict and dispatches to every upsert above with the dedup discipline applied.' },
      { name: 'record_item_sighting', module: 'server/media_db.py', description: 'Tick last_seen_at on (item, server) when the library walk confirms presence.' },
      { name: 'prune_stale_items', module: 'server/media_db.py', description: 'Remove server_items, watch_events, ratings, and trim playlist / collection membership for items not seen recently.' },
      { name: 'purge_server_data', module: 'server/media_db.py', description: 'Cascade-remove every per-server row when a server is deleted from the registry.' },
    ],
    read_path: [
      { name: 'lookup_item_by_guids', module: 'server/media_db.py', description: 'GUID -> items.id lookup. Powers the resolver Tier 1.' },
      { name: 'find_rating_key_on_server', module: 'server/media_db.py', description: 'GUID + server_id -> ratingKey. Tier 0 of the resolver.' },
      { name: 'list_managed_users', module: 'server/media_db.py', description: "Read for the Servers -> User Management panel." },
      { name: 'list_stale_items / count_stale_items', module: 'server/media_db.py', description: 'Power the Prune Missing Items preview.' },
      { name: 'get_stats', module: 'server/media_db.py', description: 'Per-table row counts for the /api/db/stats endpoint.' },
    ],
    lifecycle:
      "Created on first server boot by init_media_db (FastAPI lifespan). Migrations apply automatically on every boot via _apply_migrations - new columns / tables land idempotently. Rows are added by every snapshot, restore, and direct-transfer run; rows are removed only by explicit operator action (server removal cascade, or the Prune Missing Items button in Settings). The file lives forever otherwise. Exports: the operator can copy media.db while the app is stopped, or use the sqlite3 .export command while it's running.",
    source_schema: 'server/media_db.py :: _MIGRATIONS (around lines 116-336)',
    source_init: 'server/media_db.py :: init_media_db (around line 338)',
  },

  // ── snapshots.db ────────────────────────────────────────────────
  {
    filename: 'snapshots.db',
    filepath: 'server_data/snapshots.db',
    intro_short:
      'The pointer registry. One row per snapshot .db file on disk: friendly name, server, capture timestamp, file path, captured types. The Exports panel reads this; the per-snapshot .db files are the actual artifacts.',
    intro_long:
      "snapshots.db is a small index over the snapshots/ directory. We keep the registry separate from the per-snapshot .db files for two reasons: it lets the Exports panel render the list without opening every .db, and it gives orphan reconciliation a place to detect dead rows (registry row exists but the file is gone) and orphan files (file exists but no registry row). The registry's row id is also baked into each .db's snapshot_meta table, so reconnecting a registry entry to its file is unambiguous even after a rename.",
    tables: [
      {
        name: 'snapshots',
        purpose: 'One row per snapshot artifact.',
        key_columns: ['id', 'server_id', 'server_name', 'snapshot_name', 'file_path', 'captured_at', 'libraries_json', 'captured_types_json'],
        columns: [
          { name: 'id', type: 'TEXT PRIMARY KEY', note: 'UUID, mirrored into the .db file\'s snapshot_meta.snapshot_id.' },
          { name: 'server_id', type: 'TEXT NOT NULL', note: "The registered server this snapshot is from." },
          { name: 'server_name', type: 'TEXT NOT NULL', note: "Friendly name at capture time. Used for filename + label." },
          { name: 'snapshot_name', type: 'TEXT NOT NULL', note: "Display name. Format: 'Server - Libraries - YYYY-MM-DD HH-MM' or '... (recovered)' for reconciled orphans." },
          { name: 'file_path', type: 'TEXT NOT NULL', note: "Absolute path to the .db file." },
          { name: 'captured_at', type: 'REAL NOT NULL', note: "Unix timestamp when the snapshot was captured." },
          { name: 'libraries_json', type: 'TEXT', note: "JSON array of library names captured in this snapshot." },
          { name: 'user_count', type: 'INTEGER', note: "How many distinct users had data in this snapshot." },
          { name: 'row_counts_json', type: 'TEXT', note: "Per-table row counts at capture time. Surfaced in the Exports panel." },
          { name: 'file_size', type: 'INTEGER', note: 'Size of the .db file in bytes.' },
          { name: 'prebuilt_json_path', type: 'TEXT', note: "Path to the cached .plexexport.json sidecar if one has been materialised. NULL when downloads stream-render on demand." },
          { name: 'captured_types_json', type: 'TEXT', note: "JSON array of which data types this run actually gathered (watch_history / ratings / playlists / collections). The restore UI gates its include_* toggles on this rather than row_counts because row_counts reflects cumulative media.db state, not just what THIS run touched." },
        ],
        design_note:
          "The row id matches snapshot_meta.snapshot_id inside the .db file. That redundancy is intentional: if either side gets corrupted, the other side has the truth, and reconcile_orphaned_snapshots() can heal the split.",
      },
    ],
    write_path: [
      { name: 'register', module: 'server/snapshot_registry.py', description: 'Insert a row after _capture_snapshot_after_run writes the .db.' },
      { name: '_ingest_orphan', module: 'server/snapshot_registry.py', description: 'Insert a row for an orphan file picked up by startup reconciliation.' },
      { name: 'materialise_sidecar', module: 'server/snapshot_registry.py', description: 'Set prebuilt_json_path after rendering a .plexexport.json sidecar.' },
      { name: 'delete', module: 'server/snapshot_registry.py', description: 'Remove a row (and optionally archive the cached JSON to legacy/).' },
    ],
    read_path: [
      { name: 'list_snapshots', module: 'server/snapshot_registry.py', description: "Power the Exports panel's table." },
      { name: 'get', module: 'server/snapshot_registry.py', description: 'Lookup by id for downloads.' },
      { name: 'reconcile_orphaned_snapshots', module: 'server/snapshot_registry.py', description: "Compare on-disk .db files against the registry at startup. Heals both directions." },
    ],
    lifecycle:
      "Created on first boot by init_registry. Every successful snapshot job adds a row at the end of _capture_snapshot_after_run. Rows are removed by the operator via the Exports panel (db_admin gated), or by retention enforcement when a new snapshot tips the count past the configured cap. reconcile_orphaned_snapshots runs once at startup to recover registry entries for any .db files the operator may have manually moved into snapshots/, and to mark registry rows whose file_path no longer exists as 'available=false' for the UI's red-banner state.",
    source_schema: 'server/snapshot_registry.py :: _SCHEMA (around lines 156-186)',
    source_init: 'server/snapshot_registry.py :: init_registry',
  },

  // ── auth.db ─────────────────────────────────────────────────────
  {
    filename: 'auth.db',
    filepath: 'server_data/auth.db',
    intro_short:
      "Login users and refresh tokens. Independent of every other DB so a credential rotation doesn't touch any media data.",
    intro_long:
      "auth.db lives separately from media.db on purpose. Login users and the db_admin credential are a security concern that wants its own blast radius: a corruption / restore / migration of media.db must never put credentials at risk, and vice versa. The file holds two tables: app_users for both human login users AND the special db_admin row that gates destructive operations, and refresh_tokens for JWT refresh-cookie validation.\n\nPasswords are stored as bcrypt-style hashes; we never store plaintext. Refresh tokens are stored only by their id and validity window - the JWT itself is signed with a per-process secret and isn't persisted.",
    tables: [
      {
        name: 'app_users',
        purpose: 'Every account that can log into the app, plus the db_admin row.',
        key_columns: ['username', 'role', 'password_hash', 'last_login'],
        columns: [
          { name: 'id', type: 'INTEGER PRIMARY KEY AUTOINCREMENT', note: 'Internal id.' },
          { name: 'username', type: 'TEXT UNIQUE NOT NULL', note: 'Login username. UNIQUE so two accounts can\'t share one.' },
          { name: 'password_hash', type: 'TEXT NOT NULL', note: 'bcrypt hash of the password. Plaintext never persisted.' },
          { name: 'role', type: 'TEXT NOT NULL', note: "viewer / operator / manager / admin / root_admin / db_admin. CHECK constraint enforces the enum." },
          { name: 'display_name', type: 'TEXT', note: 'Optional friendly name shown instead of username in the UI.' },
          { name: 'last_login', type: 'REAL', note: 'Unix timestamp of the most recent successful login.' },
          { name: 'created_at', type: 'REAL NOT NULL', note: 'When the account was created.' },
        ],
        design_note:
          "The db_admin row sits in the same table as login users for storage simplicity. It's NOT a login role - the app_users.role CHECK constraint allows the value, but the login endpoints refuse to issue a JWT for it. Only the auth_router's verify-password endpoints (used to gate destructive writes) accept the db_admin credential.",
      },
      {
        name: 'refresh_tokens',
        purpose: 'Validity records for outstanding refresh-cookie tokens. Each row represents one cookie that\'s eligible for silent JWT refresh.',
        key_columns: ['id', 'username', 'issued_at', 'expires_at', 'revoked'],
        columns: [
          { name: 'id', type: 'TEXT PRIMARY KEY', note: "The refresh-cookie's jti claim. Matches what the browser sends back." },
          { name: 'username', type: 'TEXT NOT NULL', note: 'Which account this cookie authenticates.' },
          { name: 'issued_at', type: 'REAL NOT NULL', note: 'When the cookie was minted.' },
          { name: 'expires_at', type: 'REAL NOT NULL', note: 'When the cookie should be considered dead.' },
          { name: 'revoked', type: 'INTEGER NOT NULL DEFAULT 0', note: 'Server-side revocation. Logout flips this to 1.' },
        ],
      },
    ],
    write_path: [
      { name: 'create_app_user', module: 'server/auth_db.py', description: 'New row at user creation / initial setup.' },
      { name: 'update_app_user', module: 'server/auth_db.py', description: 'Edit role / display name / password.' },
      { name: 'record_login', module: 'server/auth_db.py', description: 'Stamp last_login on a successful sign-in.' },
      { name: 'mint_refresh_token', module: 'server/auth_db.py', description: 'Insert a new row when login succeeds.' },
      { name: 'revoke_refresh_token', module: 'server/auth_db.py', description: 'Flip revoked=1 on logout.' },
      { name: 'cleanup_expired_tokens', module: 'server/auth_db.py', description: 'Daemon-thread sweep that drops rows past expires_at. Runs at startup + once every 24h.' },
    ],
    read_path: [
      { name: 'verify_password', module: 'server/auth_db.py', description: 'bcrypt-check a (username, password) pair. Used by every destructive write that requires the db_admin gate.' },
      { name: 'get_app_user', module: 'server/auth_db.py', description: 'Lookup by username for login / role checks.' },
      { name: 'validate_refresh_token', module: 'server/auth_db.py', description: 'Confirm an incoming cookie\'s id is still valid.' },
    ],
    lifecycle:
      "Created on first boot if auth is enabled. The Setup page is the only path that creates the first user; subsequent users are added through Settings -> Account Management -> User Accounts (root_admin gated). The db_admin row is created through a separate Setup-style page in Account Management -> Database Admin Account; it can never be deleted (only updated) because removing it would block every future destructive write. cleanup_expired_tokens runs at startup and on a 24h daemon thread.",
    source_schema: 'server/auth_db.py :: init_auth_db (around lines 120-155)',
    source_init: 'server/auth_db.py :: init_auth_db',
  },

  // ── per-snapshot .db ────────────────────────────────────────────
  {
    filename: '<server> - <libs> - <date>.db (per snapshot)',
    filepath: 'snapshots/<server> - <libraries> - <YYYY-MM-DD HH-MM>.db',
    intro_short:
      "The point-in-time artifact. One file per snapshot run. Schema mirrors media.db's tables (filtered to one server's rows) plus two metadata tables that make the file self-describing.",
    intro_long:
      "Per-snapshot .db files are the snapshot itself. Each one is a standalone SQLite database that captures exactly what the live Plex API reported during one specific run. They use the same schema as media.db so the snapshot serializer can rebuild a .plexexport.json sidecar on demand from any file, but the contents are scoped: only the rows for the captured server, only the captured metric types.\n\nThe file is self-describing via two extra tables, snapshot_meta and snapshot_users. That means a snapshot .db file moved to a different install (or a registry that's been wiped) can still be identified, attributed to a server, and listed as a recoverable orphan by reconcile_orphaned_snapshots without consulting the registry at all.\n\nWriting these files is the one place media.db is NOT the source of truth - Rule 1 of the snapshot pipeline. The build_snapshot_db_from_payloads function ingests the in-memory live-fetch payload directly so the artifact reflects exactly what the live API reported, regardless of what's accumulated in media.db.",
    tables: [
      {
        name: '(every media.db table, filtered)',
        purpose: 'Same schema as media.db; rows are scoped to one server and (optionally) one metric subset.',
        key_columns: ['(see media.db above)'],
        columns: [
          { name: '(inherited)', type: 'inherited', note: 'See the media.db section. Snapshot files use the same DDL, no schema drift.' },
        ],
        design_note:
          "We deliberately use the same schema so tools written against media.db work against any snapshot .db file. The snapshot pipeline gates which metric tables get rows: an operator who unchecks 'watch_history' on a capture run gets a .db where watch_events is present but empty.",
      },
      {
        name: 'snapshot_meta',
        purpose: 'Single-row provenance. Makes the file self-describing.',
        key_columns: ['snapshot_id', 'server_id', 'server_name', 'captured_at', 'libraries_json', 'metrics_json'],
        columns: [
          { name: 'snapshot_id', type: 'TEXT NOT NULL', note: 'UUID matching the registry row in snapshots.db.' },
          { name: 'server_id', type: 'TEXT NOT NULL', note: "Which server this snapshot is of." },
          { name: 'server_name', type: 'TEXT NOT NULL', note: "Friendly name at capture time." },
          { name: 'captured_at', type: 'REAL NOT NULL', note: "Unix timestamp." },
          { name: 'libraries_json', type: 'TEXT NOT NULL', note: 'JSON array of captured library names.' },
          { name: 'metrics_json', type: 'TEXT NOT NULL', note: 'JSON array of captured metric types.' },
          { name: 'created_by', type: 'TEXT', note: 'Operator who triggered the run (when auth is enabled).' },
        ],
      },
      {
        name: 'snapshot_users',
        purpose: "One row per user who actually contributed data to this snapshot.",
        key_columns: ['user_handle', 'display_name', 'is_owner'],
        columns: [
          { name: 'user_handle', type: 'TEXT PRIMARY KEY', note: 'The raw user identifier. Empty string = server owner.' },
          { name: 'display_name', type: 'TEXT', note: "Operator-chosen friendly name at capture time. May be NULL." },
          { name: 'is_owner', type: 'INTEGER NOT NULL DEFAULT 0', note: '1 for the empty-string owner row.' },
        ],
        design_note:
          "Derived from DISTINCT user_handle across the populated metric tables - zero-activity users on the source server never appear here. The display_name is frozen at capture so a user who's renamed in Plex later doesn't retroactively change historical snapshots.",
      },
    ],
    write_path: [
      { name: 'build_snapshot_db_from_payloads', module: 'server/snapshot_capture.py', description: 'The post-Rule-1 builder. Walks the in-memory payload list and writes rows directly into a fresh .db file. Never reads media.db for content.' },
      { name: 'create_snapshot_db', module: 'server/snapshot_capture.py', description: 'Legacy builder kept only for the orphan-reconcile path. Reads from media.db via ATTACH + INSERT-SELECT. New captures should not use this.' },
      { name: '_write_snapshot_meta / _write_snapshot_users', module: 'server/snapshot_capture.py', description: 'Stamp the two metadata tables after the metric tables are populated.' },
    ],
    read_path: [
      { name: 'build_payload_from_db', module: 'server/snapshot_serializer.py', description: 'Read every table and emit a .plexexport.json shape. Used on first download click when no sidecar is cached.' },
      { name: '_ingest_orphan', module: 'server/snapshot_registry.py', description: 'Reads snapshot_meta to recover a registry row from an orphan file.' },
    ],
    lifecycle:
      "Created at the end of every successful snapshot job. Lives until the operator deletes it from the Exports panel (or until retention enforcement removes the oldest entries to make room). The .db is never modified after creation - a snapshot is a historical record (Rule 3), so a server change after capture does not retroactively rewrite the file. The optional .plexexport.json sidecar gets generated next to it on the first download click and cached for subsequent clicks.",
    source_schema: 'server/snapshot_capture.py :: _create_meta_tables + reused media.db DDL',
    source_init: 'server/snapshot_capture.py :: build_snapshot_db_from_payloads',
  },
];

function DbSchemaPage() {
  const [view, setView] = useState<'quick' | 'deep' | 'sources'>('quick');
  return (
    <>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>DB Schema</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12 }}>
          Every SQLite database the app owns, what it stores, why it
          looks the way it does, and how it gets read and written.
          Pick a depth level below.
        </span>
        <nav className="tabs sub-tabs" style={{ marginTop: 12 }}>
          <button
            className={view === 'quick' ? 'active' : ''}
            onClick={() => setView('quick')}
          >
            Quick View
          </button>
          <button
            className={view === 'deep' ? 'active' : ''}
            onClick={() => setView('deep')}
          >
            In-depth
          </button>
          <button
            className={view === 'sources' ? 'active' : ''}
            onClick={() => setView('sources')}
          >
            Source Reference
          </button>
        </nav>
      </div>

      <DataFlowDiagram />

      {DB_DOCS.map((db) => (
        <div key={db.filepath} className="panel">
          <h2 style={{ marginTop: 0 }}>{db.filename}</h2>
          <div className="mono" style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 8 }}>
            {db.filepath}
          </div>
          {view === 'quick' && <QuickViewBody db={db} />}
          {view === 'deep' && <InDepthBody db={db} />}
          {view === 'sources' && <SourceLinksBody db={db} />}
        </div>
      ))}

      <SecurityPanel />
    </>
  );
}

function DataFlowDiagram() {
  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>Data flow</h2>
      <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 8 }}>
        Where bytes travel for each kind of job. Boxes are stores;
        arrows are writes. media.db is a side-effect cache for the
        resolver; it is never read for snapshot content.
      </span>
      <pre style={{ background: 'var(--panel-alt, #1b2233)', padding: 12, fontSize: 11, lineHeight: 1.4, overflowX: 'auto' }}>
{`SNAPSHOT JOB
  Live Plex API
       |
       v
  in-memory payload (export_data)
       |
       +----> snapshots/<server> - <libs> - <date>.db   (the artifact)
       |              |
       |              v
       |        server_data/snapshots.db   (registry pointer)
       |
       +----> server_data/media.db   (side-effect cache, resolver Tier 0)


IMPORT JOB
  .plexexport.json archive   (or per-snapshot .db rendered on demand)
       |
       v
  Resolver consults  ----> server_data/media.db  (Tier 0 / Tier 1 lookups)
       |
       v
  Live Plex API writes  (scrobble, rate, progress, playlists, collections)


DIRECT TRANSFER
  Source live Plex API
       |
       v
  in-memory payload
       |
       v
  Resolver consults  ----> server_data/media.db
       |
       v
  Destination live Plex API writes


LOGIN / AUTH
  Browser  <--->  server_data/auth.db  (app_users, refresh_tokens)`}
      </pre>
    </div>
  );
}

function QuickViewBody({ db }: { db: DbDoc }) {
  return (
    <>
      <p style={{ fontSize: 13, color: 'var(--text-dim)', marginTop: 0 }}>{db.intro_short}</p>
      <table className="list" style={{ width: '100%' }}>
        <thead>
          <tr>
            <th style={{ width: '25%' }}>Table</th>
            <th>Purpose</th>
            <th style={{ width: '35%' }}>Key columns</th>
          </tr>
        </thead>
        <tbody>
          {db.tables.map((t) => (
            <tr key={t.name}>
              <td className="mono" style={{ fontSize: 12 }}>{t.name}</td>
              <td style={{ fontSize: 13 }}>{t.purpose}</td>
              <td className="mono" style={{ fontSize: 11, color: 'var(--text-dim)' }}>
                {t.key_columns.join(', ')}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

function InDepthBody({ db }: { db: DbDoc }) {
  return (
    <>
      {db.intro_long.split('\n\n').map((para, i) => (
        <p key={i} style={{ fontSize: 13, marginTop: i === 0 ? 0 : 12 }}>{para}</p>
      ))}

      {db.tables.map((t) => (
        <div key={t.name} style={{ marginTop: 16 }}>
          <h3 style={{ fontSize: 14, margin: '8px 0 4px 0' }}>
            <span className="mono">{t.name}</span>
          </h3>
          <p style={{ fontSize: 13, color: 'var(--text-dim)', margin: '4px 0' }}>{t.purpose}</p>
          <table className="list" style={{ width: '100%', fontSize: 12 }}>
            <thead>
              <tr>
                <th style={{ width: '25%' }}>Column</th>
                <th style={{ width: '25%' }}>Type</th>
                <th>Note</th>
              </tr>
            </thead>
            <tbody>
              {t.columns.map((c) => (
                <tr key={c.name}>
                  <td className="mono" style={{ fontSize: 11 }}>{c.name}</td>
                  <td className="mono" style={{ fontSize: 11, color: 'var(--text-dim)' }}>{c.type}</td>
                  <td style={{ fontSize: 12 }}>{c.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {t.design_note && (
            <div style={{
              fontSize: 11, color: 'var(--text-dim)', marginTop: 6,
              paddingLeft: 8, borderLeft: '2px solid var(--color-phase, #58a6ff)',
              fontStyle: 'italic',
            }}>
              <strong>Design note:</strong> {t.design_note}
            </div>
          )}
        </div>
      ))}

      <h3 style={{ fontSize: 14, margin: '16px 0 4px 0' }}>Write path</h3>
      <ul style={{ fontSize: 12, marginTop: 4 }}>
        {db.write_path.map((f) => (
          <li key={f.name}>
            <span className="mono" style={{ fontSize: 11 }}>{f.name}</span>
            {' '}<span style={{ color: 'var(--text-dim)' }}>({f.module})</span>
            {': '}{f.description}
          </li>
        ))}
      </ul>

      <h3 style={{ fontSize: 14, margin: '12px 0 4px 0' }}>Read path</h3>
      <ul style={{ fontSize: 12, marginTop: 4 }}>
        {db.read_path.map((f) => (
          <li key={f.name}>
            <span className="mono" style={{ fontSize: 11 }}>{f.name}</span>
            {' '}<span style={{ color: 'var(--text-dim)' }}>({f.module})</span>
            {': '}{f.description}
          </li>
        ))}
      </ul>

      <h3 style={{ fontSize: 14, margin: '12px 0 4px 0' }}>Lifecycle</h3>
      <p style={{ fontSize: 13, marginTop: 4 }}>{db.lifecycle}</p>
    </>
  );
}

function SourceLinksBody({ db }: { db: DbDoc }) {
  return (
    <>
      <p style={{ fontSize: 13, color: 'var(--text-dim)', marginTop: 0 }}>
        Pointers into the source so you can verify any claim on this
        page against the actual code. Paths are relative to the
        repository root.
      </p>
      <table className="list" style={{ width: '100%', fontSize: 12 }}>
        <tbody>
          <tr>
            <td style={{ width: '30%' }}><strong>Schema</strong></td>
            <td className="mono" style={{ fontSize: 11 }}>{db.source_schema}</td>
          </tr>
          <tr>
            <td><strong>Initialiser</strong></td>
            <td className="mono" style={{ fontSize: 11 }}>{db.source_init}</td>
          </tr>
          <tr>
            <td><strong>Write functions</strong></td>
            <td className="mono" style={{ fontSize: 11 }}>
              {db.write_path.map((f) => f.name).join(', ')}
              <div style={{ color: 'var(--text-dim)', marginTop: 4, fontSize: 10 }}>
                all in {Array.from(new Set(db.write_path.map((f) => f.module))).join(', ')}
              </div>
            </td>
          </tr>
          <tr>
            <td><strong>Read functions</strong></td>
            <td className="mono" style={{ fontSize: 11 }}>
              {db.read_path.map((f) => f.name).join(', ')}
              <div style={{ color: 'var(--text-dim)', marginTop: 4, fontSize: 10 }}>
                all in {Array.from(new Set(db.read_path.map((f) => f.module))).join(', ')}
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </>
  );
}

function SecurityPanel() {
  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>What's encrypted, what isn't</h2>
      <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 12 }}>
        The threat model is host-disk theft or a leaked export. Every
        store below answers the question "if someone copies this
        file off the host, what can they read?"
      </span>
      <table className="list" style={{ width: '100%' }}>
        <thead>
          <tr>
            <th>Where it lives</th>
            <th>Sensitivity</th>
            <th>At-rest treatment</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td className="mono" style={{ fontSize: 11 }}>server_data/servers.json</td>
            <td>Plex auth tokens, server URLs</td>
            <td>
              <strong>Fernet-encrypted at rest</strong> via
              <code> server_data/.keyfile</code>. A copy of servers.json
              without the keyfile is unreadable.
            </td>
          </tr>
          <tr>
            <td className="mono" style={{ fontSize: 11 }}>server_data/media.db (managed_users.*_enc columns)</td>
            <td>Per-user auth tokens, Plex Home PINs, service passwords</td>
            <td>
              <strong>Fernet-encrypted at rest</strong> with the same
              keyfile. Other columns in this DB are plaintext.
            </td>
          </tr>
          <tr>
            <td className="mono" style={{ fontSize: 11 }}>server_data/auth.db (app_users.password_hash)</td>
            <td>Login passwords</td>
            <td>
              <strong>bcrypt-hashed</strong>. Never recoverable, only
              comparable. A leak gives an attacker hashes to brute-force
              offline.
            </td>
          </tr>
          <tr>
            <td className="mono" style={{ fontSize: 11 }}>server_data/auth.db (refresh_tokens)</td>
            <td>Refresh-cookie validity records</td>
            <td>
              Plaintext id + validity window. A leak lets an attacker
              know which cookies were minted; it does NOT let them
              forge a working cookie because the JWT signing secret
              lives in the app process, not on disk.
            </td>
          </tr>
          <tr>
            <td className="mono" style={{ fontSize: 11 }}>server_data/media.db (everything else)</td>
            <td>Watch history, ratings, playlist/collection membership</td>
            <td>
              Plaintext. Consider this the same threat surface as a
              Plex database export: the data is not secret per se, but
              it is per-user activity and should be treated like any
              other user-attributed metric store.
            </td>
          </tr>
          <tr>
            <td className="mono" style={{ fontSize: 11 }}>server_data/snapshots.db</td>
            <td>Snapshot metadata only</td>
            <td>
              Plaintext. Carries server names + capture timestamps;
              no user data and no credentials.
            </td>
          </tr>
          <tr>
            <td className="mono" style={{ fontSize: 11 }}>snapshots/*.db</td>
            <td>Watch history, ratings, playlists, collections for one snapshot</td>
            <td>
              Plaintext. Same threat surface as media.db. The
              snapshot_users table identifies users by handle, never
              by token or email beyond what Plex itself returns.
            </td>
          </tr>
          <tr>
            <td className="mono" style={{ fontSize: 11 }}>server_data/settings.json</td>
            <td>Operator preferences</td>
            <td>
              Plaintext. Contains paths, cadences, retention numbers.
              No credentials.
            </td>
          </tr>
          <tr>
            <td className="mono" style={{ fontSize: 11 }}>server_data/.keyfile</td>
            <td>Fernet master key</td>
            <td>
              Plaintext on disk by design (any other state would just
              be a recursive key problem). File-system permissions are
              the only protection. Treat this file the same as you
              would an SSH private key.
            </td>
          </tr>
        </tbody>
      </table>
      <p style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 12 }}>
        Export advice: if you're backing up <code>server_data/</code>
        to a remote location, either back up the entire directory
        together (so the keyfile travels with the encrypted files
        and the export is restorable), or skip the keyfile and
        accept that the export cannot recover credentials. Splitting
        them is the worst option.
      </p>
    </div>
  );
}


// ── Run Logs ─────────────────────────────────────────────────────────────────
//
// Walks the operator through every log file the engine writes per run,
// what's in each, when it gets written, and which one to open first
// when something goes wrong. Stacked panels (one per file) rather than
// sub-tabs because when debugging you usually want to flip between
// two or three of them side by side and Ctrl-F across the page.

interface LogFileDoc {
  filename: string;
  always: boolean;             // true if every run writes it
  who_writes: string;
  what: string;
  shape: string;               // one-line shape description
  example: string;             // small concrete sample
  open_when: string;           // when to look at it
  notes?: string;
}

const RUN_LOG_FILES: LogFileDoc[] = [
  {
    filename: 'runtime.log',
    always: true,
    who_writes: 'every engine module that uses the shared "plexmigrate" logger',
    what:
      "The main per-run pipeline log. INFO-level and above. Every snapshot / restore / direct-transfer reports its high-level progress here: connection events, library walks, phase transitions, retention decisions. Read this first for any unexpected behaviour.",
    shape: '[YYYY-MM-DD HH:MM:SS] [LEVEL] message',
    example: `[2026-05-13 18:34:14] [INFO] Snapshot data types: playlists
[2026-05-13 18:34:14] [INFO] Connected as home user: jonpetry
[2026-05-13 18:36:06] [INFO] Playlist cache warmed: 58 playlist record(s) across 6 server connection(s)
[2026-05-13 18:36:06] [INFO] Capturing snapshot library: Music
[2026-05-13 18:40:48] [INFO] Snapshot[Music]: 0 watched, 27 playlists, 0 collections -> media.db`,
    open_when:
      "Almost always. Confirms the run started, what scope it covered, when each library finished, and whether any soft warnings fired. The Logs tab in the UI tails this file live by default.",
  },
  {
    filename: 'errors.log',
    always: true,
    who_writes: 'the same shared logger via the WARNING+ filter handler',
    what:
      "WARNING and ERROR lines duplicated from runtime.log into a smaller file. A successful run leaves this file empty; any non-empty errors.log means something the engine flagged as 'this matters' happened. Tracebacks land here too.",
    shape: '[YYYY-MM-DD HH:MM:SS] [WARNING | ERROR] message  (followed by traceback on Python exceptions)',
    example: `[2026-05-13 19:17:34] [WARNING] Could not snapshot data for home user 'Tom Wuest': 401 Unauthorized
[2026-05-13 19:17:34] [ERROR] Snapshot failed for library 'Movies': HTTPSConnectionPool host=plex.example timed out
Traceback (most recent call last):
  File "services/snapshotter.py", line 670, in snapshot_library
    ...`,
    open_when:
      "Whenever a run completes with a non-PASS marker on the directory name, or whenever runtime.log mentions a warning you want the full context for. Empty file = no warnings.",
    notes:
      "Same lines also appear in runtime.log; errors.log is the cheap-to-scan extract. If you only have a minute, tail this.",
  },
  {
    filename: 'media.log',
    always: true,
    who_writes: '_record_success / _record_failure helpers via the media logger',
    what:
      "One line per item the engine touched: which library, what type, what title, plus item-specific metrics (play count, rating, member count). The size of this file tracks the size of the run almost perfectly. Useful for confirming 'did we actually process every item I expected?' without reading the noisy runtime stream.",
    shape: '[YYYY-MM-DD HH:MM:SS] [EXPORT | IMPORT] [Library] <type> | <title> | <metrics>',
    example: `[2026-05-13 18:36:06] [EXPORT] [Music] playlist | ❤️ Tracks | items: 690
[2026-05-13 18:36:06] [EXPORT] [Music] playlist | All Music | items: 83636
[2026-05-13 19:17:28] [IMPORT] [Audio-Books] playlist | Deep in the mind | [CREATED] with 1 items`,
    open_when:
      "When you want to confirm individual items were touched. Especially handy for ratings and playlists where the high-level INFO line just says 'N items' but you want to know which N.",
  },
  {
    filename: 'db_access.log',
    always: true,
    who_writes: 'services/db_access_log.py wrappers around every media.db read / write',
    what:
      "Audit trail of media.db reads and writes. Each line records the table touched, the field or filter used, affected row count, and a human-readable 'intent' explaining why. Useful for confirming the engine touched media.db the way you expected and for spotting unexpected cascade deletes.",
    shape: '[YYYY-MM-DD HH:MM:SS] [LEVEL] [READ | WRITE] table=... <fields> intent=<why>',
    example: `[2026-05-13 18:34:14] [INFO] [READ] table=global_tombstones count=15 intent=tombstone filter for home-user enumeration
[2026-05-13 18:34:14] [INFO] [READ] table=managed_users field=tombstoned server_id=a6e8a4a8... hidden_count=15 intent=per-server tombstone filter`,
    open_when:
      "When you want to know precisely what media.db state the engine read or wrote. Cascade-delete diagnostics, prune sweeps, and anything in the snapshot / direct-transfer ingest path land here with full provenance.",
  },
  {
    filename: 'troubleshoot.log',
    always: false,
    who_writes: 'write_troubleshoot_log() in services/logging_ops.py at end of restore runs that had failures',
    what:
      "Operator-facing failure index, grouped by category. Each section names the problem in plain English (\"Smart Playlist\", \"File Path Not Found on New Server\", \"No Match Found\"), explains what it means, suggests fixes, and lists every affected item underneath. This is the file to share when you ask for help.",
    shape: 'Plain text grouped by category. Header + per-category block + Next Steps footer.',
    example: `PlexMigrate Troubleshooting Log - 2026-05-13 19:17:34
============================================================

── No Match Found - All Tiers Exhausted ───────────────

What this means:
  The script tried GUID lookup, exact file path, suffix path matching,
  and title search but could not find this item on the target server.
  The item may not have been added to the new library yet, or may have
  a different title.

Suggested fixes:
  1. Verify the item exists in your Plex library on the new server.
  2. If the file was renamed, use Fix Match in Plex to give it a stable plex:// GUID.
  ...

Affected items (47):
  • Music | Some Track | resolver exhausted all four tiers
  • ...`,
    open_when:
      "Any time a restore run reports unresolved items. Created on demand at the end of the run; absence means every requested operation succeeded (or the run aborted before getting here).",
    notes:
      "Categories are sourced from services/state.py TROUBLESHOOT_CATEGORIES. The 'Suggested fixes' steps are hard-coded per category so the advice stays consistent across runs.",
  },
  {
    filename: 'unresolved.log',
    always: false,
    who_writes: 'write_unresolved_log() in services/logging_ops.py for the no_tier_match bucket',
    what:
      "Strict subset of troubleshoot.log: items the resolver could not match through any tier (DB cache, API GUID, filepath, fuzzy title). Pure data, no advice. Useful for piping into a script that batch-fixes items in Plex.",
    shape: 'Plain text. Header + one tab-separated line per unresolved item.',
    example: `# PlexMigrate unresolved items - 2026-05-13 19:17:34
# Items below failed all matching tiers. Restore manually in Plex.
Library	Type	Title	GUID	Filepath
Music	track	The Long Way Down	musicbrainz://...	B:\\The Long Way Down\\...m4b`,
    open_when:
      "When you want a clean, machine-parseable list of unresolved items without the categorisation prose. Pair with the destination's Plex library search.",
  },
  {
    filename: '<Library>_success_*.log / <Library>_fail_*.log',
    always: false,
    who_writes: 'write_library_logs() at the end of each per-library restore phase',
    what:
      "Per-library success / failure breakdown. One success log + one failure log per library that produced any items. The success log carries the [CREATED] / [APPENDED] / [MERGED] / [SKIPPED] lines; the failure log carries [UNRESOLVED] / [FAILED] / [API_ERROR]. Operator can spot 'Music looked clean but Movies had 47 failures' without reading the whole runtime stream.",
    shape: 'Plain text. One line per item with a [TAG] verb prefix.',
    example: `[CREATED] [Music] playlist | My Playlist | items=42 user=Plex Owner
[APPENDED] [Music] playlist | Liked Songs | items=12 user=Plex Owner
[MERGED] [Music] track | Daft Punk - One More Time | plays=3`,
    open_when:
      "When you want to compare libraries against each other (success rate, item counts). The Logs tab in the UI surfaces these per-library so you can read one library at a time without grepping.",
    notes:
      "The filename suffix is the run timestamp so a re-run produces a fresh pair without overwriting the previous one.",
  },
];

function RunLogsPage() {
  return (
    <>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Run Logs</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12 }}>
          Every snapshot, restore, or direct-transfer run writes a
          directory of log files under <code>plex_logs/</code>. This
          page documents what's in each file, when it gets written,
          and which one to open first when something goes wrong. The
          Logs tab in the app surfaces them per-run with live tail;
          this page is the field guide.
        </span>
        <p style={{ fontSize: 13, color: 'var(--text-dim)', marginTop: 12 }}>
          Directory shape per run:
        </p>
        <pre style={{ background: 'var(--panel-alt, #1b2233)', padding: 12, fontSize: 12, lineHeight: 1.5, overflowX: 'auto' }}>
{`plex_logs/run_<server-slug>_<YYYYMMDD>_<HHMMSS>[_PASS | _FAIL]/
  runtime.log              always       INFO+ pipeline events
  errors.log               always       WARNING+ subset; tracebacks
  media.log                always       per-item EXPORT / IMPORT events
  db_access.log            always       media.db read / write audit trail
  troubleshoot.log         on failure   categorised failure index (restore only)
  unresolved.log           on failure   resolver four-tier exhaustion list (restore only)
  <Library>_success_*.log  on restore   per-library [CREATED]/[APPENDED]/[MERGED]/[SKIPPED]
  <Library>_fail_*.log     on restore   per-library [UNRESOLVED]/[FAILED]/[API_ERROR]`}
        </pre>
        <p style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 8 }}>
          The trailing <code>_PASS</code> or <code>_FAIL</code> on the
          directory name is the engine's at-a-glance verdict from
          <code> _finalise_run_dir</code>. Absence (no suffix) means
          the run was still in flight when the directory was last
          modified, or it crashed before the finaliser ran.
        </p>
      </div>

      {RUN_LOG_FILES.map((doc) => (
        <div key={doc.filename} className="panel">
          <h2 style={{ marginTop: 0 }}>
            <code style={{ fontSize: 16 }}>{doc.filename}</code>
            <span style={{
              marginLeft: 12, fontSize: 11, fontWeight: 400,
              color: doc.always ? 'var(--ok, #16a34a)' : 'var(--text-dim)',
              border: `1px solid ${doc.always ? 'var(--ok, #16a34a)' : 'var(--text-dim)'}55`,
              padding: '2px 8px', borderRadius: 10,
            }}>
              {doc.always ? 'every run' : 'on demand'}
            </span>
          </h2>
          <p style={{ fontSize: 13, marginTop: 0 }}>{doc.what}</p>
          <table className="list" style={{ width: '100%', fontSize: 12 }}>
            <tbody>
              <tr>
                <td style={{ width: '22%', verticalAlign: 'top' }}><strong>Who writes it</strong></td>
                <td style={{ color: 'var(--text-dim)' }}>{doc.who_writes}</td>
              </tr>
              <tr>
                <td style={{ verticalAlign: 'top' }}><strong>Line shape</strong></td>
                <td className="mono" style={{ fontSize: 11 }}>{doc.shape}</td>
              </tr>
              <tr>
                <td style={{ verticalAlign: 'top' }}><strong>When to open it</strong></td>
                <td>{doc.open_when}</td>
              </tr>
              {doc.notes && (
                <tr>
                  <td style={{ verticalAlign: 'top' }}><strong>Notes</strong></td>
                  <td style={{ color: 'var(--text-dim)', fontStyle: 'italic' }}>{doc.notes}</td>
                </tr>
              )}
            </tbody>
          </table>
          <h3 style={{ fontSize: 13, margin: '12px 0 4px 0' }}>Example</h3>
          <pre style={{ background: 'var(--panel-alt, #1b2233)', padding: 12, fontSize: 11, lineHeight: 1.5, overflowX: 'auto' }}>
{doc.example}
          </pre>
        </div>
      ))}

      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Reading flow when something goes wrong</h2>
        <p style={{ fontSize: 13, color: 'var(--text-dim)', marginTop: 0 }}>
          A run finished with a status you didn't expect. Open files
          in this order:
        </p>
        <ol style={{ fontSize: 13, paddingLeft: 22 }}>
          <li style={{ marginBottom: 8 }}>
            <strong>errors.log</strong> first. If it's empty, no
            warning fired; jump to step 3. If it has lines, read the
            most recent one (last line in the file) and note the
            module that fired.
          </li>
          <li style={{ marginBottom: 8 }}>
            <strong>runtime.log</strong> for the surrounding context.
            Search for the timestamp of the error in errors.log and
            read the 10 to 20 lines before it. The pipeline events
            usually point at which library or user was being
            processed when the error fired.
          </li>
          <li style={{ marginBottom: 8 }}>
            <strong>troubleshoot.log</strong> on restore runs that
            had unresolved items. It groups failures by category and
            gives you per-category fix suggestions. Share this file
            verbatim when asking for help; it's designed to be
            self-contained.
          </li>
          <li style={{ marginBottom: 8 }}>
            <strong>media.log</strong> when you suspect the run
            <em> looked </em> right but didn't actually touch what
            you expected. Confirms per-item what the engine did.
          </li>
          <li style={{ marginBottom: 8 }}>
            <strong>db_access.log</strong> when a record went missing
            from media.db unexpectedly or you want to confirm a
            cascade-delete fired correctly. Every write is timestamped
            and intent-labelled.
          </li>
          <li>
            <strong>&lt;Library&gt;_fail_*.log</strong> when only one
            library out of several misbehaved. Per-library scoping
            saves you grepping a multi-megabyte runtime.log to find
            the 47 lines that belong to the bad library.
          </li>
        </ol>
        <p style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 12 }}>
          The Logs tab in the app surfaces every file in a run
          directory and tails the selected one live. If you're
          digging through a finished run, downloading the run's
          <code> .zip </code> from the same tab gives you the whole
          set on disk so you can use grep, less, or your text editor
          of choice.
        </p>
      </div>
    </>
  );
}


// ── Troubleshooting ──────────────────────────────────────────────────────────
//
// Mirrors the categories defined in services/state.py ::
// TROUBLESHOOT_CATEGORIES so the operator can read the same advice
// the engine writes into troubleshoot.log without having to wait for
// a failed run. When the user can't self-resolve, the "Open a bug
// report" button at the bottom pre-fills a GitHub issue with the
// current app version stamped in the body.

const BUG_REPORT_REPO = 'https://github.com/AProfessionalUsernameWIP/PlexMigrate';

interface TroubleCategory {
  key: string;
  title: string;
  explanation: string;
  steps: string[];
}

const TROUBLE_CATEGORIES: TroubleCategory[] = [
  {
    key: 'playlist_type_conflict',
    title: 'Destination Playlist Type Conflict',
    explanation:
      "The playlist already exists on the destination server but is of a different media type ('audio' vs 'video' vs 'photo') than the snapshot's items. Plex enforces a single media type per playlist and rejects appends across the boundary. Almost always traces to a prior restore run that fanned out into the wrong library and left a polluted playlist on the destination.",
    steps: [
      "Open Plex on the destination server.",
      "Find each affected playlist by name (the run log lists them).",
      "Delete the playlist on the destination - it has the wrong media type and will keep blocking future restores.",
      "Re-run the restore. The engine will create a fresh playlist with the correct items.",
      "If you keep seeing this, check that the snapshot's source library name actually exists on the destination - mismatched names cause cross-library fan-out.",
    ],
  },
  {
    key: 'no_tier_match',
    title: 'No Match Found, All Tiers Exhausted',
    explanation:
      "The resolver tried GUID lookup, exact file path, suffix path matching (last 2 to 3 path components, cross-platform), and title search but could not find this item on the target server. The item may not have been added to the new library yet, or may have a different title.",
    steps: [
      "Verify the item exists in your Plex library on the new server.",
      "If the file was renamed, use Fix Match in Plex to give it a stable plex:// GUID.",
      "Re-run the restore after the item is confirmed present.",
      "If it still fails, restore Play Count or rating for this item manually in Plex.",
    ],
  },
  {
    key: 'local_guid_no_match',
    title: 'local:// GUID, No MusicBrainz Match',
    explanation:
      "This track was never matched to MusicBrainz on the old server, so it has no universal ID. Without a universal ID, the script cannot reliably find this track on a different server.",
    steps: [
      "Open Plex on the old server and navigate to the Music library.",
      "Right-click the album containing the unmatched track.",
      "Select 'Fix Match' from the context menu.",
      "Search for the correct album on MusicBrainz and select it.",
      "Wait for Plex to finish matching (may take a few minutes per album).",
      "Re-run the snapshot to capture the updated GUIDs.",
    ],
  },
  {
    key: 'file_path_not_found',
    title: 'File Path Not Found on New Server',
    explanation:
      "The file exists on the old server but could not be found at the same path on the new one. This usually means your media drive is mounted at a different location, or the folder structure changed during the move. PlexMigrate automatically attempts suffix matching (comparing the last 2 to 3 path components without the root prefix) so cross-platform moves between Windows and Linux are often resolved without configuration. If this item still failed, the tail of the path may have also changed.",
    steps: [
      "Check that your media drive is connected and mounted.",
      "Compare the file path shown below with where your files actually live.",
      "If only the root changed (e.g., C:\\Media to /mnt/plex), suffix matching should have caught it automatically. Verify the item exists in Plex.",
      "If the root AND some intermediate folders changed, re-run with --remap-path /old/root /new/root to translate the stored root prefix.",
      "If paths match but files still aren't found, check drive permissions.",
    ],
  },
  {
    key: 'ambiguous_title_match',
    title: 'Ambiguous Title Match, Multiple Results',
    explanation:
      "A search by title returned more than one result, so the script couldn't safely pick one. This happens when you have duplicate entries or similarly named items in your library.",
    steps: [
      "Open Plex and search for the item title shown below.",
      "Check for duplicate entries and remove the extras.",
      "Re-run the restore after removing duplicates.",
      "Alternatively, re-run with --no-strict-match to allow best-guess selection. Use carefully, may match the wrong item.",
    ],
  },
  {
    key: 'api_error',
    title: 'API Error During Restore',
    explanation:
      "The Plex server returned an error when the script tried to update this item. This may be a permissions issue, a network problem, or a temporary server hiccup.",
    steps: [
      "Verify your Plex token has admin access to the server.",
      "Check that the Plex server is running and reachable.",
      "Open the run log and search for the HTTP error code for this item.",
      "Try re-running the restore. Transient errors often resolve on retry.",
    ],
  },
  {
    key: 'smart_playlist_skipped',
    title: 'Smart Playlist, Requires Manual Recreation',
    explanation:
      "Smart playlists are defined by a saved filter query that contains server-specific library section IDs. Those IDs are different on every Plex installation, so the filter cannot be transferred automatically. The playlist must be recreated manually on the target server using the same filter criteria.",
    steps: [
      "Open Plex on the target server and go to the library shown below.",
      "Choose 'New Smart Playlist' from the playlist menu.",
      "Re-enter the same filter rules the playlist used on the old server.",
      "The original filter URL is recorded in the run log for this snapshot. Search for the playlist name alongside 'smart_content:'.",
    ],
  },
  {
    key: 'playlist_item_already_present',
    title: 'Playlist Item Already Present, Skipped',
    explanation:
      "This item was already in the playlist on the target server and was skipped to avoid duplicates. This is expected behaviour. PlexMigrate never adds duplicate items to existing playlists.",
    steps: [
      "No action required, this item is already correctly in the playlist.",
      "If you believe the playlist is wrong, review it directly in Plex.",
    ],
  },
  {
    key: 'collection_member_already_present',
    title: 'Collection Member Already Present, Skipped',
    explanation:
      "This item was already a member of the collection on the target server and was skipped to avoid duplicates.",
    steps: [
      "No action required, this item is already correctly in the collection.",
    ],
  },
  {
    key: 'rating_already_set',
    title: 'Rating Already Set, Skipped',
    explanation:
      "This item already has a star rating on the target server. PlexMigrate treats the target rating as authoritative and never overwrites it, even if the snapshot contains a different value.",
    steps: [
      "No action required, the existing rating is preserved.",
      "If you want to change the rating, do so directly in Plex.",
    ],
  },
];

function TroubleshootingPage() {
  const [appVersion, setAppVersion] = useState<string | null>(null);
  const [versionError, setVersionError] = useState<string | null>(null);

  useEffect(() => {
    api.getHealth()
      .then((h) => setAppVersion(h.app_version || null))
      .catch((e) => setVersionError(String(e)));
  }, []);

  const openBugReport = () => {
    // Pre-fill the GitHub issue with everything we know about the
    // current build + browser context. Operator fills in the
    // What/Expected/Actual sections; we save them the rest of the
    // boilerplate.
    const title = `[bug] `;
    const lines = [
      '**Build info** (auto-filled, please leave intact):',
      '',
      `- App version: ${appVersion || '(unknown; health check failed)'}`,
      `- Page URL: ${window.location.href}`,
      `- Browser: ${navigator.userAgent}`,
      `- Reported: ${new Date().toISOString()}`,
      '',
      '---',
      '',
      '**What I was trying to do:**',
      '',
      '',
      '**What I expected to happen:**',
      '',
      '',
      '**What actually happened:**',
      '',
      '',
      '**Run log directory** (paste the path from Settings > Logs if relevant):',
      '',
      '',
      '**Snapshot file** (paste the filename from the Exports panel if relevant):',
      '',
      '',
      '**Anything else** (errors.log tail, troubleshoot.log paste, screenshots):',
      '',
      '',
    ];
    const body = lines.join('\n');
    const url =
      `${BUG_REPORT_REPO}/issues/new`
      + `?title=${encodeURIComponent(title)}`
      + `&body=${encodeURIComponent(body)}`;
    window.open(url, '_blank', 'noopener,noreferrer');
  };

  return (
    <>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Troubleshooting</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12 }}>
          The same failure categories the engine writes into
          <code> troubleshoot.log</code> at the end of any restore
          that had unresolved items. Each section explains what the
          category means and the steps that usually resolve it. If
          nothing here matches, the Open a bug report button at the
          bottom pre-fills a GitHub issue with the build version and
          a structured template.
        </span>
        <div style={{
          marginTop: 12,
          display: 'flex',
          gap: 12,
          alignItems: 'center',
          flexWrap: 'wrap',
        }}>
          <button className="primary" onClick={openBugReport}>
            Open a bug report on GitHub
          </button>
          <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>
            Opens in a new tab. Pre-fills app version{' '}
            <code>{appVersion || '?'}</code> in the issue body.
          </span>
        </div>
        {versionError && (
          <div className="banner" style={{
            marginTop: 8, fontSize: 11,
            background: 'rgba(217, 119, 6, 0.12)',
            color: 'var(--warn, #d97706)',
            border: '1px solid var(--warn, #d97706)',
            borderRadius: 6, padding: '6px 10px',
          }}>
            Could not auto-detect app version ({versionError}). The bug
            report button still works but will leave the version field
            as &ldquo;unknown&rdquo;.
          </div>
        )}
      </div>

      {TROUBLE_CATEGORIES.map((cat) => (
        <div key={cat.key} className="panel">
          <h2 style={{ marginTop: 0 }}>{cat.title}</h2>
          <p style={{ fontSize: 13, color: 'var(--text-dim)', marginTop: 0 }}>
            <strong>What this means:</strong> {cat.explanation}
          </p>
          <h3 style={{ fontSize: 13, margin: '8px 0 4px 0' }}>Suggested fixes</h3>
          <ol style={{ fontSize: 13, paddingLeft: 22, margin: 0 }}>
            {cat.steps.map((step, i) => (
              <li key={i} style={{ marginBottom: 4 }}>{step}</li>
            ))}
          </ol>
        </div>
      ))}

      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Still stuck?</h2>
        <p style={{ fontSize: 13, color: 'var(--text-dim)' }}>
          The categories above cover every failure type the engine
          knows how to name. If your problem doesn't match any of
          them, or if a suggested fix didn't work, that's a real
          signal worth reporting. A bug report with the prefilled
          template plus a paste of the relevant run log directory's
          <code> errors.log</code> and <code>troubleshoot.log</code>
          (if present) gives the maintainer everything they need to
          reproduce.
        </p>
        <div style={{ marginTop: 12 }}>
          <button className="primary" onClick={openBugReport}>
            Open a bug report on GitHub
          </button>
        </div>
        <p style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 12 }}>
          The report lands at{' '}
          <a href={`${BUG_REPORT_REPO}/issues`} target="_blank" rel="noopener noreferrer">
            {BUG_REPORT_REPO}/issues
          </a>
          . You'll need a GitHub account to post. The button opens a
          new tab with the title and body pre-filled.
        </p>
      </div>
    </>
  );
}
