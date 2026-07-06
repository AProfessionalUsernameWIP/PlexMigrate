// Settings → Help.
//
// A flat reference page for the longer explanations. Short forms of
// these explanations appear inside InfoTip popovers next to the
// control they describe; this panel collects them all in one place
// for read-through.
//
// Sub-pages:
//   * Reference         - the original control-by-control writeup.
//   * Activity Statuses - the 11 activity-feed labels, their colors,
//                         when each fires, and what they mean.
//
// Visible to every role that can see the Settings tab. The content
// is purely informational; no permission gating per section.

import { useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import { getAllHelpTopics, getHelpTopicsByCategory } from '../help_content';
import { DeploymentMapPage } from './DeploymentMap';

// The page ids 'quick_start' and 'deep_dive' sit adjacent at the
// front of the strip so the orientation pair reads as one unit. The
// legacy ids 'how_to_use' and 'features' are kept as aliases via
// ``normalizeLegacyHelpPage`` below so any in-flight links / saved
// tab state still resolves.
type HelpPage =
  | 'quick_start' | 'deep_dive'
  | 'reference' | 'topics' | 'activity_and_phases'
  | 'api_usage' | 'db_schema' | 'run_logs'
  | 'troubleshooting' | 'dev_notes' | 'users_and_roles'
  | 'server_syncing' | 'deployment_map';

function normalizeLegacyHelpPage(raw: string): HelpPage {
  if (raw === 'how_to_use') return 'quick_start';
  if (raw === 'features') return 'deep_dive';
  return raw as HelpPage;
}


// Tab order kept centralised so the global search results "Open in
// <tab>" links and the tab strip stay in sync. Quick Start + Deep Dive
// sit at positions 0 + 1 as the operator-facing orientation pair.
const HELP_TAB_ORDER: Array<{ id: HelpPage; label: string }> = [
  { id: 'quick_start', label: 'Quick Start' },
  { id: 'deep_dive', label: 'Deep Dive' },
  { id: 'reference', label: 'Reference' },
  { id: 'topics', label: 'Topics' },
  { id: 'activity_and_phases', label: 'Activity & Phases' },
  { id: 'api_usage', label: 'API Usage' },
  { id: 'db_schema', label: 'DB Schema' },
  { id: 'run_logs', label: 'Run Logs' },
  { id: 'troubleshooting', label: 'Troubleshooting' },
  { id: 'dev_notes', label: 'Dev Notes' },
  { id: 'users_and_roles', label: 'Users & Roles' },
  { id: 'server_syncing', label: 'Server Syncing' },
  { id: 'deployment_map', label: 'Deployment Map' },
];

// ── Global Help search index ──────────────────────────────────────────────
//
// Searches across every Help sub-tab in one pass instead of relying
// on the per-page filters.
//
// Each entry: { subtab, title, sectionId?, summary, body?, keywords }.
// Search compares the lower-cased query against the concatenation of
// title + summary + keywords. The first-hit-wins entry rendering tries
// to inline-display the body when it's small (Topics, Deep Dive entries
// have rich JSX bodies); larger pages just deep-link via a "Open in
// <tab>" button.

interface HelpSearchEntry {
  subtab: HelpPage;
  title: string;
  summary: string;          // one-line — shown in the result card
  keywords?: string;        // optional extra search corpus
  body?: React.ReactNode;   // optional inline body for compact results
}

// Static index of section-level entries for the pages that don't have a
// programmatic registry. Each entry's ``title`` is a heading that
// actually exists on that sub-page so the "Open in <tab>" deep-link
// lands the operator near the right content. Adding a new section to
// any of these pages? Add a row here too so cross-tab search keeps
// covering it.
const STATIC_HELP_ENTRIES: HelpSearchEntry[] = [
  // Quick Start (orientation)
  { subtab: 'quick_start', title: 'Overview', summary: 'Hestia-MediaManager captures and moves Plex watch history, ratings, playlists, and collections across servers.' },
  { subtab: 'quick_start', title: 'Snapshot', summary: 'Read every selected data type to a .db file on disk. Nondestructive on the source.', keywords: 'capture archive backup' },
  { subtab: 'quick_start', title: 'Restore', summary: 'Apply a snapshot back into a Plex server. Merge (default, additive) vs Replace (destructive overwrite).', keywords: 'merge replace import' },
  { subtab: 'quick_start', title: 'Direct Transfer', summary: 'Read source server, write destination server, no intermediate file. Same merge/replace choice as Restore.', keywords: 'mirror sync migrate' },
  { subtab: 'quick_start', title: 'Fan-out', summary: 'One source, N destinations in a single submitted job. Each destination gets its own dashboard card and log dir.', keywords: 'multi destination broadcast' },
  { subtab: 'quick_start', title: 'Scheduled', summary: 'Recurring snapshot jobs at a cron-like cadence. Snapshot-only today.', keywords: 'cron recurring periodic' },
  // Reference
  { subtab: 'reference', title: 'Run Job form', summary: 'Control-by-control writeup for the Run Job page (modes, libraries, data toggles, advanced settings).' },
  { subtab: 'reference', title: 'Schedules', summary: 'Cadence picker, per-server defaults, history retention.' },
  { subtab: 'reference', title: 'Servers tab', summary: 'Adding / testing / removing servers; per-server defaults; managed-user sync.' },
  // Activity & Phases
  { subtab: 'activity_and_phases', title: 'Activity statuses', summary: 'The 11 activity-feed labels, colours, when each fires, what they mean.', keywords: 'started running resolving writing done failed phase' },
  // API Usage
  { subtab: 'api_usage', title: 'Plex API surface', summary: 'Endpoints the engine hits on Plex (watch state, library sections, playlists, collections).' },
  { subtab: 'api_usage', title: 'Jellyfin API surface', summary: 'Endpoints the engine hits on Jellyfin via the adapter.' },
  { subtab: 'api_usage', title: 'Emby API surface', summary: 'Endpoints the engine hits on Emby via the adapter.' },
  // DB Schema
  { subtab: 'db_schema', title: 'media.db', summary: 'Per-server managed_users, identity map, server-users cache.', keywords: 'managed_users user_identity_map' },
  { subtab: 'db_schema', title: 'snapshots.db', summary: 'Snapshot registry: every captured snapshot with metadata + users + library mix.' },
  { subtab: 'db_schema', title: 'playlist_cache.db', summary: 'Playlist Management cache: per-(server, user) playlist + items rows with v2 identity columns.' },
  { subtab: 'db_schema', title: 'jobs.db / audit.db', summary: 'Run-job history + immutable audit trail.' },
  { subtab: 'db_schema', title: 'auth.db', summary: 'Operator accounts + roles + View Mode sessions.', keywords: 'root_admin db_admin permissions' },
  { subtab: 'db_schema', title: 'Backup and recovery', summary: 'Which bind mounts to back up, how often, what to do when something is lost.', keywords: 'backup recovery preserve restore bind mount server_data snapshots plex_logs keyfile disaster' },
  // Run Logs
  { subtab: 'run_logs', title: 'Per-run log directory', summary: 'Where runtime.log + per-phase logs land; rotation rules.', keywords: 'log_dir runtime.log' },
  { subtab: 'run_logs', title: 'Application logs', summary: 'Long-lived audit logs (db_access.log, playlist_cache.log).' },
  // Troubleshooting
  { subtab: 'troubleshooting', title: 'PIN-protected user with no PIN stored', summary: 'The preflight modal surfaces these before the job runs.', keywords: 'plex home pin home-user' },
  { subtab: 'troubleshooting', title: 'Mixed-media playlists on Plex destination', summary: 'Plex forbids mixed playlists. Skip / dominant / split tunables decide what happens.' },
  { subtab: 'troubleshooting', title: 'Token rejected / 401 on a server', summary: 'Re-enter the auth token on the Servers tab. Tokens rotate when an operator signs out on Plex.tv.' },
  { subtab: 'troubleshooting', title: 'Owner duplicated as a managed user', summary: 'SystemAccount dedup gap when the local account label differs from Plex.tv username.' },
  // Dev Notes
  { subtab: 'dev_notes', title: 'Engines (snapshot / restore / direct / fan-out / adapters)', summary: 'Where each engine lives and how the adapter ABC plugs the three backends in.' },
  { subtab: 'dev_notes', title: 'Databases', summary: 'Every SQLite file + JSON config we keep on disk, what is in each, the rules for adding columns.' },
  { subtab: 'dev_notes', title: 'Live Sync (roadmap)', summary: 'Roadmap notes for the not-yet-shipped continuous source-to-destination sync.' },
  // Users & Roles
  { subtab: 'users_and_roles', title: 'Operator roles', summary: 'root_admin / db_admin / engine_admin / engine_user / view_only — what each can do.', keywords: 'permissions rbac' },
  { subtab: 'users_and_roles', title: 'Finding settings by role', summary: 'Per-role tab + control visibility map.' },
  { subtab: 'users_and_roles', title: 'View Mode', summary: 'Server-side downgrade session: act as a lower-permission role without re-logging in.' },
  // Server Syncing
  { subtab: 'server_syncing', title: 'Contract vs process', summary: 'Library + User Mapping declare what is equivalent (state); Sync Subscriptions run reconciliation on a schedule (process).', keywords: 'library mapping subscription sync state contract process' },
  { subtab: 'server_syncing', title: 'Library Mapping', summary: 'Saved equivalence: which library on server A is the same content as which library on server B.', keywords: 'mapping libraries equivalence cross-server' },
  { subtab: 'server_syncing', title: 'User Mapping', summary: 'Saved equivalence: which user on server A is the same person as which user on server B.', keywords: 'identity user_identity_map cross-server users' },
  { subtab: 'server_syncing', title: 'Sync Subscriptions', summary: 'Ongoing reconciliation of watch counts, ratings, favorites, last watched, or playlists between two servers.', keywords: 'subscription poll worker reconcile watch ratings playlists' },
  { subtab: 'server_syncing', title: 'Conflict policies', summary: 'Max (safest), Sum, Latest-wins, Source-of-truth. Decides which side wins when sides disagree.', keywords: 'max sum latest_wins source_of_truth conflict policy' },
  { subtab: 'server_syncing', title: 'Sync Activity', summary: 'Read-only health view of the sync worker: at-a-glance counts, per-subscription health, recent writes, recent failures.', keywords: 'health activity recent writes failures dry-run' },
  { subtab: 'server_syncing', title: 'Dry-run safety rail', summary: 'New subscriptions start dormant + dry-run. The worker logs intents without writing until you flip Real writes.', keywords: 'dry_run dry-run safety' },
  { subtab: 'server_syncing', title: 'Sync log file', summary: 'Sync activity goes to its own sync.log file so it never bleeds into a running job\'s runtime.log.', keywords: 'sync.log logging runtime.log isolation propagate' },
];


function buildHelpSearchEntries(): HelpSearchEntry[] {
  const out: HelpSearchEntry[] = [];
  // Deep Dive (Features) — every entry pulls its title + shortLabel +
  // extracted body text into the index so a query against any
  // substring of the feature copy surfaces the right card.
  for (const f of FEATURE_ENTRIES) {
    out.push({
      subtab: 'deep_dive',
      title: f.title,
      summary: f.shortLabel,
      keywords: `${f.id} ${extractText(f.body)}`,
      body: f.body,
    });
  }
  // Topics — same shape. Categories enrich the keyword bag.
  for (const t of getAllHelpTopics()) {
    out.push({
      subtab: 'topics',
      title: t.title,
      summary: t.shortLabel,
      keywords: `${t.id} ${t.category} ${extractText(t.body)}`,
      body: t.body,
    });
  }
  // Static section index for the prose-heavy pages.
  out.push(...STATIC_HELP_ENTRIES);
  return out;
}


function GlobalHelpSearchResults({
  query, onJump,
}: {
  query: string;
  onJump: (target: HelpPage) => void;
}) {
  // Build the search index once per mount. Deep Dive + Topics bodies
  // are cheap (already constructed JSX); rebuilds on subsequent
  // GlobalHelpSearchResults mounts cost nothing observable.
  const entries = useMemo(() => buildHelpSearchEntries(), []);
  const hits = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return [] as HelpSearchEntry[];
    return entries.filter((e) => {
      const hay = (
        e.title + ' ' + e.summary + ' ' + (e.keywords || '')
      ).toLowerCase();
      return hay.includes(q);
    });
  }, [entries, query]);

  // Group by subtab for readability. Order matches HELP_TAB_ORDER so
  // Quick Start results land at the top.
  const grouped = useMemo(() => {
    const m = new Map<HelpPage, HelpSearchEntry[]>();
    for (const h of hits) {
      const list = m.get(h.subtab) || [];
      list.push(h);
      m.set(h.subtab, list);
    }
    return HELP_TAB_ORDER
      .map((t) => ({ subtab: t.id, label: t.label, entries: m.get(t.id) || [] }))
      .filter((g) => g.entries.length > 0);
  }, [hits]);

  return (
    <>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>
          Search results
          <span style={{ marginLeft: 8, fontSize: 13, color: 'var(--text-dim)', fontWeight: 400 }}>
            ({hits.length} match{hits.length === 1 ? '' : 'es'} across {grouped.length} sub-tab{grouped.length === 1 ? '' : 's'})
          </span>
        </h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12 }}>
          Searching every Help sub-tab for &ldquo;{query}&rdquo;. Clear the search bar above to return to the sub-tabs.
        </span>
      </div>
      {hits.length === 0 && (
        <div className="panel">
          <div className="empty">No matches. Try a shorter or different keyword.</div>
        </div>
      )}
      {grouped.map((g) => (
        <div key={g.subtab} className="panel">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 10 }}>
            <h2 style={{ margin: 0 }}>
              <span
                style={{
                  display: 'inline-block',
                  background: '#3a5fb0',
                  color: '#fff',
                  padding: '2px 8px',
                  borderRadius: 999,
                  fontSize: 11,
                  fontWeight: 600,
                  marginRight: 8,
                  verticalAlign: 'middle',
                }}
              >
                {g.label}
              </span>
              <span style={{ fontSize: 14, color: 'var(--text-dim)', fontWeight: 400 }}>
                {g.entries.length} match{g.entries.length === 1 ? '' : 'es'}
              </span>
            </h2>
            <button type="button" onClick={() => onJump(g.subtab)} style={{ fontSize: 12 }}>
              Open {g.label} →
            </button>
          </div>
          {g.entries.map((e, i) => (
            <div key={`${e.subtab}-${i}-${e.title}`} style={{ marginTop: i === 0 ? 0 : 14 }}>
              <h3 style={{ marginBottom: 4 }}>{e.title}</h3>
              <div style={{ fontSize: 13, color: 'var(--text-dim)', marginBottom: e.body ? 8 : 0 }}>
                {e.summary}
              </div>
              {e.body && (
                <div style={{ fontSize: 14, lineHeight: 1.55 }}>{e.body}</div>
              )}
            </div>
          ))}
        </div>
      ))}
    </>
  );
}


export function HelpPanel() {
  // Quick Start is the first stop for a new operator. Open it by default
  // so the strip's leftmost tab is what the page actually shows.
  const [page, setPage] = useState<HelpPage>('quick_start');
  // Global search across every subtab. When non-empty, the unified
  // results view overrides the sub-page render below.
  const [globalSearch, setGlobalSearch] = useState('');
  const normalizedSearch = globalSearch.trim().toLowerCase();
  const searchActive = normalizedSearch.length > 0;
  return (
    <>
      <div className="panel" style={{ marginBottom: 8 }}>
        <label className="field" style={{ margin: 0 }}>
          <span className="label">Search all Help pages</span>
          <input
            type="text"
            placeholder="Type to search across every Help sub-tab (titles, body text, ids)"
            value={globalSearch}
            onChange={(e) => setGlobalSearch(e.target.value)}
          />
          <span className="help" style={{ fontSize: 12, color: 'var(--text-dim)' }}>
            Searches Quick Start, Deep Dive, Reference, Topics, Troubleshooting, Activity & Phases, Users & Roles, API / DB / Run Logs, and Dev Notes in one pass. Clear the field to return to the sub-tabs below.
          </span>
        </label>
      </div>
      <nav className="tabs sub-tabs" aria-disabled={searchActive}>
        {HELP_TAB_ORDER.map((t) => (
          <button
            key={t.id}
            className={(!searchActive && page === t.id) ? 'active' : ''}
            onClick={() => {
              setGlobalSearch('');
              setPage(normalizeLegacyHelpPage(t.id));
            }}
            title={searchActive ? 'Clear the search bar above to return to the sub-tabs.' : undefined}
          >
            {t.label}
          </button>
        ))}
      </nav>
      {searchActive ? (
        <GlobalHelpSearchResults
          query={normalizedSearch}
          onJump={(target) => {
            setGlobalSearch('');
            setPage(target);
          }}
        />
      ) : (
        <>
          {page === 'quick_start' && <QuickStartPage />}
          {page === 'reference' && <ReferencePage />}
          {page === 'deep_dive' && <DeepDivePage />}
          {page === 'topics' && <TopicsPage />}
          {page === 'activity_and_phases' && <ActivityStatusesPage />}
          {page === 'api_usage' && <ApiUsagePage />}
          {page === 'db_schema' && <DbSchemaPage />}
          {page === 'run_logs' && <RunLogsPage />}
          {page === 'troubleshooting' && <TroubleshootingPage />}
          {page === 'dev_notes' && <DevNotesPage />}
          {page === 'users_and_roles' && <UsersAndRolesPage />}
          {page === 'server_syncing' && <ServerSyncingHelpPage />}
          {page === 'deployment_map' && <DeploymentMapPage />}
        </>
      )}
    </>
  );
}


// ── How to Use ────────────────────────────────────────────────────────────────
//
// First-stop orientation page for new end users. Walks through what
// each job mode is for, when to pick it, and (for Restore) what the
// Merge / Replace distinction actually does to a destination. The
// Pros/Cons table is the source-of-truth for the warning copy that
// also appears in tooltips on the Run Job form's mode selector and on
// the typed-REPLACE modal - keep them in sync when editing.

function QuickStartPage() {
  return (
    <>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Quick Start</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12 }}>
          Walkthrough for new operators: which job mode to pick and what
          each one actually does. For the design decisions behind each
          mode and the full feature inventory, jump to <strong>Deep
          Dive</strong> next door. The control-by-control writeup lives on
          the <strong>Reference</strong> tab.
        </span>
      </div>

      <div className="panel">
        <h2>Overview</h2>
        <p>
          Hestia-MediaManager captures and moves Plex metadata - <strong>watch
          history</strong>, <strong>ratings</strong>,
          <strong> playlists</strong>, and <strong>collections</strong>.
          It does not touch the items in your library themselves
          (files, metadata agents, posters). The five job modes are:
        </p>
        <ul>
          <li>
            <strong>Snapshot</strong> - capture from one Plex server to
            a <code>.db</code> file on disk. Building block for
            disaster-recovery archives and for any later restore.
          </li>
          <li>
            <strong>Restore</strong> - apply a previously captured
            snapshot back into a Plex server. Default mode is
            additive (<em>Merge</em>); the opt-in <em>Replace</em>
            mode overwrites to match the snapshot exactly.
          </li>
          <li>
            <strong>Direct Transfer</strong> - read from one Plex,
            write straight into another, with no intermediate file.
            Uses the same write path as Restore (Merge / Replace).
          </li>
          <li>
            <strong>Fan-out</strong> - one source, many destinations
            in a single submitted job. Available on Restore and Direct.
          </li>
          <li>
            <strong>Scheduled</strong> - recurring snapshot jobs at a
            cron-like cadence. Schedules are snapshot-only today.
          </li>
        </ul>
      </div>

      <div className="panel">
        <h2>Snapshot</h2>
        <p>
          A snapshot reads everything you've selected (watch history,
          ratings, playlists, collections - any combination via the
          four toggles on the Run Job form) from the source server and
          writes it into a <code>.db</code> in the configured snapshot
          directory. The same job optionally renders a
          <code> .plexexport.json</code> sidecar at the end so the
          first Exports-tab download is instant.
        </p>
        <p>
          Snapshots are nondestructive on the source - they only read.
          The output directory is set under
          <strong> Settings → Servers → Run Defaults</strong>; per-job
          overrides on the Run Job form's <em>Per-Run Settings →
          Advanced</em> sub-tab.
        </p>
      </div>

      <div className="panel">
        <h2>Restore</h2>
        <p>
          A Restore job picks a registered snapshot (or a legacy
          <code> .plexexport.json</code> archive) and writes its data
          into one or more destination Plex servers. The
          <strong> destination is your live Plex</strong>, so this is
          where the choice between Merge and Replace matters.
        </p>

        <h3 style={{ marginBottom: 8 }}>Merge vs Replace</h3>
        <table className="list" style={{ width: '100%' }}>
          <thead>
            <tr>
              <th style={{ width: '20%' }}>Data type</th>
              <th>Merge <em>(default)</em></th>
              <th>Replace</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td><strong>Watch counts</strong></td>
              <td>
                Adds the difference, only ever <em>increasing</em>.
                A higher count on the destination stays as-is.
              </td>
              <td>
                <strong>Resets</strong> to the snapshot's count.
                Plays added after the snapshot are erased
                (<code>markUnplayed</code> + re-scrobble exactly N times).
              </td>
            </tr>
            <tr>
              <td><strong>Ratings</strong></td>
              <td>
                Only set when the destination has <em>no</em> rating.
                Existing ratings are preserved.
              </td>
              <td>
                <strong>Overwrites</strong> the current rating with the
                snapshot's value. Set to null if the snapshot had no rating.
              </td>
            </tr>
            <tr>
              <td><strong>Playlists</strong></td>
              <td>
                Creates the playlist if missing, appends members.
                Existing members and members not in the snapshot are
                preserved.
              </td>
              <td>
                Diffs against the snapshot. Members in both stay,
                members in the snapshot are added, members
                <strong> only on the destination are removed</strong>.
              </td>
            </tr>
            <tr>
              <td><strong>Collections</strong></td>
              <td>
                Same as playlists - creates or appends, never
                removes.
              </td>
              <td>
                Same as playlists in Replace mode - destination-only
                members are <strong>removed</strong>.
              </td>
            </tr>
            <tr>
              <td><strong>Smart playlists</strong></td>
              <td>Skipped (rules captured for inspection only).</td>
              <td>Skipped - Plex rebuilds them from their rule.</td>
            </tr>
            <tr>
              <td><strong>Library items</strong></td>
              <td>Never touched.</td>
              <td>Never touched.</td>
            </tr>
          </tbody>
        </table>

        <h3 style={{ marginBottom: 8, marginTop: 16 }}>When to pick which</h3>
        <ul>
          <li>
            <strong>Pick Merge</strong> when you want to fold one
            server's metadata into another without losing anything on
            the destination. Safe to re-run. This is the right mode
            for migrations between two active servers, periodic sync
            jobs, and almost every casual use.
          </li>
          <li>
            <strong>Pick Replace</strong> for disaster recovery
            (restore a Tuesday snapshot to undo Wednesday's bad
            ingest), point-in-time rollback, or when you specifically
            need the destination to <em>exactly</em> match a known-good
            snapshot. Replace requires you to type <code>REPLACE</code>
            on submit; the auto-capture safety belt (on by default)
            snapshots the destination first so you have a rollback
            point.
          </li>
        </ul>

        <h3 style={{ marginBottom: 8, marginTop: 16 }}>Safety belt: auto-capture before Replace</h3>
        <p>
          With the safety belt on, the worker captures a fresh
          snapshot of the destination <em>before</em> the Replace
          fires. If the pre-snapshot fails, the Replace
          <strong> aborts</strong> - the engine refuses to destroy
          data without a recovery point. The pre-snapshot ID is
          recorded on the job's audit row so you can find it later.
          Leave this on unless you specifically don't want a rollback
          point (e.g. you're restoring into a throwaway test
          instance).
        </p>
      </div>

      <div className="panel">
        <h2>Direct Transfer</h2>
        <p>
          Direct Transfer reads from a source Plex and writes into one
          or more destination Plex servers in a single run, with no
          intermediate file. The capture phase is identical to a
          Snapshot; the write phase is identical to a Restore - so the
          Merge / Replace choice on the Run Job form applies to the
          destination write just like it does for Restore.
        </p>
        <p>
          Direct Transfer is the right call when you don't need an
          archive on disk - e.g. one-shot migrations between two
          active servers, or fan-out from a primary into a small
          herd of secondaries. If you also want a portable archive,
          run a Snapshot job separately.
        </p>
      </div>

      <div className="panel">
        <h2>Playlist Management</h2>
        <p>
          A targeted alternative to a full Direct Transfer when you only
          want to move <em>specific playlists</em> between users or
          servers. Pick a source server, a source user, the individual
          playlists you want, and a destination (server, user). The
          orchestrator routes each copy through the job queue so you
          can stack many at once and they run sequentially.
        </p>
        <ul>
          <li>
            Cartesian fan-out: pick <em>N</em> source playlists ×
            <em> K</em> destination users → submits <em>N × K</em>
            independent copy jobs.
          </li>
          <li>
            Same-user no-op short-circuit: if source and destination
            resolve to the same logical user, the orchestrator skips
            the copy by default. The
            <code> playlist_mgmt_same_user_behavior</code> tunable
            (<strong>Settings → Tunables → Playlist</strong>) flips
            this to &ldquo;duplicate&rdquo; if you want to fork a
            playlist for editing one side.
          </li>
          <li>
            Owner-as-destination always uses the admin token, even
            when fan-out routes other users through their own
            per-user tokens. Owner playlists land under the owner.
          </li>
          <li>
            Active deploys persist beyond a page reload so you can
            navigate away while a batch finishes; the
            ActiveDeploysPanel polls and surfaces per-job results
            (written / skipped / failed) with a Clone Deploy button
            on terminal rows.
          </li>
        </ul>
      </div>

      <div className="panel">
        <h2>Fan-out</h2>
        <p>
          Fan-out is the multi-destination form of Restore and Direct
          Transfer: one job that writes into <em>N</em> destinations.
          Each destination runs in its own dashboard card, with its
          own per-destination log directory and error tracking, so a
          failure on one destination doesn't block the others.
        </p>
        <p>
          Replace mode is per-job, not per-destination - if you pick
          Replace and three destinations, all three get the
          point-in-time overwrite. The safety belt captures a separate
          pre-Replace snapshot for each destination so each one has
          its own rollback point.
        </p>
      </div>

      <div className="panel">
        <h2>Scheduled</h2>
        <p>
          Schedules are recurring snapshot runs at a cron-like
          cadence. The <strong>Schedules</strong> tab lists them and
          accepts new ones; the form mirrors Run Job's Snapshot mode
          plus an interval picker. The same per-server defaults from
          <strong> Servers → Advanced Settings</strong> apply.
        </p>
        <p>
          Schedules are snapshot-only today. Restore + direct
          transfer schedules are tracked in the roadmap; until then,
          stand up an external trigger (cron / systemd / Windows Task
          Scheduler) calling <code>POST /api/job/restore-from-snapshot</code>
          if you need a recurring restore.
        </p>
      </div>

      <div className="panel" style={{ background: 'rgba(74, 122, 252, 0.06)' }}>
        <h2 style={{ marginTop: 0 }}>Next: Deep Dive</h2>
        <p style={{ margin: 0 }}>
          The sections above are an &ldquo;orient + pick the right
          job&rdquo; pass. The <strong>Deep Dive</strong> tab next door
          covers the design rationale for each feature: why
          <em> Merge</em> never decreases watch counts, how the
          adapter ABC handles cross-backend transfers, what the
          Playlist Management job queue guarantees, etc. The global
          search bar at the top of this Help page also scans Deep Dive
          (and every other sub-tab) at once.
        </p>
      </div>
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
    example: '03:12:01 DONE Music Library complete',
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


