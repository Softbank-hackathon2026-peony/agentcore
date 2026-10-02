import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { authApi } from '../api/auth'
import { ApiError } from '../api/client'
import { errorMessage } from '../api/messages'
import { ME_KEY } from './useMe'

type Field = 'email' | 'nickname' | 'password'
const FIELDS: Field[] = ['email', 'nickname', 'password']

// 서버 오류를 입력 칸별 메시지로 나눈다. 칸에 대응하지 않는 오류는 `form`에 담는다.
function fieldErrors(err: unknown): { fields: Partial<Record<Field, string>>; form: string | null } {
  const fields: Partial<Record<Field, string>> = {}
  if (!(err instanceof ApiError)) return { fields, form: err ? errorMessage(err) : null }
  if (err.code === 'EMAIL_TAKEN') return { fields: { email: errorMessage(err) }, form: null }
  if (err.code === 'NICKNAME_TAKEN') return { fields: { nickname: errorMessage(err) }, form: null }
  if (err.status === 422 && Array.isArray(err.fields)) {
    for (const item of err.fields as { loc?: unknown[]; msg?: string }[]) {
      const key = item.loc?.[item.loc.length - 1]
      const field = FIELDS.find((f) => f === key)
      if (field && item.msg && !fields[field]) fields[field] = item.msg
    }
    if (Object.keys(fields).length > 0) return { fields, form: null }
  }
  return { fields, form: errorMessage(err) }
}

export function SignupPage() {
  const [email, setEmail] = useState('')
  const [nickname, setNickname] = useState('')
  const [password, setPassword] = useState('')
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const signup = useMutation({
    mutationFn: async () => {
      await authApi.signup({ email, password, nickname })
      return authApi.login({ email, password })
    },
    onSuccess: (user) => {
      queryClient.setQueryData(ME_KEY, user)
      navigate('/', { replace: true })
    },
  })
  const { fields, form } = fieldErrors(signup.error)

  return (
    <section>
      <h1>회원가입</h1>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          signup.mutate()
        }}
      >
        <div className="field">
          <label>
            이메일
            <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoComplete="email" aria-invalid={!!fields.email} aria-describedby={fields.email ? 'signup-email-error' : undefined} />
          </label>
          {fields.email && <span id="signup-email-error" className="field-error" role="alert">{fields.email}</span>}
        </div>
        <div className="field">
          <label>
            닉네임
            <input value={nickname} onChange={(e) => setNickname(e.target.value)} required minLength={2} maxLength={20} aria-invalid={!!fields.nickname} aria-describedby={fields.nickname ? 'signup-nickname-error' : undefined} />
          </label>
          {fields.nickname && <span id="signup-nickname-error" className="field-error" role="alert">{fields.nickname}</span>}
        </div>
        <div className="field">
          <label>
            비밀번호
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required minLength={8} maxLength={128} autoComplete="new-password" aria-invalid={!!fields.password} aria-describedby={fields.password ? 'signup-password-error' : undefined} />
          </label>
          {fields.password && <span id="signup-password-error" className="field-error" role="alert">{fields.password}</span>}
        </div>
        {form && <p className="error" role="alert">{form}</p>}
        <button type="submit" disabled={signup.isPending}>가입하기</button>
      </form>
    </section>
  )
}
