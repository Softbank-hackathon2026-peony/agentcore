export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly retryAfter: number | null
  readonly fields: unknown

  constructor(status: number, code: string, message: string, retryAfter: number | null = null, fields?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.retryAfter = retryAfter
    this.fields = fields
  }
}

export type FetchLike = (input: string, init: RequestInit) => Promise<Response>

export interface ClientDeps {
  fetch: FetchLike
  sleep: (ms: number) => Promise<void>
  random: () => number
}

export interface RequestOptions {
  method?: string
  body?: unknown
  headers?: Record<string, string>
}

export interface RetryInfo {
  attempt: number
  delayMs: number
  status: number
}

export type RetryListener = (info: RetryInfo) => void

export const MAX_RETRIES = 3
const RETRYABLE_STATUS = new Set([429, 503])
const BASE_DELAY_MS = 500
const MAX_DELAY_MS = 8000
const JITTER_MS = 250
// 이보다 오래 기다리라는 응답(로그인 잠금 등)은 UI를 멈추지 않도록 자동 재시도하지 않는다.
const MAX_AUTO_RETRY_AFTER_SEC = 30
const NEVER_RETRY_CODES = new Set(['TOO_MANY_ATTEMPTS'])

export function computeBackoff(attempt: number, retryAfterSec: number | null, random: () => number): number {
  const exponential = Math.min(BASE_DELAY_MS * 2 ** attempt, MAX_DELAY_MS)
  const base = retryAfterSec === null ? exponential : Math.max(retryAfterSec * 1000, exponential)
  return base + Math.floor(random() * JITTER_MS)
}

export function createClient(deps: ClientDeps) {
  let retryListener: RetryListener | null = null

  async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
    const headers: Record<string, string> = { Accept: 'application/json', ...options.headers }
    let body: string | undefined
    if (options.body !== undefined) {
      headers['Content-Type'] = 'application/json'
      body = JSON.stringify(options.body)
    }

    for (let attempt = 0; ; attempt++) {
      const res = await deps.fetch(path, { method: options.method ?? 'GET', headers, body, credentials: 'same-origin' })
      if (res.ok) {
        if (res.status === 204) return undefined as T
        return (await res.json()) as T
      }
      const error = await toApiError(res)
      if (!RETRYABLE_STATUS.has(res.status) || attempt >= MAX_RETRIES) throw error
      if (NEVER_RETRY_CODES.has(error.code)) throw error
      if (error.retryAfter !== null && error.retryAfter > MAX_AUTO_RETRY_AFTER_SEC) throw error
      const delayMs = computeBackoff(attempt, error.retryAfter, deps.random)
      retryListener?.({ attempt: attempt + 1, delayMs, status: res.status })
      await deps.sleep(delayMs)
    }
  }

  function onRetry(listener: RetryListener | null): void {
    retryListener = listener
  }

  return { request, onRetry }
}

async function toApiError(res: Response): Promise<ApiError> {
  const header = res.headers.get('Retry-After')
  const retryAfter = header !== null && /^\d+$/.test(header) ? Number(header) : null
  let code = `HTTP_${res.status}`
  let message = res.statusText || 'Request failed'
  let fields: unknown
  try {
    const data: unknown = await res.json()
    if (isErrorBody(data)) {
      code = data.code
      message = data.message ?? message
      fields = data.fields
    }
  } catch {
    // 본문이 JSON이 아니면(예: LB가 만든 HTML 502) 기본값을 쓴다.
  }
  return new ApiError(res.status, code, message, retryAfter, fields)
}

function isErrorBody(value: unknown): value is { code: string; message?: string; fields?: unknown } {
  return typeof value === 'object' && value !== null && typeof (value as { code?: unknown }).code === 'string'
}

export const api = createClient({
  fetch: (input, init) => fetch(input, init),
  sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  random: Math.random,
})
