"""
Adaptive ETA / training engine - online weighted linear regression edition.

Each per-operation bucket stores the EMA-decayed sufficient statistics for
``duration = intercept + slope * items_processed`` and solves analytically
on demand. Recent observations dominate via the EMA decay; the residual
variance feeds the confidence band.

The five-dimensional bucket key was collapsed to four (size_bucket
removed) because item count is now a continuous regressor inside each
bucket rather than a coarse key dimension. The four-tier cold-start
cascade is preserved.

Backwards compatibility note: the on-disk shape changed (new columns,
table renamed to ``eta_buckets``). The pre-release Legacy Support Policy
in CLAUDE.md applies; the engine reads run_timings.db history on first
boot to repopulate the new shape, no in-app migration of old weights.
"""

from __future__ import annotations

import logging
import math
import statistics
import threading
import time as _time_mod
from dataclasses import dataclass
from typing import Any, Dict, List, NamedTuple, Optional, Tuple


_log = logging.getLogger("plexmigrate.services.eta_training")


# ── Bucket key (4-dim; size_bucket dropped) ──────────────────────────

class BucketKey(NamedTuple):
    """Four-dim key. The size axis used to live here as a coarse S/M/L/XL
    bucket; it now lives inside the regression as a continuous regressor,
    which means all libraries of the same type+strategy on a server
    pool their observations into one model."""
    server_id: str
    label: str
    library_type: str
    bulk_strategy: str


# ── WeightedLinearRegression: one bucket's online model ──────────────

@dataclass
class WeightedLinearRegression:
    """
    Online weighted linear regression for one bucket.

    Stores EMA-decayed sufficient statistics rather than retaining the
    raw observation history. Each update applies::

        S_new = (1 - alpha) * S_old + alpha * obs

    for every statistic; the analytical solve recovers slope + intercept
    + residual variance from those decayed sums.

    The first observation seeds all six sums directly (no decay), so
    cold-start behaviour matches the old AdaptiveETA: a fresh bucket
    after one update predicts exactly the observed value at the observed
    x. Subsequent updates blend per the alpha rate.

    A separate EMA track records the typical training-time ping for the
    bucket (``sum_w_ping`` + ``sum_wp``). Ping is observed independently
    of duration so a missing ping reading doesn't poison the
    duration regression; the predict path looks up the trained ping EMA
    and applies an asymmetric multiplier when the current ping differs.

    Tunable ``alpha`` defaults to 0.2 (matches the prior EMA tunable).
    Higher reacts faster to drift; lower is more stable.
    """
    alpha: float = 0.2
    sum_w: float = 0.0
    sum_wx: float = 0.0
    sum_wy: float = 0.0
    sum_wxx: float = 0.0
    sum_wxy: float = 0.0
    sum_wyy: float = 0.0
    sample_count: int = 0
    last_observed_at: float = 0.0
    # Per-bucket ping EMA. Tracked separately from the duration
    # regression so a timing entry that lacks ping doesn't bias the
    # duration model and a noisy ping reading doesn't bias the
    # regression slope. The predict path reads ``ping_ema`` to apply
    # the latency-offset multiplier when the current ping differs.
    sum_w_ping: float = 0.0
    sum_wp: float = 0.0

    def update(
        self,
        items: Optional[float],
        duration: float,
        now: float,
        *,
        ping_ms: Optional[float] = None,
    ) -> None:
        """Apply one observation. ``items`` is items_processed (None or
        negative collapse to 0.0; the model treats it as the regressor).
        ``duration`` is in seconds. Negative durations are ignored.
        ``ping_ms`` updates the bucket's training-time ping EMA when
        present and positive; absent or non-positive readings leave the
        ping track untouched."""
        if duration is None or duration <= 0.0:
            return
        x = float(items) if (items is not None and items >= 0) else 0.0
        y = float(duration)
        if self.sample_count == 0:
            self.sum_w = 1.0
            self.sum_wx = x
            self.sum_wy = y
            self.sum_wxx = x * x
            self.sum_wxy = x * y
            self.sum_wyy = y * y
        else:
            a = self.alpha
            one_minus_a = 1.0 - a
            self.sum_w   = one_minus_a * self.sum_w   + a * 1.0
            self.sum_wx  = one_minus_a * self.sum_wx  + a * x
            self.sum_wy  = one_minus_a * self.sum_wy  + a * y
            self.sum_wxx = one_minus_a * self.sum_wxx + a * x * x
            self.sum_wxy = one_minus_a * self.sum_wxy + a * x * y
            self.sum_wyy = one_minus_a * self.sum_wyy + a * y * y
        self.sample_count += 1
        self.last_observed_at = float(now)

        if ping_ms is not None and ping_ms > 0:
            p = float(ping_ms)
            if self.sum_w_ping <= 0:
                self.sum_w_ping = 1.0
                self.sum_wp = p
            else:
                a = self.alpha
                self.sum_w_ping = (1.0 - a) * self.sum_w_ping + a * 1.0
                self.sum_wp     = (1.0 - a) * self.sum_wp     + a * p

    @property
    def ping_ema(self) -> Optional[float]:
        """Training-time ping EMA for this bucket. None when no ping
        sample has been observed yet (the predict path treats absent
        ping as 'no offset to apply')."""
        if self.sum_w_ping <= 0:
            return None
        return self.sum_wp / self.sum_w_ping

    @property
    def xbar(self) -> float:
        return self.sum_wx / self.sum_w if self.sum_w > 0 else 0.0

    @property
    def ybar(self) -> float:
        return self.sum_wy / self.sum_w if self.sum_w > 0 else 0.0

    @property
    def var_x(self) -> float:
        if self.sum_w <= 0:
            return 0.0
        xb = self.xbar
        return max(0.0, self.sum_wxx / self.sum_w - xb * xb)

    @property
    def var_y(self) -> float:
        if self.sum_w <= 0:
            return 0.0
        yb = self.ybar
        return max(0.0, self.sum_wyy / self.sum_w - yb * yb)

    @property
    def cov_xy(self) -> float:
        if self.sum_w <= 0:
            return 0.0
        return self.sum_wxy / self.sum_w - self.xbar * self.ybar

    def slope_intercept(self) -> Tuple[float, float]:
        """Analytical OLS solve. When var_x is degenerate (all
        observations at the same items count, or items is always 0),
        the line collapses to slope=0, intercept=ybar - i.e. the
        old EMA behavior."""
        if self.var_x > 1e-9:
            slope = self.cov_xy / self.var_x
            intercept = self.ybar - slope * self.xbar
        else:
            slope = 0.0
            intercept = self.ybar
        return (slope, intercept)

    def predict(self, items: Optional[float]) -> Tuple[float, float]:
        """Return (point, residual_std) at the given items count.

        ``point`` clamps to >= 0 (no negative ETAs). ``residual_std`` is
        ``sqrt(var_y * (1 - r^2))`` when the regression is informative,
        else falls back to ``sqrt(var_y)`` (the spread without modeling
        the x-axis). Both default to 0 on an empty bucket."""
        if self.sum_w <= 0:
            return (0.0, 0.0)
        slope, intercept = self.slope_intercept()
        x = float(items) if (items is not None and items >= 0) else 0.0
        point = max(0.0, intercept + slope * x)
        vx = self.var_x
        vy = self.var_y
        if vx > 1e-9 and vy > 1e-9:
            cov = self.cov_xy
            r2 = (cov * cov) / (vx * vy)
            r2 = max(0.0, min(1.0, r2))
            residual_var = vy * (1.0 - r2)
        else:
            residual_var = vy
        return (point, math.sqrt(max(0.0, residual_var)))


