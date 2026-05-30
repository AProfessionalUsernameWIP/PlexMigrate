// Shared data-loading hook.
//
// Replaces the load/loading/error scaffolding that nearly every panel
// re-implements: a useState for the data, a useState for the loading
// flag, a useState for the error string, an async fetch function, and
// a useEffect that calls it on mount and whenever its inputs change.
//
// Usage:
//   const { data, loading, error, reload } = useResourceQuery(
//     () => api.listThings(serverId),  // fetcher
//     [serverId],                      // deps - refetch when these change
//     [],                              // initial value for `data`
//   );
//
// `data` is always the declared type T (never null) because an
// initial value is required - panels that want "not loaded yet" to be
// distinguishable pass `null` as the initial and widen T accordingly.
// `reload()` re-runs the fetch on demand (Refresh buttons). The fetch
// is cancelled if the component unmounts or the deps change before it
// resolves, so a stale response never overwrites fresh state.

import { useCallback, useEffect, useState } from 'react';
import type { DependencyList, Dispatch, SetStateAction } from 'react';
import { errorText } from '../utils/format';

export interface ResourceQuery<T> {
  data: T;
  loading: boolean;
  error: string | null;
  reload: () => void;
  // Exposed so a panel's mutation handlers can write through the hook
  // (a "failed to delete" banner via setError; a brief optimistic
  // update via setData) instead of keeping a parallel useState.
  //
  // FECORE-08: an optimistic setData is TRANSIENT. The next fetch -
  // from reload() OR from any dep in `deps` changing - resolves to
  // setData(result) and overwrites it. A caller doing an optimistic
  // update must pair it with the real mutation plus a reload(), and
  // treat the optimistic value as a visual head-start, not durable
  // state. Data that must survive an unrelated refetch belongs in a
  // separate useState, not here.
  setData: Dispatch<SetStateAction<T>>;
  setError: Dispatch<SetStateAction<string | null>>;
}

export function useResourceQuery<T>(
  fetcher: () => Promise<T>,
  deps: DependencyList,
  initial: T,
): ResourceQuery<T> {
  const [data, setData] = useState<T>(initial);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Bumped by reload() to force the fetch effect to re-run.
  const [nonce, setNonce] = useState(0);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetcher()
      .then((result) => { if (!cancelled) setData(result); })
      .catch((e) => { if (!cancelled) setError(errorText(e)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  return { data, loading, error, reload, setData, setError };
}
