// Server Syncing top-level tab.
//
// Sync-related concerns get a single navigation home as a peer of Run
// Jobs, rather than being buried under the Servers tab where the
// conceptual model is hard to find.
//
// Sub-tabs:
//   - Library Mapping: equivalence between source/dest libraries
//     (two-column click-to-link picker + auto-matcher). State /
//     contract surface - declares which library on server A is the
//     same content as which library on server B.
//   - User Mapping: equivalence between source/dest user accounts.
//     Re-mounts UserMappingPanel so cross-server identity declarations
//     live in the same navigation home as cross-server library
//     declarations.
//   - Sync Subscriptions: which (library or server) pairs sync which
//     types of data under which conflict policies. Process surface -
//     declares the ongoing reconciliation a polling worker runs on
//     a schedule.
//   - Sync Activity: read-only health surface - what the worker is
//     doing right now, recent writes per subscription, recent
//     failures.

import { useState } from 'react';
import { api } from '../api';
import type { ServerView } from '../api';
import { useResourceQuery } from '../hooks/useResourceQuery';
import { LibraryMappingTab } from './LibraryMappingTab';
import { SyncSubscriptionsTab } from './SyncSubscriptionsTab';
import { UserMappingNestedView } from './UserMappingNestedView';
import { SyncActivityTab } from './SyncActivityTab';

type Tab = 'mapping' | 'users' | 'subscriptions' | 'activity';

export function ServerSyncingPage() {
  const [tab, setTab] = useState<Tab>('mapping');
  // Server list is shared between sub-tabs (all four need it for
  // dropdowns + display labels). Refetched on mount and on every
  // sub-tab switch so a newly-registered server shows up without a
  // hard reload.
  const { data: servers, error } = useResourceQuery<ServerView[]>(
    () => api.listServers(),
    [tab],
    [],
  );

  return (
    <>
      {error && <div className="banner error" style={{ marginBottom: 8 }}>Could not load servers: {error}</div>}

      <div className="panel" style={{ marginBottom: 12 }}>
        <h2 style={{ marginTop: 0 }}>Server Syncing</h2>
        <p className="help" style={{ marginBottom: 0, fontSize: 13 }}>
          One home for every cross-server declaration the engine needs.{' '}
          <strong>Library Mapping</strong> and <strong>User Mapping</strong>{' '}
          are state surfaces — they declare what is equivalent across
          servers. <strong>Sync Subscriptions</strong> is the process
          surface — it turns those equivalence declarations into an
          ongoing reconciliation a polling worker runs on a schedule.{' '}
          <strong>Sync Activity</strong> is the read-only health view —
          what the worker has done recently. See the{' '}
          <em>Help &rsaquo; Server Syncing</em> tab for the full
          contract-vs-process mental model.
        </p>
      </div>

      <nav className="subnav" style={{ display: 'flex', gap: 4, marginBottom: 12 }}>
        <button
          className={tab === 'mapping' ? 'active' : ''}
          onClick={() => setTab('mapping')}
          title="Declare which source-server libraries are equivalent to which destination-server libraries. Drives restore, transfer, and sync routing."
        >
          Library Mapping
        </button>
        <button
          className={tab === 'users' ? 'active' : ''}
          onClick={() => setTab('users')}
          title="Declare which user account on one server is the same person as which account on another server. Drives per-user fan-out and watch-state routing across servers."
        >
          User Mapping
        </button>
        <button
          className={tab === 'subscriptions' ? 'active' : ''}
          onClick={() => setTab('subscriptions')}
          title="Define which pairs sync which data types (watch counts, ratings, playlists, etc.) under which conflict policies."
        >
          Sync Subscriptions
        </button>
        <button
          className={tab === 'activity' ? 'active' : ''}
          onClick={() => setTab('activity')}
          title="Read-only view of what the sync worker has done recently across every subscription."
        >
          Sync Activity
        </button>
      </nav>

      {servers.length < 2 && (
        <div className="banner info" style={{ marginBottom: 8 }}>
          Server Syncing needs at least two registered servers. Register
          another one under the <strong>Servers</strong> tab to enable
          cross-server mapping and sync subscriptions.
        </div>
      )}

      {tab === 'mapping' && <LibraryMappingTab servers={servers} />}
      {tab === 'users' && (
        <div>
          <div className="panel" style={{ marginBottom: 12 }}>
            <h3 style={{ marginTop: 0 }}>User Mapping</h3>
            <p className="help" style={{ marginBottom: 8, fontSize: 13 }}>
              <strong>What this is:</strong> a saved equivalence table
              that tells the engine which user account on the source
              server is the same person as which account on the
              destination server. Useful when the same person has a
              different handle on each server (e.g. "alice" on Plex
              and "alice_w" on Jellyfin) or when display names diverge.
            </p>
            <p className="help" style={{ marginBottom: 8, fontSize: 13 }}>
              <strong>What relies on it:</strong> per-user fan-out
              during snapshot + restore (the engine consults this
              table before falling back to username matching), direct
              transfer of per-user watch state, the playlist copier's
              destination-user resolution, and the sync worker's
              per-user write targeting.
            </p>
            <p className="help" style={{ marginBottom: 8, fontSize: 13 }}>
              <strong>What this is not:</strong> a way to create new
              user accounts on the destination — use{' '}
              <em>User Management &rsaquo; Copy to destination</em>{' '}
              for that. This panel only records who-is-who once both
              accounts exist.
            </p>
            <p className="help" style={{ marginBottom: 0, fontSize: 13 }}>
              <strong>How this view is organised:</strong> pick a
              server in the top strip to see the users that live on
              it. Each server's tab carries a count badge of every
              identity link that involves it. Within a server, the{' '}
              <em>Overview</em> sub-tab lists every user with the
              count of links they already have; pick a specific user
              to drill into their links and add new ones.
            </p>
          </div>
          <UserMappingNestedView servers={servers} />
        </div>
      )}
      {tab === 'subscriptions' && <SyncSubscriptionsTab servers={servers} />}
      {tab === 'activity' && <SyncActivityTab servers={servers} />}
    </>
  );
}
