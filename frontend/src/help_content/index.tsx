// Central registry of help topics. Each entry has:
//   * a stable ID used by <InfoTip topicId={...} />
//   * a short label for the in-form abbreviation
//   * a body rendered both inside the popover AND on the Help tab's
//     topic-by-id view
//
// The pattern is: keep the in-form label SHORT (a noun phrase, no full
// sentences), and put the full explanation in the body here. The same
// body shows up under the Help tab so an end user who has tooltips
// disabled can still find every piece of guidance in one place.

import type { ReactNode } from 'react';

export interface HelpTopic {
  id: string;
  title: string;
  shortLabel: string;
  body: ReactNode;
  // Category groups topics together on the Help tab.
  // Examples: "jobs", "settings", "tunables", "security", "servers".
  category: string;
}

const TOPICS: HelpTopic[] = [
  {
    id: 'worker-threads',
    title: 'Worker threads',
    shortLabel: 'How many parallel workers to fire at Plex',
    category: 'jobs',
    body: (
      <>
        <p>
          The number of parallel threads the engine fires HTTP requests
          with against Plex during a snapshot or restore. Each thread
          holds one TCP socket open, so 16 workers means 16 in-flight
          Plex requests at once.
        </p>
        <p>
          <strong>Higher = faster, up to a point.</strong> Past the point
          Plex can answer, additional workers just queue waiting for a
          slot. If you see <code>Failed</code> climb on the dashboard,
          Plex is overwhelmed and you should drop workers back down.
        </p>
        <p>
          Sensible defaults: <strong>16 to 32 on a LAN</strong>,{' '}
          <strong>8 on a remote / WAN link</strong>,{' '}
          <strong>half the default on a direct server-to-server transfer</strong>{' '}
          since both Plex servers feel the load.
        </p>
      </>
    ),
  },
  {
    id: 'tooltips-enabled',
    title: 'Show tooltips throughout the UI',
    shortLabel: 'Show the (?) help icons next to form fields',
    category: 'tunables',
    body: (
      <>
        <p>
          When enabled (default), the small <strong>(?)</strong> icon
          next to many form fields opens a popover with the longer
          explanation. When disabled, the icons disappear and the
          full explanations live only on the Help tab.
        </p>
        <p>
          Disable this if you find the icons noisy or distracting.
          Every topic the icons would have shown is still available
          on the Help tab, grouped by category.
        </p>
      </>
    ),
  },
  {
    id: 'auto-rotate-tokens-on-refresh',
    title: 'Auto-rotate user tokens when refreshing a server',
    shortLabel: 'Refresh also overwrites existing user tokens',
    category: 'security',
    body: (
      <>
        <p>
          When disabled (default), the Refresh button on the Servers
          tab only captures NEW user tokens; existing stored tokens
          are never overwritten. Token rotation is driven by the
          dedicated per-user "Rotate token" button.
        </p>
        <p>
          When enabled, Refresh additionally overwrites any stored
          token whose Plex-side value has changed. Useful if your
          managed users frequently sign out and back in (which
          rotates their tokens on Plex's side). Has a per-user cost
          (one extra Plex auth call per managed user); leave off
          unless you actually need rotation.
        </p>
      </>
    ),
  },
  {
    id: 'pin-migration-username-fallback',
    title: 'Allow username-string fallback for PIN migration',
    shortLabel: 'Match PINs across servers by username if Plex ID is missing',
    category: 'security',
    body: (
      <>
        <p>
          Default off: PIN migration only suggests transferring a PIN
          between servers when both rows match by Plex user ID. This
          protects against the rare case of unrelated Plex.tv
          accounts each having a managed user named "alice".
        </p>
        <p>
          Enable this only if your managed users predate Plex's
          user-ID surface and Hestia-MediaManager doesn't have a Plex user ID
          captured for them yet. Once enabled, PIN migration will also
          consider plain username matches.
        </p>
      </>
    ),
  },
  {
    id: 'root-elevation',
    title: 'Sudo-style root elevation',
    shortLabel: 'Privileged actions require re-entering your password',
    category: 'security',
    body: (
      <>
        <p>
          Root-level actions like creating user accounts, granting
          root, or migrating PINs require a fresh password
          confirmation. Re-enter your own password in the elevation
          modal that appears, and the elevation is cached for ten
          minutes; further root actions during that window don't
          prompt again.
        </p>
        <p>
          You can drop elevation manually before it expires from your
          account dropdown. Elevation also clears on logout and
          container restart. This is the same model as
          <code> sudo </code>on Linux: log in as yourself, elevate when
          you need to, and elevation is short-lived by design.
        </p>
      </>
    ),
  },
  // ── Run Defaults topics ───────────────────────────────────────────────────
  {
    id: 'output-dir',
    title: 'Default output directory',
    shortLabel: 'Where snapshot files land by default',
    category: 'settings',
    body: (
      <>
        <p>
          Where new snapshot <code>.db</code> files are written by default.
          Per-job overrides on the Run Job form take precedence.
        </p>
        <p>
          The path must be visible inside the container. Use a path
          like <code>./snapshots</code> (which maps through Docker's
          bind mount to the host) or <code>/app/nas_exports</code> if
          you've added your own bind mount in <code>docker-compose.yml</code>.
          Windows host paths like <code>Y:\Plex</code> are rejected because
          they don't exist inside the container.
        </p>
      </>
    ),
  },
  {
    id: 'log-dir',
    title: 'Default log directory',
    shortLabel: 'Where per-run log directories are created',
    category: 'settings',
    body: (
      <>
        <p>
          Where the engine writes the per-run log directory (one folder
          per snapshot or restore, containing <code>runtime.log</code>,
          <code>errors.log</code>, per-library success/fail logs, and
          a generated <code>troubleshoot.log</code> when failures occur).
        </p>
        <p>
          Same container-visibility constraint as the output directory.
        </p>
      </>
    ),
  },
  {
    id: 'strict-match',
    title: 'Strict match',
    shortLabel: 'Require exactly one fuzzy title match',
    category: 'jobs',
    body: (
      <>
        <p>
          When enabled (default), the four-tier resolver only accepts a
          fuzzy title match when it returns exactly one candidate. If
          two or more items share a fuzzy-similar title, the engine
          refuses to guess and logs the item to the failure log for
          manual review.
        </p>
        <p>
          When disabled (equivalent to <code>--no-strict-match</code>),
          the engine accepts the first fuzzy match even if multiple
          candidates exist. Use only on catalogues you've audited;
          ambiguous matches can attribute watch history to the wrong
          item.
        </p>
      </>
    ),
  },
  {
    id: 'verbose-logging',
    title: 'Verbose logging',
    shortLabel: 'DEBUG-level console + run log output',
    category: 'jobs',
    body: (
      <>
        <p>
          Bumps the console and per-run log files to DEBUG level.
          Equivalent to <code>--verbose</code> on the CLI. Useful
          when diagnosing a job that's failing for non-obvious reasons.
        </p>
        <p>
          The extra detail is paid for in log volume; a verbose run
          can generate ten times the log bytes of a normal run.
          Default off.
        </p>
      </>
    ),
  },
  {
    id: 'json-sidecar',
    title: 'Pre-build JSON sidecar',
    shortLabel: 'Render the .plexexport.json next to the .db at capture time',
    category: 'jobs',
    body: (
      <>
        <p>
          Snapshots are stored as SQLite <code>.db</code> files. The
          legacy <code>.plexexport.json</code> sidecar is generated on
          demand from the <code>.db</code> when an operator downloads it
          from the Snapshots panel.
        </p>
        <p>
          When this is on, the sidecar is rendered at capture time
          instead. Use it if you have downstream tooling that watches
          for the JSON file and can't wait for a click-to-generate
          path. Costs an extra serialization pass at the end of every
          snapshot.
        </p>
      </>
    ),
  },
  {
    id: 'watch-ratings-strategy',
    title: 'Watch + ratings capture strategy',
    shortLabel: 'How the engine fetches owner-phase data',
    category: 'jobs',
    body: (
      <>
        <p>
          The owner-phase capture (watch history + ratings) can run in
          three strategies, picked here as the default:
        </p>
        <ul>
          <li>
            <strong>Smart (recommended)</strong>: the engine picks per
            library. Bulk-fetch when both watch history and ratings
            are wanted; server-side filter when only one is wanted.
          </li>
          <li>
            <strong>Force bulk</strong>: always bulk-fetch then filter
            locally. Best for rate-limited Plex servers (fewer API
            calls, larger response payloads).
          </li>
          <li>
            <strong>Force server-side</strong>: always run server-side
            filter scans. Best when wire-traffic from Plex is the
            constraint (smaller payloads, more API calls).
          </li>
        </ul>
        <p>
          Per-server overrides on the Advanced Settings tab take
          precedence over this default.
        </p>
      </>
    ),
  },
  {
    id: 'validate-after-capture',
    title: 'Validate snapshot after capture',
    shortLabel: 'Run the structural validator at the end of every snapshot',
    category: 'jobs',
    body: (
      <>
        <p>
          Runs the snapshot validator against a temp copy of the
          freshly-written <code>.db</code> right after capture finishes.
          The original file is never modified by the check.
        </p>
        <p>
          Cheap (a few hundred ms even on large captures); catches
          malformed snapshots at the source so a bad artefact never
          reaches a downstream restore. Errors abort the surrounding
          job; warnings log without aborting. Default on.
        </p>
      </>
    ),
  },
  {
    id: 'validate-before-restore',
    title: 'Validate snapshot before restore',
    shortLabel: 'Check the snapshot when a restore job is submitted',
    category: 'jobs',
    body: (
      <>
        <p>
          Runs the snapshot validator against a temp copy of the
          snapshot <code>.db</code> the operator selected for restore,
          before the engine starts writing to the destination.
        </p>
        <p>
          Adds a few seconds to every restore. Default off because
          most snapshots come straight from the same engine that
          captured them (already validated by Validate-after-capture).
          Turn on for belt-and-suspenders against snapshots that
          were hand-edited or moved between machines.
        </p>
      </>
    ),
  },
  {
    id: 'restore-default-mode',
    title: 'Restore default mode',
    shortLabel: 'Merge (additive) vs Replace (destructive)',
    category: 'jobs',
    body: (
      <>
        <p>
          Default restore mode for restore + direct-transfer jobs. Per-job
          overrides in the Run Job form and per-server overrides on the
          Advanced Settings tab take precedence; this is the bottom-of-
          chain fallback.
        </p>
        <p>
          <strong>Merge</strong> is additive: watch counts take the
          higher value, ratings already set are left alone, playlists and
          collections gain missing members but never lose any. Idempotent.
        </p>
        <p>
          <strong>Replace</strong> is destructive: makes the destination
          match the snapshot exactly. Watch counts get re-scrobbled to
          the snapshot value, ratings overwritten, playlists and
          collections recreated. Requires typing <code>REPLACE</code> in
          the Run Job form before submitting. A pre-replace safety
          snapshot is captured automatically as a rollback path.
        </p>
      </>
    ),
  },
  {
    id: 'restore-library-workers',
    title: 'Restore library concurrency',
    shortLabel: 'How many libraries process at once during restore',
    category: 'jobs',
    body: (
      <>
        <p>
          How many libraries the file-mediated restore processes
          concurrently. Default <strong>3</strong> (preserves the
          legacy hardcoded ceiling).
        </p>
        <p>
          Set to <strong>1</strong> to serialise libraries one at a
          time. Useful when the destination Plex server is heavily
          loaded or returning 429 Throttled responses during multi-
          library restores. Set higher to push more parallelism;
          watch the Failed counter on the dashboard for evidence
          you've gone past Plex's tolerance.
        </p>
      </>
    ),
  },
  {
    id: 'fanout-workers',
    title: 'Fan-out destination concurrency',
    shortLabel: 'How many destinations run at once in a fan-out job',
    category: 'jobs',
    body: (
      <>
        <p>
          How many destinations a fan-out job writes to in parallel.
        </p>
        <ul>
          <li>
            <strong>0</strong> (default): no cap. One worker per
            destination, all run at once.
          </li>
          <li>
            <strong>1</strong>: serialise destinations. Use when every
            destination shares a network bottleneck (uplink saturation,
            shared LAN) so they don't compete for bandwidth.
          </li>
          <li>
            <strong>N</strong>: cap at N concurrent destinations.
          </li>
        </ul>
      </>
    ),
  },
  {
    id: 'filepath-fallback',
    title: 'Filepath suffix fallback (Tier 2)',
    shortLabel: 'Match by trailing path components when GUID lookup misses',
    category: 'jobs',
    body: (
      <>
        <p>
          The resolver's Tier 2 fallback. When the GUID lookup (Tier 0
          and Tier 1) misses, the engine compares the last N segments
          of the source file path against destination items' paths.
          Catches items that were re-imported with the same on-disk
          layout but different metadata.
        </p>
        <p>
          Default on - safe for most catalogues because path-suffix
          matches are typically high-confidence. Disable only if you
          have legitimate same-filename collisions across libraries.
        </p>
      </>
    ),
  },
  {
    id: 'fuzzy-fallback',
    title: 'Fuzzy title fallback (Tier 3)',
    shortLabel: 'Match by fuzzy title comparison when all else fails',
    category: 'jobs',
    body: (
      <>
        <p>
          The resolver's last-resort tier. When GUID, exact-path, and
          path-suffix all miss, the engine attempts a fuzzy title
          comparison. This is the most error-prone tier: "The Office
          (US)" and "The Office (UK)" can fuzzy-match each other
          depending on threshold.
        </p>
        <p>
          Default <strong>off</strong>. Enable only when you've
          verified your catalogue tolerates it, and prefer turning on
          Strict match alongside so the engine refuses ambiguous fuzzy
          matches.
        </p>
      </>
    ),
  },
  // ── Schedule topics ───────────────────────────────────────────────────────
  {
    id: 'schedule-frequency',
    title: 'Schedule frequency',
    shortLabel: 'How often the schedule fires',
    category: 'schedules',
    body: (
      <>
        <p>
          Pick how often the schedule re-runs the snapshot:
        </p>
        <ul>
          <li><strong>Hourly</strong>: fires once per hour at the chosen minute.</li>
          <li><strong>Daily</strong>: fires once per day at the chosen wall-clock time.</li>
          <li><strong>Weekly</strong>: fires on the chosen weekday at the chosen time.</li>
        </ul>
        <p>
          The container's timezone (set via the <code>TZ</code> env var
          in <code>docker-compose.yml</code>) determines what "wall-clock
          time" means. The topbar clock displays this so the operator
          always sees the scheduler's view of time.
        </p>
      </>
    ),
  },
  {
    id: 'schedule-source-server',
    title: 'Schedule source server',
    shortLabel: 'Which registered server the schedule snapshots',
    category: 'schedules',
    body: (
      <>
        <p>
          Schedules are scoped to one registered server, by friendly name.
          The schedule fires against this server every time it runs.
        </p>
        <p>
          If you remove the server from the registry, every schedule
          pointing at it is paused with a clear log line on next fire.
          Edit each schedule to repoint at a different server, or
          delete schedules you no longer want.
        </p>
      </>
    ),
  },
  // ── Settings panel topics ────────────────────────────────────────────────
  {
    id: 'run-logging',
    title: 'Per-run log files',
    shortLabel: 'Write runtime.log / errors.log / media.log on each run',
    category: 'settings',
    body: (
      <>
        <p>
          Global on/off for the three per-run log streams:
          <code>runtime.log</code>, <code>errors.log</code>, and
          <code>media.log</code>. Default on.
        </p>
        <p>
          When off, the engine still runs and the live dashboard still
          updates - only the per-run files on disk are suppressed.
          Per-library success/fail logs and the generated
          troubleshoot.log are unaffected. Use sparingly: the run
          logs are the primary forensic record of what the engine
          did, and without them, troubleshooting after the fact is
          much harder.
        </p>
      </>
    ),
  },
  // ── Job-mode topics ──────────────────────────────────────────────────────
  {
    id: 'job-modes',
    title: 'Snapshot vs Restore vs Direct transfer',
    shortLabel: 'Which mode does what',
    category: 'jobs',
    body: (
      <>
        <p>
          Hestia-MediaManager ships three job modes:
        </p>
        <ul>
          <li>
            <strong>Snapshot</strong>: read from a Plex server, write
            a per-server snapshot <code>.db</code> file to the output
            directory. Building block for backups and the input for
            every later Restore.
          </li>
          <li>
            <strong>Restore</strong>: read a snapshot file, write its
            contents back into a Plex server. Default mode is
            additive (Merge); the opt-in Replace mode overwrites the
            destination to match the snapshot exactly.
          </li>
          <li>
            <strong>Direct transfer</strong>: read from one Plex
            server and write straight into another, no intermediate
            file. On the happy path nothing touches disk. If the
            in-memory path fails for any library, the engine falls
            back to a temporary snapshot for that library only.
          </li>
        </ul>
        <p>
          See the Help tab's <em>How to Use</em> page for a deeper
          walkthrough of each mode's tradeoffs.
        </p>
      </>
    ),
  },
  {
    id: 'source-server',
    title: 'Source server selector',
    shortLabel: 'Which Plex server this operation reads from',
    category: 'jobs',
    body: (
      <>
        <p>
          The registered Plex server this job reads from. The dropdown
          shows every server in the registry with a live status dot
          refreshed every 30 seconds (green = reachable, red = auth or
          connectivity failure, amber = unknown).
        </p>
        <p>
          Restore jobs don't need a source server - the source IS the
          snapshot file you pick below. Snapshot and Direct transfer
          require one.
        </p>
      </>
    ),
  },
  {
    id: 'destination-fanout',
    title: 'Destination server(s) and fan-out',
    shortLabel: 'One destination, or many in parallel',
    category: 'jobs',
    body: (
      <>
        <p>
          Pick one destination for the classic single-destination flow.
          Pick two or more to fan-out: one snapshot or direct-transfer
          source feeds many destinations in parallel, each with its own
          progress card on the dashboard.
        </p>
        <p>
          Per-user filtering, library selection, and the restore mode
          all apply per destination. A destination that fails takes
          only its own card down - siblings keep running, and each
          destination gets its own safety-belt rollback snapshot
          before any destructive write.
        </p>
      </>
    ),
  },
  {
    id: 'audit-log',
    title: 'DB access audit log',
    shortLabel: 'Write the db-access forensic trail',
    category: 'settings',
    body: (
      <>
        <p>
          Records every database mutation that the engine performs,
          tagged with the actor (app user), the timestamp, and the
          intent. Independent of the per-run log files: this is a
          forensic control, not a diagnostic convenience.
        </p>
        <p>
          Toggling this setting is gated behind the db_admin credential
          via a dedicated endpoint, and the transition is self-
          documenting (the last and first log lines record who
          disabled and re-enabled it). The plain settings PATCH
          ignores this field. Default on.
        </p>
      </>
    ),
  },
  // Identity-system primer. End users frequently ask "how does the
  // app keep the same person straight across two servers (e.g., my
  // Plex.tv account on Jade.TV AND Jade.Music)?" The short answer:
  // every user added to the app gets an app-generated UUID at insert
  // time and the cross-server identity_map keys off that UUID. This
  // topic explains the invariant + the three identifiers in play so
  // the dev_notes / db_schema material doesn't have to be the only
  // source.
  {
    id: 'user-identity-uuid',
    title: 'How users are identified across servers (the app_user_uuid)',
    shortLabel: 'Every user gets a stable UUID; identity_map links them across servers',
    category: 'servers',
    body: (
      <>
        <p>
          The app keeps three identifiers per user, each with a
          different lifetime + scope:
        </p>
        <table className="table" style={{ marginTop: 8, marginBottom: 8 }}>
          <thead>
            <tr>
              <th style={{ textAlign: 'left' }}>Identifier</th>
              <th style={{ textAlign: 'left' }}>What it is</th>
              <th style={{ textAlign: 'left' }}>Mutable?</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td><code>user_handle</code></td>
              <td>Backend username (Plex username, Jellyfin Name, Emby Name)</td>
              <td>Yes, when the operator renames on the backend</td>
            </tr>
            <tr>
              <td><code>backend_user_id</code></td>
              <td>Backend-assigned per-server stable id (Plex.tv numeric userID; J/E GUID)</td>
              <td>Usually stable; can rotate on some backends</td>
            </tr>
            <tr>
              <td><code>app_user_uuid</code></td>
              <td>App-generated canonical anchor — the validation handle this app keys off</td>
              <td><strong>No</strong> — immutable for the life of the row</td>
            </tr>
          </tbody>
        </table>
        <p>
          <strong>The invariant:</strong> every user added to this app — from any
          path (managed-user sync, snapshot capture, snapshot restore, the
          User Management endpoints, the per-user-token save endpoint,
          inline cross-platform user creation) — gets an{' '}
          <code>app_user_uuid</code> generated at insert time. The two
          writer helpers (<code>upsert_managed_user</code> +{' '}
          <code>get_or_create_server_user</code>) generate via{' '}
          <code>generate_unique_app_user_uuid</code> and the column has a
          partial UNIQUE index so duplicates fail loudly rather than
          silently land. Legacy rows (pre-migration v12) are filled on
          the next boot via an idempotent backfill. There is no path
          that adds a user without a UUID.
        </p>
        <p>
          <strong>Format:</strong>{' '}
          <code>&lt;Service&gt;-&lt;HostNameSlug&gt;-&lt;server_uid&gt;-&lt;userkey&gt;</code>
          {' '}— for example{' '}
          <code>Plex-JadeTV-plex_a1b2c3d4e5f67890abcdef1234567890-a3f9c2d8</code>.
          The Service segment is "Plex" / "Jellyfin" / "Emby"; HostNameSlug is
          the cosmetic per-server label (auto-refreshed on server rename so the
          slug stays human-readable); <code>server_uid</code> is the prefixed
          server identifier (also immutable); and the 8-hex{' '}
          <code>userkey</code> is randomly generated per (server, user) pair.
          What stays the same across a server rename: everything except the
          HostNameSlug. What stays the same across the user's lifetime:
          everything — userkey is generated once and never changes.
        </p>
        <p>
          <strong>Why an app-generated identifier rather than reusing the
          backend's?</strong> Each backend assigns its own user ids in
          its own ID space; the ids don't cross between backends and the
          app doesn't control them. <code>app_user_uuid</code> is the
          application's own anchor: format we choose, lifetime we control,
          present on every row regardless of backend, useable as a primary
          key in cross-server identity links.
        </p>
        <p>
          <strong>Cross-server identity:</strong> the{' '}
          <code>user_identity_map</code> table keys off two{' '}
          <code>app_user_uuid</code> values rather than (server_id,
          user_handle) tuples. That means an operator-authored mapping
          like "the Plex 'Crystal Jean' on Jade.TV is the same human as
          the Jellyfin 'crystal.jean' on a different server" survives
          renames on either side, backend_user_id rotation, and even
          one of the backends being re-registered. The auto-link helper
          fan-out additionally writes "auto_copy" rows for the
          easy-to-derive same-(service_type, backend_user_id) pairs so
          the operator doesn't have to map their own Plex.tv account
          across two of their own Plex servers by hand.
        </p>
        <p>
          <strong>Resolution chain</strong> (per snapshot restore /
          direct transfer / playlist copy): per-job operator override →{' '}
          <code>user_identity_map</code> lookup → backend_user_id direct
          match within service_type → case-insensitive username match →
          owner-role single-admin fallback → skip with actionable log.
          The <code>strict_identity_resolution</code> tunable cuts the
          chain short after step 2 so operators who want every routing
          to come from an explicit map (or operator-confirmed preflight
          resolution) can lock that in.
        </p>
      </>
    ),
  },
  // Companion topic to user-identity-uuid. Where the -uuid topic
  // explains WHAT the canonical identifier is + the invariant that
  // every user has one, this topic explains WHY we bother keeping a
  // cross-server link table and what scenarios it unlocks for the
  // operator. Built around three concrete pairings operators
  // actually hit: same-owner Plex, mixed-ownership Plex, and
  // cross-backend Plex ↔ Jellyfin/Emby. Sibling to user-identity-uuid
  // under the same 'servers' category so both surface together on
  // Help ▸ Topics ▸ Servers.
  {
    id: 'user-identity-map',
    title: 'Linking the same person across servers (user_identity_map)',
    shortLabel: 'How operator-authored cross-server links work + when to use them',
    category: 'servers',
    body: (
      <>
        <p>
          A registered server's user list is per-server: "Crystal Jean"
          on Server A is a different row from "Crystal Jean" on Server B
          even when it's the same human. The{' '}
          <code>user_identity_map</code> table is the explicit answer to
          "these two rows are the same person." Every cross-server job
          (snapshot restore, direct transfer, fan-out, Playlist Transfer)
          consults the map before falling back to username matching.
        </p>
        <p>
          <strong>How it works.</strong> The table stores ordered pairs
          of <code>app_user_uuid</code> values (the immutable per-row
          identifier from the user-identity-uuid topic). When the engine
          needs to route a source user's payload to a destination user, it
          walks a 5-step resolution chain:
        </p>
        <ol>
          <li>
            Per-job operator override (Map decision from the
            Cross-Platform Preflight modal — applies only to this one job)
          </li>
          <li>
            <code>user_identity_map</code> lookup (authoritative;
            survives renames + backend_user_id rotation)
          </li>
          <li>
            <code>backend_user_id</code> direct match within the same
            service_type (catches "same Plex.tv account on two of my own
            Plex servers" automatically)
          </li>
          <li>
            Case-insensitive username match (the legacy fallback)
          </li>
          <li>
            Owner-role single-admin fallback (when the source is an
            owner and the destination has exactly one admin)
          </li>
        </ol>
        <p>
          <strong>Why we re-keyed it onto UUIDs.</strong> An earlier
          version of this table stored
          <code>(server_id, user_handle)</code> pairs directly. That
          worked until a destination user renamed themselves, the
          backend rotated a user_id, or a server was re-registered with a
          new id — any of those silently invalidated every stored link.
          Re-keying onto two <code>app_user_uuid</code> values (v12)
          made the links immune to all three of those events: the UUID
          is generated once per row and never changes for that row's
          lifetime, so a link between two UUIDs stays valid as long as
          either underlying row exists.
        </p>
        <p>
          <strong>Why the operator should care:</strong> three concrete
          pairings the resolver chain handles cleanly because of the
          identity map.
        </p>
        <h4 style={{ marginTop: 16, marginBottom: 4 }}>
          Scenario 1: Same Plex.tv account, two Plex servers (you own both)
        </h4>
        <p>
          You run two Plex servers and you're the owner on both — e.g.,
          a 4K server in the living room and a music-only server in the
          office, both linked to the same Plex.tv account. Your Plex.tv
          numeric userID is the same on both servers (Plex.tv assigns it
          per human, not per server), and the <strong>auto-link helper</strong>{' '}
          writes a same-(service_type, backend_user_id) <code>auto_copy</code>{' '}
          row in <code>user_identity_map</code> the first time the
          managed-users sync runs against both servers. <strong>No
          operator action required.</strong> A Direct Transfer of your
          watch history from Server A → Server B routes correctly to your
          own row on B even if your display name is "Operator" on one and
          "[Username] (admin)" on the other.
        </p>
        <p>
          <strong>What the identity map saves you from:</strong> step 4
          of the resolution chain (case-insensitive username match) would
          have worked here in most cases — but ONLY when the display
          name is identical on both sides. The moment you customise the
          display name on one server but not the other, the username
          fallback silently misses and the engine falls through to step 5
          (single-admin fallback), which fires only when the destination
          has exactly one admin. On a Plex server with multiple admins
          (rare but possible), step 5 fails too and your payload gets
          dropped with a "skipped, no resolution path" log line. The
          identity-map row makes the routing deterministic regardless of
          display-name drift or admin-count quirks.
        </p>
        <h4 style={{ marginTop: 16, marginBottom: 4 }}>
          Scenario 2: Owner on Plex A, just a Plex Home user on Plex B
        </h4>
        <p>
          You run Plex Server A. A friend runs Plex Server B and has
          added you as a Plex Home user (so you can access their library
          from their Plex Home menu). On Server A you appear as <strong>owner</strong>;
          on Server B you appear as a <strong>managed</strong> user. Your
          Plex.tv userID is still the same on both — same human, same
          Plex.tv account — but the auto-link helper writes an{' '}
          <code>auto_copy</code> row whose <code>source = 'auto_copy'</code>{' '}
          column is the giveaway: the link was inferred from the shared
          backend_user_id, not authored manually.
        </p>
        <p>
          <strong>What this unlocks:</strong> when you snapshot Server A
          (where you're the owner with full library access) and restore
          to Server B (where you're a Home user), the engine resolves
          "the owner of Server A" → "the Home user on Server B who has
          the same Plex.tv account." Your watch history lands on your
          own row on Server B instead of either (a) the wrong friend's
          row (step 4 username collision if you and your friend share a
          first name) or (b) being silently dropped because step 5
          single-admin fallback doesn't fire when the source is a
          non-owner. Without the identity-map row, restoring your data
          to your friend's server would be a coin flip.
        </p>
        <h4 style={{ marginTop: 16, marginBottom: 4 }}>
          Scenario 3: Plex ↔ Jellyfin (or Plex ↔ Emby), same human
        </h4>
        <p>
          You have a Plex account on Server A AND a Jellyfin account on
          Server B (or vice versa, or Emby instead of Jellyfin). Same
          human, different backends. Plex's backend_user_id is a numeric
          Plex.tv userID; Jellyfin's is a GUID; Emby's is a different
          GUID. Step 3 of the resolver chain — backend_user_id direct
          match — is{' '}
          <em>service_type-scoped</em> precisely so a Plex.tv numeric
          userID can never accidentally collide with a Jellyfin GUID that
          coerces to the same string. That means step 3 will never
          auto-link a Plex row to a Jellyfin row, even when the row
          should be linked.
        </p>
        <p>
          <strong>This is where you have to author the link manually.</strong>{' '}
          The Cross-Platform Preflight modal (Run Job ▸ when destination
          is a different backend than source) surfaces the unresolved
          source user with three operator choices: <strong>Map</strong>{' '}
          to a specific destination user (writes a{' '}
          <code>source = 'manual'</code> row in user_identity_map at the
          end of the job, optional checkbox), <strong>Create</strong> the
          user on the destination if they don't exist yet (inline
          backend user creation via the adapter, also writes a manual
          mapping row), or <strong>Drop</strong> the user from this job
          (writes nothing; the choice applies to this job only).
        </p>
        <p>
          <strong>What this unlocks:</strong> after you've authored the
          Plex ↔ Jellyfin mapping once, every subsequent snapshot
          restore / direct transfer / Playlist Transfer between the same
          two users uses it without re-asking. A scheduled cross-backend
          job fires deterministically — no operator at fire time to ack
          ambiguities, no risk of the engine guessing at write time.
          And because the link is keyed by app_user_uuid rather than
          handle, a rename on either side (e.g., the Jellyfin user
          changes their Name) doesn't break the link.
        </p>
        <h4 style={{ marginTop: 16, marginBottom: 4 }}>
          What the identity map is worth in plain numbers
        </h4>
        <p>
          For a single-operator install with one Plex server: nothing.
          You're the owner on one server, there's no cross-server
          routing to do, and the identity map stays empty. Adding it
          costs nothing because the resolver chain short-circuits at
          step 5 (single-admin fallback) for owner-attributed work.
        </p>
        <p>
          For an operator with multiple servers OR who routinely
          transfers data with other people OR who runs cross-backend
          restores: the identity map is the difference between "the
          engine always routes my data to the right row" and "the
          engine guesses and sometimes silently drops my data on the
          floor." The transitive-closure work (v13) goes one step
          further: when you map Plex user A ↔ Jellyfin user B, and
          someone else later maps Plex user A ↔ Emby user C, the
          system automatically infers B ↔ C without you having to
          author the third edge by hand. Deleting the manual A ↔ B
          mapping cascades to remove the inferred B ↔ C edge so the
          model stays internally consistent.
        </p>
        <p>
          <strong>Where to author + inspect mappings.</strong> Two
          surfaces today: (1) the Servers ▸ User Mapping panel for
          standalone reads + writes, and (2) the Cross-Platform
          Preflight modal that opens automatically when you submit a
          cross-backend job with unresolved source users. Both write to
          the same <code>user_identity_map</code> table; the only
          difference is whether you're authoring proactively or
          reactively to a job that needs ambiguity resolved before it
          can run.
        </p>
      </>
    ),
  },
];

const TOPIC_INDEX: Record<string, HelpTopic> = Object.fromEntries(
  TOPICS.map((t) => [t.id, t]),
);

export function getHelpTopic(id: string): HelpTopic | undefined {
  return TOPIC_INDEX[id];
}

export function getAllHelpTopics(): HelpTopic[] {
  return [...TOPICS];
}

export function getHelpTopicsByCategory(): Record<string, HelpTopic[]> {
  const out: Record<string, HelpTopic[]> = {};
  for (const t of TOPICS) {
    if (!out[t.category]) out[t.category] = [];
    out[t.category].push(t);
  }
  return out;
}