// Shared <tr> renderer for the Activity Status and Processing Phase
// reference tables. StatusRow and PhaseRow are structurally identical
// (label/color/when/modes/example), and the two map() callbacks below
// rendered identical markup with only the source array differing.
function StatusBadgeRow({ row }: { row: StatusRow | PhaseRow }) {
  return (
    <tr>
      <td>
        <span
          style={{
            display: 'inline-block',
            padding: '2px 8px',
            borderRadius: 4,
            fontWeight: 600,
            fontSize: 11,
            background: row.color + '22',
            color: row.color,
            border: `1px solid ${row.color}55`,
          }}
        >
          {row.label}
        </span>
      </td>
      <td style={{ fontSize: 12, color: 'var(--text-dim)' }}>{row.modes}</td>
      <td>
        <div style={{ fontSize: 13 }}>{row.when}</div>
        {row.example && (
          <div className="mono" style={{ fontSize: 11, color: 'var(--text-dim)', marginTop: 4 }}>
            e.g. {row.example}
          </div>
        )}
      </td>
    </tr>
  );
}


function ActivityStatusesPage() {
  // Nested sub-tab inside the "label reference" panel. Same UX as
  // Account Management → (Database Admin Account | User Accounts):
  // the panel header + the table both swap when the end user clicks
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
                  <StatusBadgeRow key={r.label} row={r} />
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
                  <StatusBadgeRow key={r.label} row={r} />
                ))}
              </tbody>
            </table>

            {/* Stall escalation reference. The dashboard re-colors a
                phase tag amber once its age crosses the per-phase
                amber threshold, and red once it crosses red. End users
                tune the threshold scale via
                Settings ▸ General Settings ▸ Dashboard Stall Colours. */}
            <h3 style={{ marginTop: 24 }}>Stall escalation - when a phase tag changes colour</h3>
            <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 12 }}>
              Each phase has an <strong>amber</strong> threshold and a <strong>red</strong>{' '}
              threshold (in seconds). If a worker stays in one phase past the amber
              window the tag flips to the amber pill; past the red window it flips to
              the red pill and the row pulses. Tune the windows globally with the
              <strong> ETR colour multiplier</strong> under{' '}
              <em>Settings ▸ General Settings</em>. The numbers below are the ship
              defaults (multiplier = 1.0).
            </span>
            <table className="list" style={{ width: '100%', maxWidth: 760 }}>
              <thead>
                <tr>
                  <th style={{ width: '28%' }}>Normal (in-flight)</th>
                  <th style={{ width: '36%' }}>Amber (slow)</th>
                  <th style={{ width: '36%' }}>Red (stuck)</th>
                </tr>
              </thead>
              <tbody>
                {([
                  ['fetching', 45, 120, 'phase'],
                  ['capturing', 30, 60, 'capturing'],
                  ['indexing', 45, 90, 'phase'],
                  ['resolving', 30, 75, 'phase'],
                  ['scrobbling', 20, 45, 'merged'],
                  ['rating', 15, 30, 'rated'],
                  ['merging', 30, 60, 'appended'],
                ] as Array<[string, number, number, string]>).map(([name, a, r, normalCls]) => (
                  <tr key={`stall-${name}`}>
                    <td>
                      <span className={`tag ${normalCls}`}>{name}</span>
                    </td>
                    <td>
                      <span className="tag stall-amber" style={{ marginRight: 6 }}>{name}</span>
                      <span style={{ fontSize: 12, color: 'var(--text-dim)' }}>after {a}s</span>
                    </td>
                    <td>
                      <span className="tag stall-red" style={{ marginRight: 6 }}>{name}</span>
                      <span style={{ fontSize: 12, color: 'var(--text-dim)' }}>after {r}s</span>
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
DONE    Music  Library complete`}
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
// Jellyfin / Emby reference docs. The official-rest sources publish
// OpenAPI; we link to the section root rather than a specific path so
// links keep working when the API version is bumped.
const _JELLYFIN_API = 'https://api.jellyfin.org/';
const _JELLYFIN_WEBHOOK = 'https://github.com/jellyfin/jellyfin-plugin-webhook';
const _EMBY_API = 'https://swagger.emby.media/';
const _EMBY_WEBHOOK = 'https://github.com/MediaBrowser/Emby/wiki/Webhooks';

interface ApiCall {
  name: string;       // python-plexapi method name OR HTTP endpoint path
  href: string;       // documentation URL
  source: 'plexapi' | 'community' | 'official-rest' | 'plugin';
  what: string;       // plain-English "what it does on the server side"
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
      {service === 'jellyfin' && <JellyfinApiSection />}
      {service === 'emby' && <EmbyApiSection />}
    </>
  );
}


// ── Adapter-status banner used by both Jellyfin and Emby sections.
//
// The endpoints in those sections describe what the corresponding
// adapter WILL call once the adapter implementation lands. Until
// then, the registry refuses to save servers whose service field is
// anything other than ``plex``, so no calls actually fire. Keeping
// the catalogue visible in advance gives the operator a way to audit
// the design and surface concerns before code lands.
function AdapterUnderDevelopmentBanner({ name }: { name: string }) {
  return (
    <div className="panel" style={{ borderLeft: '3px solid var(--color-warn, #d4a72c)' }}>
      <h2 style={{ marginTop: 0 }}>{name} adapter under development</h2>
      <p style={{ fontSize: 13, color: 'var(--text-dim)', marginTop: 0 }}>
        The endpoints below describe what the {name} adapter will call once
        the multi-backend PR ships. Today the server registry refuses to
        save a server whose service field is anything other than{' '}
        <code>plex</code>, so no {name} calls fire from this app yet. The
        catalogue is published in advance so the operator can audit the
        plan and flag concerns.
      </p>
      <p style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 8 }}>
        A few design decisions that affect what some of these endpoints
        will be called with (rating mapping, user identifier choice,
        collection scope, owner identity) are still open; the help text
        below stays neutral on those decisions instead of pre-committing
        to a recommendation.
      </p>
    </div>
  );
}


// ── Jellyfin API catalogue ──────────────────────────────────────────────────
//
// Ten groups: eight that mirror the Plex section's structure call-for-call
// so the three tabs read as siblings, plus two Jellyfin-specific groups
// covering user management (no Plex analogue) and webhooks (Feature 5
// foundation).
//
// Sources confirmed against api.jellyfin.org (the OpenAPI reference) and
// the jellyfin-plugin-webhook repo for webhook endpoints. Per the
// authoring rules in the outline, the ``why`` line on each entry names
// the adapter method or pipeline step it implements so a reader can
// grep for it once the adapter ships.

const JELLYFIN_API_GROUPS: ApiGroup[] = [
  {
    title: 'Connecting & identifying servers',
    intro:
      "Jellyfin servers expose a REST API at the operator-configured URL. Authentication is done with an admin API key (created in the dashboard) and carried on every request via a structured Authorization header. The adapter opens one requests.Session per server connection and reuses it for the run, sharing the existing retry adapter that the Plex code path uses today.",
    calls: [
      {
        name: 'POST /Users/AuthenticateByName',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: "Authenticates a username / password pair and returns an AccessToken plus the user's UUID. The AccessToken is then reused on subsequent requests in the same session.",
        why: "Used when the operator registers a Jellyfin server using a per-user login rather than a pre-issued API key. The adapter caches the returned token in the encrypted credential store the same way Plex tokens are cached today.",
      },
      {
        name: 'Authorization: MediaBrowser Token="...", Client="...", DeviceId="...", Version="..."',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: 'Structured Authorization header carried on every authenticated request. Jellyfin also accepts the legacy X-Emby-Token header, but the MediaBrowser scheme is the documented preferred form.',
        why: "The adapter builds this header once when the connection opens and lets requests.Session attach it to every call, matching the way the Plex code path attaches X-Plex-Token implicitly through plexapi.",
        note: "Client / DeviceId / Version are required identification fields, not optional. The adapter passes a stable per-install DeviceId so Jellyfin's session list doesn't show a fresh entry for every Hestia-MediaManager run.",
      },
      {
        name: 'GET /System/Info',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: "Returns the server's stable Id (the machineIdentifier equivalent), Name, Version, and OperatingSystem.",
        why: "Drives the same registry keying we already use for Plex: the server's Id is the foreign key in media.db, so renaming the server in Jellyfin's dashboard doesn't look like a brand-new server to Hestia-MediaManager.",
      },
      {
        name: 'GET /System/Ping',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: 'Lightweight liveness check; returns 200 with a short string body when the server is reachable and not in a startup state.',
        why: 'Used by the Servers panel reachability indicator and as a fast preflight before any large run, so a server that is down at submit time fails the job up front rather than midway through capture.',
      },
    ],
  },
  {
    title: 'Reading users on a server',
    intro:
      "Jellyfin and Emby differ materially from Plex here: every user is server-local (there is no plex.tv-style external account graph), and an admin token can write on behalf of any user via UserId in the URL path. The adapter enumerates users with one call and does not need to round-trip per-user tokens for read or write paths.",
    calls: [
      {
        name: 'GET /Users',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: "Returns the full UserDto array for every account known to this server (administrators and ordinary users alike).",
        why: "Powers the snapshot user-enumeration step. Replaces the Plex-side combination of server.systemAccounts() + account.users() with a single call; there is no plex.tv hop because Jellyfin's user list is purely server-local.",
      },
      {
        name: 'GET /Users/{userId}',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: 'Returns one UserDto, including the user policy (admin flag, enabled-libraries list, parental rating limits).',
        why: 'Used during the preflight user-diff so the create-user-on-destination modal can show the operator each missing user with the source\'s policy as a starting point.',
      },
    ],
  },
  {
    title: 'Browsing the library',
    intro:
      "Libraries are addressed by GUID Ids (not the numeric keys Plex uses); item listings are paginated and require explicit StartIndex / Limit. The adapter takes care of pagination internally so the snapshotter sees the same list-of-items return shape it gets from the Plex code path.",
    calls: [
      {
        name: 'GET /Library/MediaFolders',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: "Lists every library on the server (admin context). Each entry carries an Id (GUID), a Name, and a CollectionType in the {movies, tvshows, music, mixed, playlists, ...} set.",
        why: "Powers the library picker on Run Job and Schedules forms when a Jellyfin server is selected. Library identity is the GUID so a rename does not invalidate the per-server library_sections row.",
      },
      {
        name: 'GET /Users/{userId}/Views',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: 'Lists the libraries one particular user has access to. Same response shape as MediaFolders but already filtered by the user policy.',
        why: 'Used when the operator has scoped a job to one managed user, so the library picker shows only libraries that user actually sees.',
      },
      {
        name: 'GET /Users/{userId}/Items?ParentId={libraryId}&Recursive=true&IncludeItemTypes=Movie,Episode,Audio&Fields=ProviderIds,Path,MediaSources,UserData&StartIndex=N&Limit=N',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: "Recursive leaf-level item walk inside one library. Returns Movies / Episodes / Tracks directly (the playable leaves) rather than parent Show / Artist containers. The Fields list opts into the GUID providers (Imdb, Tmdb, Tvdb, MusicBrainz) and per-user state in one round trip.",
        why: 'This is the workhorse of snapshot capture on a Jellyfin source. Equivalent to Plex section.searchTracks / searchEpisodes / search at the same logical level: leaves-with-metadata in one call.',
        note: 'Pagination is operator-invisible: the adapter loops StartIndex internally until the response is shorter than the page size, then yields the joined result. Page size matches the operator-tunable smart_bulk_threshold_items default of 5000.',
      },
      {
        name: 'GET /Users/{userId}/Items?... &Filters=IsPlayed',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: 'Server-side filter returning only items the user has marked played at least once.',
        why: 'The Jellyfin analogue of section.search(viewCount__gt=0). Used by the Smart watch+ratings strategy to fetch just the played items when the operator does not want a full library walk.',
      },
      {
        name: 'GET /Users/{userId}/Items?... &Filters=IsFavorite',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: 'Server-side filter returning only items the user has marked favorite.',
        why: "Jellyfin's rating model is split into a binary favorite flag (always exposed) and a numeric rating on UserData (newer builds). The favorite filter is the cheap path when the destination only needs to know which items are bookmarked.",
        note: "Plex has one rating concept (0.5-10 stars). Mapping between Plex stars and Jellyfin's split favorite + numeric model is an open design decision.",
      },
    ],
  },
  {
    title: 'Finding the same item on a different server',
    intro:
      "Cross-server matching reuses the same GUID-keyed strategy Hestia-MediaManager already uses for Plex source-to-Plex destination runs. Jellyfin items carry their external provider GUIDs on UserItemDataDto.ProviderIds (Imdb, Tmdb, Tvdb, MusicBrainz), which feed straight into services/guid_translator.py.",
    calls: [
      {
        name: 'GET /Users/{userId}/Items/{itemId}?Fields=ProviderIds,Path,UserData',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: "Single-item fetch by per-server Id (the ratingKey equivalent). One round trip, no search.",
        why: 'Tier 0 of the resolver. After a successful GUID match on the first run, the adapter writes the (item, server, jellyfin_id) tuple into server_items the same way the Plex adapter writes (item, server, rating_key), so the second run on the same server pair goes straight to this call.',
      },
      {
        name: 'GET /Users/{userId}/Items?Recursive=true&Filters=...&AnyProviderIdEquals={scheme}.{id}',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: "Searches the server for an item that carries the supplied external GUID (Imdb.tt0133093 etc.).",
        why: "Tier 1 of the resolver. Used the first time a snapshot item is restored to a destination, when server_items has no cached mapping. The result is then cached so the next run hits Tier 0.",
        note: "Per-server item IDs are stored as TEXT (rather than INTEGER) because Jellyfin item IDs are GUID strings, not integers.",
      },
    ],
  },
  {
    title: 'Playlists',
    intro:
      "Jellyfin playlists are first-class items with their own Id; the read paths are the same recursive Items query used elsewhere, narrowed by IncludeItemTypes=Playlist. Add / create take a comma-separated Ids list in one call, so the URL-length chunking workaround the Plex adapter applies is not needed here.",
    calls: [
      {
        name: 'GET /Users/{userId}/Items?IncludeItemTypes=Playlist&Recursive=true',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: 'Lists all playlists visible to one user.',
        why: 'Powers snapshot enumeration of per-user playlists during capture.',
      },
      {
        name: 'GET /Playlists/{playlistId}/Items?UserId={userId}',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: 'Returns the ordered member list of one playlist as seen by the supplied user.',
        why: 'Drives per-playlist member capture. Ordering is preserved on serialise so the destination ends up with the same sequence.',
      },
      {
        name: 'POST /Playlists?Name={name}&Ids={a,b,c}&UserId={userId}&MediaType={Audio|Video}',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: 'Creates a new playlist owned by the supplied user, optionally pre-populated with a comma-separated Ids list.',
        why: "Used by the restorer when a destination is missing a playlist the snapshot recorded. MediaType is required at create time because Jellyfin playlists are typed (audio vs video).",
        note: "Plex's create flow returns the new ratingKey; Jellyfin returns the Id in the response body. The adapter normalises both into a single playlist_id string the rest of the engine consumes.",
      },
      {
        name: 'POST /Playlists/{playlistId}/Items?Ids={a,b,c}&UserId={userId}',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: 'Appends one or more items to an existing playlist in a single call.',
        why: 'Used to top up an existing destination playlist when the snapshot adds new members. No 100-item chunking is needed (the Plex 100-cap exists because of URL-length, not API limits).',
      },
    ],
  },
  {
    title: 'Collections',
    intro:
      "Jellyfin and Emby BoxSets are server-wide rather than library-scoped (Plex collections are scoped to one library). Hestia-MediaManager surfaces this as a divergence on cross-scope writes; the collection-scope behaviour is an open design decision and is not pre-committed here.",
    calls: [
      {
        name: 'GET /Users/{userId}/Items?IncludeItemTypes=BoxSet&Recursive=true',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: 'Lists every BoxSet (Jellyfin collection) on the server visible to the user.',
        why: 'Snapshot enumeration of collections during capture. Server-wide scope means the adapter does not need a per-library loop here.',
      },
      {
        name: 'GET /Users/{userId}/Items?ParentId={boxSetId}',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: 'Returns the member list of one BoxSet.',
        why: 'Per-collection member capture, the same way section.collections + collection.items composes on the Plex side.',
      },
      {
        name: 'POST /Collections?Name={name}&Ids={a,b,c}',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: 'Creates a new BoxSet, optionally pre-populated. No library binding (BoxSets are server-wide).',
        why: 'Used when the restorer needs to create a destination BoxSet that does not yet exist.',
      },
      {
        name: 'POST /Collections/{collectionId}/Items?Ids={a,b,c}',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: 'Appends items to an existing BoxSet in a single call.',
        why: 'Used to top up an existing destination BoxSet when the snapshot adds new members.',
      },
    ],
  },
  {
    title: 'Writing back to the server (watch, rating, favorite, resume)',
    intro:
      "Jellyfin exposes dedicated endpoints for each write concern. Crucially, an admin token can write on behalf of any user via UserId in the URL, so the per-user-token round-trip the Plex adapter performs is not needed. Position values are in 100-nanosecond ticks (10,000 ticks = 1 ms); the adapter converts to and from milliseconds at the boundary.",
    calls: [
      {
        name: 'POST /Users/{userId}/PlayedItems/{itemId}?DatePlayed={ISO8601}',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: 'Marks an item played for the supplied user; increments PlayCount; sets LastPlayedDate to the supplied timestamp. Returns the updated UserItemDataDto.',
        why: 'Powers the restorer\'s "mark as watched" step. The response carries the resulting PlayCount, but the merge-strategy decision (higher / sum / replace) is made client-side; the adapter exposes a set_watched(count) primitive that loops this call when an increment-style write is needed.',
      },
      {
        name: 'DELETE /Users/{userId}/PlayedItems/{itemId}',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: 'Clears the played flag on an item for one user; resets PlayCount and LastPlayedDate.',
        why: 'Used by the Replace restore mode when the snapshot records the item as unwatched but the destination has it marked played. No Plex equivalent: Plex does not expose an unwatched-flip endpoint, so the Replace mode there has a documented gap that does not apply on Jellyfin.',
      },
      {
        name: 'POST /Users/{userId}/Items/{itemId}/UserData',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: 'Sets one or more user-data fields in a single call: Played, PlayCount, PlaybackPositionTicks, IsFavorite, Rating.',
        why: 'The adapter\'s combined-write fast path. Used when a single item has multiple changes in one snapshot (e.g. mark watched + set rating); avoids three round trips.',
        note: 'PlaybackPositionTicks is in 100-nanosecond units; the adapter multiplies milliseconds by 10000 on write and divides on read.',
      },
      {
        name: 'POST /Sessions/Playing/Progress',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: 'Posts a play-progress update with PositionTicks, ItemId, SessionId. Mirrors a live client reporting playback progress.',
        why: 'Alternative resume-position write path. Used when the operator needs the destination to behave as if the user had just paused (e.g. for handoff scenarios). The UserData path is the simpler write for static resume points.',
      },
      {
        name: 'POST /Users/{userId}/Items/{itemId}/Rating?Likes={true|false}',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: "Sets the binary favorite-style rating on an item.",
        why: "One of two rating-write paths. The numeric Rating field is set via the UserData endpoint instead. Which is used per item depends on the rating-mapping behaviour, which is still an open design decision.",
      },
      {
        name: 'POST /Users/{userId}/FavoriteItems/{itemId}',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: 'Marks an item as favorite for one user. DELETE clears the favorite flag.',
        why: 'Distinct from the rating endpoint: favorite is a binary flag independent of numeric rating. Snapshot captures both; restore applies both.',
      },
    ],
  },
  {
    title: 'Library refresh (after writes)',
    intro:
      "Jellyfin updates UserData immediately on the writes above, so the post-restore refresh step the Plex adapter sometimes triggers is not generally required. The adapter still exposes refresh endpoints for the operator-opt-in path when the run summary suggests a refresh is wanted.",
    calls: [
      {
        name: 'POST /Items/{itemId}/Refresh',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: 'Forces Jellyfin to re-scan and re-match the metadata for one item.',
        why: 'Operator-opt-in only. The adapter does not call this by default; mass-refresh during a multi-thousand-item restore can leave the destination UI sluggish for minutes.',
      },
      {
        name: 'POST /Library/Refresh',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: 'Triggers a full library scan on the server.',
        why: 'Reserved for operator-initiated maintenance. Hestia-MediaManager never fires this from a run.',
      },
    ],
  },
  {
    title: 'User management (no Plex equivalent)',
    intro:
      "Plex sharing happens through plex.tv UI; Hestia-MediaManager cannot create or delete Plex users. Jellyfin and Emby expose first-class user-management endpoints, which is what enables the create-users-on-destination flow during direct transfer. That flow is on the roadmap and not yet shipped.",
    calls: [
      {
        name: 'POST /Users/New',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: 'Creates a new user with the supplied Name and Password. Returns the new UserDto including the assigned Id.',
        why: "Used during direct transfer when the source has a user the destination does not. The operator confirms each new user in the preflight modal; the adapter then issues this call per row and persists the resulting Id in the per-job mapping (schema delta SD-3).",
      },
      {
        name: 'POST /Users/{userId}/Password',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: 'Changes an existing user\'s password. Takes CurrentPw and NewPw, or ResetPassword=true to clear without knowing the current.',
        why: 'Used by the operator-initiated password rotation in the User Management panel after a transfer. Not required by the create flow itself (POST /Users/New takes the initial password).',
      },
      {
        name: 'POST /Users/{userId}/Policy',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: 'Updates a user\'s policy: IsAdministrator, IsDisabled, EnableAllFolders, EnabledFolders, parental rating limits.',
        why: 'Applied during user creation to mirror the source user\'s library-access policy onto the destination. The operator can edit the proposed policy in the preflight modal before submit.',
      },
      {
        name: 'DELETE /Users/{userId}',
        href: _JELLYFIN_API,
        source: 'official-rest',
        what: 'Permanently deletes a user account on the server.',
        why: 'Exposed by the adapter for completeness; Hestia-MediaManager does not call DELETE during any automated run. Reserved for operator-initiated cleanup through the User Management panel, behind a typed-confirmation gate.',
      },
    ],
  },
  {
    title: 'Webhooks (Feature 5 foundation)',
    intro:
      "The Jellyfin webhook plugin installs from the plugin catalog and fires application/json payloads to Hestia-MediaManager's webhook receiver. The event types listed here are the ones Hestia-MediaManager's sync normaliser cares about; the plugin itself emits more events that Hestia-MediaManager ignores. Implementation is on the roadmap and not yet shipped.",
    calls: [
      {
        name: 'PlaybackStart / PlaybackProgress / PlaybackStop',
        href: _JELLYFIN_WEBHOOK,
        source: 'plugin',
        what: 'Fired by the webhook plugin when a session enters / progresses through / leaves a play state. Payload carries UserId, ServerId, ItemId, and ProviderIds.',
        why: 'Drives live watch sync. PlaybackStop with a sufficiently-watched threshold maps to a scrobble on every linked destination through services/sync_normalizer.py.',
      },
      {
        name: 'UserDataSaved',
        href: _JELLYFIN_WEBHOOK,
        source: 'plugin',
        what: 'Fired when a UserData write happens (rating change, favorite flip, played flag change).',
        why: 'The Jellyfin-side trigger for rating sync and favorite sync. Plex does not have an analogous webhook event, so cross-backend rating sync is one-way from Jellyfin / Emby to Plex unless the operator wires a polling loop.',
      },
      {
        name: 'ItemAdded',
        href: _JELLYFIN_WEBHOOK,
        source: 'plugin',
        what: 'Fired when the library scanner adds a new item.',
        why: 'Used by the targeted-scan endpoint to refresh Hestia-MediaManager\'s cached item identity for one new item without a full library walk.',
      },
    ],
  },
];


// ── Emby API catalogue ─────────────────────────────────────────────────────
//
// Emby shares its REST ancestry with Jellyfin (the projects forked from a
// common base) so the endpoint paths overlap substantially. The catalogue
// below documents only the endpoints that diverge from Jellyfin or that
// Hestia-MediaManager's Emby adapter calls in a materially different way. For
// shared endpoints the Jellyfin tab is authoritative; the Emby tab
// cross-references it rather than duplicating the row.

const EMBY_API_GROUPS: ApiGroup[] = [
  {
    title: 'Connecting & identifying servers',
    intro:
      "Emby's REST API mirrors Jellyfin's at the path level for most discovery endpoints, but the Authorization header uses an Emby scheme rather than Jellyfin's MediaBrowser scheme. The adapter builds the header once per connection and reuses one requests.Session for the run, the same shape as the Jellyfin and Plex adapters.",
    calls: [
      {
        name: 'POST /Users/AuthenticateByName',
        href: _EMBY_API,
        source: 'official-rest',
        what: "Authenticates a username / password and returns AccessToken + User.Id. Identical request shape to Jellyfin; the response is read the same way.",
        why: 'Used when an Emby server is registered with credentials rather than a pre-issued API key. Caching follows the same encrypted credential store pattern as the Plex and Jellyfin adapters.',
      },
      {
        name: 'Authorization: Emby UserId="...", Token="...", Client="...", DeviceId="...", Version="..."',
        href: _EMBY_API,
        source: 'official-rest',
        what: 'Structured Authorization header carried on every authenticated request. Emby also accepts X-Emby-Token as a legacy alternative; the structured form is the documented preferred path.',
        why: "The single material divergence from Jellyfin at the auth layer: the scheme name is 'Emby' rather than 'MediaBrowser'. The shared HTTP-adapter mixin parameterises this so the rest of the request-construction code is shared between Jellyfin and Emby.",
      },
      {
        name: 'GET /System/Info',
        href: _EMBY_API,
        source: 'official-rest',
        what: "Same payload shape as Jellyfin: server Id, Name, Version, OperatingSystem.",
        why: 'Same use as on the Jellyfin tab: stable foreign key for media.db, detects renames without invalidating cached identity.',
      },
      {
        name: 'GET /System/Ping',
        href: _EMBY_API,
        source: 'official-rest',
        what: 'Liveness check; returns 200 with a short body when the server is reachable.',
        why: 'Used by the Servers panel reachability indicator and the preflight gate before large runs.',
      },
    ],
  },
  {
    title: 'Reading users on a server',
    intro:
      "User enumeration is identical to Jellyfin: GET /Users returns the full UserDto array, and admin tokens can write on behalf of any user via UserId in the URL path. The adapter shares this code path with the Jellyfin adapter through the _HttpMediaAdapter mixin.",
    calls: [
      {
        name: 'GET /Users  ·  GET /Users/{userId}',
        href: _EMBY_API,
        source: 'official-rest',
        what: "Same response shape as the Jellyfin endpoints of the same name. See the Jellyfin tab for the full description.",
        why: "Shared code path with the Jellyfin adapter; documented separately here only so the Emby tab is browseable on its own.",
      },
    ],
  },
  {
    title: 'Browsing the library',
    intro:
      "Library and item endpoints mirror Jellyfin's at the path level. Two material divergences worth flagging: Emby exposes a MinUserRating filter (Jellyfin uses Filters=IsFavorite as the favorite-only filter), and Emby's CollectionType enum has a slightly different vocabulary in older builds.",
    calls: [
      {
        name: 'GET /Library/MediaFolders  ·  GET /Users/{userId}/Views',
        href: _EMBY_API,
        source: 'official-rest',
        what: 'Same response shape as the Jellyfin endpoints of the same name.',
        why: 'Same use: library picker, per-user scoping. See the Jellyfin tab.',
      },
      {
        name: 'GET /Users/{userId}/Items?ParentId={libraryId}&Recursive=true&IncludeItemTypes=Movie,Episode,Audio&Fields=ProviderIds,Path,MediaSources,UserData&StartIndex=N&Limit=N',
        href: _EMBY_API,
        source: 'official-rest',
        what: 'Recursive leaf-level item walk inside one library. Same path and parameter set as Jellyfin.',
        why: 'Workhorse of snapshot capture on an Emby source. Shared code path with the Jellyfin adapter.',
      },
      {
        name: 'GET /Users/{userId}/Items?... &Filters=IsPlayed',
        href: _EMBY_API,
        source: 'official-rest',
        what: "Server-side filter for items the user has marked played. Same semantics as Jellyfin.",
        why: 'Used by the Smart strategy on Emby sources.',
      },
      {
        name: 'GET /Users/{userId}/Items?... &MinUserRating=1',
        href: _EMBY_API,
        source: 'official-rest',
        what: "Server-side filter returning items with a numeric rating at or above the supplied value. Emby's preferred analogue of Jellyfin's binary IsFavorite filter when the operator wants numeric thresholding.",
        why: "Emby's rating model exposes both the binary favorite and a numeric rating; Hestia-MediaManager picks the filter at run time. The rating-mapping behaviour is still an open design decision.",
      },
    ],
  },
  {
    title: 'Finding the same item on a different server',
    intro:
      "Cross-server identity uses the same ProviderIds-on-UserItemDataDto path as Jellyfin. Schema delta SD-1 (storing per-server item IDs as TEXT instead of INTEGER) applies to Emby for the same reason it applies to Jellyfin: Emby item IDs are GUID strings.",
    calls: [
      {
        name: 'GET /Users/{userId}/Items/{itemId}?Fields=ProviderIds,Path,UserData',
        href: _EMBY_API,
        source: 'official-rest',
        what: "Single-item fetch by per-server Id. Same payload shape as Jellyfin.",
        why: 'Tier 0 of the resolver, same way as the Jellyfin adapter writes through server_items.',
      },
      {
        name: 'GET /Users/{userId}/Items?Recursive=true&AnyProviderIdEquals={scheme}.{id}',
        href: _EMBY_API,
        source: 'official-rest',
        what: 'Searches by external GUID. Same parameter shape as Jellyfin.',
        why: 'Tier 1 of the resolver. Result is cached into server_items so the second run hits Tier 0.',
      },
    ],
  },
  {
    title: 'Playlists',
    intro:
      "Playlist read and write paths are identical to Jellyfin (POST /Playlists with comma-separated Ids, POST /Playlists/{id}/Items for appends). The adapter shares this code path with the Jellyfin adapter.",
    calls: [
      {
        name: 'GET /Users/{userId}/Items?IncludeItemTypes=Playlist&Recursive=true  ·  GET /Playlists/{id}/Items  ·  POST /Playlists  ·  POST /Playlists/{id}/Items',
        href: _EMBY_API,
        source: 'official-rest',
        what: "Same payload shapes as the equivalent Jellyfin endpoints. Documented in detail on the Jellyfin tab.",
        why: 'Shared code path with the Jellyfin adapter; documented separately here only so the Emby tab is browseable on its own.',
      },
    ],
  },
  {
    title: 'Collections',
    intro:
      "Emby BoxSets are server-wide, matching Jellyfin's scope and diverging from Plex's library-scoped collections. How Hestia-MediaManager handles this scope divergence on cross-scope writes is still an open design decision.",
    calls: [
      {
        name: 'GET /Users/{userId}/Items?IncludeItemTypes=BoxSet  ·  POST /Collections  ·  POST /Collections/{id}/Items',
        href: _EMBY_API,
        source: 'official-rest',
        what: 'Same payload shapes as the equivalent Jellyfin endpoints.',
        why: 'Shared code path with the Jellyfin adapter.',
      },
    ],
  },
  {
    title: 'Writing back to the server (watch, rating, favorite, resume)',
    intro:
      "Write endpoints mirror Jellyfin's path-for-path. PlaybackPositionTicks is in 100-nanosecond units (10,000 ticks = 1 ms) on Emby too; the conversion is done in the shared _HttpMediaAdapter mixin.",
    calls: [
      {
        name: 'POST /Users/{userId}/PlayedItems/{itemId}?DatePlayed={ISO8601}',
        href: _EMBY_API,
        source: 'official-rest',
        what: 'Marks an item played; increments PlayCount; sets LastPlayedDate. DELETE clears the flag.',
        why: 'Powers the restorer\'s "mark as watched" step on Emby destinations.',
      },
      {
        name: 'POST /Users/{userId}/Items/{itemId}/UserData',
        href: _EMBY_API,
        source: 'official-rest',
        what: 'Combined UserData write: Played, PlayCount, PlaybackPositionTicks, IsFavorite, Rating.',
        why: "Adapter's combined-write fast path. Used when one item has multiple changes in one snapshot.",
      },
      {
        name: 'POST /Users/{userId}/Items/{itemId}/Rating?Likes={true|false}  ·  POST /Users/{userId}/FavoriteItems/{itemId}',
        href: _EMBY_API,
        source: 'official-rest',
        what: 'Binary favorite-style rating write paths. The numeric Rating field is set through the UserData endpoint.',
        why: "Two write paths so the adapter can match the rating-mapping decision (D-RATE) at run time.",
      },
    ],
  },
  {
    title: 'Library refresh (after writes)',
    intro:
      "Emby applies UserData writes immediately, the same as Jellyfin, so post-restore refresh is not required for the writes Hestia-MediaManager issues. The endpoints exist for operator-opt-in maintenance only.",
    calls: [
      {
        name: 'POST /Items/{itemId}/Refresh  ·  POST /Library/Refresh',
        href: _EMBY_API,
        source: 'official-rest',
        what: 'Same shapes as Jellyfin.',
        why: 'Operator-opt-in only; Hestia-MediaManager never fires these from a run.',
      },
    ],
  },
  {
    title: 'User management (no Plex equivalent)',
    intro:
      "Emby exposes the same user-management endpoints as Jellyfin (POST /Users/New, POST /Users/{id}/Password, POST /Users/{id}/Policy, DELETE /Users/{id}). The create-users-on-destination flow (on the roadmap, not yet shipped) works the same way against Emby.",
    calls: [
      {
        name: 'POST /Users/New  ·  POST /Users/{id}/Password  ·  POST /Users/{id}/Policy  ·  DELETE /Users/{id}',
        href: _EMBY_API,
        source: 'official-rest',
        what: 'Identical request shapes to the Jellyfin equivalents.',
        why: 'Shared code path with the Jellyfin adapter.',
      },
    ],
  },
  {
    title: 'Webhooks (Feature 5 foundation)',
    intro:
      "Emby ships a built-in Webhooks plugin (Jellyfin's is an installable add-on). The payload shape mirrors Plex's multipart-with-JSON form rather than Jellyfin's application/json, so Hestia-MediaManager's webhook receiver keeps separate parsers per backend. Implementation is on the roadmap and not yet shipped.",
    calls: [
      {
        name: 'playback.start / playback.stop',
        href: _EMBY_WEBHOOK,
        source: 'plugin',
        what: 'Fired when a session starts or stops. Payload carries User.Id, Server.Id, Item.Id, Item.ProviderIds.',
        why: 'Drives live watch sync from Emby sources; playback.stop with a sufficiently-watched threshold maps to a scrobble on every linked destination.',
      },
      {
        name: 'item.markplayed',
        href: _EMBY_WEBHOOK,
        source: 'plugin',
        what: 'Fired when an item is marked played outside a playback session (e.g. via the UI mark-as-watched menu).',
        why: 'The Emby-side trigger for non-playback watch state changes. Hestia-MediaManager\'s normaliser treats it the same as playback.stop with full watch.',
      },
      {
        name: 'item.rate',
        href: _EMBY_WEBHOOK,
        source: 'plugin',
        what: 'Fired when a rating or favorite flag changes on an item.',
        why: 'The Emby-side trigger for rating sync. Cross-backend rating sync from Emby into Plex is subject to the same one-way limitation noted on the Jellyfin tab.',
      },
    ],
  },
];


function JellyfinApiSection() {
  return (
    <>
      <AdapterUnderDevelopmentBanner name="Jellyfin" />
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Jellyfin</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12 }}>
          Every call the {' '}
          <strong>Jellyfin adapter</strong> will make against a Jellyfin
          server, mirrored against the Plex tab's structure so the two
          read as siblings. Endpoint paths link to the official OpenAPI
          reference at api.jellyfin.org; webhook entries link to the
          jellyfin-plugin-webhook repository.
        </span>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginTop: 8 }}>
          Each entry's "why" line names the adapter method or pipeline
          step the endpoint implements so a reader can grep the codebase
          for it once the adapter ships.
        </span>
      </div>

      {JELLYFIN_API_GROUPS.map((group) => (
        <ApiGroupTable key={group.title} group={group} />
      ))}
    </>
  );
}


function EmbyApiSection() {
  return (
    <>
      <AdapterUnderDevelopmentBanner name="Emby" />
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Emby</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12 }}>
          Endpoint catalogue for the Emby adapter. Because Emby and
          Jellyfin share REST ancestry, only divergences from the
          Jellyfin tab are documented in full here; for shared endpoints
          this tab cross-references the Jellyfin entry rather than
          duplicating the row. Endpoint paths link to the official Emby
          Swagger reference.
        </span>
      </div>

      {EMBY_API_GROUPS.map((group) => (
        <ApiGroupTable key={group.title} group={group} />
      ))}
    </>
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
        <ApiGroupTable key={group.title} group={group} />
      ))}
    </>
  );
}


function _sourceBadgeLabel(source: ApiCall['source']): string {
  switch (source) {
    case 'plexapi':       return 'python-plexapi (official)';
    case 'community':     return 'community-maintained reference';
    case 'official-rest': return 'official REST API documentation';
    case 'plugin':        return 'plugin-shipped feature';
  }
}


function ApiGroupTable({ group }: { group: ApiGroup }) {
  return (
    <div className="panel">
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
                    {_sourceBadgeLabel(c.source)}
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
      "media.db is the longest-lived SQLite file the app owns. It accumulates state from every snapshot, restore, and direct-transfer run, so on the second run of the same source server the resolver can skip the slow GUID-lookup round-trip and go straight to the cached ratingKey. The schema is split into four concerns: a GUID-keyed item pool that's shared across every server the app has ever talked to; per-(item, server) join tables that pin each item's local identity on each server; the v0.13.0 identity layer in server_users that names the people who own / have access to each server (with role + multi-backend tag); and per-server activity tables (watch events, ratings, playlist members, collection members) that record what each user on each server has done with each item.\n\nThe split matters because Plex's `ratingKey` is a per-server identifier: the same movie has different ratingKeys on Server A and Server B, but the same `imdb://` GUID on both. We key the items table by GUID so cross-server matching is structural rather than discovered each run.\n\nThe v0.13.0 identity layer (server_users) replaced an earlier convention where the server owner was an implicit sentinel - an empty-string `user_handle` on every activity row. That worked while we only spoke to Plex, but Jellyfin and Emby have first-class owner / admin / managed concepts that don't fit an empty-string-is-the-owner trick. Lifting identity into its own table with a role column and a backend tag means the engine drives the same CRUD path for every backend, and the schema absorbs multi-admin servers (Jellyfin) without another migration.",
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
        name: 'server_users',
        purpose: 'Per-server identity layer (v0.13.0). One row per known user on a server - owner or managed - with role, display name, and a backend tag so the engine can drive Plex / Jellyfin / Emby uniformly.',
        key_columns: ['server_id', 'user_handle', 'role', 'backend', 'display_name'],
        columns: [
          { name: 'id', type: 'INTEGER PRIMARY KEY AUTOINCREMENT', note: 'Internal id. Referenced by server_user_id on every wide table (watch_events / ratings / playlists / collections).' },
          { name: 'server_id', type: 'TEXT NOT NULL', note: 'Which server this user belongs to.' },
          { name: 'user_handle', type: 'TEXT NOT NULL', note: 'Stable per-server identifier. Empty string = the owner sentinel (kept for one release while every reader switches to the FK; role is the source of truth for "is this the owner").' },
          { name: 'display_name', type: 'TEXT', note: 'Human-readable name from the live API. May be NULL on rows created from legacy data; the next snapshot/walk fills it in.' },
          { name: 'role', type: "TEXT NOT NULL CHECK (role IN ('owner', 'managed'))", note: 'Server owner or managed home user. The engine routes Plex tokens by this field.' },
          { name: 'backend', type: "TEXT NOT NULL DEFAULT 'plex' CHECK (backend IN ('plex', 'emby', 'jellyfin'))", note: 'Which media-server product this row describes. Plex today; future Jellyfin / Emby adapters will write their own backend string and the engine reads them uniformly.' },
          { name: 'backend_user_id', type: 'TEXT', note: "The backend's native user id (Plex userID, Jellyfin user GUID, etc.). May be NULL on rows backfilled from pre-v0.13.0 data." },
          { name: 'app_user_uuid', type: 'TEXT (partial UNIQUE index)', note: "v12 (USER-MGMT-IDENTITY-AUDIT). App-generated canonical identifier in the form <Service>-<HostNameSlug>-<server_uid>-<userkey>. Immutable for the row's lifetime. Generated at insert time by every writer path; legacy rows are filled by the boot-time _backfill_app_user_uuids helper. This is the validation handle the cross-server identity_map keys off." },
          { name: 'created_at', type: 'REAL NOT NULL', note: 'When the row was first inserted.' },
          { name: 'last_seen_at', type: 'REAL', note: 'Bumped on every CRUD touch via get_or_create_server_user.' },
        ],
        design_note:
          "Replaces the legacy 'user_handle = \"\"' owner sentinel that ran through every wide table pre-v0.13.0. The owner is now a first-class row with role='owner', queryable directly; managed users are role='managed'. The backend column makes the schema multi-backend without a migration when Jellyfin / Emby support lands - Plex's one-owner-per-server model and Jellyfin's multiple-admins model both fit because role + backend are independent. Separate from managed_users (credentials, Fernet-encrypted, host-bound); server_users is identity metadata and is safe to copy into a portable snapshot .db file. UNIQUE(server_id, user_handle) prevents duplicate identity rows; cascading deletes on server purge are handled in order in server/media_db.py :: purge_server_data. The app_user_uuid column (v12) is the canonical anchor across servers — see the 'How users are identified across servers' topic on the Help tab for the full primer.",
      },
      {
        name: 'watch_events',
        purpose: "Per-(item, server, user) watch state. One row per item per user per server.",
        key_columns: ['item_id', 'server_id', 'server_user_id', 'view_count', 'last_viewed_at'],
        columns: [
          { name: 'id', type: 'INTEGER PRIMARY KEY AUTOINCREMENT', note: 'Internal id.' },
          { name: 'item_id', type: 'INTEGER NOT NULL', note: 'FK into items.id.' },
          { name: 'server_id', type: 'TEXT NOT NULL', note: 'Which server saw the watch.' },
          { name: 'user_handle', type: "TEXT NOT NULL DEFAULT ''", note: 'Legacy denormalised handle. Empty string = owner sentinel. Kept for one release while every reader switches to server_user_id; dropped in a follow-up migration.' },
          { name: 'server_user_id', type: 'INTEGER REFERENCES server_users(id)', note: 'v0.13.0 identity FK. The authoritative answer to "who watched this." Lookup display_name / role / backend via JOIN against server_users.' },
          { name: 'view_count', type: 'INTEGER NOT NULL DEFAULT 0', note: 'Total times the user has played this item.' },
          { name: 'view_offset', type: 'INTEGER NOT NULL DEFAULT 0', note: 'Last resume position in milliseconds.' },
          { name: 'last_viewed_at', type: 'REAL', note: 'Unix timestamp of the last play.' },
          { name: 'updated_at', type: 'REAL NOT NULL', note: 'When we last wrote this row.' },
        ],
      },
      {
        name: 'ratings',
        purpose: 'Per-(item, server, user) star ratings.',
        key_columns: ['item_id', 'server_id', 'server_user_id', 'rating'],
        columns: [
          { name: 'id', type: 'INTEGER PRIMARY KEY AUTOINCREMENT', note: 'Internal id.' },
          { name: 'item_id', type: 'INTEGER NOT NULL', note: 'FK into items.id.' },
          { name: 'server_id', type: 'TEXT NOT NULL', note: 'Which server holds the rating.' },
          { name: 'user_handle', type: "TEXT NOT NULL DEFAULT ''", note: 'Legacy denormalised handle. Owner = "". Kept transiently alongside server_user_id; dropped in a follow-up migration.' },
          { name: 'server_user_id', type: 'INTEGER REFERENCES server_users(id)', note: 'v0.13.0 identity FK. JOIN to server_users to get display_name / role / backend.' },
          { name: 'rating', type: 'REAL NOT NULL', note: 'Plex rating value (0.0 to 10.0). UI divides by 2 to display 0-5 stars.' },
          { name: 'updated_at', type: 'REAL NOT NULL', note: 'When this rating was last seen / written.' },
        ],
      },
      {
        name: 'playlists',
        purpose: 'Per-(server, user) playlist rows. Membership is denormalised into item_ids_json so we can read the whole list in one query.',
        key_columns: ['server_id', 'server_user_id', 'name', 'item_ids_json', 'is_smart'],
        columns: [
          { name: 'id', type: 'INTEGER PRIMARY KEY AUTOINCREMENT', note: 'Internal id.' },
          { name: 'server_id', type: 'TEXT NOT NULL', note: 'Which server owns the playlist.' },
          { name: 'user_handle', type: "TEXT NOT NULL DEFAULT ''", note: 'Legacy denormalised handle. "" = server-wide playlist (lives under the owner row); non-empty = user-private. Kept transiently alongside server_user_id.' },
          { name: 'server_user_id', type: 'INTEGER REFERENCES server_users(id)', note: 'v0.13.0 identity FK. Server-wide playlists FK to the owner row (role="owner"); user-private playlists FK to a managed row.' },
          { name: 'name', type: 'TEXT NOT NULL', note: 'Playlist title.' },
          { name: 'description', type: 'TEXT', note: 'Optional summary text.' },
          { name: 'is_smart', type: 'INTEGER NOT NULL DEFAULT 0', note: '1 if this is a smart playlist (filter-based, no static members).' },
          { name: 'smart_filter_json', type: 'TEXT', note: 'The smart-playlist filter URL when is_smart=1. Server-local and not portable.' },
          { name: 'item_ids_json', type: 'TEXT', note: 'JSON array of items.id values. Ordered. Empty for smart playlists.' },
          { name: 'updated_at', type: 'REAL NOT NULL', note: 'When this row was last written.' },
        ],
        design_note:
          "We denormalise membership into a JSON array on purpose. Playlists are read as whole lists, never queried by single member, so a normalised playlist_items join table would force a JOIN on every read with no upside. UNIQUE(server_id, user_handle, name) lets a user have a private playlist with the same name as a server-wide one without collision. The server-wide-vs-user-private split that ingest_snapshot_payload enforces (a library-level playlist visible to every home user lands exactly once under the owner row rather than N times) reads from server_users.role rather than guessing from user_handle in v0.13.0.",
      },
      {
        name: 'collections',
        purpose: 'Same shape as playlists but for collections. Unordered.',
        key_columns: ['server_id', 'server_user_id', 'name', 'item_ids_json'],
        columns: [
          { name: 'id', type: 'INTEGER PRIMARY KEY AUTOINCREMENT', note: 'Internal id.' },
          { name: 'server_id', type: 'TEXT NOT NULL', note: 'Which server owns the collection.' },
          { name: 'user_handle', type: "TEXT NOT NULL DEFAULT ''", note: 'Legacy denormalised handle. "" = server-wide; non-empty = user-private (rare for collections). Kept transiently alongside server_user_id.' },
          { name: 'server_user_id', type: 'INTEGER REFERENCES server_users(id)', note: 'v0.13.0 identity FK. JOIN to server_users for role + display_name.' },
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
          { name: 'app_user_uuid', type: 'TEXT (partial UNIQUE index)', note: "v12 (USER-MGMT-IDENTITY-AUDIT). App-generated canonical identifier in the form <Service>-<HostNameSlug>-<server_uid>-<userkey>. Generated at insert time by upsert_managed_user; legacy rows are filled by the boot-time _backfill_app_user_uuids helper. This is the validation handle the cross-server identity_map keys off." },
          { name: 'last_seen', type: 'REAL', note: "When the user was last observed by a live API sync." },
          { name: 'created_at', type: 'REAL NOT NULL', note: 'When the row was first inserted.' },
          { name: 'updated_at', type: 'REAL NOT NULL', note: 'When the row was last written.' },
        ],
        design_note:
          "Fernet ciphertext lives inline in the .db rather than in a separate secrets file. The decryption key lives in server_data/.keyfile, separate from media.db. A export of media.db without .keyfile is unreadable for credential cells. The app_user_uuid column (v12) gives every managed user a stable, app-controlled identifier — see the 'How users are identified across servers' topic on the Help tab for the full primer + format spec.",
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
        name: 'library_sections',
        purpose: "Per-server snapshot of the library list. Records each library's stable identity (section key + title + type) so the engine can detect renamed libraries and the resolver can scope queries to one library when needed.",
        key_columns: ['server_id', 'section_key', 'section_title', 'section_type'],
        columns: [
          { name: 'server_id', type: 'TEXT NOT NULL', note: "Which server this library belongs to." },
          { name: 'section_key', type: 'INTEGER NOT NULL', note: "The backend's stable numeric library id (Plex section key, Jellyfin library GUID converted, etc.). The Library-Section Identity Invariant requires section_key > 0 for every row." },
          { name: 'section_title', type: 'TEXT NOT NULL', note: "Display name at last sync." },
          { name: 'section_type', type: 'TEXT NOT NULL', note: "movie / show / artist / etc." },
          { name: 'first_seen_at', type: 'REAL NOT NULL', note: "When this library was first observed." },
          { name: 'last_seen_at', type: 'REAL NOT NULL', note: "Bumped whenever a refresh confirms the library still exists." },
        ],
        design_note:
          "PRIMARY KEY (server_id, section_key) — a renamed library keeps the same key on the backend, so the row updates in place. A library removed from the backend stops getting last_seen_at refreshes; consumers can detect that.",
      },
      {
        name: 'user_identity_map',
        purpose: "Cross-server identity links. One row per ordered pair of (user_a_uuid, user_b_uuid) that the operator or auto-discovery has established as the same person. Re-keyed from (server_id, user_handle) tuples to (app_user_uuid, app_user_uuid) pairs in v12 so links survive renames + backend_user_id rotation.",
        key_columns: ['user_a_uuid', 'user_b_uuid', 'source', 'derived_from_id'],
        columns: [
          { name: 'id', type: 'INTEGER PRIMARY KEY AUTOINCREMENT', note: 'Internal id.' },
          { name: 'user_a_uuid', type: 'TEXT NOT NULL', note: "app_user_uuid of the first user in the pair. References managed_users.app_user_uuid or server_users.app_user_uuid." },
          { name: 'user_b_uuid', type: 'TEXT NOT NULL', note: "app_user_uuid of the second user in the pair." },
          { name: 'source', type: "TEXT NOT NULL CHECK (source IN ('manual', 'auto_copy'))", note: "Origin of the link: 'manual' (User Mapping panel + cross-platform preflight modal), 'auto_copy' (auto-derived from same-(service_type, backend_user_id) pairs by the auto-link helper)." },
          { name: 'derived_from_id', type: 'INTEGER (v13)', note: 'When non-NULL, this row was fanned-out transitively from another row (e.g., adding A↔B when A↔C and B↔C already existed). Deleting the parent cascades to children so the operator removing a manual edge cleans up the inferred edges it spawned. NULL = standalone row (manual or auto_copy by backend_user_id).' },
          { name: 'created_at', type: 'REAL NOT NULL', note: 'When the link was first recorded.' },
        ],
        design_note:
          "v12 re-keyed this table from (server_id, user_handle) tuples to (app_user_uuid, app_user_uuid) pairs. Why: handles + backend_user_ids rotate, but app_user_uuid is immutable. An operator-authored mapping like 'Crystal Jean on Jade.TV is the same human as crystal.jean on a Jellyfin server' now survives renames on either side, backend_user_id rotation, and even one of the backends being re-registered. v13 added derived_from_id + transitive-closure fanout so adding a link to an existing equivalence class joins everyone correctly. The lookup paths treat the pair symmetrically: querying for either direction returns the link. Resolution-chain priority: per-job operator override → user_identity_map lookup → backend_user_id direct match → username match → owner-role single-admin fallback. See the 'How users are identified across servers' topic for the full primer.",
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

  // ── run_timings.db ──────────────────────────────────────────────
  {
    filename: 'run_timings.db',
    filepath: 'server_data/run_timings.db',
    intro_short:
      "Operational telemetry + ETA training store. Three tables: per-operation timings (run_timings), per-job summaries (run_history), and the learned regression weights the predictor uses to estimate future job durations (eta_buckets).",
    intro_long:
      "run_timings.db is the observability spine of the app. Every time_operation block in the engine writes one row to run_timings; every finished job writes one row to run_history. Those two tables drive the Run History tab and the Recent Runtimes panel.\n\nThe third table, eta_buckets, holds the learned weights for the adaptive ETA predictor. Each row is the EMA-decayed sufficient statistics for one online weighted linear regression of the form duration = intercept + slope * items_count, keyed by (server, operation label, library type, bulk strategy). The trainer is fed by the same run_timings rows; running a few jobs lets the predictor walk past its cold-start defaults and into real per-server numbers.\n\nThis file is deliberately separate from media.db. The two have different lifecycles (telemetry is bounded by retention; media state grows with library size), different audit characteristics (telemetry tells you what the engine did; media tells you what the server contains), and very different blast radii (losing media.db re-runs as a backfill; losing run_timings just resets the ETA learning). Keeping them apart prevents a write-amplification path on the cumulative store and makes either DB easy to wipe independently.",
    tables: [
      {
        name: 'run_timings',
        purpose: 'One row per timed operation (snapshot of one library, gather of one user, etc.). The ETA trainer ingests these.',
        key_columns: ['run_id', 'label', 'server_id', 'duration_seconds', 'items_processed'],
        columns: [
          { name: 'id', type: 'INTEGER PRIMARY KEY AUTOINCREMENT', note: 'Internal id.' },
          { name: 'run_id', type: 'TEXT NOT NULL', note: "Groups every entry from one job together. Joined to run_history.run_id for drill-down." },
          { name: 'scope', type: 'TEXT NOT NULL', note: "What level the operation ran at: run / library / user / batch / operation." },
          { name: 'label', type: 'TEXT NOT NULL', note: "Operation name, e.g. snapshot_watch_history / snapshot_ratings / gather_user / build_snapshot_db_from_payloads." },
          { name: 'server_id', type: 'TEXT', note: "Which server the operation ran against. Required for the ETA trainer to attribute the timing to a bucket." },
          { name: 'library', type: 'TEXT', note: "Library name when the scope is library; NULL otherwise." },
          { name: 'user_handle', type: 'TEXT', note: "User handle when the scope is per-user; NULL otherwise." },
          { name: 'started_at', type: 'REAL NOT NULL', note: 'Unix start timestamp.' },
          { name: 'ended_at', type: 'REAL NOT NULL', note: 'Unix end timestamp.' },
          { name: 'duration_seconds', type: 'REAL NOT NULL', note: "perf_counter delta - monotonic, immune to NTP jumps." },
          { name: 'items_processed', type: 'INTEGER', note: 'How many items this operation touched. The regression variable for ETA training.' },
          { name: 'etr_at_start', type: 'REAL', note: "What the live ETR tracker said at the moment the operation began (when applicable)." },
          { name: 'extra_json', type: 'TEXT', note: "Free-form annotations as JSON. Carries library_type, strategy, ping_ms_at_start, bulk_used, etc." },
          { name: 'recorded_at', type: 'REAL NOT NULL', note: "When the row was inserted into the DB." },
        ],
        design_note:
          "Retention is bounded: only the most recent N distinct run_ids survive (operator-tunable, default 200). That cap keeps the file small enough to scan in the dashboard without pagination but deep enough for the ETA trainer to detect drift across weeks. Older runs roll off automatically at insert time.",
      },
      {
        name: 'run_history',
        purpose: "One row per finished job (snapshot / restore / direct). Powers the Run History tab and the Recent Runtimes panel.",
        key_columns: ['run_id', 'started_at', 'state', 'server_id', 'job_type', 'duration_ms'],
        columns: [
          { name: 'run_id', type: 'TEXT PRIMARY KEY', note: "Matches the run_id used in run_timings, so the UI can drill from a history row into its per-operation entries." },
          { name: 'started_at', type: 'REAL NOT NULL', note: 'When the job started.' },
          { name: 'finished_at', type: 'REAL NOT NULL', note: 'When the job ended (any terminal state).' },
          { name: 'job_type', type: 'TEXT NOT NULL', note: 'snapshot / restore / direct.' },
          { name: 'server_id', type: 'TEXT', note: 'The source server for snapshot / direct; the destination for restore.' },
          { name: 'server_name', type: 'TEXT', note: 'Friendly name at job time, for display when the server is later renamed or removed.' },
          { name: 'libraries', type: 'TEXT NOT NULL', note: 'JSON array of library names this job touched.' },
          { name: 'users_affected', type: 'INTEGER NOT NULL DEFAULT 0', note: 'Distinct user count touched by this job.' },
          { name: 'users_affected_list', type: 'TEXT', note: 'JSON array of user handles, parallel to users_affected.' },
          { name: 'state', type: 'TEXT NOT NULL', note: 'completed / failed / cancelled.' },
          { name: 'duration_ms', type: 'INTEGER NOT NULL', note: 'Total wall-clock duration in milliseconds.' },
          { name: 'run_log_dir', type: 'TEXT', note: 'Pointer to the run-specific log directory under plex_logs/.' },
          { name: 'has_settings_log', type: 'INTEGER NOT NULL DEFAULT 0', note: '1 if a run-settings.log was written.' },
          { name: 'has_restoration_log', type: 'INTEGER NOT NULL DEFAULT 0', note: '1 if a restoration-summary.log was written.' },
          { name: 'error_summary', type: 'TEXT', note: 'Short error string when state != completed.' },
          { name: 'recorded_at', type: 'REAL NOT NULL', note: "When the row was inserted." },
        ],
        design_note:
          "Same retention discipline as run_timings (operator-tunable, default 200). The 'why two tables when one would do?' question: run_timings is per-operation (~30 rows per job); run_history is per-job (1 row). Splitting lets the dashboard scan the small table for the list view and drill into the large one only when the operator opens a specific row.",
      },
      {
        name: 'eta_buckets',
        purpose: "Per-bucket learned regression weights for the adaptive ETA predictor. One row per (server, operation, library type, strategy).",
        key_columns: ['server_id', 'label', 'library_type', 'bulk_strategy', 'sample_count'],
        columns: [
          { name: 'server_id', type: 'TEXT NOT NULL', note: "Which server's timings trained this bucket." },
          { name: 'label', type: 'TEXT NOT NULL', note: 'The operation label (snapshot_watch_history, snapshot_ratings, etc.).' },
          { name: 'library_type', type: 'TEXT NOT NULL', note: "movie / show / artist / etc.; empty string when the operation does not bind to a single library." },
          { name: 'bulk_strategy', type: 'TEXT NOT NULL', note: "Which Plex watch+ratings strategy was in use (smart / force_bulk / force_server_side)." },
          { name: 'sum_w', type: 'REAL NOT NULL', note: 'EMA-decayed effective sample count.' },
          { name: 'sum_wx', type: 'REAL NOT NULL', note: 'EMA-decayed sum of items_count observations.' },
          { name: 'sum_wy', type: 'REAL NOT NULL', note: 'EMA-decayed sum of duration observations.' },
          { name: 'sum_wxx', type: 'REAL NOT NULL', note: 'EMA-decayed sum of items_count squared. Together with the others, feeds the closed-form OLS solve.' },
          { name: 'sum_wxy', type: 'REAL NOT NULL', note: 'EMA-decayed sum of items_count × duration.' },
          { name: 'sum_wyy', type: 'REAL NOT NULL', note: 'EMA-decayed sum of duration squared. Used for residual variance and the confidence band.' },
          { name: 'sample_count', type: 'INTEGER NOT NULL', note: 'Raw observation count (independent of EMA weighting). Drives the display gate that hides confident bands below a threshold.' },
          { name: 'last_observed_at', type: 'REAL NOT NULL', note: "When this bucket was last updated. Stale buckets can be aged out separately from active ones." },
          { name: 'sum_w_ping', type: 'REAL NOT NULL DEFAULT 0', note: 'Separate EMA track for training-time ping (ms). Empty when no ping samples have been observed.' },
          { name: 'sum_wp', type: 'REAL NOT NULL DEFAULT 0', note: 'EMA-decayed sum of ping observations. With sum_w_ping, recovers the bucket-mean ping for the latency-offset multiplier.' },
        ],
        design_note:
          "The on-disk shape lets the trainer hot-restore from boot: a single SELECT * builds the in-memory model. The 8 sufficient-statistics columns let the predict path do a closed-form OLS solve without revisiting the raw observation history. PRIMARY KEY on the four dimensions means a bucket update is a single UPSERT.",
      },
    ],
    write_path: [
      { name: 'persist_entries', module: 'server/run_timings_db.py', description: 'Bulk insert from the in-memory timing buffer at end_run. Applies retention afterward.' },
      { name: 'record_run_history', module: 'server/run_timings_db.py', description: 'One per finished job; called from the job finalization path in server/jobs.py.' },
      { name: 'persist_eta_buckets', module: 'server/run_timings_db.py', description: 'Upserts the trainer\'s touched buckets after every batch_update at job end.' },
      { name: 'ETATrainer.batch_update', module: 'services/eta_training.py', description: 'The trainer-side entry point: folds a job\'s timing rows into the bucket regressions, then persists.' },
    ],
    read_path: [
      { name: 'list_recent_run_history', module: 'server/run_timings_db.py', description: 'Drives the Run History tab and Recent Runtimes panel.' },
      { name: 'get_run_entries', module: 'server/run_timings_db.py', description: 'Per-run drill-down: every timing row for one run_id.' },
      { name: 'get_label_history', module: 'server/run_timings_db.py', description: 'Per-label history; consumed by the live ETR tracker for its rolling window.' },
      { name: 'load_all_eta_buckets', module: 'server/run_timings_db.py', description: 'One-shot load at trainer construction; populates the in-memory bucket dict.' },
      { name: 'ETATrainer.predict_for_job', module: 'services/eta_training.py', description: "The Run Job form's ETA preview reads this. Walks the cascade, applies latency offset, returns a per-library and rolled-up estimate." },
    ],
    lifecycle:
      "Created on first boot by init_db (FastAPI lifespan). Auto-backfill on first boot of a new build: if eta_buckets is empty AND run_timings has rows, the trainer replays history to repopulate. Retention runs at insert time; the file stays bounded to the configured run cap. Operators can wipe eta_buckets independently via Settings -> Run History -> Training data recovery, or flush the entire DB via the same panel's Flush button.",
    source_schema: 'server/run_timings_db.py :: _SCHEMA',
    source_init: 'server/run_timings_db.py :: init_db',
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
          { name: 'user_count', type: 'INTEGER', note: "Full roster captured: owner + every managed user the engine attempted to gather (regardless of whether they had data)." },
          { name: 'user_count_with_data', type: 'INTEGER', note: "Subset of user_count that actually contributed rows to at least one metric table. The Exports panel renders 'N of M users' when the two differ." },
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
      "auth.db lives separately from media.db on purpose. Login users and the db_admin credential are a security concern that wants its own blast radius: a corruption / restore / migration of media.db must never put credentials at risk, and vice versa. The file holds two tables: app_users for both human login users AND the special db_admin row that gates destructive operations, and refresh_tokens for JWT refresh-cookie validation.\n\nPasswords are stored as bcrypt-style hashes; we never store plaintext. Refresh tokens are stored only by their id and validity window - the JWT itself is signed with a per-process secret and isn't persisted.\n\nThis database is deliberately excluded from the export / import surface under Settings › Databases. Exporting credentials as JSON would defeat the bcrypt-at-rest discipline; the auth DB is meant to be host-bound and never travel.",
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
      "Created on first boot. The Setup wizard is the only path that creates the first user; subsequent users are added through Settings -> Account Management -> User Accounts (root_admin gated). The db_admin row is created through a separate Setup-style page in Account Management -> Database Admin Account; it can never be deleted (only updated) because removing it would block every future destructive write. cleanup_expired_tokens runs at startup and on a 24h daemon thread.",
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
          { name: 'created_by', type: 'TEXT', note: 'Operator who triggered the run.' },
        ],
      },
      {
        name: 'snapshot_users',
        purpose: "Full roster captured during this snapshot: the owner plus every managed user the engine attempted, with a flag for which ones actually contributed data.",
        key_columns: ['user_handle', 'display_name', 'is_owner', 'had_data'],
        columns: [
          { name: 'user_handle', type: 'TEXT PRIMARY KEY', note: 'The raw user identifier. Empty string = server owner.' },
          { name: 'display_name', type: 'TEXT', note: "Operator-chosen friendly name at capture time. May be NULL." },
          { name: 'is_owner', type: 'INTEGER NOT NULL DEFAULT 0', note: '1 for the empty-string owner row.' },
          { name: 'had_data', type: 'INTEGER NOT NULL DEFAULT 0', note: '1 if the user appeared in at least one populated metric table, 0 if the engine ran their gathers but no rows came back. Lets the Exports panel render "N captured / M roster" using the two counts.' },
        ],
        design_note:
          "Carries every user the engine attempted to gather, not just users with data. That way the operator sees the full snapshot scope on the Exports panel (e.g. \"3 of 9 users\" when an 8-managed-user server has 5 inactive accounts). display_name is frozen at capture so a user later renamed in Plex doesn't retroactively change historical snapshots.",
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
  // Per-database subtab: each DB gets its own page within DB Schema
  // so the end user can land on a specific database directly instead
  // of scrolling through every one. The depth selector below picks
  // how much detail to show for the selected DB.
  const [selectedDbIdx, setSelectedDbIdx] = useState<number>(0);
  const selectedDb = DB_DOCS[selectedDbIdx] || DB_DOCS[0];
  return (
    <>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>DB Schema</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12 }}>
          Every SQLite database the app owns, what it stores, why it
          looks the way it does, and how it gets read and written.
          Pick a database below, then choose a depth level. Operators
          can also download or restore individual tables of these
          databases under <strong>Settings &rsaquo; Databases</strong>.
        </span>
        <nav className="tabs sub-tabs" style={{ marginTop: 12, flexWrap: 'wrap' }}>
          {DB_DOCS.map((db, i) => (
            <button
              key={db.filepath}
              className={selectedDbIdx === i ? 'active' : ''}
              onClick={() => setSelectedDbIdx(i)}
            >
              {db.filename}
            </button>
          ))}
        </nav>
        <nav className="tabs sub-tabs" style={{ marginTop: 8 }}>
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

      <div className="panel">
        <h2 style={{ marginTop: 0 }}>{selectedDb.filename}</h2>
        <div className="mono" style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 8 }}>
          {selectedDb.filepath}
        </div>
        {view === 'quick' && <QuickViewBody db={selectedDb} />}
        {view === 'deep' && <InDepthBody db={selectedDb} />}
        {view === 'sources' && <SourceLinksBody db={selectedDb} />}
      </div>

      <SecurityPanel />
      <BackupAndRecoveryPanel />
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
        <p key={`para-${i}-${para.slice(0, 16)}`} style={{ fontSize: 13, marginTop: i === 0 ? 0 : 12 }}>{para}</p>
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


