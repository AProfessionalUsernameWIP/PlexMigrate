// React entry point.
//
// Renders <App /> into #root. Strict mode is enabled because it
// surfaces double-render side effects early and the codebase is
// small enough not to be impacted by the extra renders.

import React from 'react';
import ReactDOM from 'react-dom/client';
import { App } from './App';
import './styles.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
