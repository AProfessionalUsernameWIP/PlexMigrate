// Small inline help-icon component.
//
// Renders a circular (?) button next to its anchor. Hover, focus, or
// click reveals a popover containing the longer explanation. Used to
// keep visible help text short while still surfacing the full
// context on demand.
//
// Item 5 (admin-management plan, 2026-05-15): InfoTip supports two
// modes:
//   * Legacy mode (children prop): the parent passes the body as
//     JSX. Used by older call sites that haven't been migrated to
//     the help_content registry yet.
//   * Registry mode (topicId prop): the body is looked up in
//     help_content/ at render time. The same body also appears under
//     the Help tab, so an end user who has tooltips disabled can find
//     every explanation in one place.
//
// The global ``tooltips_enabled`` setting (root_admin-controlled) is
// read from ``TooltipContext``. When disabled, no (?) icon renders at
// all in either mode; the help content is still reachable via the
// Help tab.
//
// Positioning (2026-05-15 follow-up): the popover uses ``position:
// fixed`` and computes its placement from the button's
// ``getBoundingClientRect()``. This escapes any ancestor with
// ``overflow: hidden`` / ``overflow: auto`` / a ``transform`` that
// would otherwise clip an ``position: absolute`` popover. The
// popover also FLIPS above the icon when there isn't enough room
// below, and FLIPS to right-aligned when the icon is near the right
// edge. Without this, hovering an icon near the viewport bottom
// caused a visible flicker as the browser scrolled / re-laid-out
// content under the popover, briefly leaving the icon's hover area
// and bouncing the open state.