// ── Backup and recovery ─────────────────────────────────────────────────────
//
// What to back up off-host, why, and how to put it back when things
// have already gone wrong. Reads as the operational follow-up to the
// SecurityPanel above: that one answers "what's encrypted"; this one
// answers "what's lost when this drive dies."
//
// The three bind-mount paths mirror docker-compose.yml. If those mounts
// move, update this panel too.

function BackupAndRecoveryPanel() {
  return (
    <>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Backup and recovery</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12, marginBottom: 12 }}>
          Practical guide to preserving Hestia-MediaManager state
          across host failures, drive swaps, and clean reinstalls.
          What to copy off-host, how often, and what each layer costs
          you if you lose it.
        </span>

        <h3 style={{ marginTop: 16 }}>The three persistent directories</h3>
        <p style={{ fontSize: 13, marginTop: 0 }}>
          Everything the app keeps across <code>docker compose down</code>{' '}
          lives in three host bind mounts. The container side is fixed;
          the host side is whatever the operator set in
          <code> docker-compose.yml</code> (defaults shown).
        </p>
        <table className="list" style={{ width: '100%' }}>
          <thead>
            <tr>
              <th style={{ width: '22%' }}>Host path</th>
              <th style={{ width: '22%' }}>Container path</th>
              <th>What's inside</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td className="mono" style={{ fontSize: 11 }}>./server_data</td>
              <td className="mono" style={{ fontSize: 11 }}>/app/server_data</td>
              <td>
                The <strong>only directory that isn't regeneratable</strong>.
                Holds <code>settings.json</code>, <code>schedules.json</code>,
                <code> servers.json</code> (Fernet-encrypted tokens),
                <code> .keyfile</code> (the Fernet master key),
                <code> auth.db</code> (operator accounts + refresh tokens),
                <code> media.db</code> (the cumulative resolver cache),
                <code> snapshots.db</code> (the snapshot registry),
                and <code>run_timings.db</code> (telemetry + ETA training).
              </td>
            </tr>
            <tr>
              <td className="mono" style={{ fontSize: 11 }}>./snapshots</td>
              <td className="mono" style={{ fontSize: 11 }}>/app/snapshots</td>
              <td>
                Per-snapshot <code>.db</code> files plus their cached
                <code> .plexexport.json</code> sidecars. Each file is a
                point-in-time capture of one server. Re-runnable in
                principle (you can run a fresh snapshot job at any
                time), but the historical record is lost the moment
                you drop the directory.
              </td>
            </tr>
            <tr>
              <td className="mono" style={{ fontSize: 11 }}>./plex_logs</td>
              <td className="mono" style={{ fontSize: 11 }}>/app/plex_logs</td>
              <td>
                Per-run log directories. Diagnostic-only; the engine
                writes them, the dashboard surfaces them, nothing
                depends on them surviving. Safe to drop or to back up
                on a much longer cadence than the other two.
              </td>
            </tr>
          </tbody>
        </table>

        <h3 style={{ marginTop: 24 }}>Priority of what to back up</h3>
        <ul>
          <li style={{ marginBottom: 6 }}>
            <strong><code>./server_data</code> first</strong>. Losing
            this means losing every registered server, every encrypted
            Plex token, every operator login, every saved schedule, and
            every tunable change. Back up nightly at minimum.
          </li>
          <li style={{ marginBottom: 6 }}>
            <strong><code>./snapshots</code> next</strong>. The
            registry pointer in <code>server_data/snapshots.db</code>{' '}
            assumes the files are where it left them. Capturing a fresh
            snapshot from a still-running source recovers <em>current</em>{' '}
            state, but not the older points-in-time you wanted history
            for. Back up on the same cadence as your snapshot job
            schedule, or whenever a new snapshot lands.
          </li>
          <li>
            <strong><code>./plex_logs</code> last (or never)</strong>.
            Helpful for post-incident review; not required for any
            functionality. Most operators skip it from off-host backups
            and let the on-host directory rotate naturally.
          </li>
        </ul>

        <h3 style={{ marginTop: 24 }}>How to back up safely</h3>
        <p>
          The four databases in <code>server_data</code>
          (<code>auth.db</code>, <code>media.db</code>,
          <code> snapshots.db</code>, <code>run_timings.db</code>) are
          live SQLite. Two options:
        </p>
        <ul>
          <li style={{ marginBottom: 6 }}>
            <strong>Stop cleanly, then copy.</strong>
            <code> docker compose down</code> flushes WAL + SHM into
            the main <code>.db</code> file. After it returns,
            <code> server_data/</code> is in a quiescent state and a
            plain <code>cp -r</code> / <code>rsync</code> / <code>robocopy</code>{' '}
            captures everything correctly. Bring the stack back up with
            <code> docker compose up -d</code> when the copy finishes.
          </li>
          <li>
            <strong>Copy mid-run, but include the WAL sidecars.</strong>{' '}
            If you can't stop the containers, your backup tool MUST grab
            the <code>.db-wal</code> and <code>.db-shm</code> files
            alongside each <code>.db</code>, or use a snapshot-aware
            tool (ZFS / Btrfs / LVM / Volume Shadow Copy) that
            captures the directory atomically. Copying just the
            <code> .db</code> mid-write gives you a torn database that
            won't open cleanly.
          </li>
        </ul>

        <h3 style={{ marginTop: 24 }}>The keyfile rule</h3>
        <p>
          <code>server_data/.keyfile</code> is the Fernet master key
          that decrypts the auth tokens in <code>servers.json</code>{' '}
          and the per-user credential columns in
          <code> media.db</code>. Back up <strong>both
          together</strong> or restoration silently degrades:
        </p>
        <ul>
          <li>
            Keyfile + encrypted files together → tokens decrypt on
            restore; servers reconnect without re-entering anything.
          </li>
          <li>
            Encrypted files without keyfile → every saved token is
            unrecoverable ciphertext. You'll have to re-enter Plex
            tokens for every registered server. Other state (snapshot
            history, settings, schedules) is fine.
          </li>
          <li>
            Keyfile without encrypted files → useless on its own.
          </li>
        </ul>

        <h3 style={{ marginTop: 24 }}>Recovery scenarios</h3>
        <table className="list" style={{ width: '100%' }}>
          <thead>
            <tr>
              <th style={{ width: '32%' }}>What you have</th>
              <th>How to come back up</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td><strong>Full backup of all three directories</strong></td>
              <td>
                Drop them in place on the new host, run
                <code> docker compose up -d</code>. Operator accounts,
                tokens, schedules, snapshot history, and the captured
                snapshot files are all live again. No wizard re-run.
              </td>
            </tr>
            <tr>
              <td><strong>Only <code>./snapshots</code> survived</strong></td>
              <td>
                Run the first-boot wizard to recreate operator accounts.
                Re-add each server on the Servers tab (you'll have to
                re-enter Plex tokens). Then drop the
                <code> ./snapshots</code> directory into place; the
                Exports panel's startup reconcile picks up the orphan
                files and rebuilds the registry rows so you can restore
                from them.
              </td>
            </tr>
            <tr>
              <td>
                <strong>Lost <code>server_data</code> only</strong>{' '}
                (snapshots intact)
              </td>
              <td>
                Same path as above: wizard for accounts, re-register
                servers, let snapshot reconcile rehydrate the registry.
                You lose the cumulative <code>media.db</code> resolver
                cache; the next few jobs will be slower while it
                rebuilds.
              </td>
            </tr>
            <tr>
              <td>
                <strong>Databases corrupt, but <code>servers.json</code>{' '}
                + <code>.keyfile</code> intact</strong>
              </td>
              <td>
                Delete the corrupt <code>auth.db</code> and let the
                first-boot wizard create fresh operator accounts. The
                keyfile (still on disk) decrypts the existing
                <code> servers.json</code> tokens after the new
                accounts are created, so registered servers reconnect
                without re-entering tokens. This recovery path is
                documented in <code>OVERVIEW.md</code> in the repo.
              </td>
            </tr>
            <tr>
              <td><strong>Nothing</strong></td>
              <td>
                Fresh install. Wizard creates the Admin + Root Admin
                accounts; re-register every server with a freshly
                copied Plex token; re-create schedules from memory.
                Snapshot history is gone but the source servers
                themselves are unaffected.
              </td>
            </tr>
          </tbody>
        </table>

        <p style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 16 }}>
          The bind-mount paths above are defaults from the shipped
          <code> docker-compose.yml</code>. Custom deployments may
          mount these directories elsewhere; check your compose file
          before scripting a backup job.
        </p>
      </div>
    </>
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
    example: `Hestia-MediaManager Troubleshooting Log - 2026-05-13 19:17:34
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
    example: `# Hestia-MediaManager unresolved items - 2026-05-13 19:17:34
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
      "The file exists on the old server but could not be found at the same path on the new one. This usually means your media drive is mounted at a different location, or the folder structure changed during the move. Hestia-MediaManager automatically attempts suffix matching (comparing the last 2 to 3 path components without the root prefix) so cross-platform moves between Windows and Linux are often resolved without configuration. If this item still failed, the tail of the path may have also changed.",
    steps: [
      "Check that your media drive is connected and mounted.",
      "Compare the file path shown below with where your files actually live.",
      "If only the root changed (e.g., C:\\Media to /mnt/plex), suffix matching should have caught it automatically. Verify the item exists in Plex.",
      "If the root AND some intermediate folders changed, the suffix fallback can't bridge it. Rename the destination folders to share at least two trailing path components with the source, or re-run the source snapshot after renaming.",
      "If paths match but files still aren't found, check drive permissions.",
    ],
  },
  {
    key: 'ambiguous_title_match',
    title: 'Ambiguous Title Match, Multiple Results',
    explanation:
      "A search by title returned more than one result, so the engine couldn't safely pick one. This happens when you have duplicate entries or similarly named items in your library.",
    steps: [
      "Open Plex and search for the item title shown below.",
      "Check for duplicate entries and remove the extras.",
      "Re-run the restore after removing duplicates.",
      "If you must keep the duplicates, toggle Strict Match off on the Run Job form's Per-Run Settings to allow best-guess selection. Use carefully, may match the wrong item.",
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
      "This item was already in the playlist on the target server and was skipped to avoid duplicates. This is expected behaviour. Hestia-MediaManager never adds duplicate items to existing playlists.",
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
      "This item already has a star rating on the target server. Hestia-MediaManager treats the target rating as authoritative and never overwrites it, even if the snapshot contains a different value.",
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
              <li key={`step-${i}-${step.slice(0, 24)}`} style={{ marginBottom: 4 }}>{step}</li>
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


// ── Features ──────────────────────────────────────────────────────────────────
//
// Operator-facing deep-dive on the six major features the app ships
// today. Each entry has a title, a short label (for the filter
// header), and a body that covers what the feature does, when to
// use it, and the key gotcha or design rationale.
//
// The filter is a substring match across title + shortLabel + body
// text (extracted from React children). Same pattern as the Topics
// page so the two pages feel identical to use.
//
// Naming note: the operator-facing tab label is "Playlist Transfer",
// but internal identifiers, API URLs (/api/playlist-mgmt/*), and
// Pydantic class names keep the "PlaylistMgmt" prefix - the API
// contract is deliberately not changed to match the label.

interface FeatureEntry {
  id: string;
  title: string;
  shortLabel: string;
  body: React.ReactNode;
}

const FEATURE_ENTRIES: FeatureEntry[] = [
  {
    id: 'snapshot-job',
    title: 'Snapshot Job (Run Job)',
    shortLabel: 'Capture watch history, ratings, playlists, and collections from a server to a file',
    body: (
      <>
        <p>
          A snapshot job reads a Plex / Jellyfin / Emby server's
          per-user state (watch history, ratings, playlists,
          collections) and writes it to a portable{' '}
          <code>.plexexport.json</code> sidecar plus a binary{' '}
          <code>.db</code> snapshot file under{' '}
          <code>server_data/snapshots/</code>. The sidecar is the
          operator-readable artifact; the .db is the engine's
          single-source-of-truth that future restores read from.
        </p>
        <p>
          <strong>When to use it.</strong> Whenever you want a
          portable record of a server's state — before a major Plex
          version upgrade, before a library rebuild, as part of a
          rolling weekly backup, or before any operation that could
          touch user data. Snapshots are read-only against the source
          server: nothing on the source is mutated.
        </p>
        <p>
          <strong>What gets captured.</strong> Watch history is
          per-(item, server, user) and includes view counts + resume
          positions + last-played timestamps. Ratings are per-(item,
          server, user) numeric 0-10 with the binary IsFavorite
          mirror on backends that expose it (Jellyfin / Emby).
          Playlists capture name + ordered member list + the
          is_smart flag; smart playlists carry their criteria as
          opaque JSON for the operator's reference but don't
          restore (their criteria are server-local and not
          portable). Collections capture name + unordered member
          list. Every captured row carries the source server's
          prefixed UID + the user's <code>app_user_uuid</code>, so
          downstream cross-server identity resolution works without
          needing the source server registered on the restoring
          install (see the user-identity-uuid topic).
        </p>
        <p>
          <strong>Per-user fan-out.</strong> When the source server
          is linked to a Plex.tv account with Plex Home users, the
          engine authenticates as each managed user (parallel auth
          with the user's stored token, falling back to stored-PIN
          sign-in if needed) and captures their data independently.
          Users without a usable auth signal are dropped from the
          run with a clear log line rather than silently bleeding
          their data into the admin row.
        </p>
        <p>
          <strong>Library + metric scoping.</strong> The Data to
          Migrate panel lets the operator pick which libraries and
          which metric types (watch_history / ratings / playlists /
          collections) each library captures. Per-library overrides
          beat the global include_* flags. This lets you do things
          like "snapshot only watch history on Movies; only
          playlists on Music" in one job.
        </p>
        <p>
          <strong>Key gotcha.</strong> Snapshots are server-scoped.
          You pick ONE server per job; multi-server captures run
          one job per server (queue them or schedule them
          independently). The .db file is per-server too, which
          makes operator-readable diffs easy and means a server's
          snapshot can be deleted without affecting any other.
        </p>
      </>
    ),
  },
  {
    id: 'restoration-job',
    title: 'Restoration Job (Restore)',
    shortLabel: 'Replay a snapshot or .plexexport.json file onto a destination server',
    body: (
      <>
        <p>
          A restoration job reads a snapshot file (.plexexport.json
          sidecar or registered .db snapshot) and writes its state
          onto a destination server. Per-user data lands under the
          matching destination user; resolved items go via GUID
          match against the destination's library so the same movie
          / track / show finds its destination row regardless of
          backend.
        </p>
        <p>
          <strong>Two modes: Merge vs Replace.</strong> Merge
          (default, additive) writes view counts higher than the
          destination's current value, writes ratings only when the
          destination has none, and appends playlist / collection
          members that aren't already present. Nothing on the
          destination is removed. Replace (opt-in, destructive)
          overwrites view counts unconditionally, overwrites
          ratings, and diffs playlist membership so members in the
          snapshot but not currently present are added AND members
          currently present but not in the snapshot are removed.
          Replace requires both a typed confirmation modal AND the{' '}
          <code>confirm_replace=true</code> flag on the API payload
          — two layers of opt-in so the destructive shape is
          impossible to fire accidentally.
        </p>
        <p>
          <strong>Cross-platform restores.</strong> When the source
          and destination are different backends (e.g., Plex source
          → Jellyfin destination), the engine routes through the
          adapter-driven restore path. Items are resolved by GUID
          (IMDb / TMDB / TVDB / MusicBrainz) rather than backend-
          native IDs; ratings are mapped via the D-RATE rule
          (numeric rating ≥ favorite_threshold also writes
          IsFavorite=true on backends that expose it); collections
          on Plex sources land as library-prefixed BoxSets on
          Jellyfin / Emby destinations so same-named collections
          from different libraries don't merge. The Cross-Platform
          Preflight modal surfaces user-resolution ambiguities
          (operator picks Drop / Map / Create / Accept per source
          user) before the job submits so the engine never has to
          guess at write time.
        </p>
        <p>
          <strong>Per-user fan-out.</strong> Each source user's
          payload routes to a destination user via a 5-step
          resolution chain: per-job operator override →
          user_identity_map lookup → backend_user_id direct match
          within service_type → case-insensitive username match →
          owner-role single-admin fallback. Users with no
          destination match are skipped with an actionable log
          line; the operator can either invite the user to the
          destination server and re-run, or add an identity_map row
          and re-run. See the user-identity-uuid topic for the full
          identity story.
        </p>
        <p>
          <strong>Replace mode safety belt.</strong> Before the
          engine starts overwriting data in Replace mode, an
          automatic pre-Replace snapshot of the destination is
          captured. The captured snapshot's id is stamped onto the
          job summary so the operator can find it on the Snapshots
          tab and roll back via a Merge restore. The safety belt
          can be disabled via the auto_capture_before_replace
          per-run setting but is on by default.
        </p>
        <p>
          <strong>Key gotcha.</strong> Items not on the destination
          (movie missing from the library, etc.) are SKIPPED rather
          than failed. The Restoration Summary panel on the
          dashboard shows per-container restored/total counts and
          lets the operator expand each container to see which
          members didn't resolve. Smart playlists are always
          skipped (their criteria don't port across servers).
        </p>
      </>
    ),
  },
  {
    id: 'direct-transfer-job',
    title: 'Direct Transfer Job',
    shortLabel: 'Read from one registered server and write to another without a file on disk',
    body: (
      <>
        <p>
          Direct transfer is "snapshot + restore" fused into one
          job, with no intermediate file written to disk. The
          engine reads source state in memory and writes it onto
          the destination in the same run. Faster than the
          snapshot-then-restore two-step when the operator doesn't
          need a portable .plexexport.json artifact.
        </p>
        <p>
          <strong>When to use it.</strong> Routine server-to-server
          migrations where you don't need a snapshot file on disk.
          Initial population of a new destination server from a
          source you don't plan to re-export later. Same-backend
          mirroring (Plex → Plex, Jellyfin → Jellyfin).
        </p>
        <p>
          <strong>When NOT to use it.</strong> When you want a
          historical record on disk — use a snapshot job instead,
          then run a restoration job from the snapshot. When the
          destination might fail mid-run and you'd want to retry
          without re-reading the source — snapshot-then-restore is
          more resilient because the snapshot is the resumable
          artifact. When you're testing cross-backend resolution
          and want to inspect what GUIDs were captured — snapshot
          first so you can read the .plexexport.json.
        </p>
        <p>
          <strong>Same engine as restore.</strong> Direct transfer
          uses the same per-user fan-out, the same 5-step user
          resolution chain, the same Merge / Replace mode
          semantics, the same Replace safety belt (auto-snapshot
          before overwrite), and the same cross-platform preflight
          modal as a file-mediated restore. The only thing missing
          is the disk write between read and write.
        </p>
        <p>
          <strong>Per-user scope.</strong> The user_filter
          parameter lets the operator restrict a direct transfer
          to a subset of source users (owner only, owner + named
          managed users, etc.). Useful when migrating just one
          person's Plex Home account between servers.
        </p>
        <p>
          <strong>Key gotcha.</strong> Both servers feel the load
          at the same time. Halve your worker count compared to a
          snapshot or restore against a single server — 16 workers
          on a direct transfer means 16 source reads AND 16
          destination writes in flight, which can overwhelm
          smaller installs.
        </p>
      </>
    ),
  },
  {
    id: 'fan-out-job',
    title: 'Fan-out Job (multi-destination direct transfer)',
    shortLabel: 'One source, many destinations — write the same state to N servers in parallel',
    body: (
      <>
        <p>
          Fan-out is a direct transfer with multiple destinations
          in one job. The engine reads source state once, then
          writes it to every destination in parallel. The job's
          progress dashboard shows per-destination state so the
          operator can see one destination succeed even if another
          is slow or failing.
        </p>
        <p>
          <strong>When to use it.</strong> Multi-server households
          where every server should mirror the same per-user state
          (e.g., one Plex Home shared across two Plex servers in
          different locations, two Jellyfin instances that should
          both reflect the operator's Plex.tv account watch
          history). Initial bulk population of a new server fleet
          from an existing source. Geo-distributed setups where
          one server is the authoritative read source and N
          destinations are the local read endpoints.
        </p>
        <p>
          <strong>Per-destination result tracking.</strong> The
          job's summary breaks out written / skipped / failed /
          unsupported counts per destination so a partial failure
          (3 of 4 destinations succeed) doesn't read as a total
          failure. The job's overall state surfaces as completed
          when every destination succeeded, partial when at least
          one succeeded and at least one failed, failed when
          every destination failed.
        </p>
        <p>
          <strong>Cross-backend fan-out.</strong> Destinations
          don't have to share a backend. A Plex source can
          fan-out to one Plex + one Jellyfin + one Emby destination
          in one job. Each per-destination write uses the
          adapter-driven restore path with cross-platform
          resolution (preflight modal applies per destination).
        </p>
        <p>
          <strong>Key gotcha.</strong> Source read load is bounded
          by the source's tolerance, but DESTINATION write load
          multiplies. N destinations means N parallel write
          streams; halve workers compared to a single-destination
          direct transfer and start small the first time a new
          fan-out shape runs against a fleet.
        </p>
      </>
    ),
  },
  {
    id: 'scheduling',
    title: 'Scheduling',
    shortLabel: 'Recurring snapshot, restore, direct, or fan-out jobs on a cron-style schedule',
    body: (
      <>
        <p>
          Every job mode can be scheduled. The Schedules tab is a
          mirror of the Run Job form: same source / destination
          pickers, same library + metric scoping, same per-run
          settings, plus a schedule-only cron expression and an
          optional disabled flag. A scheduled job fires on its
          cadence with the same shape it would have if the
          operator clicked Run on the Run Job form.
        </p>
        <p>
          <strong>Schedule semantics.</strong> The scheduler loop
          checks for due jobs every 30 seconds (tunable via
          scheduler_tick_seconds). A schedule's next_run_at
          timestamp is computed from its cron expression and
          stored on the row; the loop fires every schedule whose
          next_run_at has passed and re-computes the next
          firing. Missed firings (due during a server outage)
          fire at most once on the next start; the scheduler
          doesn't try to "catch up" multiple missed windows.
        </p>
        <p>
          <strong>Cross-platform resolutions on schedules.</strong>{' '}
          Schedules can carry stored cross-platform resolutions
          (Drop / Map / Create decisions from the Cross-Platform
          Preflight modal) so a scheduled cross-backend restore
          fires deterministically — no operator at fire time to
          ack ambiguities. The schedule list endpoint surfaces a
          resolutions_status badge per row by comparing the
          stored decisions against the current destination user
          roster, so the operator knows when stored mappings have
          rotted (e.g., a destination user was renamed since the
          preflight was authored).
        </p>
        <p>
          <strong>Replace-mode schedules.</strong> A schedule with
          restore_mode='replace' must explicitly set
          confirm_replace=true on its stored config — same two-
          layer opt-in as a one-shot Replace job. Scheduled
          Replace jobs auto-capture a pre-Replace safety snapshot
          on every firing (same as the one-shot version).
        </p>
        <p>
          <strong>Key gotcha.</strong> Schedules don't surface
          "this firing has been queued but the worker is busy"
          state visibly today. If a long-running snapshot is
          mid-flight when a schedule fires, the new firing
          queues behind it; the operator sees the schedule's
          last_run_at stay stale until the queue drains.
        </p>
      </>
    ),
  },
  {
    id: 'playlist-transfer',
    title: 'Playlist Transfer',
    shortLabel: 'Transfer one playlist from one user to another, preserving destination ownership',
    body: (
      <>
        <p>
          Playlist Transfer (Run Jobs ▸ Playlist Transfer) copies
          ONE playlist from a source server's user to a
          destination server's user — and gives the destination
          user actual <em>ownership</em> of the new playlist, not
          just visibility. Works for any pairing of Plex /
          Jellyfin / Emby on either side.
        </p>
        <p>
          <strong>Why we built this.</strong> The other job modes
          (snapshot / restore / direct transfer / fan-out)
          replicate state in bulk: every library, every user,
          every playlist that satisfies the operator's filters.
          That's the right shape for migrations and backups, but
          it's the wrong shape for the everyday "I want to send
          this one playlist to my partner's account on my
          Jellyfin" workflow. Playlist Transfer is the surgical
          tool for that case.
        </p>
        <p>
          <strong>Why preserving destination ownership is the
          point.</strong> The naive way to "send a playlist to
          another user" is to have the owner / admin create a
          playlist that the other user can see. That works on
          paper but it sucks in practice: the destination user
          can't rename it, can't reorder it, can't add their own
          tracks to it, can't delete it — anything they try is
          forbidden because the playlist isn't theirs. The
          original goal of this feature was to make the new
          playlist <em>belong</em> to the destination user, so
          they can manage it the same way they'd manage a
          playlist they created themselves. Anything less than
          true ownership is a gift you can't unwrap.
        </p>
        <p>
          <strong>Why this is hard with Plex.</strong> Plex's REST
          API doesn't expose a "create as user X" verb the way
          Jellyfin and Emby do (those backends let an admin token
          create a playlist with UserId in the URL, and the
          playlist lands owned by that user). On Plex, the only
          token that can create a playlist owned by user X is
          user X's own token — and Plex Home users have rotating
          per-user tokens that aren't shared with the admin
          token. Without the user's own token, the best the
          admin can do is create a playlist owned by the admin
          and share it; the destination user can see it but not
          modify it. That's the trap the naive approach falls
          into.
        </p>
        <p>
          <strong>How we sidestep the trap.</strong> Two pieces of
          infrastructure work together:
        </p>
        <ol>
          <li>
            <strong>Per-user token storage.</strong> The Servers ▸
            User Management panel + the dedicated{' '}
            <code>POST /api/managed-users/{'{server_id}'}/{'{username}'}/plex-home-token</code>{' '}
            endpoint let the operator paste a Plex Home user's
            X-Plex-Token (obtained from plex.tv ▸ Authorized
            Devices). The token is Fernet-encrypted at rest using
            the per-install <code>server_data/.keyfile</code> and
            stored on the per-server <code>managed_users</code>{' '}
            row.
          </li>
          <li>
            <strong>The{' '}
            <code>playlist_mgmt_plex_home_auth_mode</code> tunable
            (Settings ▸ Tunables ▸ Playlist Transfer).</strong>{' '}
            Two values: <code>owner_token</code> (default, legacy
            behaviour: admin creates the playlist, destination
            user sees but doesn't own it) and{' '}
            <code>per_user_token</code> (the orchestrator pulls
            the saved per-user token + builds a per-user
            PlexServer instance + writes AS the user — the new
            playlist lands genuinely owned by the destination
            user). When per_user_token is on and the operator
            hasn't saved a token for the target user, the API
            returns <code>DEST_USER_TOKEN_MISSING</code> (HTTP
            412) with an operator-actionable message; the UI
            opens the per-user-token form pre-targeted at the
            right user instead of showing a generic copy
            failure. There's also a strict-mode flag
            (strict_identity_resolution) that lets the operator
            choose between "refuse to write unless we have the
            per-user token" and "silent admin fallback when we
            don't" — same tunable controls both the
            identity-resolution chain elsewhere in the app.
          </li>
        </ol>
        <p>
          <strong>The identity-management overlay.</strong>{' '}
          Playlist Transfer leans on the same{' '}
          <code>app_user_uuid</code> + <code>user_identity_map</code>{' '}
          infrastructure documented in the user-identity-uuid
          topic. When the destination user picker resolves a
          user, the orchestrator consults the resolution chain
          (per-job override → identity_map → backend_user_id
          direct match → username match) so a destination user
          whose handle differs from the source still routes
          correctly. This is why the app maintains an entire
          identity-management schema on top of just managing the
          user base — it's what lets one user's playlist land in
          a different user's account on a different backend with
          the destination user keeping full ownership.
        </p>
        <p>
          <strong>The data flow.</strong> The orchestrator (1)
          connects to the source server, (2) reads the named
          playlist's items (cache-first when fresh, live
          otherwise — controlled by the playlist_cache_*
          tunables), (3) resolves each item against the
          destination server via cross-backend GUID match, (4)
          builds a destination UserContext using either the
          admin token (owner_token mode) or the saved per-user
          token (per_user_token mode), and (5) calls the
          destination adapter's create_playlist with that
          UserContext so the new playlist lands attributed to
          the right user. Items the destination doesn't have are
          skipped with a per-row reason; the UI surfaces the
          skipped count so the operator knows the gap.
        </p>
        <p>
          <strong>Mixed-media handling.</strong> Jellyfin and
          Emby allow a single playlist to mix audio + video +
          photos; Plex doesn't. When transferring a mixed
          playlist to a Plex destination, the operator picks one
          of three behaviors via the mixed_media_behavior tunable
          (per-run override too): <code>skip</code> (default — log
          + drop the playlist), <code>dominant</code> (write a
          single playlist using the dominant media type's items),
          or <code>split</code> (write N suffixed playlists, one
          per type that's present). Per-user overrides live under
          the cross_platform_resolutions block so different
          source users can have different mixed-media policies in
          the same job.
        </p>
        <p>
          <strong>Caching.</strong> The Playlist Transfer surface
          maintains its own per-(server, user) playlist cache
          (server_data/playlist_cache.db) so re-opening the panel
          + picking a previously-seen server doesn't fire N
          live API calls. Cache TTLs are operator-controlled via
          the playlist_cache_* tunables; the UI shows fresh /
          stale / refreshing badges per user and exposes a
          "Refresh" button that bypasses the cache. The cache
          rows carry the user's <code>app_user_uuid</code> so
          backend_user_id rotation doesn't orphan cache entries.
        </p>
        <p>
          <strong>Key gotcha.</strong> Smart playlists are always
          excluded from the source picker — their criteria don't
          port across backends, and even within Plex the
          criteria reference local library section keys that
          aren't meaningful on another server. A "N smart
          playlist(s) hidden" hint appears at the top of the
          source list when any are filtered. To recreate a smart
          playlist on the destination, the operator must
          manually re-author it using the destination's smart-
          filter UI.
        </p>
      </>
    ),
  },
];


function DeepDivePage() {
  const [filter, setFilter] = useState('');
  const normalizedFilter = filter.trim().toLowerCase();

  const matches = (entry: FeatureEntry): boolean => {
    if (!normalizedFilter) return true;
    const haystack = (
      entry.title + ' ' + entry.shortLabel + ' ' + entry.id + ' ' +
      // Cheap body-text extraction so the filter also matches body
      // copy. React children render with JSX.toString() noisy; we
      // walk the tree and accumulate every string node we hit.
      extractText(entry.body)
    ).toLowerCase();
    return haystack.includes(normalizedFilter);
  };

  const visible = FEATURE_ENTRIES.filter(matches);

  return (
    <>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Deep Dive</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12 }}>
          Detailed coverage of the major features this app ships today:
          what each one does, when to use it, and the design decision
          behind it. For a faster orientation that lists the job modes
          and when to pick each, see <strong>Quick Start</strong>.
          This filter searches titles, summaries, and the body text of
          every feature; the global search bar above scans every Help
          sub-tab at once.
        </span>
        <label className="field" style={{ marginTop: 12 }}>
          <span className="label">Filter</span>
          <input
            type="text"
            placeholder="Search feature titles, summaries, or body text"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
          {normalizedFilter && (
            <span className="help" style={{ fontSize: 12 }}>
              Showing {visible.length} of {FEATURE_ENTRIES.length} features
              matching &ldquo;{filter}&rdquo;.
            </span>
          )}
        </label>
      </div>

      {visible.map((entry) => (
        <div key={entry.id} className="panel">
          <h2 style={{ marginTop: 0 }}>{entry.title}</h2>
          <div style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 12 }}>
            <code>{entry.id}</code> &middot; {entry.shortLabel}
          </div>
          <div style={{ fontSize: 14, lineHeight: 1.55 }}>{entry.body}</div>
        </div>
      ))}

      {visible.length === 0 && (
        <div className="panel">
          <div className="empty">No features match the current filter.</div>
        </div>
      )}

      {!normalizedFilter && (
        <div className="panel" style={{ background: 'rgba(74, 122, 252, 0.06)' }}>
          <h2 style={{ marginTop: 0 }}>See also: Quick Start</h2>
          <p style={{ margin: 0 }}>
            Looking for the &ldquo;which job mode do I pick?&rdquo;
            orientation instead of design rationale? <strong>Quick
            Start</strong> covers Snapshot, Restore (Merge vs Replace),
            Direct Transfer, Playlist Management, Fan-out, and
            Scheduled in plain-English &ldquo;when to use this&rdquo;
            terms. The two tabs are complementary on purpose: Quick
            Start tells you which lever to pull; Deep Dive tells you
            what each lever does internally.
          </p>
        </div>
      )}
    </>
  );
}


// Walk a React node tree and concatenate every string leaf for
// substring filtering on the Features page. Cheap; runs only when
// the filter input is non-empty.
function extractText(node: React.ReactNode): string {
  if (node == null || typeof node === 'boolean') return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(extractText).join(' ');
  if (typeof node === 'object' && 'props' in node && node.props) {
    const props = node.props as { children?: React.ReactNode };
    return extractText(props.children);
  }
  return '';
}


// ── Topics ────────────────────────────────────────────────────────────────────
//
// Renders the help_content registry as a flat, searchable list grouped
// by category. The same bodies appear inside InfoTip popovers next to
// the controls they describe; this page is the canonical reference when
// tooltips are disabled or the operator wants to read end-to-end.

function TopicsPage() {
  const [filter, setFilter] = useState('');
  const grouped = getHelpTopicsByCategory();
  const normalizedFilter = filter.trim().toLowerCase();

  const matches = (haystack: string) =>
    !normalizedFilter || haystack.toLowerCase().includes(normalizedFilter);

  const visibleByCategory: typeof grouped = {};
  for (const [category, topics] of Object.entries(grouped)) {
    const kept = topics.filter(
      (t) => matches(t.title) || matches(t.shortLabel) || matches(t.id),
    );
    if (kept.length > 0) visibleByCategory[category] = kept;
  }
  const totalVisible = Object.values(visibleByCategory).reduce(
    (n, ts) => n + ts.length,
    0,
  );

  return (
    <>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Topics</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12 }}>
          Every explanation that appears in an InfoTip popover lives here too.
          Use this page if you have tooltips disabled, or if you want to read
          through related topics in one place rather than chasing icons in the
          UI. Topics are grouped by area; a filter narrows by keyword.
        </span>
        <label className="field" style={{ marginTop: 12 }}>
          <span className="label">Filter</span>
          <input
            type="text"
            placeholder="Search topic titles, short labels, or ids"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
          {normalizedFilter && (
            <span className="help" style={{ fontSize: 12 }}>
              Showing {totalVisible} topic(s) matching &ldquo;{filter}&rdquo;.
            </span>
          )}
        </label>
      </div>

      {Object.entries(visibleByCategory).map(([category, topics]) => (
        <div key={category} className="panel">
          <h2 style={{ marginTop: 0, textTransform: 'capitalize' }}>{category}</h2>
          {topics.map((t) => (
            <div key={t.id} style={{ marginTop: 18 }}>
              <h3 style={{ marginBottom: 4 }}>{t.title}</h3>
              <div style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 8 }}>
                <code>{t.id}</code> &middot; {t.shortLabel}
              </div>
              <div style={{ fontSize: 14, lineHeight: 1.55 }}>{t.body}</div>
            </div>
          ))}
        </div>
      ))}

      {totalVisible === 0 && (
        <div className="panel">
          <div className="empty">No topics match the current filter.</div>
        </div>
      )}
    </>
  );
}


