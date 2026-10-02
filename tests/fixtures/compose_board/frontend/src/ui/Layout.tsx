import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useMe } from '../auth/useMe'

export function Layout({ children }: { children: ReactNode }) {
  const { data: me, isError, isPending } = useMe()
  return (
    <div className="container">
      <header className="header">
        <Link to="/" className="brand">재난 커뮤니티</Link>
        <nav>
          {isPending || (isError && me === undefined) ? null : me ? (
            <>
              <Link to="/write">글쓰기</Link>
              <Link to="/me">내 정보</Link>
            </>
          ) : (
            <>
              <Link to="/login">로그인</Link>
              <Link to="/signup">회원가입</Link>
            </>
          )}
        </nav>
      </header>
      <main>{children}</main>
    </div>
  )
}
