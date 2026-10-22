import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { setTransport } from './api/client'
import { App } from './App'
import './i18n'
import './fonts'
import './index.css'
import { applyStoredTheme } from './lib/theme'

applyStoredTheme()

async function boot() {
  // The mock transport is a separate chunk that only the dev server's mock mode ever loads.
  if (__LATET_MOCK__) {
    const { mockTransport } = await import('./mock')
    setTransport(mockTransport)
  }
  const root = document.getElementById('root')
  if (!root) throw new Error('#root is missing from index.html')
  createRoot(root).render(
    <StrictMode>
      <App />
    </StrictMode>,
  )
}

void boot()
