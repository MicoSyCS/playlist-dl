import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource/share-tech-mono/400.css'
import './styles.css'
import App from './App'

const root = document.getElementById('root')
if (!root) throw new Error('missing #root')

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