// ── Dev Notes ────────────────────────────────────────────────────────────────
//
// Developer-facing walkthrough of the moving parts under Hestia-MediaManager.
// Plain English; audience is a new contributor who has read the
// codebase tour but has not yet traced any one pixel back to a
// function. Four nested sub-tabs:
//
//   * ETR        - how the dashboard's countdown is collected.
//   * Engines    - the snapshot / restore / direct / fan-out / adapter
//                  engines and where they live.
//   * Databases  - every SQLite file + JSON config we keep on disk,
//                  what's in each, and the rules for adding columns.
//   * Live Sync  - roadmap notes for the not-yet-shipped continuous
//                  source-to-destination sync.
//
// Keep this page in sync with services/timing.py,
// services/run_timer.py, server/run_timings_db.py,
// services/eta_training.py, services/snapshotter.py,
// services/restorer.py, services/snapshotter_adapter.py,
// services/restorer_adapter.py, server/direct_transfer.py,
// server/fan_out.py, services/adapters/__init__.py, server/media_db.py,
// server/auth_db.py, server/snapshot_registry.py, and
// server/snapshot_capture.py when any of those move.

type DevNotesSubTab = 'engines' | 'databases' | 'live_sync';

function DevNotesPage() {
  const [sub, setSub] = useState<DevNotesSubTab>('engines');
  return (
    <>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Dev Notes</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12 }}>
          Engineering notes for contributors. Pick a topic below. Each
          section is written for someone who is new to the codebase and
          wants to understand the moving parts before tracing the code.
        </span>
        <nav className="tabs sub-tabs" style={{ marginTop: 12 }}>
          <button
            className={sub === 'engines' ? 'active' : ''}
            onClick={() => setSub('engines')}
          >
            Engines
          </button>
          <button
            className={sub === 'databases' ? 'active' : ''}
            onClick={() => setSub('databases')}
          >
            Databases
          </button>
          <button
            className={sub === 'live_sync' ? 'active' : ''}
            onClick={() => setSub('live_sync')}
          >
            Live Sync
          </button>
        </nav>
      </div>

      {sub === 'engines' && <DevNotesEnginesSection />}
      {sub === 'databases' && <DevNotesDatabasesSection />}
      {sub === 'live_sync' && <DevNotesLiveSyncSection />}
    </>
  );
}


