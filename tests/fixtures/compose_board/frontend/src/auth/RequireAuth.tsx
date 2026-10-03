import type { ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { loginPath } from './redirect'
import { useMe } from './useMe'

export function RequireAuth({ children }: { children: ReactNode }) {
  const { data: me, isPending, isError, refetch } = useMe()
  const location = useLocation()
  if (isPending) return <p>불러오는 중…</p>
  // 로그인 여부를 알 수 없는 상태(예: 503)는 로그아웃으로 취급하지 않는다.
  if (isError && me === undefined) {
    return (
      <div role="alert">
        <p>서비스를 일시적으로 사용할 수 없습니다. 잠시 후 다시 시도하세요.</p>
        <button onClick={() => void refetch()}>다시 시도</button>
      </div>
    )
  }
  if (!me) return <Navigate to={loginPath(location.pathname + location.search)} replace />
  return <>{children}</>
}
