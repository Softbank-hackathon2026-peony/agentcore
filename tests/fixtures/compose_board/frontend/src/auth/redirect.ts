export function loginPath(next: string): string {
  return `/login?next=${encodeURIComponent(next)}`
}

// 다른 사이트로 튕기는 오픈 리다이렉트를 막기 위해 같은 출처의 경로만 허용한다.
export function safeNext(raw: string | null): string {
  if (!raw || !raw.startsWith('/') || raw.startsWith('//') || raw.includes('\\')) return '/'
  try {
    if (new URL(raw, window.location.origin).origin !== window.location.origin) return '/'
  } catch {
    return '/'
  }
  return raw
}
