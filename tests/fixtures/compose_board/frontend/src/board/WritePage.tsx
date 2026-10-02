import { useMutation } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { boardApi } from '../api/board'
import { errorMessage } from '../api/messages'
import { useMe } from '../auth/useMe'
import { newId } from '../lib/id'
import { usePendingPosts } from './PendingPostsContext'

export function WritePage() {
  const { data: me } = useMe()
  const { add } = usePendingPosts()
  const navigate = useNavigate()
  // 폼을 열 때 한 번만 만든다. 사용자가 다시 눌러도, 클라이언트가 자동 재시도해도 같은 키를 쓴다.
  const [idempotencyKey] = useState(newId)
  const [title, setTitle] = useState('')
  const [body, setBody] = useState('')

  const create = useMutation({
    mutationFn: () => boardApi.create({ title, body }, idempotencyKey),
    onSuccess: (res) => {
      add({ id: res.id, title, body, author_nickname: me?.nickname ?? '', submittedAt: Date.now(), state: 'pending' })
      navigate('/')
    },
  })

  return (
    <section>
      <h1>글쓰기</h1>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          create.mutate()
        }}
      >
        <label>
          제목
          <input value={title} onChange={(e) => setTitle(e.target.value)} required maxLength={100} />
        </label>
        <label>
          본문
          <textarea value={body} onChange={(e) => setBody(e.target.value)} required maxLength={5000} rows={10} />
        </label>
        {create.isError && <p className="error" role="alert">{errorMessage(create.error)}</p>}
        <button type="submit" disabled={create.isPending}>등록</button>
      </form>
    </section>
  )
}
