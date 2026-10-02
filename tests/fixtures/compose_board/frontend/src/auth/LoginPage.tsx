import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { authApi } from '../api/auth'
import { errorMessage } from '../api/messages'
import { safeNext } from './redirect'
import { ME_KEY } from './useMe'

export function LoginPage() {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const login = useMutation({
    mutationFn: authApi.login,
    onSuccess: (user) => {
      queryClient.setQueryData(ME_KEY, user)
      navigate(safeNext(params.get('next')), { replace: true })
    },
  })

  return (
    <section>
      <h1>로그인</h1>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          login.mutate({ email, password })
        }}
      >
        <label>
          이메일
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="email" />
        </label>
        <label>
          비밀번호
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required autoComplete="current-password" />
        </label>
        {login.isError && <p className="error" role="alert">{errorMessage(login.error)}</p>}
        <button type="submit" disabled={login.isPending}>로그인</button>
      </form>
      <p>
        계정이 없나요? <Link to="/signup">회원가입</Link>
      </p>
    </section>
  )
}