// ── Dev Notes > Engines ─────────────────────────────────────────────────────
//
// What an "engine" means in this codebase, the five engines we ship,
// and the adapter abstraction layer that lets non-Plex backends slot
// in. Pairs with services/snapshotter.py, services/restorer.py,
// services/snapshotter_adapter.py, services/restorer_adapter.py,
// server/direct_transfer.py, server/fan_out.py, and
// services/adapters/__init__.py.

function DevNotesEnginesSection() {
  return (
    <>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>What we mean by &ldquo;engine&rdquo;</h2>
        <p>
          An <strong>engine</strong> is the code path that actually
          does the work of one job mode end to end - it reads from a
          source, transforms, and either writes to disk or writes back
          to a destination. Engines are NOT routers, not job
          schedulers, not UI. They are the pipeline.
        </p>
        <p>
          A submitted job in <code>server/jobs.py</code> dispatches to
          exactly one engine based on the job&apos;s
          {' '}<code>mode</code> and the source/destination
          {' '}<code>service_type</code>. The router&apos;s only job
          is to pick the right engine; the engine&apos;s only job is
          to move the data.
        </p>
      </div>

      <div className="panel">
        <h2>The five engines, at a glance</h2>
        <table className="list" style={{ width: '100%' }}>
          <thead>
            <tr>
              <th style={{ width: '24%' }}>Engine</th>
              <th style={{ width: '14%' }}>Job mode</th>
              <th>What it does</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td><code>services/snapshotter.py</code></td>
              <td>snapshot (Plex source)</td>
              <td>Reads watch history, ratings, playlists, and collections from a live Plex server and writes them into a per-snapshot <code>.db</code> file (schema v15+) plus optional <code>.plexexport.json</code> sidecar.</td>
            </tr>
            <tr>
              <td><code>services/restorer.py</code></td>
              <td>restore (Plex destination)</td>
              <td>Reads a snapshot payload and applies it to a Plex server. Implements Merge (additive) and Replace (destination-only members removed). All write-path safety rules live here.</td>
            </tr>
            <tr>
              <td><code>server/direct_transfer.py</code></td>
              <td>direct (Plex)</td>
              <td>Reads from a Plex source and writes straight into a Plex destination, no intermediate <code>.db</code> on disk. Calls the same primitives as Snapshot + Restore but skips the disk hop.</td>
            </tr>
            <tr>
              <td><code>services/snapshotter_adapter.py</code></td>
              <td>snapshot (Jellyfin / Emby source)</td>
              <td>Backend-agnostic snapshot engine. Drives the adapter ABC instead of plexapi directly. Smaller / younger than its Plex sibling; per-user fan-out and playlists/collections capture are actively being filled in.</td>
            </tr>
            <tr>
              <td><code>services/restorer_adapter.py</code></td>
              <td>restore (Jellyfin / Emby destination)</td>
              <td>Backend-agnostic restore engine. Same payload shape as the Plex restorer consumes, so a Plex snapshot can in principle be restored to a non-Plex destination once cross-backend GUID resolution lands.</td>
            </tr>
          </tbody>
        </table>
        <p style={{ fontSize: 12, color: 'var(--text-dim)' }}>
          <code>server/fan_out.py</code> is a coordinator, not a sixth
          engine. It wraps one of the five engines above and runs it
          in parallel against multiple destinations. Each
          destination gets its own thread, its own
          {' '}<code>DashboardState</code>, its own log directory; the
          engine itself doesn&apos;t know it&apos;s being fanned out.
        </p>
      </div>

      <div className="panel">
        <h2>The Plex / non-Plex split</h2>
        <p>
          You may notice we have TWO snapshotters and TWO restorers.
          That is deliberate. Plex is the original target and the Plex
          engines (<code>snapshotter.py</code> + <code>restorer.py</code>)
          are tuned against plexapi&apos;s data shape, its idioms, and
          its quirks. Moving them onto the backend-agnostic adapter
          surface in one cut would introduce a large regression
          window for the most-used path.
        </p>
        <p>
          Instead, the multi-backend roadmap runs two engine families
          in parallel:
        </p>
        <ul>
          <li>
            <strong>Plex engines</strong>: stay on plexapi for now.
            Already battle-tested. No rewrite.
          </li>
          <li>
            <strong>Adapter engines</strong>
            ({' '}<code>*_adapter.py</code>): drive the
            {' '}<code>MediaServerAdapter</code> ABC. Used today for
            Jellyfin and Emby; could eventually subsume the Plex path
            once the Plex adapter has full feature parity.
          </li>
        </ul>
        <p>
          The dispatch happens in <code>server/jobs.py</code> by
          looking at the source / destination server&apos;s
          {' '}<code>service_type</code>. Plex sources/destinations
          route to the Plex engines; everything else routes to the
          adapter engines.
        </p>
      </div>

      <div className="panel">
        <h2>The adapter layer (services/adapters/)</h2>
        <p>
          The adapter package is what makes &ldquo;backend-agnostic&rdquo;
          actually work. It is one Python ABC
          (<code>MediaServerAdapter</code>) plus one
          implementation per backend:
        </p>
        <ul>
          <li>
            <code>services/adapters/plex.py</code> -
            {' '}<code>PlexAdapter</code>. Shipped.
          </li>
          <li>
            <code>services/adapters/jellyfin.py</code> -
            {' '}<code>JellyfinAdapter</code>. ~849 lines. Shipped.
          </li>
          <li>
            <code>services/adapters/emby.py</code> -
            {' '}<code>EmbyAdapter</code>. ~98 lines, subclasses
            JellyfinAdapter and overrides the spots where Emby&apos;s
            API differs. Shipped.
          </li>
          <li>
            <code>services/adapters/_http_base.py</code> - shared
            httpx-based HTTP client used by the Jellyfin and Emby
            adapters (Plex uses plexapi, which has its own session).
          </li>
        </ul>
        <p>
          The ABC&apos;s surface was derived <em>from the engine&apos;s
          actual call sites</em>, not from a clean-sheet design. If the
          adapter engines don&apos;t call a method today, the ABC
          doesn&apos;t declare it. This is on purpose; over-abstraction
          here would create churn every time a real backend quirk
          surfaces.
        </p>
        <p>
          A <code>ServerConnection</code> dataclass wraps one connected
          adapter instance plus the credentials and base URL it was
          built from. Engines accept a <code>ServerConnection</code>,
          never raw plexapi objects, when running on the adapter path.
          See <code>server/server_registry.py:connect_registered_server</code>
          for how a saved row in <code>servers.json</code> becomes
          a connected adapter at job start.
        </p>
      </div>

      <div className="panel">
        <h2>End to end: a single Snapshot job</h2>
        <ol>
          <li>
            UI submits Run Job. <code>server/jobs.py</code> picks the
            mode (<code>snapshot</code>) and reads the source
            server&apos;s <code>service_type</code> from the registry.
          </li>
          <li>
            Dispatch: Plex source -&gt; call
            {' '}<code>services.snapshotter.run_snapshot(...)</code>;
            Jellyfin / Emby source -&gt; call
            {' '}<code>services.snapshotter_adapter.snapshot_library_adapter(...)</code>
            for each library.
          </li>
          <li>
            The engine reads from the source (plexapi for Plex; the
            adapter&apos;s <code>iter_items</code> /
            {' '}<code>fetch_watch_state</code> /
            {' '}<code>fetch_ratings</code> for non-Plex).
          </li>
          <li>
            The engine writes a per-snapshot SQLite file
            (<code>schema_version &gt;= 15</code>, with the
            library-section identity invariant - see the Databases
            tab) and optionally renders a
            {' '}<code>.plexexport.json</code> sidecar.
          </li>
          <li>
            <code>server/snapshot_registry.py</code> records one row
            in <code>snapshots.db</code> with the path, byte size,
            schema version, and per-library item counts.
          </li>
          <li>
            <code>server/run_timings_db.py</code> persists the timing
            entries and the run_history row (see the ETR tab for what
            that buys us). The eta_training trainer folds the new
            observations into its buckets.
          </li>
        </ol>
        <p style={{ fontSize: 12, color: 'var(--text-dim)' }}>
          A Restore job follows the same shape with source / destination
          swapped: the engine reads a snapshot payload and writes to
          the destination&apos;s API surface. Direct Transfer skips
          step 4-5 and pipes the in-memory dict straight from snapshot
          to restore.
        </p>
      </div>
    </>
  );
}


