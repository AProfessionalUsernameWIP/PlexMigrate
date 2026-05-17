// Account ▸ Account Management ▸ Access Control
//
// Root-admin-only page for granting / revoking individual permissions
// on a per-user basis. The grant layer is additive AND removable:
//
//   effective = role_baseline ∪ extra \ revoked
//
// with the one safety carve-out that root_admin is immune to revokes
// (the backend resolver always returns the full permission set for
// that role, regardless of what's saved). The UI greys out revoke
// toggles for a root_admin row to make that contract visible.
//
// UX shape:
//   * Left rail: user picker (every non-db_admin row). Selecting a
//     row loads its current grant/revoke state.
//   * Right pane: one row per permission with two toggles -
//     "From role baseline" and "Granted/Revoked override". Three
//     visual states per permission: baseline-on (no override),
//     baseline-on-but-revoked (red strikethrough), baseline-off-but-granted
//     (green plus). The Effective column on the right always shows the
//     final resolved state for clarity.

import { useEffect, useMemo, useState } from 'react';
import { api, ManagedUser, Permission, Role, UserPermissionsResponse } from '../api';
import { PERMISSION_LABELS } from '../contexts/AuthContext';


// State of one permission row in the table. Pure derivation from
// (baseline, extra, revoked) - kept local so the row JSX stays simple.
type PermState = 'baseline_on' | 'baseline_off' | 'granted' | 'revoked';

function permState(
  p: Permission,
  baseline: Set<Permission>,
  extra: Set<Permission>,
  revoked: Set<Permission>,
): PermState {
  const isBaseline = baseline.has(p);
  if (isBaseline && revoked.has(p)) return 'revoked';
  if (!isBaseline && extra.has(p)) return 'granted';
  return isBaseline ? 'baseline_on' : 'baseline_off';
}


