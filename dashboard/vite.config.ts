import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig, type ProxyOptions } from 'vite'

// The daemon the dev server talks to in real mode (override with LATET_DAEMON).
const DAEMON = process.env.LATET_DAEMON ?? 'http://127.0.0.1:5283'

// The Enterprise Edition's code (src/ee/) in a file of its own, whose first line says its license: LICENSE puts every
// directory named ee under the Latet Enterprise License, ee/LICENSE.
const EE_LICENSE_LINE =
  '/*! Latet Enterprise Edition: licensed under the Latet Enterprise License (ee/LICENSE); see LICENSE. */'

export default defineConfig(({ command, mode }) => {
  // Mock mode exists only in the dev server: a production build never contains the fixtures.
  const mock = command === 'serve' && (mode === 'mock' || process.env.VITE_LATET_MOCK === '1')

  const toDaemon: ProxyOptions = {
    target: DAEMON,
    changeOrigin: true,
    // The daemon may compare Origin with its own address: present the dev server's calls as its own.
    configure: (proxy) => {
      proxy.on('proxyReq', (req) => {
        if (req.getHeader('origin')) req.setHeader('origin', DAEMON)
        req.removeHeader('referer')
      })
    },
  }

  return {
    base: '/_latet/ui/',
    plugins: [react(), tailwindcss()],
    define: {
      __LATET_MOCK__: JSON.stringify(mock),
    },
    server: {
      // 127.0.0.1, not localhost: the session cookie set by the daemon is scoped to this host.
      host: '127.0.0.1',
      port: 5173,
      proxy: mock
        ? undefined
        : {
            // The one-time login link must reach the daemon, which sets the cookie and redirects back.
            '/_latet/ui/login': toDaemon,
            // Everything else under /_latet except the UI itself (the API, the SSE stream).
            '^/_latet/(?!ui(?:/|$))': toDaemon,
          },
    },
    preview: { host: '127.0.0.1' },
    build: {
      outDir: '../src/latet/web',
      emptyOutDir: true,
      sourcemap: false,
      assetsDir: 'assets',
      rolldownOptions: {
        output: {
          postBanner: (chunk) => (chunk.name === 'ee' ? EE_LICENSE_LINE : ''),
          // Libraries change less often than the app: separate chunks stay cached across releases.
          codeSplitting: {
            groups: [
              { name: 'react', test: /node_modules[\\/](react|react-dom|scheduler)[\\/]/, priority: 2 },
              { name: 'vendor', test: /node_modules[\\/]/, priority: 1 },
              // The five languages' strings, apart from the app's code (they would push it past 500 kB).
              { name: 'locales', test: /src[\\/]locales[\\/]/, priority: 1 },
              // The ee chunk holds src/ee alone and imports the Community modules it uses from the main chunk, which
              // imports it too (a circular chunk import): code in src/ee must not use an index export at module top
              // level (a constant, a class, a call), or it meets that binding before it is set (a TDZ error at load).
              { name: 'ee', test: /src[\\/]ee[\\/]/, priority: 1, includeDependenciesRecursively: false },
            ],
          },
        },
      },
    },
  }
})