// ── Dev Notes > Databases ───────────────────────────────────────────────────
//
// What lives in server_data/ and what each file is responsible for.
// Pairs with server/auth_db.py, server/media_db.py,
// server/snapshot_registry.py, server/snapshot_capture.py,
// server/snapshot_serializer.py, server/run_timings_db.py,
// server/persistence.py.

function DevNotesDatabasesSection() {
  return (
    <>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Disk layout: where state lives</h2>
        <p>
          Everything Hestia-MediaManager persists across container restarts
          lives in three host bind mounts:
          {' '}<code>./server_data</code> (config + databases),
          {' '}<code>./snapshots</code> (captured snapshot files), and
          {' '}<code>./plex_logs</code> (per-run log directories).
          {' '}<code>server_data/</code> holds four SQLite databases,
          three JSON config files, the Fernet keyfile, and a privileged-write
          audit log:
        </p>
        <pre style={{ background: 'var(--panel-bg-strong, #1c1c1c)', padding: 10, fontSize: 12, overflowX: 'auto' }}>
{`server_data/
├── auth.db            # operator login accounts (always on)
├── media.db           # primary media-state cache
├── snapshots.db       # registry of captured snapshots
├── run_timings.db     # per-op telemetry + ETA weights + run history
├── settings.json      # tunables + persistent UI settings
├── servers.json       # registered source / destination servers (Fernet-encrypted tokens)
├── schedules.json     # saved scheduled-snapshot entries
├── .keyfile           # Fernet master key (decrypts servers.json + per-user tokens)
└── db_access.log      # audit log for privileged DB writes

snapshots/             # per-snapshot .db files + .plexexport.json sidecars
plex_logs/             # per-run log directories`}
        </pre>
        <p>
          Per-snapshot <code>.db</code> files default to the
          {' '}<code>./snapshots</code> bind mount but can be redirected
          to any container-side path via the operator-configured
          {' '}<code>output_dir</code> (Settings &gt; Servers &gt; Run
          Defaults). The snapshot registry holds the authoritative
          catalogue with full paths regardless of where the files
          land. Backup advice for all three mounts lives on the DB
          Schema tab under <em>Backup and recovery</em>.
        </p>
      </div>

      <div className="panel">
        <h2>media.db: the primary cache</h2>
        <p>
          The big one. Schema version 8 today
          (<code>server.media_db.CURRENT_SCHEMA_VERSION</code>) with
          an idempotent boot-time migration that auto-archives any
          {' '}<code>.bak</code> of an older version. The boot path
          refuses to run against a newer schema than it knows about.
        </p>
        <p>
          Key tables:
        </p>
        <ul>
          <li>
            <strong><code>items</code></strong>: one row per logical
            entity. Keyed by upstream metadata IDs (IMDb / TVDB /
            TMDB / MusicBrainz), <em>not</em> by Plex&apos;s
            {' '}<code>ratingKey</code>. The ratingKey is per-server
            and per-install; the upstream ID is identical on every
            server, so cross-server matching is a simple GUID
            lookup.
          </li>
          <li>
            <strong><code>server_items</code></strong>: the
            many-to-many bridge. One row per (server, item) pair,
            carrying that server&apos;s backend-specific item ID
            (ratingKey for Plex; ItemId for Jellyfin / Emby).
          </li>
          <li>
            <strong><code>watch_events</code></strong>: per-user
            view counts, offsets, and last-viewed timestamps.
          </li>
          <li>
            <strong><code>ratings</code></strong>: per-user star
            ratings (0-10 internal, 1-5 displayed).
          </li>
          <li>
            <strong><code>playlists</code> + <code>collections</code></strong>:
            per-server, per-user playlists / collections. Linked
            back to <code>items</code> via <code>server_items</code>.
          </li>
          <li>
            <strong><code>library_sections</code></strong>: one row
            per library (Movies, TV, Music, ...) on each server.
            Every per-server row above carries a positive
            {' '}<code>section_key</code> FK to this table. This is
            the v0.15 identity invariant; the rules for keeping it
            intact are documented in the project&apos;s coding
            guidelines.
          </li>
        </ul>
      </div>

      <div className="panel">
        <h2>Per-snapshot .db files (schema v15+)</h2>
        <p>
          When a Snapshot job runs, the engine materialises a fresh
          SQLite file that mirrors the relevant
          {' '}<code>media.db</code> rows filtered to one server. It
          is NOT a byte-for-byte copy of <code>media.db</code> - the
          sqlite online export API would drag every server&apos;s data
          along, which is wasteful and breaks the per-server-snapshot
          model.
        </p>
        <p>
          Instead, <code>server/snapshot_capture.py</code>:
        </p>
        <ol>
          <li>Reads the live <code>media.db</code> schema DDL from
          {' '}<code>sqlite_master</code>.</li>
          <li>Applies that schema to a fresh, empty snapshot file.</li>
          <li>Uses <code>ATTACH DATABASE</code> to bridge source and
          destination, then runs <code>INSERT ... SELECT</code> with
          {' '}<code>WHERE server_id = ?</code> on the per-server
          tables and a join through <code>server_items</code> for the
          shared <code>items</code> table.</li>
          <li>Writes a <code>snapshot_meta</code> row recording
          {' '}<code>schema_version</code>, capture timestamp, and
          source server identity.</li>
        </ol>
        <p>
          The serializer
          ({' '}<code>server/snapshot_serializer.py</code>) refuses any
          snapshot file with
          {' '}<code>snapshot_meta.schema_version &lt; 15</code>. The
          v15 invariant guarantees every per-server row has a positive
          {' '}<code>section_key</code> so the restore engine can route
          work to the correct destination library without
          divide-by-N approximations.
        </p>
      </div>

      <div className="panel">
        <h2>snapshots.db: the registry, not the payload</h2>
        <p>
          <code>snapshots.db</code> stores metadata <em>about</em>
          captured snapshots; it does NOT store snapshot contents.
          One row per captured snapshot, holding the path on disk
          (where the actual <code>.db</code> lives), byte size, the
          schema version of the captured file, per-library item
          counts, capture timestamp, and source server identity.
        </p>
        <p>
          Lifecycle helpers in
          {' '}<code>server/snapshot_registry.py</code>:
          {' '}<code>register</code> on capture,
          {' '}<code>list_snapshots</code> for the panel,
          {' '}<code>get</code> for the download endpoint,
          {' '}<code>delete</code> for the operator-initiated remove
          (row + file + JSON sidecar), and
          {' '}<code>enforce_retention</code> which trims oldest
          rows by per-server cap. The Settings &gt; Servers &gt; Run
          Defaults panel surfaces the cap.
        </p>
      </div>

      <div className="panel">
        <h2>run_timings.db: telemetry, learner, and history</h2>
        <p>
          Despite the name, this file holds three tables, not one. All
          three live in one file because their lifecycles are tightly
          coupled: every completed job writes to all three at the same
          drain point.
        </p>
        <ul>
          <li>
            <strong><code>run_timings</code></strong>: one row per
            timed operation
            (<code>services/run_timer.py:TimingEntry</code>). The raw
            material the dashboard&apos;s Recent Runtimes panel reads.
            See the ETR tab for the full story.
          </li>
          <li>
            <strong><code>eta_weights</code></strong>: trained EMA +
            variance per
            {' '}<code>BucketKey</code> (server, label, library type,
            bulk strategy, size bucket). The adaptive learner
            (<code>services/eta_training.py</code>) reads on boot,
            writes on job completion. Empty buckets are fine; the
            cold-start fallback chain handles new servers.
          </li>
          <li>
            <strong><code>run_history</code></strong>: one row per
            completed run with mode, server, library list, start /
            end, success/failure, item totals, and the affected-user
            list. Added in the recent dashboard / log reorg. Drives
            the Recent Runtimes panel.
          </li>
        </ul>
        <p>
          Retention is bounded for all three. The
          {' '}<code>run_timings_retention_count</code> tunable
          (default 200) caps the rolling window of preserved
          {' '}<code>run_id</code>s; older rows are pruned at end-of-run.
          A value of <code>0</code> means &ldquo;unlimited&rdquo;.
        </p>
      </div>

      <div className="panel">
        <h2>auth.db: the always-on credential store</h2>
        <p>
          Operator login accounts for the web UI: usernames, bcrypt
          password hashes, display names, role flags, refresh-token
          state. Created on first boot by the Setup wizard and loaded
          on every container start; there is no auth-off mode.
        </p>
        <p>
          Kept in a separate file from <code>media.db</code>
          deliberately. The two have completely different sensitivity
          and lifecycle: auth is tiny + sensitive + worth frequent
          backups; media is large + regenerable + low-sensitivity.
        </p>
      </div>

      <div className="panel">
        <h2>JSON config files</h2>
        <table className="list" style={{ width: '100%' }}>
          <thead>
            <tr>
              <th style={{ width: '24%' }}>File</th>
              <th>Contents</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td><code>settings.json</code></td>
              <td>Single document. Holds tunables (the System Tunables panel writes here), run defaults, retention caps, and persistent UI preferences. Rewritten in full on every change.</td>
            </tr>
            <tr>
              <td><code>servers.json</code></td>
              <td>List of registered source / destination servers. Each row carries id, name, URL, encrypted token, <code>service_type</code> (plex / jellyfin / emby), and per-server overrides. Edited via Settings &gt; Servers.</td>
            </tr>
            <tr>
              <td><code>schedules.json</code></td>
              <td>List of saved scheduled snapshot entries. Each row is a cron-like spec plus the job parameters that fire at trigger time.</td>
            </tr>
          </tbody>
        </table>
        <p style={{ fontSize: 12, color: 'var(--text-dim)' }}>
          All three files are read on boot, validated, and held in
          memory; writes round-trip through the
          {' '}<code>server/persistence.py</code> layer with file
          locks so two writers cannot tear a JSON document.
        </p>
      </div>

      <div className="panel">
        <h2>Adding a column: the rules of the road</h2>
        <ul>
          <li>
            <strong>media.db schema changes</strong>: bump
            {' '}<code>CURRENT_SCHEMA_VERSION</code> in
            {' '}<code>server/media_db.py</code> and add a migration
            step in the same module. The boot path auto-archives any
            file with a lower version into <code>.bak</code>; do NOT
            try to silently in-place mutate.
          </li>
          <li>
            <strong>Snapshot-file schema changes</strong>: bump
            {' '}<code>SNAPSHOT_SCHEMA_VERSION</code> in
            {' '}<code>server/snapshot_capture.py</code>. The
            serializer refuses any snapshot below the current value,
            so old snapshots can&apos;t be silently misread.
          </li>
          <li>
            <strong>Never default section_key to 0 or make it
            nullable</strong>. The library-section identity invariant
            (v0.15) requires a positive
            {' '}<code>section_key</code> on every per-server row.
          </li>
          <li>
            <strong>Run-timings schema changes</strong>: bump the
            {' '}<code>schema_version</code> stored in the
            {' '}<code>schema_meta</code> table and add the
            {' '}<code>ALTER TABLE</code> in the init path. This file
            is regenerable; if migration is too gnarly, dropping and
            recreating is acceptable (the worst case is losing 200
            past runs of ETA training data).
          </li>
          <li>
            <strong>JSON config changes</strong>: add a default to
            the load path so older config files still parse. The
            settings + servers + schedules loaders all tolerate
            missing keys.
          </li>
        </ul>
      </div>
    </>
  );
}


