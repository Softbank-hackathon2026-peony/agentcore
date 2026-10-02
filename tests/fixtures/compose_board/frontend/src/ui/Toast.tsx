import { useEffect, useState, type ReactNode } from 'react'
import { MAX_RETRIES, api } from '../api/client'

export function ToastProvider({ children }: { children: ReactNode }) {
  const [message, setMessage] = useState<string | null>(null)

  useEffect(() => {
    api.onRetry(({ attempt }) => setMessage(`요청이 많아 잠시 후 자동으로 다시 시도합니다 (${attempt}/${MAX_RETRIES})`))
    return () => api.onRetry(null)
  }, [])

  useEffect(() => {
    if (!message) return
    const timer = setTimeout(() => setMessage(null), 3000)
    return () => clearTimeout(timer)
  }, [message])

  return (
    <>
      {children}
      {message && <div className="toast" role="status">{message}</div>}
    </>
  )
}
