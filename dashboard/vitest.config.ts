import { defineConfig } from 'vitest/config'

export default defineConfig({
  define: { __LATET_MOCK__: 'false' },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts', 'scripts/**/*.test.mjs'],
    setupFiles: ['src/test-setup.ts'],
    // The palette's test reads the tokens of index.css as text; other CSS stays out of the tests.
    css: { include: [/src[\\/]index\.css\b/] },
  },
})