// ── Dev Notes > Live Sync ───────────────────────────────────────────────────
//
// Roadmap notes for the not-yet-shipped continuous source-to-destination
// sync. This page is forward-looking: it describes what would be needed
// to add, what's already in place that helps, and the open design
// questions. Nothing here is shipped; treat the design as a strawman
// to be torn down in a real planning round before any code lands.

function DevNotesLiveSyncSection() {
  return (
    <>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>What &ldquo;Live Sync&rdquo; means here</h2>
        <p>
          Today every cross-server move is an <em>explicit job</em>:
          the operator submits Snapshot, Restore, Direct Transfer, or
          schedules one to fire on a cron. There is no path that
          notices &ldquo;the user scrobbled an episode on Server A&rdquo;
          and propagates that to Server B without an operator-issued
          job.
        </p>
        <p>
          <strong>Live Sync</strong>, as proposed, would be a
          long-running background service that watches one or more
          source servers for changes, diffs against the last known
          state, and applies the diff to one or more destinations
          continuously. Think of it as the Direct Transfer engine
          run on a tight loop, but smart enough to do almost nothing
          when nothing has changed.
        </p>
        <p style={{ fontSize: 12, color: 'var(--text-dim)' }}>
          Nothing on this page is shipped. This section is a starting
          point for the design conversation, not a substitute for it.
        </p>
      </div>

      <div className="panel">
        <h2>What we already have that helps</h2>
        <p>
          A surprising amount of the work is already done. The pieces
          a Live Sync would compose:
        </p>
        <ul>
          <li>
            <strong>The adapter ABC</strong>
            (<code>services/adapters/__init__.py</code>) gives a
            uniform read surface across Plex, Jellyfin, and Emby. A
            poller can ask any backend &ldquo;what has changed since
            timestamp X?&rdquo; through the same API.
          </li>
          <li>
            <strong>media.db with upstream-keyed items</strong> means
            cross-server matching is a hash lookup. A change observed
            on Server A resolves to a destination
            {' '}<code>backend_item_id</code> on Server B by going
            through the upstream GUID, not by name fuzzy-match.
          </li>
          <li>
            <strong>Direct Transfer&apos;s additive merge rules</strong>
            already handle the &ldquo;don&apos;t overwrite, don&apos;t
            reduce, only ever increase view counts&rdquo; constraint
            that Live Sync needs by default. The engine&apos;s
            existing Merge mode is the right write-path primitive.
          </li>
          <li>
            <strong>Fan-out</strong>
            ({' '}<code>server/fan_out.py</code>) already drives one
            source against many destinations in parallel. A Live Sync
            could be modelled as &ldquo;a long-lived fan-out job that
            never finishes&rdquo;.
          </li>
          <li>
            <strong>Run telemetry + ETA training</strong> means each
            tick of the sync loop generates the same observability
            data as a regular run. Operators see the same dashboard
            surfaces; the learner gets more samples.
          </li>
          <li>
            <strong>ContextVar-backed per-run state</strong>
            ({' '}<code>services/state.py</code>) is the threading
            primitive that lets two destinations run in parallel
            without leaking state into each other. The same model
            extends to N concurrent sync loops.
          </li>
        </ul>
      </div>

      <div className="panel">
        <h2>What is missing</h2>
        <ol>
          <li>
            <strong>A change-detection surface on the source.</strong>
            Plex has WebSocket eventing (<code>/:/eventsource</code>)
            and the <code>/status/sessions</code> endpoint for live
            playback; Jellyfin and Emby have similar but
            backend-specific channels. The adapter ABC has no
            {' '}<code>subscribe_to_changes</code> method today.
            Adding one is the single biggest piece of new work; it
            requires per-backend implementations and a fallback path
            for backends that don&apos;t support push (poll every N
            seconds against a <code>changed_since</code> query).
          </li>
          <li>
            <strong>A long-running daemon-mode loop</strong>. Today
            {' '}<code>server/jobs.py</code> assumes jobs are finite:
            they start, finish, and emit a final state. Live Sync is
            an infinite job that emits intermediate run-history rows
            but never terminates. The dashboard&apos;s job state
            machine needs a new state (something like
            {' '}<code>RUNNING_LIVE</code>) and the cancel path needs
            to mean &ldquo;stop the loop&rdquo; rather than &ldquo;abort
            mid-item&rdquo;.
          </li>
          <li>
            <strong>A diff persistence layer</strong>. The poller
            needs to remember &ldquo;the last view count I saw for
            (item, user) on Server A&rdquo; so it can produce a delta
            rather than re-applying the full state on every tick.
            {' '}<code>media.db</code> already carries the
            authoritative cross-server view; one new
            {' '}<code>sync_cursor</code> column or a sibling table
            of high-water marks would close the gap. SQL only -
            another JSON file here would be a step backward.
          </li>
          <li>
            <strong>Backpressure + rate limiting</strong>. A Plex
            server can be made very unhappy by 1000 scrobbles in 1
            second. The existing
            {' '}<code>scrobble_workers_default_cap</code> tunable +
            the retry budgets in
            {' '}<code>services/tunables.py</code> are the right
            knobs; Live Sync needs to honor them through the same
            adapter rate-limit primitives the regular engines use.
          </li>
          <li>
            <strong>Conflict policy</strong>. If two destinations are
            in sync AND the operator edits a rating directly on one,
            does the sync push the edit to the other or treat the
            edit as a divergence to preserve? Today Merge mode says
            &ldquo;never overwrite&rdquo;; under Live Sync that means
            the manual edit wins forever. That may or may not be the
            right default; it&apos;s a real product decision.
          </li>
          <li>
            <strong>UI surface</strong>. A new tab (Servers &gt; Live
            Sync?) with a per-source-server panel: enabled, poll
            cadence, destinations, last-tick result, next-tick ETA,
            running counters, a Pause button. Closely related to the
            Schedules panel but driven by &ldquo;long-running
            process&rdquo; semantics rather than &ldquo;cron-fires-job&rdquo;.
          </li>
        </ol>
      </div>

      <div className="panel">
        <h2>A proposed shipping order</h2>
        <p>
          Splitting into independently shippable increments so each
          step de-risks the next. None of this is a commitment; it&apos;s
          a strawman for the planning round.
        </p>
        <ol>
          <li>
            <strong>Step 1 - poll-mode prototype, Plex source only.</strong>
            Extend the adapter ABC with a
            {' '}<code>changes_since(cursor)</code> method, implement
            it for the Plex adapter using
            {' '}<code>/status/sessions</code> + the existing watch
            history endpoint with a <code>since=</code> filter, and
            wire a 60-second-cadence loop in a new
            {' '}<code>server/live_sync.py</code>. Output goes through
            the existing Direct Transfer engine in Merge mode. No UI;
            launch from a CLI flag. Goal: prove the end-to-end loop
            on a single source-destination pair.
          </li>
          <li>
            <strong>Step 2 - cursor persistence + restart safety.</strong>
            Add the <code>sync_cursor</code> column to
            {' '}<code>media.db</code> (per source server). On boot,
            resume from the last persisted cursor; never replay the
            same window twice. A crash mid-tick must not double-apply.
          </li>
          <li>
            <strong>Step 3 - push-mode on Plex.</strong> Replace
            polling with Plex&apos;s WebSocket eventing. Polling stays
            as the fallback for offline / unreachable sources.
          </li>
          <li>
            <strong>Step 4 - daemonised job lifecycle.</strong> A new
            {' '}<code>RUNNING_LIVE</code> state in
            {' '}<code>server/jobs.py</code>, intermediate
            {' '}<code>run_history</code> rows per tick, a UI panel
            with start / pause / stop controls. ETA training picks up
            the new label automatically; no learner change needed.
          </li>
          <li>
            <strong>Step 5 - Jellyfin + Emby source coverage.</strong>
            Per-adapter <code>changes_since</code> implementations.
            The engine&apos;s code path is already backend-agnostic by
            this point, so the work is contained inside the adapter
            modules.
          </li>
          <li>
            <strong>Step 6 - conflict-policy tunable + UI surface.</strong>
            Operator-facing knob for divergent-destination behaviour.
            Default to today&apos;s Merge semantics (manual edit
            wins); opt-in for &ldquo;source is authoritative&rdquo;
            on a per-Live-Sync basis.
          </li>
        </ol>
        <p style={{ fontSize: 12, color: 'var(--text-dim)' }}>
          Steps 1-3 are the engineering proof; steps 4-6 are the
          productisation. The split lets us validate the read-side
          (change detection) and the cursor model before paying the
          UI / lifecycle cost.
        </p>
      </div>

      <div className="panel">
        <h2>Open questions before any code</h2>
        <ul>
          <li>
            <strong>One-way or two-way?</strong> Almost certainly
            one-way (source -&gt; destinations) for the first cut.
            Two-way introduces the conflict policy work as a hard
            prerequisite. Worth confirming.
          </li>
          <li>
            <strong>Does &ldquo;live&rdquo; cover playlists +
            collections, or only watch history + ratings?</strong>
            The latter is much cheaper (smaller diffs, simpler
            cursor model). Playlists+collections often warrant manual
            review before propagation.
          </li>
          <li>
            <strong>What happens when the destination is offline?</strong>
            Queue the diff and apply on reconnect, or drop and let the
            next tick re-derive? Storage cost vs. correctness window.
          </li>
          <li>
            <strong>How does Live Sync interact with a Replace-mode
            Restore?</strong> If a Restore is mid-flight on a
            destination, should the sync loop pause for that
            destination? Yes, probably; needs a lock + a UI surface
            so the operator sees why a sync is paused.
          </li>
          <li>
            <strong>Is the loop one-per-source or one-per-(source,
            destination)?</strong> The fan-out model already does
            many destinations per source in one job; preserving that
            shape keeps the existing dashboard surfaces useful.
          </li>
        </ul>
      </div>
    </>
  );
}


// ── Users & Roles ───────────────────────────────────────────────────────────
//
// What every operator-facing role can and cannot do, where to find
// settings that look hidden, and three behaviours that surprise new
// users. The source of truth for the role list lives in
// server/auth_router.py (ROLE_PERMISSIONS, _ROLE_RANK) and
// frontend/src/contexts/AuthContext.tsx mirrors it for UI gating.
// Keep this page in sync if either side changes.

type UsersAndRolesSubTab = 'overview' | 'finding_settings' | 'behaviors' | 'mechanisms';

function UsersAndRolesPage() {
  const [sub, setSub] = useState<UsersAndRolesSubTab>('overview');
  return (
    <>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Users and Roles</h2>
        <span className="help" style={{ display: 'block', color: 'var(--text-dim)', fontSize: 12 }}>
          The app has six account roles. This page covers what each one
          can do, where to find role-gated settings that look hidden,
          three behaviours new users hit first, and how the supporting
          mechanisms (View Mode, sudo-style elevation, per-user grants)
          fit together.
        </span>
        <nav className="tabs sub-tabs" style={{ marginTop: 12 }}>
          <button
            className={sub === 'overview' ? 'active' : ''}
            onClick={() => setSub('overview')}
          >
            Overview
          </button>
          <button
            className={sub === 'finding_settings' ? 'active' : ''}
            onClick={() => setSub('finding_settings')}
          >
            Finding Settings
          </button>
          <button
            className={sub === 'behaviors' ? 'active' : ''}
            onClick={() => setSub('behaviors')}
          >
            Behaviours
          </button>
          <button
            className={sub === 'mechanisms' ? 'active' : ''}
            onClick={() => setSub('mechanisms')}
          >
            View Mode &amp; Elevation
          </button>
        </nav>
      </div>

      {sub === 'overview' && <UsersOverviewSection />}
      {sub === 'finding_settings' && <UsersFindingSettingsSection />}
      {sub === 'behaviors' && <UsersBehavioursSection />}
      {sub === 'mechanisms' && <UsersMechanismsSection />}
    </>
  );
}


// ── Users & Roles > Overview ────────────────────────────────────────────────

function UsersOverviewSection() {
  return (
    <>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>The six roles</h2>
        <p>
          Five of these roles can sign in. The sixth (Database Admin) is
          a non-login credential that gates destructive User Management
          writes; you never log in as it.
        </p>
        <table className="table" style={{ marginTop: 8 }}>
          <thead>
            <tr>
              <th style={{ textAlign: 'left' }}>Role</th>
              <th style={{ textAlign: 'left' }}>What you can do</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td><strong>Viewer</strong></td>
              <td>See the dashboard and the list of registered servers. Nothing else.</td>
            </tr>
            <tr>
              <td><strong>Operator</strong></td>
              <td>Everything Viewer can do, plus start jobs, see schedules, browse logs and snapshots.</td>
            </tr>
            <tr>
              <td><strong>Manager</strong></td>
              <td>Everything Operator can do, plus stop jobs and edit schedules.</td>
            </tr>
            <tr>
              <td><strong>Admin</strong></td>
              <td>
                Everything Manager can do, plus edit settings, register and remove servers,
                manage other user accounts (except the Root account), and reach the
                Database Admin Account page. Cannot edit System Tunables or Access Control.
              </td>
            </tr>
            <tr>
              <td><strong>Root Admin</strong></td>
              <td>
                Full control. Owns System Tunables, Access Control, and root promotions.
                The only role that can grant or revoke another root.
              </td>
            </tr>
            <tr>
              <td><strong>Database Admin</strong></td>
              <td>
                A non-login credential row. You never sign in as Database Admin.
                The credential is used as a second password to confirm destructive
                User Management writes (deleting Plex users, wiping per-user data).
                Created and updated by an Admin or Root Admin under
                Account &rsaquo; Account Management &rsaquo; Database Admin Account.
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h2>How accounts are created</h2>
        <p>
          On a fresh install, the first-boot wizard creates two accounts
          at once: a day-to-day <strong>Admin</strong> account and a
          separate <strong>Root Admin</strong> account. After setup, you
          are signed in as the Admin account. The Root Admin password is
          held back for the few actions that need it (described in
          Behaviours and View Mode &amp; Elevation).
        </p>
        <p>
          If you completed setup before this requirement landed, an
          &ldquo;Upgrade Split&rdquo; prompt will appear on your next
          sign-in and walk you through creating the missing Root
          account.
        </p>
        <p>
          Once setup is complete, the Root Admin can create additional
          accounts at any role from Account &rsaquo; Account Management
          &rsaquo; User Accounts. The Root role can only be granted via
          the dedicated &ldquo;Grant root&rdquo; action on a user row
          (not by editing the role dropdown), and granting it requires a
          fresh password re-confirmation.
        </p>
      </div>

      <div className="panel">
        <h2>The Database Admin Account</h2>
        <p>
          If you find yourself asked for a &ldquo;Database Admin&rdquo;
          password during a destructive User Management write, that is
          the credential under Account &rsaquo; Account Management
          &rsaquo; Database Admin Account, not your usual login
          password. It exists because destructive user writes (deleting
          a Plex managed user, wiping per-user metadata) are easy to
          fire by accident; gating them on a credential you do not type
          all day avoids that mistake. An Admin or Root Admin sets and
          updates this credential at any time.
        </p>
      </div>
    </>
  );
}


// ── Users & Roles > Finding Settings ────────────────────────────────────────

function UsersFindingSettingsSection() {
  return (
    <>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Where to find settings that look hidden</h2>
        <p>
          Most settings live exactly where you would expect, but a
          handful are intentionally tucked behind a role. If a feature
          mentioned in help text is not on your screen, the most likely
          reason is that your role is below the threshold for that
          surface. Sign in as an Admin or Root Admin to see the rest.
        </p>
        <table className="table">
          <thead>
            <tr>
              <th style={{ textAlign: 'left' }}>You are looking for</th>
              <th style={{ textAlign: 'left' }}>Where it lives</th>
              <th style={{ textAlign: 'left' }}>Who can see it</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>Add or remove a Plex / Jellyfin / Emby server</td>
              <td>Servers &rsaquo; Overview</td>
              <td>Admin, Root Admin</td>
            </tr>
            <tr>
              <td>Run defaults (workers, output directory, log directory)</td>
              <td>Servers &rsaquo; Run Defaults</td>
              <td>Admin, Root Admin</td>
            </tr>
            <tr>
              <td>System Tunables (HTTP timeouts, JWT lifetime, SQLite busy timeout)</td>
              <td>Settings &rsaquo; System Tunables</td>
              <td><strong>Root Admin only</strong></td>
            </tr>
            <tr>
              <td>
                Database viewer (browse the schema + data of every SQLite
                database the app creates: auth, media, snapshots, run timings,
                playlist cache, per-capture snapshot files)
              </td>
              <td>Settings &rsaquo; Databases</td>
              <td><strong>Root Admin only</strong></td>
            </tr>
            <tr>
              <td>Database Admin Account credential</td>
              <td>Account &rsaquo; Account Management &rsaquo; Database Admin Account</td>
              <td>Admin, Root Admin</td>
            </tr>
            <tr>
              <td>Per-user permission grants and revokes</td>
              <td>Account &rsaquo; Account Management &rsaquo; Access Control</td>
              <td><strong>Root Admin only</strong></td>
            </tr>
            <tr>
              <td>Create or delete a user account</td>
              <td>Account &rsaquo; Account Management &rsaquo; User Accounts</td>
              <td>Admin (cannot touch Root), Root Admin</td>
            </tr>
            <tr>
              <td>Promote a user to Root Admin</td>
              <td>Account &rsaquo; Account Management &rsaquo; User Accounts (Grant root button)</td>
              <td>Root Admin (with elevation)</td>
            </tr>
            <tr>
              <td>Stop a running job</td>
              <td>Dashboard &rsaquo; Stop button</td>
              <td>Manager and up</td>
            </tr>
            <tr>
              <td>Schedule editing</td>
              <td>Run Job &rsaquo; Schedules</td>
              <td>Manager and up</td>
            </tr>
            <tr>
              <td>Application logs (boot, errors, audit)</td>
              <td>Settings &rsaquo; Logs</td>
              <td>Operator and up</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h2>Where the Developer tab comes from</h2>
        <p>
          If you see a <strong>Developer</strong> tab in the top bar,
          the backend container is running with{' '}
          <code>PLEXMIGRATE_DEBUG_MODE=1</code>. Production deployments
          leave the variable unset and the tab does not render. None of
          its panels are gated by role; the env var is the only gate.
        </p>
      </div>
    </>
  );
}


// ── Users & Roles > Behaviours ──────────────────────────────────────────────

function UsersBehavioursSection() {
  return (
    <>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Three behaviours new users hit first</h2>
        <ol>
          <li style={{ marginBottom: 10 }}>
            <strong>
              You need to re-enter your password to do certain things,
              even though you are already signed in.
            </strong>
            <br />
            This is intentional. Creating a user, deleting a user,
            changing a user&apos;s role, granting Root, editing
            per-user permissions, and a handful of other root-level
            writes require a fresh password re-confirmation. The
            re-auth modal opens automatically when you click the
            button; the elevation lasts 10 minutes by default so
            repeated edits in the same sitting only prompt once.
          </li>
          <li style={{ marginBottom: 10 }}>
            <strong>
              Root Admin can preview the UI as any lower role.
            </strong>
            <br />
            Click &ldquo;Switch View Mode&rdquo; in the topbar and pick
            a role to drop to. The whole UI behaves as if you were
            that role; the backend honours the drop, not just the
            visuals. Use this to verify what an Operator or Viewer
            actually sees without making a test account. Closing the
            drop requires your password (so a tab left open cannot be
            silently raised by a hostile page).
          </li>
          <li>
            <strong>
              Closing the browser tab does not sign you out.
            </strong>
            <br />
            A long-lived refresh cookie (7 days by default, HttpOnly
            and scoped to <code>/api/auth</code>) is what brings the
            session back when you reopen the tab. To actually end the
            session, click <strong>Sign Out</strong> in the topbar.
            A password change also revokes every existing refresh
            cookie for your account, so any device that was still
            signed in has to log in again.
          </li>
        </ol>
      </div>

      <div className="panel">
        <h2>Session shape, at a glance</h2>
        <p>
          What survives what. The access token lives in browser memory
          only (never localStorage or sessionStorage); the refresh
          cookie is HttpOnly and the browser cannot read it.
        </p>
        <table className="table">
          <thead>
            <tr>
              <th style={{ textAlign: 'left' }}>Event</th>
              <th style={{ textAlign: 'left' }}>Access token</th>
              <th style={{ textAlign: 'left' }}>Refresh cookie</th>
              <th style={{ textAlign: 'left' }}>View Mode</th>
              <th style={{ textAlign: 'left' }}>Elevation</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>Page reload</td>
              <td>Re-minted via silent refresh</td>
              <td>Survives</td>
              <td>Survives</td>
              <td>Survives</td>
            </tr>
            <tr>
              <td>Close tab</td>
              <td>Discarded</td>
              <td>Survives</td>
              <td>Survives</td>
              <td>Survives</td>
            </tr>
            <tr>
              <td>Sign Out</td>
              <td>Discarded</td>
              <td>Revoked + cleared</td>
              <td>Cleared</td>
              <td>Cleared</td>
            </tr>
            <tr>
              <td>Password change (self)</td>
              <td>Survives until 30-min expiry</td>
              <td>All revoked</td>
              <td>Cleared</td>
              <td>Cleared</td>
            </tr>
            <tr>
              <td>Container restart</td>
              <td>Discarded</td>
              <td>Survives in cookie + DB row</td>
              <td><strong>Cleared</strong></td>
              <td><strong>Cleared</strong></td>
            </tr>
            <tr>
              <td>Idle 7 days</td>
              <td>Auto-refresh fails, sign out</td>
              <td>Expires</td>
              <td>Cleared on next sign-in</td>
              <td>Cleared on next sign-in</td>
            </tr>
          </tbody>
        </table>
      </div>
    </>
  );
}


// ── Users & Roles > View Mode & Elevation ───────────────────────────────────

