// Shared modal shell.
//
// Consolidates the fixed-overlay backdrop + centered panel that ~24
// modals across the app each hand-rolled. Bakes in the behaviors most
// of them were missing or did inconsistently: a document-level
// Escape-to-close, backdrop-click-to-close (target-guarded so a click
// inside the panel never closes it), and role="dialog" / aria-modal
// for screen readers. The backdrop + z-index are fixed here so every
// modal stacks consistently (some hand-rolled ones used z-index 50
// and rendered behind others).

import { useEffect } from 'react';
import type { ReactNode } from 'react';

interface ModalProps {
  // Defaults to true so modals the parent conditionally mounts
  // (`{showX && <Modal .../>}`) can omit it. Modals that own their
  // own open state pass it explicitly.
  open?: boolean;
  onClose: () => void;
  // Rendered as the panel's <h2> heading when provided.
  title?: string;
  // Panel max width in px (default 520). Caps at 92vw on narrow screens.
  width?: number;
  // Panel max height as a CSS length (default '80vh'). Content past
  // it scrolls inside the panel.
  maxHeight?: string;
  // 'top' (default): panel sits near the top (paddingTop 8vh) - the
  // app's dominant modal placement. 'center': vertically centered
  // with a flat 20px gutter.
  align?: 'top' | 'center';
  // Optional colored panel border for modals that signal a privileged
  // or cautionary action. Omit for the neutral default border.
  tone?: 'accent' | 'warn';
  ariaLabel?: string;
  children: ReactNode;
}

export function Modal({
  open = true,
  onClose,
  title,
  width = 520,
  maxHeight = '80vh',
  align = 'top',
  tone,
  ariaLabel,
  children,
}: ModalProps) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={ariaLabel ?? title}
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.55)',
        zIndex: 1000,
        display: 'flex',
        justifyContent: 'center',
        alignItems: align === 'center' ? 'center' : 'flex-start',
        paddingTop: align === 'center' ? undefined : '8vh',
        padding: align === 'center' ? 20 : undefined,
      }}
    >
      <div
        className="panel"
        onClick={(e) => e.stopPropagation()}
        style={{
          width: `min(${width}px, 92vw)`,
          maxHeight,
          overflowY: 'auto',
          borderColor: tone === 'accent' ? 'var(--accent)'
            : tone === 'warn' ? 'var(--warn)' : undefined,
        }}
      >
        {title && <h2 style={{ marginTop: 0 }}>{title}</h2>}
        {children}
      </div>
    </div>
  );
}
