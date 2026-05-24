// Thin REST + WebSocket client for the Hestia-MediaManager server.
//
// Phase 3c completed the per-resource decomposition. The legacy
// monolithic ``api`` const + ``DashboardWsClient`` /
// ``DevConsoleWsClient`` definitions now live under ``./api/*`` and
// are recomposed by the barrel at ``./api/index``. This file is the
// historical entry point - every consumer that does
// ``import { api } from '../api'`` / ``import { dashboardWsClient }
// from '../api'`` / etc. keeps working unchanged because we re-export
// the barrel's surface verbatim.

export * from './api/index';
