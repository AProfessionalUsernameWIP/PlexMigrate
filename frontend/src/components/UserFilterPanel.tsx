// Phase A (admin-management plan follow-up, 2026-05-15): user filter panel.
//
// Sits ABOVE the user-selection list on the Run Job and Schedules
// forms. End user picks filter criteria; only users matching every
// active filter populate the selection grid below. The motivating
// case: stop running jobs against managed users with no stored PIN
// or token, since the engine has no way to authenticate as them and
// the work just produces noise in the log.
//
// Filters:
//   * has_token             - user has a stored Plex auth_token
//   * has_pin               - user has a stored Plex Home PIN
//   * has_credential        - shorthand for "either token OR PIN" -
//                             the looser version of the above two
//   * present_on_all_servers - username exists on every registered
//                              server's managed_users list
//   * present_only_here      - username exists on this server but
//                              not on any other registered server
//
// All filters are AND-combined: a user must satisfy every active
// filter to populate. Empty filter set = every user populates
// (matches the pre-Phase-A behaviour).
//
// The panel asks the parent for the live user list (kind/plex_id/
// raw_name/display_name) and a map of plex_id -> {has_token, has_pin}
// for the current server, plus an optional cross-server presence map
// for the cross-server filters. The parent passes the filtered IDs
// back into its existing user picker; this component never owns the
// selection state itself.

import { useEffect, useMemo, useState } from 'react';
import type { ServerUser, ServerManagedUser } from '../api';
import { api } from '../api';
import { InfoTip } from './InfoTip';

export interface UserFilterCriteria {
  has_token: boolean;
  has_pin: boolean;
  has_credential: boolean;
  present_on_all_servers: boolean;
  present_only_here: boolean;
}

export const EMPTY_FILTER: UserFilterCriteria = {
  has_token: false,
  has_pin: false,
  has_credential: false,
  present_on_all_servers: false,
  present_only_here: false,
};

interface Props {
  // The live source-server user list (the same list the parent's
  // selection grid is rendering against).
  sourceUsers: ServerUser[];
  // The current source server's id. Used for cross-server presence
  // checks (present_on_all_servers / present_only_here) which are
  // inherently "this server vs everything else" questions.
  sourceServerId: string | null;
  // Server ids the credential filters (has_token / has_pin /
  // has_credential) look up against. AND semantics: a user must have
  // the credential on EVERY id in this list to pass the filter.
  //
  // Defaults to ``[sourceServerId]`` when omitted (snapshot / direct
  // mode — credentials must exist on the source so the engine can
  // authenticate to capture / mirror them).
  //
  // Restore mode passes the DESTINATION ids: during restore the engine
  // impersonates the user on each destination to write per-user data,
  // so the relevant credential is "is there a stored token/PIN on
  // every destination server we're restoring to?" The source side of
  // a restore is a snapshot file, not a server with credentials.
  credentialServerIds?: string[];
  // Drives the help-text wording so the panel reads correctly under
  // both "captured from <source>" (snapshot/direct) and "restored to
  // <destinations>" (restore) framings. Default 'capture'.
  mode?: 'capture' | 'restore';
  // Disabled when the parent has no source server selected yet.
  // The panel collapses to a hint in that state.
  disabled?: boolean;
  // Called whenever the filtered set of plex_ids changes. The parent
  // uses this to narrow its selection grid - users not in this set
  // are hidden, and the parent's own user_filter persists only the
  // end user's affirmative selection from the narrowed pool.
  onFilteredChange: (
    filteredPlexIds: Set<string>,
    criteria: UserFilterCriteria,
  ) => void;
}

