import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { authApi } from '../api/auth'
import { errorMessage } from '../api/messages'
import { ME_KEY, useMe } from './useMe'

export function MePage() {
  const { data: me } = useMe()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [password, setPassword] = useState('')

  // 먼저 이동한 뒤 사용자 정보를 지운다. 순서가 반대면 RequireAuth가 /login으로 보내 버린다.
  const signOut = () => {
    navigate('/', { replace: true })
    queryClient.setQueryData(ME_KEY, null)
  }
  const logout = useMutation({ mutationFn: () => authApi.logout(), onSuccess: signOut })
  const remove = useMutation({ mutationFn: (pw: string) => authApi.deleteAccount(pw), onSuccess: signOut })

  if (!me) return null

  return (
    <section>
      <h1>내 정보</h1>
      <dl>
        <dt>닉네임</dt>
        <dd>{me.nickname}</dd>
        <dt>이메일</dt>
        <dd>{me.email}</dd>
      </dl>
      <button onClick={() => logout.mutate()} disabled={logout.isPending}>로그아웃</button>

      <h2>회원 탈퇴</h2>
      <p>탈퇴해도 작성한 글은 '탈퇴한 사용자' 이름으로 남습니다.</p>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          remove.mutate(password)
        }}
      >
        <label>
          비밀번호 확인
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required autoComplete="current-password" />
        </label>
        {remove.isError && <p className="error" role="alert">{errorMessage(remove.error)}</p>}
        <button type="submit" className="danger" disabled={remove.isPending}>회원 탈퇴</button>
      </form>
    </section>
  )
}