function UsersMechanismsSection() {
  return (
    <>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Sudo-style elevation</h2>
        <p>
          Some Root Admin actions need a fresh proof of password even
          when you are already signed in as Root. Calling those
          endpoints without elevation returns a 403, the UI opens an
          inline re-auth modal, you type your password, and the
          original request retries silently. Elevation lasts 10 minutes
          by default (configurable in Settings &rsaquo; System Tunables
          via <code>elevation_ttl_seconds</code>, floor 60s, ceiling
          3600s).
        </p>
        <p>Actions that require elevation (all Root Admin-only):</p>
        <ul>
          <li>Create, update, delete a user account</li>
          <li>Reset another user&apos;s password</li>
          <li>Grant or revoke Root on another user</li>
          <li>Edit per-user permission grants and revokes</li>
          <li>Rotate a managed-user Plex token</li>
          <li>Apply a PIN migration</li>
        </ul>
        <p>
          Drop elevation immediately with the &ldquo;Clear
          elevation&rdquo; button (equivalent to <code>sudo -k</code>)
          or by signing out.
        </p>
      </div>

      <div className="panel">
        <h2>View Mode: previewing the UI as a lower role</h2>
        <p>
          Any role above Viewer can temporarily preview the UI as a
          lower role. The drop is <strong>server-enforced</strong>:
          every API call honours the dropped role, not just the
          visuals. The trigger is the <strong>Switch View Mode</strong>
          button in the topbar.
        </p>
        <table className="table">
          <thead>
            <tr>
              <th style={{ textAlign: 'left' }}>Your real role</th>
              <th style={{ textAlign: 'left' }}>Can drop to</th>
            </tr>
          </thead>
          <tbody>
            <tr><td>Viewer</td><td>(nothing; you already are the lowest)</td></tr>
            <tr><td>Operator</td><td>Viewer</td></tr>
            <tr><td>Manager</td><td>Operator, Viewer</td></tr>
            <tr><td>Admin</td><td>Manager, Operator, Viewer</td></tr>
            <tr><td>Root Admin</td><td>Manager, Operator, Viewer</td></tr>
          </tbody>
        </table>
        <ul style={{ marginTop: 10 }}>
          <li>Dropping is free.</li>
          <li>Raising back to your real role requires your password.</li>
          <li>The drop survives page reloads (it is keyed on the refresh-token id).</li>
          <li>It clears on Sign Out, password change, user delete, or container restart.</li>
        </ul>
      </div>

      <div className="panel">
        <h2>Per-user permission overrides (Access Control)</h2>
        <p>
          Roles set the baseline. The Root Admin can layer two extra
          lists on top of any non-Root user&apos;s baseline:
        </p>
        <ul>
          <li>
            <strong>Extra permissions</strong> add capabilities on top
            of the role&apos;s baseline. Example: grant System
            Tunables editing to one trusted Admin without promoting
            them to Root.
          </li>
          <li>
            <strong>Revoked permissions</strong> subtract capabilities
            from the role&apos;s baseline. Example: revoke &ldquo;Stop
            jobs&rdquo; from a single Manager who needs everything
            else but should not stop in-flight work.
          </li>
        </ul>
        <p>
          Resolution rule: effective set = role baseline + extras
          - revokes. Safety nets:
        </p>
        <ul>
          <li>
            A Root Admin row is immune to revokes; the resolver always
            returns the full set so an accidental edit cannot lock the
            only Root out.
          </li>
          <li>Unknown permission strings are silently ignored.</li>
          <li>Editing any of this requires a fresh elevation (see above).</li>
          <li>Database Admin rows have no per-user permissions.</li>
        </ul>
        <p>
          Surface in the UI: Account &rsaquo; Account Management
          &rsaquo; Access Control (Root Admin only).
        </p>
      </div>
    </>
  );
}


// ── Server Syncing ──────────────────────────────────────────────────────────
//
// In-depth explainer for the Server Syncing top-level tab and its
// four sub-tabs. The mental model framing (Library Mapping +
// User Mapping = state/contract; Sync Subscriptions = process;
// Sync Activity = read-only health view) is the load-bearing idea
// here. Everything else hangs off it.

type ServerSyncingHelpTab =
  | 'overview'
  | 'mapping'
  | 'users'
  | 'subscriptions'
  | 'activity'
  | 'workflows';

function ServerSyncingHelpPage() {
  const [tab, setTab] = useState<ServerSyncingHelpTab>('overview');
  const tabs: Array<{ id: ServerSyncingHelpTab; label: string }> = [
    { id: 'overview', label: 'Overview' },
    { id: 'mapping', label: 'Library Mapping' },
    { id: 'users', label: 'User Mapping' },
    { id: 'subscriptions', label: 'Sync Subscriptions' },
    { id: 'activity', label: 'Sync Activity' },
    { id: 'workflows', label: 'Common Workflows' },
  ];
  return (
    <>
      <h2 style={{ marginTop: 0 }}>Server Syncing</h2>
      <nav className="subnav" style={{ display: 'flex', gap: 4, marginBottom: 12, flexWrap: 'wrap' }}>
        {tabs.map((t) => (
          <button
            key={t.id}
            className={tab === t.id ? 'active' : ''}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </nav>
      {tab === 'overview' && <ServerSyncingOverviewSection />}
      {tab === 'mapping' && <ServerSyncingMappingSection />}
      {tab === 'users' && <ServerSyncingUsersSection />}
      {tab === 'subscriptions' && <ServerSyncingSubscriptionsSection />}
      {tab === 'activity' && <ServerSyncingActivitySection />}
      {tab === 'workflows' && <ServerSyncingWorkflowsSection />}
    </>
  );
}


function ServerSyncingOverviewSection() {
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>The mental model: contract vs process</h3>
      <p>
        The Server Syncing tab gathers four surfaces that work together
        to keep multiple media servers in agreement with each other.
        They split cleanly into two halves.
      </p>
      <p>
        <strong>State / contract surfaces</strong> declare what is
        equivalent across servers. They are saved facts. They do not
        move data on their own.
      </p>
      <ul>
        <li>
          <strong>Library Mapping</strong> says &ldquo;the library
          named &lsquo;Music&rsquo; on server A is the same content
          as the library named &lsquo;Tunes&rsquo; on server B.&rdquo;
        </li>
        <li>
          <strong>User Mapping</strong> says &ldquo;the account
          &lsquo;alice&rsquo; on server A is the same person as the
          account &lsquo;alice_w&rsquo; on server B.&rdquo;
        </li>
      </ul>
      <p>
        <strong>Process surfaces</strong> turn those equivalence
        declarations into an active reconciliation that runs over time.
      </p>
      <ul>
        <li>
          <strong>Sync Subscriptions</strong> declares an ongoing
          contract: for this server pair (or library pair), keep this
          data type in agreement on this schedule, under this conflict
          policy. A polling worker wakes on the configured interval,
          reads both sides, computes what each side should be, and
          (when not in dry-run) issues exact-target writes to the
          side that is behind.
        </li>
        <li>
          <strong>Sync Activity</strong> is the read-only health view
          of what the worker has done recently &mdash; useful for
          answering &ldquo;is sync working&rdquo; without touching
          anything.
        </li>
      </ul>
      <h4>Why split it this way</h4>
      <p>
        A mapping is a phone-book entry. A subscription is a newsletter
        subscription. You can have a phone-book entry (mapping) without
        ever subscribing to anything &mdash; the entry alone is what the
        engine uses for snapshot restore, direct transfer, and
        cross-backend safety. The newsletter (subscription) only makes
        sense once you have somewhere to send the issues.
      </p>
      <p>
        Concretely: a subscription with no underlying library mapping
        has nothing to walk. Library-pair subscriptions need their one
        pair declared. Server-pair subscriptions expand to every mapped
        library pair between the two servers at poll time &mdash; if
        no pairs exist, the worker has zero work to do and the
        subscription is silently inert. Set mappings up first; create
        subscriptions second.
      </p>
      <h4>What lives on top of each surface</h4>
      <table className="list" style={{ width: '100%' }}>
        <thead>
          <tr>
            <th>Engine path</th>
            <th>Consumes Library Mapping</th>
            <th>Consumes User Mapping</th>
            <th>Consumes Subscriptions</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>Snapshot capture</td>
            <td>no</td>
            <td>yes (per-user fan-out)</td>
            <td>no</td>
          </tr>
          <tr>
            <td>Snapshot restore</td>
            <td>yes (filters unmapped libraries before write)</td>
            <td>yes (per-user resolution)</td>
            <td>no</td>
          </tr>
          <tr>
            <td>Direct transfer</td>
            <td>yes (same library-pair filter)</td>
            <td>yes</td>
            <td>no</td>
          </tr>
          <tr>
            <td>Cross-backend Replace</td>
            <td>required (refuses to run without mapping)</td>
            <td>yes</td>
            <td>no</td>
          </tr>
          <tr>
            <td>Sync worker (poll)</td>
            <td>yes (expands server-scope subs)</td>
            <td>yes (per-user write target)</td>
            <td>yes (its job spec)</td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}


function ServerSyncingMappingSection() {
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Library Mapping</h3>
      <p>
        <strong>What it is.</strong> A saved equivalence table:
        which library on the source server is the same content as
        which library on the destination server.
      </p>
      <p>
        <strong>Why it exists.</strong> Library names diverge across
        servers. Multiple libraries on each side can share a type
        (Music, Audiobooks, and Podcasts can all be{' '}
        <code>type=artist</code> on Plex; only an operator knows which
        pairs with which). Snapshot restore and direct transfer need
        to know the mapping to avoid writing tracks into the wrong
        library &mdash; or worse, into a destination library that
        doesn&apos;t exist at all (which under Replace mode would
        wipe the wrong target).
      </p>
      <h4>The four-tier auto-matcher</h4>
      <p>
        Auto-match suggests pairings in priority order. Each tier is
        more authoritative than the next, so once a tier produces a
        confident pair, the lower tiers are not consulted for that
        source library:
      </p>
      <ol>
        <li>
          <strong>GUID overlap (content).</strong> Compares the GUID
          sets of items in each library. Two libraries that share most
          of their content are almost certainly the same library on
          two servers.
        </li>
        <li>
          <strong>Path-tail overlap.</strong> Compares the trailing
          path segments of items&apos; file paths. Useful when both
          servers point at the same NAS and have different mount
          points.
        </li>
        <li>
          <strong>Library type.</strong> Falls back to the library
          type (<code>movie</code>, <code>show</code>, <code>artist</code>)
          when GUID / path signals are missing. Only useful when each
          side has exactly one library of that type.
        </li>
        <li>
          <strong>Name fuzzy.</strong> Tie-breaker only, when nothing
          else helps. &ldquo;Music&rdquo; vs &ldquo;Tunes&rdquo; gets
          a low score here; the operator confirms manually.
        </li>
      </ol>
      <h4>Manual click-to-link flow</h4>
      <p>
        Click a source-library card on the left, then a
        destination-library card on the right &mdash; the bottom banner
        shows the staged preview. Nothing saves until you click{' '}
        <em>Confirm</em>. To &ldquo;skip&rdquo; a source library
        entirely (so the engine knows it has no destination), use the
        per-card <em>Skip</em> button. Mapped rows show with a green
        border; suggested-but-unsaved pairs show with a dashed amber
        border; the pending preview pair shows with an accent border.
      </p>
      <h4>The same-server short-circuit</h4>
      <p>
        Restoring J.TV from a snapshot of itself does not consult the
        mapping table &mdash; the engine treats source = destination
        as a special case where each library is its own equivalent.
        The mapping table only kicks in when source and destination
        are different servers.
      </p>
      <h4>The per-run override</h4>
      <p>
        For one-off restores where you want to bypass the mapping
        table entirely, there is a per-run{' '}
        <em>Ignore library mapping</em> toggle hidden behind the{' '}
        <code>reveal_ignore_library_mapping_toggle</code> tunable.
        When that tunable is on (System Tunables &rsaquo; UI &amp;
        Display), the per-run override appears on the Run Job form.
        Default off so the option doesn&apos;t bloat the everyday UI.
      </p>
    </div>
  );
}


function ServerSyncingUsersSection() {
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>User Mapping</h3>
      <p>
        <strong>What it is.</strong> A saved equivalence table at the
        user-account level: which account on server A is the same
        person as which account on server B.
      </p>
      <p>
        <strong>Why it exists.</strong> The same person can have a
        different handle on each server &mdash; &ldquo;alice&rdquo; on
        Plex, &ldquo;alice_w&rdquo; on Jellyfin, &ldquo;Alice
        Wojcik&rdquo; as a Plex display name and{' '}
        <code>alice@example.com</code> as the underlying Plex
        username. Without an explicit declaration, the engine falls
        back to username matching, which gets it wrong any time the
        handles diverge.
      </p>
      <p>
        The User Mapping table here is the same table managed under{' '}
        <em>User Management &rsaquo; Identity Links</em> (the panel
        is re-mounted under Server Syncing because identity
        declarations are conceptually cross-server, and Server Syncing
        is where the other cross-server declarations live). Edits in
        either surface are persisted to the same store and visible
        from both.
      </p>
      <h4>Resolution priority</h4>
      <p>
        Engine paths consult mappings in this order when looking up
        the destination user for a given source user:
      </p>
      <ol>
        <li>
          <strong>Explicit identity-map row</strong> &mdash; the
          declaration you make here.
        </li>
        <li>
          <strong>backend_user_id match</strong> &mdash; same
          underlying account ID across two servers (e.g. same Plex
          owner on two of your own Plex servers).
        </li>
        <li>
          <strong>Personal-token match</strong> &mdash; Plex-specific:
          two server connections sharing a personal token are the
          same operator.
        </li>
        <li>
          <strong>Case-insensitive username match</strong> &mdash;
          last-resort fallback.
        </li>
        <li>
          <strong>Skip + log</strong> &mdash; nothing matched, the
          per-user write is dropped (and logged in restoration.log /
          the worker&apos;s write log).
        </li>
      </ol>
      <p>
        With the{' '}
        <code>strict_identity_resolution</code> tunable on, step 3 is
        gated &mdash; only steps 1, 2, and 4 are used. Pick that when
        you want fall-backs to be obvious in logs.
      </p>
    </div>
  );
}


function ServerSyncingSubscriptionsSection() {
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Sync Subscriptions</h3>
      <p>
        <strong>What it is.</strong> An ongoing reconciliation
        contract between two servers. Each subscription declares:
      </p>
      <ul>
        <li>which two servers,</li>
        <li>(optionally) which library pair within those servers,</li>
        <li>which data type to reconcile,</li>
        <li>under which conflict policy,</li>
        <li>on which polling interval,</li>
        <li>in which direction (one-way or bidirectional),</li>
        <li>whether to dry-run the writes or really issue them.</li>
      </ul>
      <h4>Scope: server vs library</h4>
      <p>
        Leave the source + destination library fields blank to declare
        a <em>server-scope</em> subscription. At poll time the worker
        expands it to every mapped library pair between the two
        servers by reading the Library Mapping table. Set the library
        fields to declare a <em>library-scope</em> subscription &mdash;
        the worker walks exactly that one pair.
      </p>
      <p>
        Server-scope subscriptions are the easy default for the
        &ldquo;keep these two servers fully in sync&rdquo; case.
        Library-scope subscriptions are right when you want different
        policies per library pair (e.g. Movies on{' '}
        <em>source-of-truth</em>, but Music on <em>max</em>).
      </p>
      <h4>Sync types</h4>
      <table className="list" style={{ width: '100%' }}>
        <thead>
          <tr><th>Type</th><th>What gets reconciled</th></tr>
        </thead>
        <tbody>
          <tr><td><code>watch_counts</code></td><td>Number of plays per item per user.</td></tr>
          <tr><td><code>ratings</code></td><td>User-set numeric ratings (0-10 on Plex; 0-10 on the others).</td></tr>
          <tr><td><code>favorites</code></td><td>The IsFavorite boolean per item per user.</td></tr>
          <tr><td><code>last_watched</code></td><td>The LastPlayedDate timestamp per item per user.</td></tr>
          <tr><td><code>playlists</code></td><td>Playlist contents (auto-migrate selected playlists from source to destination; optionally auto-add new ones).</td></tr>
        </tbody>
      </table>
      <h4>Conflict policies</h4>
      <table className="list" style={{ width: '100%' }}>
        <thead>
          <tr><th>Policy</th><th>Target value</th><th>When to use</th></tr>
        </thead>
        <tbody>
          <tr>
            <td><strong>Max (safest)</strong></td>
            <td><code>max(source, dest)</code></td>
            <td>You never want to lose a play that happened on either side. Default for new subscriptions.</td>
          </tr>
          <tr>
            <td><strong>Sum</strong></td>
            <td><code>source + dest</code></td>
            <td>Different people use each server and you want the combined total reflected on both.</td>
          </tr>
          <tr>
            <td><strong>Latest wins</strong></td>
            <td>Whichever side&apos;s timestamp is newer.</td>
            <td>You actively use both servers and want the most recent edit to win.</td>
          </tr>
          <tr>
            <td><strong>Source is truth</strong></td>
            <td>The source value, always.</td>
            <td>The source server is canonical; the destination should always mirror it.</td>
          </tr>
        </tbody>
      </table>
      <h4>User scope (who gets synced)</h4>
      <p>
        Every subscription carries a <em>user scope</em> that decides
        whose data the worker touches:
      </p>
      <ul>
        <li>
          <strong>Owner only</strong> (default): only the source
          server&apos;s owner is synced. Safest default — managed
          users on either side are never read or written.
        </li>
        <li>
          <strong>All users</strong>: every user on the source is
          synced. Each source user is resolved to a destination user
          via <em>User Mapping</em> first (identity_map row) and
          case-insensitive username match second. Users with no
          destination match are logged + skipped, not silently
          dropped.
        </li>
        <li>
          <strong>Specific users</strong>: pick exactly which users
          on the source are synced. The user-multi-select sources
          from the source server&apos;s live user list, so you
          choose from real handles. Users not in the picked set
          are never touched. This is the per-user opt-out gate —
          someone who doesn&apos;t want to be synced just gets left
          out of the filter.
        </li>
      </ul>
      <p>
        Plex managed-user sync caveat: the worker has no per-user
        Plex token plumbing yet. When a write target is a managed
        user on a Plex backend, the worker records a "skip with
        reason" row in Sync Activity rather than misattributing the
        write to the owner. Jellyfin / Emby per-user sync works
        end-to-end because their UserData API is admin-token + URL
        user-id, so the worker has everything it needs.
      </p>
      <h4>Where sync activity logs</h4>
      <p>
        Sync engine activity goes to its own dedicated file —{' '}
        <code>sync.log</code> under your data directory — instead of
        the per-run <code>runtime.log</code> of any currently-running
        job. This separation is deliberate: a snapshot or restore job
        that happens to run at the same time as a sync poll cycle
        would otherwise have its <code>runtime.log</code> polluted
        with reconcile traces, playlist-merge decisions, etc. The
        dedicated logger has <em>propagate = false</em> so its
        records never bubble up to the shared{' '}
        <code>plexmigrate</code> root logger.
      </p>
      <p>
        Manual / operator-initiated playlist copies (the Playlist
        Management copy flow, restore-mode playlists) are NOT
        affected by this split — they keep writing to the active
        job&apos;s <code>runtime.log</code> as before, because they
        don&apos;t pass the sync logger into the copy orchestrator.
        Only copies initiated by the sync poll worker land in
        sync.log.
      </p>
      <p>
        View it under <em>Settings &rsaquo; Logs &rsaquo;
        Application Logs &rsaquo; Sync Activity</em>.
      </p>
      <h4>Playlist merge behaviour</h4>
      <p>
        Playlist sync is an <em>ongoing</em> reconcile, not a one-shot
        copy. Each poll cycle would otherwise create a fresh
        duplicate-named playlist on the destination. The sync worker
        therefore calls the playlist copier in <em>merge</em> mode:
        if a playlist with the same case-insensitive name already
        exists for the target user on the destination, the worker
        dedups the source items against the existing items by
        backend_item_id and appends only the missing entries via
        <code>add_to_playlist</code>. First-cycle behaviour (no
        existing playlist) is unchanged — it falls through to
        create. The manual Playlist Management copy flow is
        untouched; it still defaults to <em>create</em> mode for
        operator-initiated single copies.
      </p>
      <h4>Safety rails</h4>
      <ul>
        <li>
          <strong>Dormant by default.</strong> New subscriptions start
          with <em>enabled = false</em>. The worker ignores them until
          you click Enable.
        </li>
        <li>
          <strong>Dry-run by default.</strong> New subscriptions start
          with <em>dry_run = true</em>. Even when enabled, the worker
          logs intended writes without issuing them. Review the
          per-row write log, then click <em>Real writes</em> to flip
          dry-run off.
        </li>
        <li>
          <strong>Per-row caps.</strong> The worker caps per-pair work
          at 200 items per poll cycle so a thundering herd cannot
          burn through the destination&apos;s rate limits.
        </li>
        <li>
          <strong>Audit log.</strong> Every computed write is recorded
          in <code>sync_writes</code> regardless of dry-run, with
          before / after values, the issued flag, and any error.
        </li>
      </ul>
      <h4>Playlists</h4>
      <p>
        Playlist subscriptions have one extra control:{' '}
        <em>Auto-sync new playlists</em>. When on, any new playlist
        appearing on the source side automatically gets added to the
        subscription&apos;s selection list (with{' '}
        <code>added_by = &apos;auto&apos;</code>) and synced on the
        next poll. When off, only operator-selected playlists are
        synced; new playlists on the source are ignored until you
        pick them.
      </p>
    </div>
  );
}


function ServerSyncingActivitySection() {
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Sync Activity</h3>
      <p>
        Read-only health view of the sync engine. Three blocks:
      </p>
      <ol>
        <li>
          <strong>At-a-glance counts.</strong> Total subscriptions,
          total observations logged (every per-cycle reading of source
          + dest state is one observation; logged regardless of
          dry-run), total writes recorded (issued + intent-only
          combined).
        </li>
        <li>
          <strong>Per-subscription health.</strong> For each
          subscription: current state (active / dry-run / dormant),
          last poll, last write, last status payload from the worker.
          If a subscription&apos;s last status carries an error, this
          is where you see it.
        </li>
        <li>
          <strong>Recent writes feed.</strong> A merged feed of the
          last 20 writes from every subscription, sorted newest first.
          Toggle <em>Show only failures</em> to filter down to writes
          that errored out. Each row shows the item, user, before /
          after values, and (on failures) the error message.
        </li>
      </ol>
      <p>
        Use this tab to answer &ldquo;is sync working&rdquo; without
        opening per-subscription detail. For per-row drill-down, go to{' '}
        <em>Sync Subscriptions &rsaquo; View log</em> on the
        subscription in question.
      </p>
    </div>
  );
}


function ServerSyncingWorkflowsSection() {
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Common workflows</h3>

      <h4>1. First-time set-up between two new servers</h4>
      <ol>
        <li>Register both servers under <em>Servers</em>.</li>
        <li>
          On <em>Server Syncing &rsaquo; Library Mapping</em>, pick
          source + destination. Click <em>Auto-match</em>, then walk
          the columns and confirm or fix each pair. Use <em>Skip</em>{' '}
          on any source library you don&apos;t want to sync.
        </li>
        <li>
          On <em>Server Syncing &rsaquo; User Mapping</em>, declare
          any cross-server identity links. The engine will fall back
          to username matching when no explicit row exists, but an
          explicit row is more reliable.
        </li>
        <li>
          On <em>Server Syncing &rsaquo; Sync Subscriptions</em>,
          create one server-scope subscription for each data type you
          want kept in agreement. They start dormant + dry-run. Click
          Enable to start the worker.
        </li>
        <li>
          Watch <em>Sync Activity &rsaquo; Recent writes</em> for a
          full poll cycle. Each row shows the intended before / after
          value with status <em>intent only</em>.
        </li>
        <li>
          When the dry-run output looks right, flip{' '}
          <em>Real writes</em> on for the subscriptions you trust.
        </li>
      </ol>

      <h4>2. One-shot migration before turning on continuous sync</h4>
      <p>
        Sync subscriptions reconcile gradually over many poll cycles.
        If you have two servers that are out of agreement by a lot,
        you often want a single one-shot run to catch the destination
        up, then turn on subscriptions for steady-state agreement.
      </p>
      <ol>
        <li>
          Set up Library + User Mapping as above.
        </li>
        <li>
          Run a <em>Direct Transfer</em> or <em>Restore</em> job
          from source to destination. The engine consults the same
          mapping table for routing, so a library named differently
          on each side still ends up in the right place.
        </li>
        <li>
          Once the one-shot completes, create Sync Subscriptions for
          ongoing reconciliation.
        </li>
      </ol>

      <h4>3. Cross-backend (Plex {'<->'} Jellyfin or Emby)</h4>
      <p>
        Library Mapping is more than a routing helper here: it is a
        safety belt. Replace-mode restore against a cross-backend
        destination refuses to run without a mapping, because the
        engine cannot otherwise be sure which destination library a
        source library is supposed to overwrite. Set the mapping up
        first; the engine will then route correctly, even when the
        destination library list is shaped very differently from the
        source.
      </p>

      <h4>4. Same-server snapshot restore</h4>
      <p>
        Restoring J.TV from a snapshot of J.TV does not consult the
        mapping table &mdash; the engine treats source = destination
        as a special case. You don&apos;t need a mapping for these
        runs. The mapping table only kicks in when source and
        destination are different servers.
      </p>

      <h4>5. Operator override for a one-off run</h4>
      <p>
        If you want a one-off run that bypasses the mapping table
        entirely, the per-run <em>Ignore library mapping</em> toggle
        does that. It is hidden by default; turn on{' '}
        <code>reveal_ignore_library_mapping_toggle</code> under
        System Tunables to surface it on the Run Job form. When on,
        the engine falls back to exact-name matching between source
        and destination libraries.
      </p>
    </div>
  );
}

