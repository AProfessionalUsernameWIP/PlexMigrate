// Helpers for working with Plex Media Server version strings.
//
// PMS reports its version as a string like "1.32.5.7349-abcdef" - a
// dotted-numeric prefix followed by an optional build-hash suffix.
// The parser below extracts the leading numeric tuple and tolerates
// any trailing junk; absent / unparseable inputs return null.

export interface PlexVersionTuple {
  major: number;
  minor: number;
  patch: number;
  build: number;
}


/**
 * Parse a Plex version string into its numeric tuple. Returns null
 * when the input is empty / null / undefined or doesn't start with at
 * least a single number.
 */
export function parsePlexVersion(raw: string | null | undefined): PlexVersionTuple | null {
  if (!raw) return null;
  const m = String(raw).match(/^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?/);
  if (!m) return null;
  return {
    major: parseInt(m[1] ?? '0', 10) || 0,
    minor: parseInt(m[2] ?? '0', 10) || 0,
    patch: parseInt(m[3] ?? '0', 10) || 0,
    build: parseInt(m[4] ?? '0', 10) || 0,
  };
}


/**
 * Compare two parsed version tuples. Returns negative if ``a < b``,
 * zero if equal, positive if ``a > b``. Used for the "≥ N.M" gates
 * the feature toggles read.
 */
export function compareVersions(a: PlexVersionTuple, b: PlexVersionTuple): number {
  if (a.major !== b.major) return a.major - b.major;
  if (a.minor !== b.minor) return a.minor - b.minor;
  if (a.patch !== b.patch) return a.patch - b.patch;
  return a.build - b.build;
}


/**
 * Returns true iff this PMS supports Fast Collection Detection.
 *
 * Feature requires Plex Media Server ≥ 1.32 - that's where the
 * ``librarySectionUserID`` attribute on collection objects landed.
 * Older servers don't expose it and the engine would auto-fall-back
 * to the slower rating-key dedup; we explicitly gate the toggle off
 * in the UI for clarity rather than letting the end user enable a
 * feature that silently degrades.
 *
 * **Unknown version is treated as unsupported.** A row added before
 * the plex_version field landed (or one that hasn't been refreshed
 * since this code shipped) returns false here. End users get a
 * "refresh this server to enable" hint in the toggle's tooltip.
 */
export function serverSupportsFastCollections(version: string | null | undefined): boolean {
  const v = parsePlexVersion(version);
  if (v === null) return false;
  const min: PlexVersionTuple = { major: 1, minor: 32, patch: 0, build: 0 };
  return compareVersions(v, min) >= 0;
}