export function UserFilterPanel({
  sourceUsers,
  sourceServerId,
  credentialServerIds,
  mode = 'capture',
  disabled,
  onFilteredChange,
}: Props) {
  const [criteria, setCriteria] = useState<UserFilterCriteria>(EMPTY_FILTER);
  // managed_users rows keyed by ``${serverId}::${username}`` across
  // every server in ``effectiveCredServerIds`` below. Credential
  // filters AND-across-servers using this map.
  const [credMU, setCredMU] = useState<Record<string, Record<string, ServerManagedUser>> | null>(null);
  // For cross-server filters: usernames present on each OTHER server.
  // Loaded lazily only when one of the cross-server criteria is active.
  const [crossPresence, setCrossPresence] = useState<{
    other_server_ids: string[];
    usernames_by_server: Record<string, Set<string>>;
  } | null>(null);
  const [loadingCross, setLoadingCross] = useState(false);

  // Resolve which server ids the credential filters should check. In
  // snapshot / direct mode this is just ``[sourceServerId]``; in
  // restore mode the caller passes destination ids.
  const effectiveCredServerIds = useMemo(() => {
    if (credentialServerIds && credentialServerIds.length > 0) {
      return credentialServerIds.filter((x): x is string => !!x);
    }
    return sourceServerId ? [sourceServerId] : [];
  }, [credentialServerIds, sourceServerId]);
  const credServerKey = effectiveCredServerIds.join('|');
  // Hide the "present on all / only here" filters when we don't have a
  // single anchor server (restore mode is a snapshot file -> N
  // destinations; "this server" doesn't map cleanly). The credential
  // filters still work because they AND across the explicit list.
  const showCrossServerFilters = mode !== 'restore' && !!sourceServerId;

  // Load managed_users for every credential-check server whenever the
  // set changes. The map is { serverId: { username: row } } so the
  // AND-across-servers filter can check each (server, user) pair.
  useEffect(() => {
    if (effectiveCredServerIds.length === 0) {
      setCredMU(null);
      return;
    }
    let cancelled = false;
    Promise.all(effectiveCredServerIds.map(async (sid) => {
      try {
        const resp = await api.listServerManagedUsers(sid, true);
        const out: Record<string, ServerManagedUser> = {};
        for (const row of resp.users || []) out[row.username] = row;
        return [sid, out] as const;
      } catch {
        return [sid, {} as Record<string, ServerManagedUser>] as const;
      }
    })).then((pairs) => {
      if (cancelled) return;
      const out: Record<string, Record<string, ServerManagedUser>> = {};
      for (const [sid, m] of pairs) out[sid] = m;
      setCredMU(out);
    });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [credServerKey]);

  // Lazily fetch other-server presence the first time a cross-server
  // filter is toggled on. Cached for the lifetime of this mount.
  useEffect(() => {
    const needsCross =
      (criteria.present_on_all_servers || criteria.present_only_here)
      && !!sourceServerId
      && crossPresence === null
      && !loadingCross;
    if (!needsCross) return;
    let cancelled = false;
    setLoadingCross(true);
    (async () => {
      try {
        const allServers = await api.listServers();
        const others = allServers
          .filter((s) => s.id !== sourceServerId)
          .map((s) => s.id);
        const usernames_by_server: Record<string, Set<string>> = {};
        await Promise.all(others.map(async (sid) => {
          try {
            const resp = await api.listServerManagedUsers(sid, true);
            usernames_by_server[sid] = new Set(
              (resp.users || []).map((u) => u.username),
            );
          } catch {
            usernames_by_server[sid] = new Set();
          }
        }));
        if (!cancelled) {
          setCrossPresence({ other_server_ids: others, usernames_by_server });
        }
      } finally {
        if (!cancelled) setLoadingCross(false);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [criteria.present_on_all_servers, criteria.present_only_here, sourceServerId]);

  // Compute the filtered set of plex_ids. AND-combine every active
  // criterion. ``sourceUsers`` carries the live user list; we map
  // back to managed_users rows by raw_name (which matches the
  // managed_users.username field). Owner rows always satisfy the
  // credential filters (the owner row uses the host token).
  //
  // Credential filters AND across every server in
  // ``effectiveCredServerIds``: in restore mode this means the user
  // must have a stored token / PIN on EVERY destination server, since
  // the engine has to authenticate as them on each one to write per-
  // user data.
  const filteredIds = useMemo(() => {
    const out = new Set<string>();
    const credServers = effectiveCredServerIds;
    for (const u of sourceUsers) {
      // Owner rows bypass the per-user credential filters because the
      // owner is authenticated via the host admin token, not via a
      // managed-user row. Filtering owner out would skip library-wide
      // data, which is rarely what the end user wants from a
      // credential-presence filter.
      const isOwner = u.kind === 'owner';

      if (!isOwner && (criteria.has_token || criteria.has_pin || criteria.has_credential)) {
        if (credServers.length === 0) {
          // No credential servers configured AND a credential filter
          // is active: we can't evaluate this user; treat as
          // filtered-out so the picker doesn't lie.
          continue;
        }
        if (!credMU) continue; // still loading
        let passes = true;
        for (const sid of credServers) {
          const mu = (credMU[sid] || {})[u.raw_name];
          if (criteria.has_token && !(mu && mu.has_token)) { passes = false; break; }
          if (criteria.has_pin && !(mu && mu.has_pin)) { passes = false; break; }
          if (criteria.has_credential && !(mu && (mu.has_token || mu.has_pin))) {
            passes = false; break;
          }
        }
        if (!passes) continue;
      }

      if (showCrossServerFilters && criteria.present_on_all_servers) {
        if (!crossPresence) {
          // Still loading; treat as filtered-out for now. The effect
          // above will fire onFilteredChange again when the data
          // resolves.
          continue;
        }
        const everywhere = crossPresence.other_server_ids.every((sid) =>
          (crossPresence.usernames_by_server[sid] || new Set()).has(u.raw_name)
        );
        if (!everywhere && crossPresence.other_server_ids.length > 0) continue;
      }
      if (showCrossServerFilters && criteria.present_only_here) {
        if (!crossPresence) continue;
        const anywhereElse = crossPresence.other_server_ids.some((sid) =>
          (crossPresence.usernames_by_server[sid] || new Set()).has(u.raw_name)
        );
        if (anywhereElse) continue;
      }
      out.add(u.plex_id);
    }
    return out;
  }, [sourceUsers, credMU, crossPresence, criteria, effectiveCredServerIds, showCrossServerFilters]);

  // Push the filtered IDs back to the parent on every change.
  useEffect(() => {
    onFilteredChange(filteredIds, criteria);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filteredIds, criteria]);

  const toggle = (key: keyof UserFilterCriteria) =>
    setCriteria((prev) => ({ ...prev, [key]: !prev[key] }));

  // Count only the criteria that are visible + evaluable in this mode.
  // Hidden cross-server filters don't contribute (avoids the
  // "filters active" header lying about a switch the end user can't see).
  const activeCount = (
    Number(criteria.has_token)
    + Number(criteria.has_pin)
    + Number(criteria.has_credential)
    + (showCrossServerFilters ? Number(criteria.present_on_all_servers) : 0)
    + (showCrossServerFilters ? Number(criteria.present_only_here) : 0)
  );
  const visibleOfTotal = `${filteredIds.size} of ${sourceUsers.length}`;
  // Restore mode framing the credential filter copy reflects: the
  // engine has to authenticate as each user on EVERY destination to
  // write per-user data. Snapshot/direct framing keeps the original
  // "authenticate to capture" wording.
  const credentialScopeNote = mode === 'restore'
    ? (effectiveCredServerIds.length > 1
        ? `across all ${effectiveCredServerIds.length} destinations`
        : 'on the destination')
    : '';

  if (disabled) {
    return (
      <fieldset className="field" style={{ borderRadius: 8, padding: '10px 12px' }}>
        <legend style={{ padding: '0 6px', fontWeight: 600 }}>User filters</legend>
        <span className="help" style={{ marginTop: 0 }}>
          {mode === 'restore'
            ? 'Pick a snapshot and at least one destination to enable filtering.'
            : 'Pick a source server first to enable filtering.'}
        </span>
      </fieldset>
    );
  }

  return (
    <fieldset className="field" style={{ borderRadius: 8, padding: '10px 12px' }}>
      <legend style={{ padding: '0 6px', fontWeight: 600 }}>
        User filters
        <InfoTip>
          <p>
            Narrow which users populate the selection grid below.
            Filters are AND-combined: a user must satisfy every
            active filter to appear. With no filters active, every
            user the source server reports populates (the
            pre-filter default).
          </p>
          <p>
            The credential filters (PIN / token) skip users we
            can't authenticate as. The cross-server filters compare
            usernames against every other registered server's
            managed-users list.
          </p>
        </InfoTip>
      </legend>
      <span className="help" style={{ marginTop: 0 }}>
        {activeCount === 0
          ? <>No filters active. All {sourceUsers.length} user(s) populate.</>
          : <>
              {activeCount} filter(s) active. <strong>{visibleOfTotal}</strong> users
              match. The selection grid below shows only the matching set.
            </>
        }
      </span>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginTop: 6 }}>
        <label className="switch">
          <input
            type="checkbox"
            checked={criteria.has_token}
            onChange={() => toggle('has_token')}
          />
          <span>Has a stored auth token{credentialScopeNote && ` ${credentialScopeNote}`}</span>
          <span className="help">Skip users we can't authenticate as.</span>
        </label>
        <label className="switch">
          <input
            type="checkbox"
            checked={criteria.has_pin}
            onChange={() => toggle('has_pin')}
          />
          <span>Has a stored Plex Home PIN{credentialScopeNote && ` ${credentialScopeNote}`}</span>
          <span className="help">Skip users we can't sign in as via PIN.</span>
        </label>
        <label className="switch">
          <input
            type="checkbox"
            checked={criteria.has_credential}
            onChange={() => toggle('has_credential')}
          />
          <span>Has either a token OR a PIN{credentialScopeNote && ` ${credentialScopeNote}`}</span>
          <span className="help">
            Looser version of the two above. Recommended default for jobs
            that need user impersonation - the engine has no way to
            authenticate as a user with neither credential.
          </span>
        </label>
        {showCrossServerFilters && (
          <>
            <label className="switch">
              <input
                type="checkbox"
                checked={criteria.present_on_all_servers}
                onChange={() => toggle('present_on_all_servers')}
              />
              <span>
                Present on every registered server
                {loadingCross && <em style={{ marginLeft: 6, fontSize: 11, color: 'var(--text-dim)' }}>(loading…)</em>}
              </span>
              <span className="help">
                Only show users whose username appears on every other registered
                server too. Useful when you want a job to operate on users that
                exist everywhere.
              </span>
            </label>
            <label className="switch">
              <input
                type="checkbox"
                checked={criteria.present_only_here}
                onChange={() => toggle('present_only_here')}
              />
              <span>
                Present only on this server
                {loadingCross && <em style={{ marginLeft: 6, fontSize: 11, color: 'var(--text-dim)' }}>(loading…)</em>}
              </span>
              <span className="help">
                Inverse of the above: only users whose username is on this server
                and NO other registered server. Useful for "tidy up the
                singleton users" cleanup jobs.
              </span>
            </label>
          </>
        )}
      </div>

      {activeCount > 0 && filteredIds.size === 0 && (
        <div
          className="banner"
          style={{
            background: 'rgba(239, 68, 68, 0.12)',
            border: '1px solid var(--bad, #ef4444)',
            color: 'var(--text)',
            marginTop: 8,
            fontSize: 12,
          }}
        >
          <strong>No users match the current filters.</strong> The save button
          is disabled while no user matches. Relax a filter or pick a different
          source server.
        </div>
      )}
    </fieldset>
  );
}
