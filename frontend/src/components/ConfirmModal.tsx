// Shared confirm dialog.
//
// Replaces the native window.confirm() calls in the destructive-action
// handlers. Native confirm is unstyled, theme-blind, blocks the JS
// thread, and cannot render a rich body. useConfirm() hands back a
// promise-based confirm() so a handler reads:
//
//   if (!(await confirm({ body: 'Delete this?', danger: true }))) return;
//
// ConfirmProvider must wrap the app so the single modal instance
// renders above every panel.

import { createContext, useCallback, useContext, useState } from 'react';
import type { ReactNode } from 'react';
import { Modal } from './Modal';

export interface ConfirmOptions {
  title?: string;
  body: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  // Tints the confirm button + modal border for destructive actions.
  danger?: boolean;
}

type ConfirmFn = (opts: ConfirmOptions) => Promise<boolean>;

const ConfirmContext = createContext<ConfirmFn | null>(null);

interface PendingConfirm extends ConfirmOptions {
  resolve: (ok: boolean) => void;
}

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState<PendingConfirm | null>(null);

  const confirm = useCallback<ConfirmFn>(
    (opts) => new Promise<boolean>((resolve) => {
      setPending({ ...opts, resolve });
    }),
    [],
  );

  // Resolve the in-flight promise and drop the modal. Recreated each
  // render so it closes over the current ``pending``; the buttons
  // capture the matching one when the modal is shown.
  const settle = (ok: boolean) => {
    if (pending) pending.resolve(ok);
    setPending(null);
  };

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      {pending && (
        <Modal
          onClose={() => settle(false)}
          title={pending.title ?? 'Confirm'}
          align="center"
          width={440}
          tone={pending.danger ? 'warn' : 'accent'}
        >
          <div style={{ fontSize: 13, lineHeight: 1.6, whiteSpace: 'pre-line' }}>
            {pending.body}
          </div>
          <div
            style={{
              marginTop: 16, display: 'flex', gap: 8,
              justifyContent: 'flex-end',
            }}
          >
            <button onClick={() => settle(false)}>
              {pending.cancelLabel ?? 'Cancel'}
            </button>
            <button
              className="primary"
              onClick={() => settle(true)}
              style={pending.danger ? {
                background: 'var(--warn, #f5a623)',
                borderColor: 'var(--warn, #f5a623)',
                color: '#1a1a1a',
              } : undefined}
            >
              {pending.confirmLabel ?? 'Confirm'}
            </button>
          </div>
        </Modal>
      )}
    </ConfirmContext.Provider>
  );
}

export function useConfirm(): ConfirmFn {
  const ctx = useContext(ConfirmContext);
  if (ctx === null) {
    throw new Error('useConfirm must be used within <ConfirmProvider>');
  }
  return ctx;
}
