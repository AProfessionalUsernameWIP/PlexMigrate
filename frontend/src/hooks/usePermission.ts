// PR-A3 - permission hook used by every gated component.
//
// Reads from AuthContext and returns whether the **effective** role
// (which respects Switch View Mode for root_admin) has the named
// permission. ``effective`` is the operative word: when root_admin
// drops to viewer via Switch View Mode, ``usePermission('jobs.start')``
// returns false even though the underlying JWT still permits it.
//
// FOOTGUN: never use this hook's result to gate a backend API call.
// The backend trusts the JWT, not the override. Use it strictly to
// hide / disable UI affordances. For backend gating, see
// ``require_role`` in ``server/auth_router.py``.

import type { Permission } from '../api';
import { useAuthContext } from '../contexts/AuthContext';


export function usePermission(p: Permission): boolean {
  const ctx = useAuthContext();
  return ctx.effectivePermissions.includes(p);
}


/** True iff the effective role has at least ONE of the permissions. */
export function usePermissionAny(...perms: Permission[]): boolean {
  const ctx = useAuthContext();
  return perms.some((p) => ctx.effectivePermissions.includes(p));
}
