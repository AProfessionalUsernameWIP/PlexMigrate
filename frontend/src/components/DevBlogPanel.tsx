// Dev Blog tab.
//
// Iframes the static dev-blog site that lives in `public/dev-blog/`.
// Vite serves anything under `public/` at the application root, so the
// blog is reachable at `/dev-blog/index.html` from the running app.
// We render it inside an iframe so the blog keeps its own dark dev-blog
// aesthetic (distinct from the surrounding app UI) and feels like a
// different website embedded inside this one.
//
// Why iframe instead of porting to React:
//   * Zero rebuild work; the static HTML + CSS already exists.
//   * The blog's visual style is deliberately different from the app's
//     and should stay that way.
//   * One source of truth: the same files can be served as GitHub Pages
//     too.
//   * The user can pop the blog out to a real browser tab via the
//     chrome bar, which is useful when sharing the link with someone.

import { useRef, useState } from 'react';

const BLOG_ENTRY = '/dev-blog/index.html';

export function DevBlogPanel() {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  // The iframe boots at the landing page; subsequent in-blog navigation
  // is handled by the blog itself. The Home button below re-points the
  // iframe at the entry URL when the user wants to start over.
  const [reloadKey, setReloadKey] = useState(0);

  const openInNewTab = () => {
    window.open(BLOG_ENTRY, '_blank', 'noopener,noreferrer');
  };
  const resetToHome = () => {
    // Bumping the key remounts the iframe so it boots fresh at
    // BLOG_ENTRY regardless of how deep the user navigated.
    setReloadKey((k) => k + 1);
  };

  return (
    <div
      className="panel"
      style={{
        padding: 0,
        overflow: 'hidden',
        display: 'flex',
        flexDirection: 'column',
        // The iframe needs an explicit height; fill the available
        // viewport minus the chrome bar + the surrounding app
        // padding. 220px is a reasonable approximation of the app's
        // header + tab strip + container padding.
        minHeight: 'calc(100vh - 220px)',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '10px 16px',
          background: 'var(--bg-elev, #161b22)',
          borderBottom: '1px solid var(--border, #30363d)',
          fontSize: 13,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <strong style={{ fontFamily: 'var(--font-mono, monospace)' }}>
            <span style={{ color: 'var(--accent, #58a6ff)' }}>&gt;_</span> Dev Blog
          </strong>
          <span style={{ color: 'var(--text-dim)', fontSize: 12 }}>
            About the developer + walkthroughs of how this app was built
          </span>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button
            type="button"
            onClick={resetToHome}
            style={{ fontSize: 12 }}
            title="Reload the dev blog landing page."
          >
            Home
          </button>
          <button
            type="button"
            onClick={openInNewTab}
            style={{ fontSize: 12 }}
            title="Open the dev blog in a new browser tab without the app chrome."
          >
            Open in new tab ↗
          </button>
        </div>
      </div>
      <iframe
        key={reloadKey}
        ref={iframeRef}
        src={BLOG_ENTRY}
        title="PlexBackUp Dev Blog"
        style={{
          flex: 1,
          width: '100%',
          border: 'none',
          background: '#0d1117',
        }}
      />
    </div>
  );
}