# ── Estimate payload + display helpers ───────────────────────────────

@dataclass
class ETAEstimate:
    """Structured prediction the API + UI consume. ``point`` is the
    regression prediction at the queried items count (with the latency
    multiplier already applied); ``low``/``high`` are the confidence
    interval; ``std`` is the residual std after regression.
    ``samples`` + ``tier`` drive the display gate.

    ``tier`` is the fallback chain depth that produced the estimate
    (1 = exact bucket, 2-4 = increasingly broad aggregates, 5 =
    hardcoded default). The UI widens the displayed range as the tier
    grows. ``latency_multiplier`` is the multiplier applied; 1.0 means
    no offset (either feature disabled, current ping unavailable, or
    training-time ping data not yet collected)."""
    point: float
    low: float
    high: float
    std: float
    samples: int
    tier: int
    confidence_z: float
    bucket: Optional[BucketKey] = None
    latency_multiplier: float = 1.0


# Tier-widening multipliers for the cascade. Applied to the residual
# std at the rollup level so the displayed range honestly widens when
# the estimate came from a broader aggregate rather than the exact
# bucket.
_TIER_WIDEN = {2: 1.2, 3: 1.5, 4: 2.0, 5: 3.0}


def _widen_for_tier(tier: int) -> float:
    return _TIER_WIDEN.get(tier, 1.0)


# ── Engine concurrency model ─────────────────────────────────────────
#
# The snapshotter pipelines work along three concurrency axes:
#
#   1. Library-level: up to ``workers`` libraries process in parallel
#      via a ThreadPoolExecutor in the engine. The prediction already
#      divides the library rollup by min(workers, library_count).
#
#   2. Metric-level (owner phase): the engine's owner gather uses a
#      4-worker pool that runs snapshot_watch_history /
#      snapshot_ratings / snapshot_playlists / snapshot_collections
#      concurrently. Wall-clock per library = max(metric times), not
#      sum. Modeled here as parallelism=METRIC_PARALLELISM on each
#      metric contribution; combine_steps divides by the parallelism,
#      giving sum/4 (a close approximation to max when metrics are
#      similar).
#
#   3. User-level (managed users): the engine's per-user gather uses
#      an 8-worker pool. N managed users complete in
#      ceil(N / USER_PARALLELISM) batches rather than serially.
#
# Both metric and user parallelism caps match the snapshotter's
# hardcoded constants and are tunable in case the engine's pool sizes
# change.

_DEFAULT_METRIC_PARALLELISM = 4
_DEFAULT_USER_PARALLELISM = 8


def _resolve_concurrency_tunables() -> Tuple[float, float]:
    """Return (metric_parallelism, user_parallelism) from tunables; fall
    through to module defaults on any failure."""
    metric = float(_DEFAULT_METRIC_PARALLELISM)
    user = float(_DEFAULT_USER_PARALLELISM)
    try:
        from services import tunables
        metric = float(tunables.eta_metric_parallelism())
        user = float(tunables.eta_user_parallelism())
    except Exception:
        pass
    return max(1.0, metric), max(1.0, user)


# ── Latency offset ───────────────────────────────────────────────────
#
# Adjusts the regression's point estimate when current ping differs
# from the per-bucket training-time ping EMA. Asymmetric on purpose:
# worse latency inflates the estimate (most operations are bottlenecked
# more on server-side work than on round-trip, so the inflation is
# half-strength + capped); better latency deflates with diminishing
# return (and a floor) so a momentarily-clean ping reading doesn't
# crater the prediction.

_LATENCY_INFLATION_STRENGTH_DEFAULT = 0.5
_LATENCY_INFLATION_CAP_DEFAULT = 2.0
_LATENCY_DEFLATION_STRENGTH_DEFAULT = 0.3
_LATENCY_DEFLATION_FLOOR_DEFAULT = 0.8


def latency_multiplier(
    current_ping_ms: Optional[float],
    trained_ping_ms: Optional[float],
    *,
    inflation_strength: float = _LATENCY_INFLATION_STRENGTH_DEFAULT,
    inflation_cap: float = _LATENCY_INFLATION_CAP_DEFAULT,
    deflation_strength: float = _LATENCY_DEFLATION_STRENGTH_DEFAULT,
    deflation_floor: float = _LATENCY_DEFLATION_FLOOR_DEFAULT,
) -> float:
    """Return the post-regression multiplier to apply to a predicted
    duration based on current vs trained ping. Returns 1.0 (no offset)
    when either side is missing or non-positive.

    Shape::

        ratio = current_ping / trained_ping
        if ratio >= 1: min(1 + inflation_strength * (ratio - 1), cap)
        if ratio <  1: max(floor, 1 - deflation_strength * (1 - ratio))
    """
    if current_ping_ms is None or trained_ping_ms is None:
        return 1.0
    if current_ping_ms <= 0 or trained_ping_ms <= 0:
        return 1.0
    ratio = current_ping_ms / trained_ping_ms
    if ratio >= 1.0:
        return min(1.0 + inflation_strength * (ratio - 1.0), inflation_cap)
    return max(deflation_floor, 1.0 - deflation_strength * (1.0 - ratio))


