// Small inline help-icon component.
//
// Renders a circular (?) button next to its anchor. Hover, focus, or
// click reveals a popover containing the longer explanation. Used to
// keep visible help text short while still surfacing the full
// context on demand. The same long explanations also live in
// HelpPanel.tsx so an operator can read them as a flat reference.

import { useState } from 'react';

export function InfoTip({
  children,
  label = '?',
  maxWidth = 320,
}: {
  children: React.ReactNode;
  label?: string;
  maxWidth?: number;
}) {
  const [open, setOpen] = useState(false);

  // The tooltip is often anchored inside an <h2> or .label, both of
  // which carry text-transform: uppercase and letter-spacing in the
  // global stylesheet. Reset those properties explicitly on both the
  // button and the popover so the (?) chip and the body text always
  // render in normal sentence case.
  const resetTextStyle = {
    textTransform: 'none' as const,
    letterSpacing: 'normal' as const,
    fontStyle: 'normal' as const,
  };

  return (
    <span style={{ position: 'relative', display: 'inline-block', ...resetTextStyle }}>
      <button
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
      {open && (
        <span
          role="tooltip"
          style={{
            position: 'absolute',
            top: 'calc(100% + 6px)',
            left: 0,
            zIndex: 100,
            background: '#161a23',
            color: '#e2e6ee',
            border: '1px solid #2c3344',
            borderRadius: 6,
            padding: '10px 12px',
            fontSize: 13,
            fontWeight: 400,
            width: maxWidth,
            maxWidth: '80vw',
            boxShadow: '0 6px 20px rgba(0,0,0,0.55)',
            lineHeight: 1.5,
            whiteSpace: 'normal',
            textAlign: 'left',
            fontFamily: 'inherit',
            ...resetTextStyle,
          }}
        >
          {children}
        </span>
      )}
    </span>
  );
}
