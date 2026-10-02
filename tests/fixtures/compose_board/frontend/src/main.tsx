import { QueryClientProvider } from '@tanstack/react-query'
import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { App } from './App'
import { loginPath } from './auth/redirect'
import { createQueryClient } from './queryClient'
import './styles.css'

const queryClient = createQueryClient(() => {
  window.location.assign(loginPath(window.location.pathname + window.location.search))
})

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
)