def _resolve_latency_tunables() -> Tuple[bool, float, float, float, float]:
    """Read the four latency-offset tunables. Falls through to the
    documented defaults on any failure; never raises."""
    enabled = True
    inflation_s = _LATENCY_INFLATION_STRENGTH_DEFAULT
    inflation_c = _LATENCY_INFLATION_CAP_DEFAULT
    deflation_s = _LATENCY_DEFLATION_STRENGTH_DEFAULT
    deflation_f = _LATENCY_DEFLATION_FLOOR_DEFAULT
    try:
        from services import tunables
        enabled = bool(tunables.eta_latency_offset_enabled())
        inflation_s = float(tunables.eta_latency_inflation_strength())
        inflation_c = float(tunables.eta_latency_inflation_cap())
        deflation_s = float(tunables.eta_latency_deflation_strength())
        deflation_f = float(tunables.eta_latency_deflation_floor())
    except Exception:
        pass
    return enabled, inflation_s, inflation_c, deflation_s, deflation_f


def _resolve_strict_per_server() -> bool:
    """Read the eta_strict_per_server tunable; default True."""
    try:
        from services import tunables
        return bool(tunables.eta_strict_per_server())
    except Exception:
        return True


def estimate_from(
    bucket: WeightedLinearRegression,
    *,
    items: Optional[float] = None,
    z: float = 1.0,
    tier: int = 1,
    key: Optional[BucketKey] = None,
    tier_widen: bool = True,
    current_ping_ms: Optional[float] = None,
) -> ETAEstimate:
    """Project a :class:`WeightedLinearRegression` into an
    :class:`ETAEstimate` at the queried items count. ``items=None`` is
    legal (the regression returns intercept-only).

    When ``current_ping_ms`` is provided AND the bucket has a trained
    ping EMA, an asymmetric latency multiplier is applied to ``point``
    (and the confidence band scales linearly with it). The
    multiplier is surfaced on the returned estimate via
    ``latency_multiplier`` so the UI can annotate ETAs that were shifted
    by latency rather than learned drift."""
    point, residual_std = bucket.predict(items)
    widen = _widen_for_tier(tier) if (tier_widen and tier > 1) else 1.0
    std = residual_std * widen

    mult = 1.0
    enabled, inf_s, inf_c, def_s, def_f = _resolve_latency_tunables()
    if enabled and current_ping_ms is not None:
        mult = latency_multiplier(
            current_ping_ms, bucket.ping_ema,
            inflation_strength=inf_s,
            inflation_cap=inf_c,
            deflation_strength=def_s,
            deflation_floor=def_f,
        )
        point = point * mult
        std = std * mult

    margin = z * std
    return ETAEstimate(
        point=round(point, 2),
        low=round(max(0.0, point - margin), 2),
        high=round(point + margin, 2),
        std=round(std, 2),
        samples=bucket.sample_count,
        tier=tier,
        confidence_z=z,
        bucket=key,
        latency_multiplier=round(mult, 4),
    )