export function AccessControlPanel() {
  const [users, setUsers] = useState<ManagedUser[] | null>(null);
  const [selectedUsername, setSelectedUsername] = useState<string | null>(null);
  const [data, setData] = useState<UserPermissionsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);

  // Local mirror of the grant/revoke sets so toggles work without
  // a round-trip per click. Cleared / re-seeded on user-select and
  // on successful save.
  const [extra, setExtra] = useState<Set<Permission>>(new Set());
  const [revoked, setRevoked] = useState<Set<Permission>>(new Set());

  // Initial user list load. Excludes db_admin rows (the backend filter
  // already drops them).
  useEffect(() => {
    api.listManagedUsers()
      .then((r) => {
        setUsers(r.users);
        // Auto-select the first user so the end user lands somewhere
        // useful. Skip self to discourage editing own permissions
        // (the backend allows it but it's rarely what you want).
        if (r.users.length > 0) setSelectedUsername(r.users[0].username);
      })
      .catch((e) => setError(String(e)));
  }, []);

  // Reload the selected user's permission state whenever the
  // username changes.
  useEffect(() => {
    if (!selectedUsername) return;
    setLoading(true);
    setError(null);
    setOk(null);
    api.getUserPermissions(selectedUsername)
      .then((r) => {
        setData(r);
        setExtra(new Set(r.extra));
        setRevoked(new Set(r.revoked));
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [selectedUsername]);

  const baselineSet = useMemo(
    () => new Set<Permission>(data?.baseline ?? []),
    [data],
  );
  const allPermissions = data?.all_permissions ?? [];
  const isRootAdmin = data?.role === 'root_admin';

  // Live recomputation of the effective set as the end user toggles
  // - no need to wait for save to see what it'll look like.
  const effectivePreview = useMemo(() => {
    if (!data) return new Set<Permission>();
    if (isRootAdmin) return new Set(data.all_permissions);
    const out = new Set<Permission>();
    for (const p of data.baseline) {
      if (!revoked.has(p)) out.add(p);
    }
    for (const p of extra) {
      out.add(p);
    }
    return out;
  }, [data, extra, revoked, isRootAdmin]);

  // Toggling a permission cycles through the legal states for it.
  // For a BASELINE permission: on (no override) → revoked → on.
  // For a NON-BASELINE permission: off (no override) → granted → off.
  // Root admin's revoke toggles are no-ops (the backend would reject
  // them anyway).
  const toggle = (p: Permission) => {
    setOk(null);
    if (baselineSet.has(p)) {
      // Baseline-on: revoking removes it from the effective set.
      // Toggle revoke state for this permission.
      setRevoked((prev) => {
        const next = new Set(prev);
        if (next.has(p)) next.delete(p);
        else if (!isRootAdmin) next.add(p);  // root admin can't be revoked from
        return next;
      });
      // A baseline perm should never appear in extra; clear it
      // defensively in case prior state was inconsistent.
      setExtra((prev) => {
        if (!prev.has(p)) return prev;
        const next = new Set(prev);
        next.delete(p);
        return next;
      });
    } else {
      // Baseline-off: granting adds it to the effective set.
      setExtra((prev) => {
        const next = new Set(prev);
        if (next.has(p)) next.delete(p);
        else next.add(p);
        return next;
      });
      // Same defensive cleanup on revoked.
      setRevoked((prev) => {
        if (!prev.has(p)) return prev;
        const next = new Set(prev);
        next.delete(p);
        return next;
      });
    }
  };

  const hasChanges = useMemo(() => {
    if (!data) return false;
    const oldExtra = new Set(data.extra);
    const oldRevoked = new Set(data.revoked);
    if (oldExtra.size !== extra.size || oldRevoked.size !== revoked.size) return true;
    for (const p of extra) if (!oldExtra.has(p)) return true;
    for (const p of revoked) if (!oldRevoked.has(p)) return true;
    return false;
  }, [data, extra, revoked]);

  const reset = () => {
    if (!data) return;
    setExtra(new Set(data.extra));
    setRevoked(new Set(data.revoked));
    setOk(null);
  };

  const save = async () => {
    if (!data || !selectedUsername) return;
    setSaving(true);
    setError(null);
    setOk(null);
    try {
      const result = await api.setUserPermissions(selectedUsername, {
        extra: Array.from(extra),
        revoked: Array.from(revoked),
      });
      setData(result);
      setExtra(new Set(result.extra));
      setRevoked(new Set(result.revoked));
      setOk(`Saved - ${result.effective.length} effective permission(s).`);
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  };

  if (users === null) {
    return <div className="panel"><div className="empty">Loading user list…</div></div>;
  }
  if (users.length === 0) {
    return (
      <div className="panel">
        <h2>Access Control</h2>
        <div className="empty">No managed users yet. Create one from the User Accounts page first.</div>
      </div>
    );
  }

  return (
    <>
      {error && <div className="banner error">{error}</div>}
      {ok && <div className="banner good">{ok}</div>}

      <div className="banner info" style={{ fontSize: 13 }}>
        <strong>Root admin only.</strong> Grant or revoke individual permissions on top
        of each user's role. Saves are immediate - the affected user's next request
        picks up the new permission set; no re-login required.
        Root admin is immune to revokes (always full permissions) so the recovery
        path can't be locked out.
      </div>

      <div className="panel" style={{ padding: 0, overflow: 'hidden' }}>
        <div style={{ display: 'grid', gridTemplateColumns: '240px 1fr', minHeight: 480 }}>
          {/* ── User picker rail ─ */}
          <div style={{ borderRight: '1px solid var(--border, #2a3146)', padding: 0 }}>
            <div style={{
              padding: '10px 12px',
              borderBottom: '1px solid var(--border, #2a3146)',
              background: 'var(--panel-alt, #1b2233)',
            }}>
              <strong>Users</strong>
              <div style={{ fontSize: 11, color: 'var(--text-dim)' }}>{users.length} account(s)</div>
            </div>
            <div style={{ maxHeight: 600, overflowY: 'auto' }}>
              {users.map((u) => (
                <button
                  key={u.username}
                  type="button"
                  onClick={() => setSelectedUsername(u.username)}
                  style={{
                    display: 'block',
                    width: '100%',
                    textAlign: 'left',
                    padding: '10px 12px',
                    border: 'none',
                    borderBottom: '1px solid var(--border, #2a3146)',
                    background: selectedUsername === u.username
                      ? 'var(--panel-hi, #243049)'
                      : 'transparent',
                    color: 'inherit',
                    cursor: 'pointer',
                    font: 'inherit',
                  }}
                >
                  <div style={{ fontWeight: 600 }}>
                    {u.display_name || u.username}
                  </div>
                  <div style={{ fontSize: 11, color: 'var(--text-dim)' }}>
                    {u.username} · <span className={`tag ${u.role === 'root_admin' ? 'capturing' : 'phase'}`}>{u.role}</span>
                  </div>
                </button>
              ))}
            </div>
          </div>

          {/* ── Per-permission editor ─ */}
          <div style={{ padding: 16 }}>
            {loading || !data ? (
              <div className="empty">Loading permissions…</div>
            ) : (
              <>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 12 }}>
                  <div>
                    <h3 style={{ margin: 0 }}>
                      {data.username} <span className={`tag ${isRootAdmin ? 'capturing' : 'phase'}`}>{data.role}</span>
                    </h3>
                    <div style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 2 }}>
                      Effective: {effectivePreview.size} of {allPermissions.length} permission(s)
                      {isRootAdmin && (
                        <span style={{ marginLeft: 8, color: 'var(--warn, #d97706)' }}>
                          (root admin - revokes ignored)
                        </span>
                      )}
                    </div>
                  </div>
                </div>

                <table className="list" style={{ width: '100%' }}>
                  <thead>
                    <tr>
                      <th style={{ width: '45%' }}>Permission</th>
                      <th style={{ width: '15%' }}>Baseline</th>
                      <th style={{ width: '20%' }}>Override</th>
                      <th style={{ width: '20%' }}>Effective</th>
                    </tr>
                  </thead>
                  <tbody>
                    {allPermissions.map((p) => {
                      const st = permState(p, baselineSet, extra, revoked);
                      const isEffective = effectivePreview.has(p);
                      return (
                        <tr
                          key={p}
                          onClick={() => toggle(p)}
                          style={{ cursor: 'pointer' }}
                        >
                          <td>
                            <div style={{ fontWeight: 600 }}>{p}</div>
                            <div style={{ fontSize: 11, color: 'var(--text-dim)' }}>
                              {PERMISSION_LABELS[p]}
                            </div>
                          </td>
                          <td>
                            {baselineSet.has(p) ? (
                              <span className="tag done">on</span>
                            ) : (
                              <span className="tag skipped">off</span>
                            )}
                          </td>
                          <td>
                            {st === 'granted' && (
                              <span className="tag" style={{
                                color: '#3fb950', background: '#0e2014', border: '1px solid #3fb95055',
                              }}>+ granted</span>
                            )}
                            {st === 'revoked' && (
                              <span className="tag" style={{
                                color: '#f87171', background: '#2a1414', border: '1px solid #f8717155',
                                opacity: isRootAdmin ? 0.5 : 1,
                              }}>− revoked{isRootAdmin ? ' (ignored)' : ''}</span>
                            )}
                            {(st === 'baseline_on' || st === 'baseline_off') && (
                              <span style={{ fontSize: 11, color: 'var(--text-dim)' }}>(none)</span>
                            )}
                          </td>
                          <td>
                            {isEffective ? (
                              <span className="tag done">on</span>
                            ) : (
                              <span className="tag skipped">off</span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>

                <div className="row-buttons" style={{ marginTop: 16 }}>
                  <button onClick={reset} disabled={!hasChanges || saving}>Reset</button>
                  <button className="primary" onClick={() => void save()} disabled={!hasChanges || saving}>
                    {saving ? 'Saving…' : 'Save permissions'}
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      </div>
    </>
  );
}

// CSS classes referenced above (.tag.done / .tag.skipped / .tag.phase /
// .tag.capturing) already live in styles.css; no new CSS needed.
