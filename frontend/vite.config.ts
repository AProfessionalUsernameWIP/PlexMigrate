// Vite build configuration for the Hestia-MediaManager frontend.
//
// In production the bundle is served by nginx (see frontend/nginx.conf)
// and same-origin proxying handles /api and /ws. In `npm run dev`
// (used by developers running the backend outside Docker), we proxy
// the same paths to localhost:8000 so the dev server feels identical
// to the deployed bundle.

import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
        changeOrigin: true,
      },
    },
  },
  build: {
    // Match the path nginx serves from.
    outDir: 'dist',
    sourcemap: false,
  },
});
