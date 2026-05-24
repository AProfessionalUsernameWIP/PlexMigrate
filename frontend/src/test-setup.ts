// Vitest setup file. Imports @testing-library/jest-dom's custom
// matchers (toBeInTheDocument, toHaveTextContent, etc.) so every
// test file gets them without per-file boilerplate.
//
// Loaded once per test process via vitest.config.ts's
// `test.setupFiles`.

import '@testing-library/jest-dom/vitest';
import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';

// RTL's auto-cleanup hook registers via a global afterEach when
// jest/vitest expose globals. We configured vitest with
// globals: false, so register the cleanup explicitly here. Without
// this, every render(...) call accumulates in document.body across
// tests and findByRole sees "multiple elements" duplicates on the
// second test in a file.
afterEach(() => {
  cleanup();
});
