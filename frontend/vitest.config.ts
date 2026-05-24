// Vitest configuration for the Hestia-MediaManager frontend.
//
// Extends vite.config.ts so component aliases + the React plugin
// chain that build / dev / preview rely on also apply in tests.
// Tests live colocated with the source they exercise (*.test.tsx
// next to the component). Vitest picks them up via the `include`
// glob below.
//
// First batch documented in Plan[FRONTEND-TEST-INFRA]-2026-05-17.md.

import { defineConfig, mergeConfig } from 'vitest/config';
import viteConfig from './vite.config';

export default mergeConfig(viteConfig, defineConfig({
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
    globals: false,                       // explicit imports; no test-globals magic
    include: ['src/**/*.test.{ts,tsx}'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'html'],
      exclude: ['src/**/*.test.{ts,tsx}', 'src/test-helpers/**'],
    },
  },
}));