def _format_compact(seconds: float) -> str:
    """Render a duration as ``"X sec"`` (under 90s), ``"X min"``
    (90-3599s), or ``"Xh Ym"`` (>= 3600s). Picks the most readable
    unit for the magnitude so the end user never has to mentally
    convert."""
    s = max(0.0, float(seconds))
    if s < 90:
        return f"{round(s)} sec"
    if s < 3600:
        return f"{round(s / 60)} min"
    h = int(s // 3600)
    m = round((s - h * 3600) / 60)
    if m == 60:
        h += 1
        m = 0
    return f"{h}h {m}m"


def _format_range_compact(low_s: float, high_s: float) -> str:
    """Render a range. Same unit on both ends collapses to
    ``"L-H min"``; cross-unit ranges (a sub-hour low with an over-hour
    high) format each end independently."""
    if low_s < 90 and high_s < 90:
        return f"{round(low_s)}-{round(high_s)} sec"
    if low_s < 3600 and high_s < 3600:
        return f"{round(low_s / 60)}-{round(high_s / 60)} min"
    return f"{_format_compact(low_s)}-{_format_compact(high_s)}"


def format_eta(estimate: ETAEstimate, *, min_samples: int = 5) -> str:
    """Render an estimate for inline display. Gated by sample count
    AND tier so a fresh bucket or a tier-5 default doesn't surface a
    tight-but-wrong range. Unit is chosen by magnitude:

      * < 90 seconds  -> ``"~45 sec"``
      * 90s - 1 hour  -> ``"~12 min"``
      * >= 1 hour     -> ``"~1h 23m"``
    """
    p_str = _format_compact(estimate.point)
    if estimate.tier >= 5 or estimate.samples < min_samples:
        return f"~{p_str} (still learning)"
    if estimate.tier >= 4:
        return f"~{p_str} (rough estimate)"
    # No range when the formatted low and high collapse to the same
    # rendered value: that's the "tight std" case where displaying a
    # range would add noise without information.
    low_str = _format_compact(estimate.low)
    high_str = _format_compact(estimate.high)
    if low_str == high_str:
        return f"~{p_str}"
    return f"~{p_str} (typically {_format_range_compact(estimate.low, estimate.high)})"


def format_eta_minutes(estimate: ETAEstimate, *, min_samples: int = 5) -> str:
    """Whole-job ETAs delegate to :func:`format_eta`; the magnitude
    selector inside the formatter handles unit choice end-to-end. The
    function name is kept for the call sites that semantically want
    "job rollup" formatting; they get the same compact h/m/s output."""
    return format_eta(estimate, min_samples=min_samples)


# ── Whole-job rollup math ────────────────────────────────────────────

@dataclass
class _StepContribution:
    """One step's contribution to a whole-job rollup. ``parallelism``
    lets a caller mark steps that run in parallel; the rollup divides
    the contributed duration accordingly."""
    estimate: ETAEstimate
    parallelism: float = 1.0


def combine_steps(
    contributions: list, *, z: float = 1.0,
) -> ETAEstimate:
    """Aggregate per-step ETAEstimates into a whole-job ETAEstimate
    using the parallel-scheduling makespan formula:

        wall_clock = max(longest_individual, sum_of_all / pool_size)

    All contributions share a single pool whose size is the max
    ``parallelism`` across contributions (call sites pass a uniform
    parallelism value per group). The makespan is the LPT lower bound
    for a parallel job pool: when one contribution dominates, wall-
    clock is gated by that single piece of work; when contributors
    are balanced, wall-clock is approximately the amortized
    sum/pool. Picking the max of those two captures the correct
    behaviour at both extremes.

    The rolled samples is the MEDIAN of contributors' samples (a
    single cold step among many trained ones does not gate the whole
    job, but a job whose contributors are mostly cold does); the
    rollup tier is the MAX across contributors. The rolled
    latency_multiplier is the point-weighted mean of contributors'
    multipliers. Empty input returns a tier-5 zero estimate."""
    if not contributions:
        return ETAEstimate(point=0.0, low=0.0, high=0.0, std=0.0,
                           samples=0, tier=5, confidence_z=z)
    pool_size = max(1.0, max(c.parallelism for c in contributions))

    points: List[float] = []
    bare_variances: List[float] = []
    samples_list: List[int] = []
    multipliers: List[float] = []
    max_tier = 1
    for c in contributions:
        # Strip the per-step tier-widening factor before composing
        # variances; the aggregate's tier-widen is applied once on the
        # result below.
        widen = _widen_for_tier(c.estimate.tier)
        bare_std = c.estimate.std / widen
        points.append(float(c.estimate.point))
        bare_variances.append(bare_std ** 2)
        samples_list.append(int(c.estimate.samples))
        multipliers.append(float(c.estimate.latency_multiplier))
        max_tier = max(max_tier, c.estimate.tier)

    sum_point = sum(points)
    max_point = max(points)
    amortized_point = sum_point / pool_size
    total_point = max(max_point, amortized_point)

    # Variance picks the dominant term's spread: longest-individual or
    # amortized-sum. This matches the makespan choice above; if one
    # contribution is the bottleneck, its variance gates the rollup,
    # else the contributions blend.
    if max_point >= amortized_point:
        bare_variance = bare_variances[points.index(max_point)]
    else:
        bare_variance = sum(bare_variances) / (pool_size ** 2)

    aggregate_std = math.sqrt(max(0.0, bare_variance))
    final_std = aggregate_std * _widen_for_tier(max_tier)
    margin = z * final_std
    rolled_samples = int(statistics.median(samples_list)) if samples_list else 0
    # Point-weighted multiplier rollup; ignore zero-point contributors
    # to avoid biasing the average toward neutral 1.0 entries.
    weighted_mult_num = sum(m * p for m, p in zip(multipliers, points) if p > 0)
    weighted_mult_den = sum(p for p in points if p > 0)
    rolled_mult = (weighted_mult_num / weighted_mult_den) if weighted_mult_den > 0 else 1.0
    return ETAEstimate(
        point=round(total_point, 2),
        low=round(max(0.0, total_point - margin), 2),
        high=round(total_point + margin, 2),
        std=round(final_std, 2),
        samples=rolled_samples,
        tier=max_tier,
        confidence_z=z,
        latency_multiplier=round(rolled_mult, 4),
    )


# ── Hardcoded per-label defaults (tier 5 of the cascade) ─────────────
#
# Brand-new install with zero history hits this map. Encoded as
# (fixed_seconds, seconds_per_item) tuples so even the cold-start
# guess scales with library size: a 200-item Music library no longer
# inherits the same per-step time as a 50k-item Movies library.
#
# Calibrated against observed real-world run times on a typical
# instance. Once the bucket fires once at tier 1 these values are
# bypassed entirely; they only need to be sensible first guesses.

TIER5_DEFAULTS_BY_LABEL: Dict[str, Tuple[float, float]] = {
    # Recalibrated to reflect that Plex bulk-fetches don't scale
    # linearly with item count. A 80k-item library doesn't take 80x
    # the time of a 1k-item library because the API returns thousands
    # of items per HTTP query. Old rates (0.05s/item) predicted hours
    # for large libraries when reality is minutes.
    "snapshot_watch_history":  (15.0, 0.002),
    "snapshot_ratings":        (10.0, 0.001),
    "snapshot_playlists":      (10.0, 0.0),
    "snapshot_collections":    (10.0, 0.0),
    "bulk_fetch_for_filters":  (3.0, 0.0005),
    "restore_watch_history":   (15.0, 0.003),
    "restore_ratings":         (10.0, 0.002),
    "restore_playlists":       (10.0, 0.0),
    "restore_collections":     (10.0, 0.0),
    "direct_library_transfer": (10.0, 0.005),
}


def tier5_default(label: str) -> Tuple[float, float]:
    """Return ``(fixed_seconds, seconds_per_item)`` for the cold-start
    fallback. Reads the end user-tunable ``eta_tier5_defaults_seconds``
    dict first (each value may be a single number for fixed-only OR a
    [fixed, per_item] pair); falls through to the module map; falls
    through to ``(10.0, 0.0)`` as a safe floor when the label is
    unknown to both."""
    try:
        from services import tunables
        overrides = tunables.eta_tier5_defaults_seconds()
        if label in overrides:
            raw = overrides[label]
            if isinstance(raw, (list, tuple)) and len(raw) >= 2:
                return (float(raw[0]), float(raw[1]))
            if isinstance(raw, (int, float)):
                return (float(raw), 0.0)
    except Exception:
        pass
    return TIER5_DEFAULTS_BY_LABEL.get(label, (10.0, 0.0))


def tier5_predicted_seconds(label: str, items: Optional[float]) -> float:
    """Evaluate the tier-5 default at the given items count. Helper for
    consumers (and tests) that want the scalar prediction without going
    through the trainer's full cascade."""
    fixed, per_item = tier5_default(label)
    x = float(items) if (items is not None and items >= 0) else 0.0
    return max(0.0, fixed + per_item * x)


# ── ETATrainer: multi-bucket store + four-tier fallback ───────────────

class ETATrainer:
    """Multi-bucket regression store with the four-tier cold-start
    cascade.

    Lifecycle:
      * Constructed empty.
      * ``load_from_db()`` populates the in-memory dict from the
        eta_buckets table on first use.
      * ``batch_update(entries)`` is called from ``end_run`` to fold
        a run's timing entries into the bucket regressors and persist
        them back atomically. Best-effort; failures are logged and
        swallowed; the run completion path never blocks on this.
      * ``estimate(bucket_key, items=...)`` walks the 4-tier cascade
        and returns a single :class:`ETAEstimate` for that bucket at
        the given items count.
      * ``reset_server(server_id)`` clears every bucket for one server
        (D-RESET end user escape hatch).

    Thread safety: a single ``threading.RLock`` guards the in-memory
    dict. Updates take the lock for the duration of the batch; reads
    take it briefly to copy the relevant rows."""

    def __init__(self, alpha: float = 0.2, confidence_z: float = 1.0):
        self._alpha = float(alpha)
        self._confidence_z = float(confidence_z)
        self._buckets: Dict[BucketKey, WeightedLinearRegression] = {}
        self._lock = threading.RLock()
        self._loaded = False

    # ── Load / persist ────────────────────────────────────────────────

    def load_from_db(self) -> None:
        """Populate the in-memory dict from eta_buckets. Idempotent;
        a second call is a no-op once the first has succeeded. Any DB
        failure leaves the trainer empty and operational (every predict
        call falls through to tier 5)."""
        with self._lock:
            if self._loaded:
                return
            try:
                from server.run_timings_db import load_all_eta_buckets
                for row in load_all_eta_buckets():
                    key = BucketKey(
                        server_id=row["server_id"],
                        label=row["label"],
                        library_type=row["library_type"],
                        bulk_strategy=row["bulk_strategy"],
                    )
                    self._buckets[key] = WeightedLinearRegression(
                        alpha=self._alpha,
                        sum_w=float(row["sum_w"]),
                        sum_wx=float(row["sum_wx"]),
                        sum_wy=float(row["sum_wy"]),
                        sum_wxx=float(row["sum_wxx"]),
                        sum_wxy=float(row["sum_wxy"]),
                        sum_wyy=float(row["sum_wyy"]),
                        sample_count=int(row["sample_count"]),
                        last_observed_at=float(row["last_observed_at"]),
                        sum_w_ping=float(row.get("sum_w_ping", 0.0) or 0.0),
                        sum_wp=float(row.get("sum_wp", 0.0) or 0.0),
                    )
                self._loaded = True
                _log.info(
                    "eta_training: loaded %d bucket(s) from eta_buckets",
                    len(self._buckets),
                )
            except Exception:
                _log.exception(
                    "eta_training: load_from_db failed; trainer remains empty"
                )

    def batch_update(self, entries) -> int:
        """Fold one run's TimingEntry list into the bucket store and
        persist the touched buckets. Returns the number of buckets
        upserted. Never raises; failures are logged.

        ``entries`` is any iterable of objects that expose ``label``,
        ``server_id``, ``items_processed``, ``duration_seconds``, and
        an ``extra`` dict (matches ``services.run_timer.TimingEntry``).
        Library type and bulk strategy are read out of the entry's
        extra dict when present; missing dimensions collapse to the
        empty-string sentinel."""
        if not entries:
            return 0
        self.load_from_db()
        now = _time_mod.time()
        touched: Dict[BucketKey, WeightedLinearRegression] = {}
        entries_consumed = 0

        with self._lock:
            for entry in entries:
                key = self._key_from_entry(entry)
                if key is None:
                    continue
                bucket = self._buckets.get(key)
                if bucket is None:
                    bucket = WeightedLinearRegression(alpha=self._alpha)
                    self._buckets[key] = bucket
                duration = float(getattr(entry, "duration_seconds", 0.0) or 0.0)
                if duration <= 0.0:
                    continue
                items = getattr(entry, "items_processed", None)
                extra = getattr(entry, "extra", None) or {}
                ping_ms = extra.get("ping_ms_at_start")
                bucket.update(items, duration, now=now, ping_ms=ping_ms)
                touched[key] = bucket
                entries_consumed += 1

            rows = [self._bucket_to_row(k, b) for k, b in touched.items()]

        try:
            from server.run_timings_db import persist_eta_buckets
            persist_eta_buckets(rows)
        except Exception:
            _log.exception(
                "eta_training: persist_eta_buckets failed; in-memory "
                "weights remain updated, on-disk weights are stale"
            )

        # Visibility: one INFO summary line per run-end batch so an
        # end user watching the engine logs can confirm training is
        # firing. Per-bucket detail at DEBUG so a deep dive is one
        # log-level bump away.
        if touched:
            servers = sorted({k.server_id for k in touched})
            _log.info(
                "eta_training: run-end update | entries=%d | buckets_touched=%d "
                "| servers=%s",
                entries_consumed, len(touched), ",".join(servers) or "-",
            )
            if _log.isEnabledFor(logging.DEBUG):
                for k, b in touched.items():
                    _log.debug(
                        "  bucket %s/%s type=%r strategy=%r samples=%d "
                        "ema_x=%.1f ema_y=%.2f ping=%s",
                        k.server_id, k.label, k.library_type, k.bulk_strategy,
                        b.sample_count, b.xbar, b.ybar,
                        f"{b.ping_ema:.1f}ms" if b.ping_ema is not None else "n/a",
                    )
        return len(rows)

    def backfill_from_history(self, *, reset_first: bool = False) -> Dict[str, int]:
        """Warm-start the trainer by replaying every row in ``run_timings``
        through the regression update in chronological order.

        The trainer's normal hook only fires at ``end_run``, so it starts
        cold even though the ``run_timings`` table has been recording
        per-operation entries from day one. This method walks that
        history once and folds it into the bucket store so the
        predictor's tier-1 cells light up immediately.

        ``reset_first=True`` clears every bucket before replaying;
        useful when the end user wants the bucket store to exactly
        reflect ``run_timings``. Default False so a backfill on an
        already-trained install only ADDs to the existing weights.

        Returns ``{"entries_read": N, "buckets_touched": M}``."""
        self.load_from_db()
        try:
            from server.run_timings_db import iter_all_run_timings_for_backfill
            rows = iter_all_run_timings_for_backfill()
        except Exception:
            _log.exception("eta_training backfill: read failed")
            return {"entries_read": 0, "buckets_touched": 0}

        touched: Dict[BucketKey, WeightedLinearRegression] = {}
        with self._lock:
            if reset_first:
                self._buckets.clear()
            for r in rows:
                class _Row:
                    pass
                stub = _Row()
                stub.server_id = r.get("server_id", "") or ""
                stub.label = r.get("label", "") or ""
                stub.duration_seconds = float(r.get("duration_seconds") or 0.0)
                stub.items_processed = r.get("items_processed")
                stub.extra = r.get("extra", {})
                key = self._key_from_entry(stub)
                if key is None:
                    continue
                if stub.duration_seconds <= 0.0:
                    continue
                bucket = self._buckets.get(key)
                if bucket is None:
                    bucket = WeightedLinearRegression(alpha=self._alpha)
                    self._buckets[key] = bucket
                # Use the entry's started_at as the observation time so
                # EMA recency math reflects the actual run history.
                ping_ms = stub.extra.get("ping_ms_at_start") if isinstance(stub.extra, dict) else None
                bucket.update(
                    stub.items_processed,
                    stub.duration_seconds,
                    now=float(r.get("started_at") or 0.0),
                    ping_ms=ping_ms,
                )
                touched[key] = bucket

            persist_rows = [self._bucket_to_row(k, b) for k, b in touched.items()]

        try:
            from server.run_timings_db import persist_eta_buckets
            persist_eta_buckets(persist_rows)
        except Exception:
            _log.exception("eta_training backfill: persist failed")

        _log.info(
            "eta_training backfill: replayed %d entries -> %d bucket(s) "
            "(reset_first=%s)",
            len(rows), len(persist_rows), reset_first,
        )
        return {
            "entries_read": len(rows),
            "buckets_touched": len(persist_rows),
        }

    def flush_all(self, *, include_run_timings: bool = False) -> Dict[str, int]:
        """End user's 'Flush all training data' escape hatch. Clears
        every bucket in memory AND on disk. When
        ``include_run_timings`` is True the underlying run_timings
        table is also wiped so a subsequent backfill cannot repopulate
        the bucket store from history. Returns counts deleted per
        layer."""
        self.load_from_db()
        with self._lock:
            in_memory = len(self._buckets)
            self._buckets.clear()
        eta_deleted = 0
        timings_deleted = 0
        try:
            from server.run_timings_db import flush_all_eta_buckets
            eta_deleted = flush_all_eta_buckets()
        except Exception:
            _log.exception("eta_training: flush_all_eta_buckets failed")
        if include_run_timings:
            try:
                from server.run_timings_db import flush_all_run_timings
                timings_deleted = flush_all_run_timings()
            except Exception:
                _log.exception("eta_training: flush_all_run_timings failed")
        _log.info(
            "eta_training: flush_all | in_memory=%d eta_buckets=%d run_timings=%d",
            in_memory, eta_deleted, timings_deleted,
        )
        return {
            "in_memory_buckets_cleared": in_memory,
            "eta_buckets_rows_deleted": eta_deleted,
            "run_timings_rows_deleted": timings_deleted,
        }

    def reset_server(self, server_id: str) -> int:
        """Clear every bucket for one server in memory AND in the DB.
        D-RESET: the end user's "I just upgraded this server's storage;
        learned timings are wrong" button. Returns the count of buckets
        removed."""
        if not server_id:
            return 0
        self.load_from_db()
        with self._lock:
            to_remove = [k for k in self._buckets if k.server_id == server_id]
            for k in to_remove:
                del self._buckets[k]
        try:
            from server.run_timings_db import reset_eta_buckets_for_server
            reset_eta_buckets_for_server(server_id)
        except Exception:
            _log.exception(
                "eta_training: reset_eta_buckets_for_server failed for %r",
                server_id,
            )
        return len(to_remove)

    # ── Estimate (single bucket + 4-tier fallback) ───────────────────

    def estimate(
        self, key: BucketKey, *,
        items: Optional[float] = None,
        min_samples: int = 5,
        current_ping_ms: Optional[float] = None,
    ) -> ETAEstimate:
        """Walk the cascade and return an estimate for the requested
        bucket at the given items count.

        Two-pass selection:
          1. Confident pass: first tier whose aggregate has at least
             ``min_samples`` observations wins. Display surfaces a
             confident estimate ("typically L-H").
          2. Anchor pass: if no tier hit confidence, fall back to the
             most-specific tier with ANY observations (>= 1). Display
             still says "still learning" but the number reflects the
             end user's actual history instead of a hardcoded guess.

        Tier 5 (rate-based hardcoded default) only fires when there is
        truly no relevant history on any tier.

        ``current_ping_ms`` is threaded through ``estimate_from`` so
        the per-bucket trained ping EMA can power an asymmetric offset
        on the predicted duration.

        When the ``eta_strict_per_server`` tunable is true (default),
        tiers 3 and 4 are skipped at both passes - cross-server data
        never influences the prediction."""
        self.load_from_db()
        strict = _resolve_strict_per_server()
        with self._lock:
            # Build candidate aggregates per tier in priority order.
            tiers: List[Tuple[int, WeightedLinearRegression]] = []
            b1 = self._buckets.get(key)
            if b1 is not None:
                tiers.append((1, b1))
            t2 = self._aggregate_buckets(
                lambda k: k.server_id == key.server_id and k.label == key.label,
            )
            if t2 is not None:
                tiers.append((2, t2))
            if not strict:
                t3 = self._aggregate_buckets(
                    lambda k: k.label == key.label
                    and k.library_type == key.library_type,
                )
                if t3 is not None:
                    tiers.append((3, t3))
                t4 = self._aggregate_buckets(lambda k: k.label == key.label)
                if t4 is not None:
                    tiers.append((4, t4))

            # Confident pass: most-specific tier meeting min_samples.
            for tier, agg in tiers:
                if agg.sample_count >= min_samples:
                    return estimate_from(agg, items=items,
                                         z=self._confidence_z,
                                         tier=tier, key=key,
                                         current_ping_ms=current_ping_ms)

            # Anchor pass: most-specific tier with ANY observations.
            # The display gate still surfaces "still learning" because
            # the rolled samples is below min_samples; the end user
            # sees a real-data-based estimate instead of a tier-5 guess.
            for tier, agg in tiers:
                if agg.sample_count >= 1:
                    return estimate_from(agg, items=items,
                                         z=self._confidence_z,
                                         tier=tier, key=key,
                                         current_ping_ms=current_ping_ms)

        # Tier 5: rate-based hardcoded default. Only fires when no
        # relevant history exists at any tier.
        default_point = tier5_predicted_seconds(key.label, items)
        seed = WeightedLinearRegression(alpha=self._alpha)
        seed.update(items, default_point, now=0.0)
        est = estimate_from(seed, items=items,
                            z=self._confidence_z, tier=5, key=key)
        return ETAEstimate(
            point=est.point, low=est.low, high=est.high,
            std=est.std, samples=0, tier=5,
            confidence_z=est.confidence_z, bucket=key,
            latency_multiplier=1.0,
        )

    def _aggregate_buckets(self, predicate) -> Optional[WeightedLinearRegression]:
        """Build a synthetic bucket by summing the sufficient statistics
        of every bucket matching ``predicate``. Because the sums are
        decay-weighted, the union of two buckets is just the sum of
        their stats - the analytical solve recovers the right
        slope/intercept across the combined data. Ping sums combine
        the same way so cross-bucket aggregates still expose a useful
        ping EMA for the latency offset.

        Returns None when no bucket matches."""
        matching = [(k, b) for k, b in self._buckets.items() if predicate(k)]
        if not matching:
            return None
        total_samples = sum(b.sample_count for _, b in matching)
        if total_samples == 0:
            return None
        agg = WeightedLinearRegression(alpha=self._alpha)
        for _, b in matching:
            agg.sum_w      += b.sum_w
            agg.sum_wx     += b.sum_wx
            agg.sum_wy     += b.sum_wy
            agg.sum_wxx    += b.sum_wxx
            agg.sum_wxy    += b.sum_wxy
            agg.sum_wyy    += b.sum_wyy
            agg.sum_w_ping += b.sum_w_ping
            agg.sum_wp     += b.sum_wp
        agg.sample_count = total_samples
        agg.last_observed_at = max(b.last_observed_at for _, b in matching)
        return agg

    # ── Whole-job prediction ─────────────────────────────────────────

    def predict_for_job(
        self,
        *,
        mode: str,
        source_server_id: str,
        libraries: list,
        metrics_enabled: Dict[str, bool],
        user_count: int = 1,
        workers: int = 1,
        bulk_strategy: str = "smart",
        min_samples: int = 5,
        current_ping_ms: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Roll up per-step estimates into a whole-job estimate.

        ``libraries`` is a list of dicts ``{name, library_type,
        items_count}``. ``metrics_enabled`` is the four-flag dict
        ``{watch_history, ratings, playlists, collections}`` (missing
        keys default to ON; explicit False suppresses that metric's
        contribution). Returns the same response shape as before:
        top-level rollup + per_library breakdown."""
        self.load_from_db()
        metric_parallelism, user_parallelism = _resolve_concurrency_tunables()
        per_library: list = []
        contributions: list = []

        metric_labels = {
            "watch_history": "snapshot_watch_history",
            "ratings":       "snapshot_ratings",
            "playlists":     "snapshot_playlists",
            "collections":   "snapshot_collections",
        }
        if mode == "restore":
            metric_labels = {
                "watch_history": "restore_watch_history",
                "ratings":       "restore_ratings",
                "playlists":     "restore_playlists",
                "collections":   "restore_collections",
            }
        elif mode == "direct":
            metric_labels = {
                "watch_history": "snapshot_watch_history",
                "ratings":       "snapshot_ratings",
                "playlists":     "snapshot_playlists",
                "collections":   "snapshot_collections",
            }

        for lib in libraries:
            lib_name = lib.get("name", "")
            lib_type = lib.get("library_type", "") or ""
            items = lib.get("items_count")

            lib_contribs: list = []
            for metric_key, label in metric_labels.items():
                if metrics_enabled.get(metric_key, True) is False:
                    continue
                key = BucketKey(
                    server_id=source_server_id,
                    label=label,
                    library_type=lib_type,
                    bulk_strategy=bulk_strategy,
                )
                est = self.estimate(
                    key, items=items, min_samples=min_samples,
                    current_ping_ms=current_ping_ms,
                )
                # User-loop scaling: owner runs the owner-trained metric
                # once. Each batch of managed users (up to
                # user_parallelism per batch) runs that metric once
                # more. effective_user_loops = 1 (owner) + ceil(managed
                # / user_parallelism) honestly models the 8-pool fan-out
                # rather than the naive O(user_count) the model used
                # before.
                managed_users = max(0, int(user_count) - 1)
                if managed_users == 0:
                    effective_user_loops = 1.0
                else:
                    effective_user_loops = 1.0 + math.ceil(
                        managed_users / max(1.0, user_parallelism)
                    )
                est_scaled = ETAEstimate(
                    point=est.point * effective_user_loops,
                    low=est.low * effective_user_loops,
                    high=est.high * effective_user_loops,
                    std=est.std * effective_user_loops,
                    samples=est.samples,
                    tier=est.tier,
                    confidence_z=est.confidence_z,
                    bucket=est.bucket,
                    latency_multiplier=est.latency_multiplier,
                )
                lib_contribs.append(_StepContribution(est_scaled))

            if not lib_contribs:
                continue
            # Metric-level parallelism: cap at the number of metrics
            # actually contributing. A single-metric job has no parallel
            # work to exploit so its rollup is the metric's full point;
            # a four-metric job approximates max via the 4-worker pool.
            effective_metric_parallelism = max(
                1.0, min(metric_parallelism, float(len(lib_contribs))),
            )
            for c in lib_contribs:
                c.parallelism = effective_metric_parallelism
            lib_rollup = combine_steps(lib_contribs, z=self._confidence_z)
            per_library.append({
                "name": lib_name,
                "library_type": lib_type,
                "items_count": items,
                "point": lib_rollup.point,
                "low": lib_rollup.low,
                "high": lib_rollup.high,
                "std": lib_rollup.std,
                "samples": lib_rollup.samples,
                "tier": lib_rollup.tier,
                "latency_multiplier": lib_rollup.latency_multiplier,
                "display": format_eta_minutes(lib_rollup, min_samples=min_samples),
            })
            # Library-level parallelism: workers process libraries
            # concurrently up to min(workers, library_count).
            parallelism = max(1.0, min(float(workers), float(len(libraries))))
            contributions.append(
                _StepContribution(lib_rollup, parallelism=parallelism)
            )

        total = combine_steps(contributions, z=self._confidence_z)
        return {
            "mode": mode,
            "point": total.point,
            "low": total.low,
            "high": total.high,
            "std": total.std,
            "samples": total.samples,
            "tier": total.tier,
            "confidence_z": total.confidence_z,
            "latency_multiplier": total.latency_multiplier,
            "display": format_eta_minutes(total, min_samples=min_samples),
            "per_library": per_library,
        }

    # ── Diagnostics surface ──────────────────────────────────────────

    def training_status(
        self, *, server_id: Optional[str] = None, min_samples: int = 5,
    ) -> Dict[str, Any]:
        """Return a snapshot of the bucket store, grouped by server.
        Drives the Run Job form's 'Training progress' disclosure and
        any external tooling that wants to inspect training depth.

        ``server_id`` filters to one server when provided. ``min_samples``
        is the threshold used to classify each bucket as tier-1-ready
        (>= min_samples), anchor (1 to min_samples-1), or untrained
        (0)."""
        self.load_from_db()
        by_server: Dict[str, List[Dict[str, Any]]] = {}
        with self._lock:
            for key, bucket in self._buckets.items():
                if server_id and key.server_id != server_id:
                    continue
                row = {
                    "label": key.label,
                    "library_type": key.library_type,
                    "bulk_strategy": key.bulk_strategy,
                    "samples": bucket.sample_count,
                    "tier_one_ready": bucket.sample_count >= min_samples,
                    "anchor_ready": bucket.sample_count >= 1,
                    "ping_ema_ms": (
                        round(bucket.ping_ema, 2)
                        if bucket.ping_ema is not None else None
                    ),
                    "last_observed_at": bucket.last_observed_at,
                    "predicted_at_xbar_seconds": round(bucket.ybar, 2),
                }
                by_server.setdefault(key.server_id, []).append(row)

        result: List[Dict[str, Any]] = []
        for sid in sorted(by_server.keys()):
            rows = sorted(
                by_server[sid],
                key=lambda r: (r["label"], r["library_type"], r["bulk_strategy"]),
            )
            tier_one = sum(1 for r in rows if r["tier_one_ready"])
            anchor = sum(
                1 for r in rows if r["anchor_ready"] and not r["tier_one_ready"]
            )
            result.append({
                "server_id": sid,
                "buckets": rows,
                "summary": {
                    "total_buckets": len(rows),
                    "tier_one_count": tier_one,
                    "anchor_count": anchor,
                    "min_samples_for_tier_one": min_samples,
                },
            })
        return {"by_server": result}

    # ── Helpers ──────────────────────────────────────────────────────

    def _key_from_entry(self, entry) -> Optional[BucketKey]:
        """Build a BucketKey from a TimingEntry. Returns None when the
        entry is missing the minimum fields (server_id + label) to
        place it on a meaningful bucket."""
        server_id = getattr(entry, "server_id", "") or ""
        label = getattr(entry, "label", "") or ""
        if not server_id or not label:
            return None
        extra = getattr(entry, "extra", None) or {}
        library_type = str(extra.get("library_type", "") or "")
        bulk_strategy = str(extra.get("strategy", "") or "")
        return BucketKey(
            server_id=str(server_id),
            label=str(label),
            library_type=library_type,
            bulk_strategy=bulk_strategy,
        )

    @staticmethod
    def _bucket_to_row(k: BucketKey, b: WeightedLinearRegression) -> Dict[str, Any]:
        return {
            "server_id": k.server_id,
            "label": k.label,
            "library_type": k.library_type,
            "bulk_strategy": k.bulk_strategy,
            "sum_w": b.sum_w,
            "sum_wx": b.sum_wx,
            "sum_wy": b.sum_wy,
            "sum_wxx": b.sum_wxx,
            "sum_wxy": b.sum_wxy,
            "sum_wyy": b.sum_wyy,
            "sample_count": b.sample_count,
            "last_observed_at": b.last_observed_at,
            "sum_w_ping": b.sum_w_ping,
            "sum_wp": b.sum_wp,
        }

    # ── Test seam ────────────────────────────────────────────────────

    def _reset_for_tests(self) -> None:
        with self._lock:
            self._buckets.clear()
            self._loaded = False


# ── Module-level singleton accessor ──────────────────────────────────

_trainer_singleton: Optional[ETATrainer] = None
_trainer_lock = threading.Lock()


def get_trainer() -> ETATrainer:
    """Return the process-wide trainer. Lazily constructed on first
    call; tunables are resolved from settings.json at construction
    time so a settings reload requires a process restart (acceptable
    for these values)."""
    global _trainer_singleton
    if _trainer_singleton is not None:
        return _trainer_singleton
    with _trainer_lock:
        if _trainer_singleton is not None:
            return _trainer_singleton
        alpha, z = _resolve_tunables()
        _trainer_singleton = ETATrainer(alpha=alpha, confidence_z=z)
        return _trainer_singleton


def _resolve_tunables() -> tuple:
    """Read ``eta_alpha`` and ``eta_confidence_z`` from settings. Falls
    through to (0.2, 1.0) when settings cannot be read; never raises.
    Bad values fall through to the defaults so a typo cannot poison
    the engine."""
    alpha = 0.2
    z = 1.0
    try:
        from server.persistence import load_settings
        s = load_settings() or {}
        raw_alpha = s.get("eta_alpha")
        if isinstance(raw_alpha, (int, float)) and 0.0 < float(raw_alpha) <= 1.0:
            alpha = float(raw_alpha)
        raw_z = s.get("eta_confidence_z")
        if isinstance(raw_z, (int, float)) and 0.0 < float(raw_z) <= 3.0:
            z = float(raw_z)
    except Exception:
        pass
    return alpha, z


def _reset_singleton_for_tests() -> None:
    """Test-only: forget the singleton so the next get_trainer() rebuilds
    against the current settings + DB."""
    global _trainer_singleton
    with _trainer_lock:
        _trainer_singleton = None