import { useContext, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { TooltipContext } from '../contexts/TooltipContext';
import { getHelpTopic } from '../help_content';

type Props =
  | {
      // Legacy mode: caller passes the popover body directly.
      children: React.ReactNode;
      label?: string;
      maxWidth?: number;
      topicId?: undefined;
    }
  | {
      // Registry mode: caller passes a topic id; the body is looked
      // up at render time from the help_content registry.
      topicId: string;
      label?: string;
      maxWidth?: number;
      children?: undefined;
    };

interface Placement {
  // Top edge of the popover in viewport coords (px).
  top: number;
  // Left edge of the popover in viewport coords (px).
  left: number;
  // Effective max-width applied to the popover (so it doesn't
  // overflow horizontally on narrow viewports).
  width: number;
}

const GAP_PX = 6;
const VIEWPORT_MARGIN_PX = 8;

export function InfoTip(props: Props) {
  const [open, setOpen] = useState(false);
  const [placement, setPlacement] = useState<Placement | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const popoverRef = useRef<HTMLSpanElement | null>(null);
  const { enabled: tooltipsEnabled } = useContext(TooltipContext);

  // Resolve the body. In registry mode, an unknown id falls back to a
  // short placeholder so the missing-content state is visible (vs
  // silently rendering nothing, which is harder to debug).
  let body: React.ReactNode;
  if ('topicId' in props && props.topicId) {
    const topic = getHelpTopic(props.topicId);
    body = topic ? topic.body : (
      <em style={{ color: 'var(--text-dim)' }}>
        (No help content registered for id <code>{props.topicId}</code>.)
      </em>
    );
  } else {
    body = (props as { children: React.ReactNode }).children;
  }

  const label = props.label ?? '?';
  const requestedMaxWidth = props.maxWidth ?? 320;

  // Compute the popover's viewport-fixed position from the button's
  // bounding rect. Flip above when there's no room below; flip left
  // when there's no room on the right. Re-measures on open, on
  // scroll, and on resize so the popover follows the button if the
  // end user scrolls the page while it's open.
  const reposition = () => {
    const btn = buttonRef.current;
    if (!btn) return;
    const rect = btn.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    // Constrain width: never wider than the smaller of the requested
    // max-width and 80% of the viewport, with a small safety margin.
    const width = Math.min(
      requestedMaxWidth,
      Math.max(160, Math.floor(vw * 0.8) - 2 * VIEWPORT_MARGIN_PX),
    );

    // Estimate the popover height. We don't have the rendered height
    // until after the first paint, so estimate generously (320px)
    // for the flip decision. The actual popover may be shorter; this
    // only matters at the bottom-of-viewport edge case.
    let estimatedHeight = 320;
    const pop = popoverRef.current;
    if (pop) {
      estimatedHeight = pop.getBoundingClientRect().height || estimatedHeight;
    }

    const spaceBelow = vh - rect.bottom - VIEWPORT_MARGIN_PX;
    const spaceAbove = rect.top - VIEWPORT_MARGIN_PX;
    const placeAbove =
      spaceBelow < estimatedHeight + GAP_PX
      && spaceAbove > spaceBelow;

    const top = placeAbove
      ? Math.max(VIEWPORT_MARGIN_PX, rect.top - GAP_PX - estimatedHeight)
      : rect.bottom + GAP_PX;

    // Horizontal: prefer left-aligned with the icon, flip to right-
    // aligned when that would overflow.
    let left = rect.left;
    if (left + width + VIEWPORT_MARGIN_PX > vw) {
      left = Math.max(VIEWPORT_MARGIN_PX, vw - width - VIEWPORT_MARGIN_PX);
    }
    if (left < VIEWPORT_MARGIN_PX) {
      left = VIEWPORT_MARGIN_PX;
    }

    setPlacement({ top, left, width });
  };

  // Compute the initial placement when the popover opens. Use
  // useLayoutEffect so the first paint already carries the final
  // position; otherwise the popover briefly flashes at the default
  // top-left of the viewport.
  useLayoutEffect(() => {
    if (open) {
      reposition();
    } else {
      setPlacement(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Re-measure on scroll/resize while open. Listening on window with
  // capture so we catch scroll inside nested scrollable containers
  // too (which is what causes the original flicker symptom).
  useEffect(() => {
    if (!open) return;
    const onChange = () => reposition();
    window.addEventListener('scroll', onChange, true);
    window.addEventListener('resize', onChange);
    return () => {
      window.removeEventListener('scroll', onChange, true);
      window.removeEventListener('resize', onChange);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Re-measure once the popover has its real height (after the
  // initial paint). The first measurement uses an estimate; this
  // second pass corrects the flip decision when the body turns out
  // to be smaller than the estimate.
  useLayoutEffect(() => {
    if (open && placement && popoverRef.current) {
      // Schedule on the next frame so the browser has laid out the
      // popover at the estimated position first.
      const id = window.requestAnimationFrame(() => reposition());
      return () => window.cancelAnimationFrame(id);
    }
    return undefined;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, placement?.top, placement?.left]);

  // Root_admin disabled tooltips globally? Render nothing. The full
  // body still lives on the Help tab.
  if (!tooltipsEnabled) return null;

  // Anchors are often inside <h2> / .label, which carry text-transform
  // uppercase + letter-spacing globally. Reset on both the button and
  // the popover so the chip and body render in normal sentence case.
  const resetTextStyle = {
    textTransform: 'none' as const,
    letterSpacing: 'normal' as const,
    fontStyle: 'normal' as const,
  };

  return (
    <span style={{ position: 'relative', display: 'inline-block', ...resetTextStyle }}>
      <button
        ref={buttonRef}
        type="button"
        onClick={(e) => { e.preventDefault(); setOpen((v) => !v); }}
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        aria-label="More info"
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          width: 16,
          height: 16,
          borderRadius: '50%',
          border: '1px solid var(--text-dim)',
          background: 'transparent',
          color: 'var(--text-dim)',
          fontSize: 11,
          fontWeight: 600,
          padding: 0,
          cursor: 'help',
          verticalAlign: 'middle',
          marginLeft: 6,
          lineHeight: 1,
          ...resetTextStyle,
        }}
      >
        {label}
      </button>
      {open && placement && (
        <span
          ref={popoverRef}
          role="tooltip"
          style={{
            position: 'fixed',
            top: placement.top,
            left: placement.left,
            width: placement.width,
            zIndex: 1100,
            background: '#161a23',
            color: '#e2e6ee',
            border: '1px solid #2c3344',
            borderRadius: 6,
            padding: '10px 12px',
            fontSize: 13,
            fontWeight: 400,
            boxShadow: '0 6px 20px rgba(0,0,0,0.55)',
            lineHeight: 1.5,
            whiteSpace: 'normal',
            textAlign: 'left',
            fontFamily: 'inherit',
            // ``pointerEvents: 'none'`` keeps the popover from
            // stealing mouse events from the icon. Without this, a
            // mouse path from icon → popover would briefly trip
            // onMouseLeave on the button (because the popover is
            // technically a sibling, not a child of the button)
            // and the popover would close on its way down. The
            // end user's reported flicker was the visible
            // consequence of that.
            pointerEvents: 'none',
            ...resetTextStyle,
          }}
        >
          {body}
        </span>
      )}
    </span>
  );
}
