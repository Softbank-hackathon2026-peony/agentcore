import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import { boardApi } from '../api/board'
import { errorMessage } from '../api/messages'
import { useMe } from '../auth/useMe'
import { POSTS_KEY, postKey } from './keys'

export function PostPage() {
  const { id = '' } = useParams()
  const { data: me } = useMe()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const status = useQuery({ queryKey: postKey(id), queryFn: () => boardApi.get(id) })
  const remove = useMutation({
    mutationFn: () => boardApi.remove(id),
    onSuccess: () => {
      queryClient.removeQueries({ queryKey: postKey(id) })
      void queryClient.invalidateQueries({ queryKey: POSTS_KEY })
      navigate('/', { replace: true })
    },
  })

  if (status.isPending) return <p>불러오는 중…</p>
  if (status.isError) return <p className="error" role="alert">{errorMessage(status.error)}</p>
  if (status.data.status === 'pending') return <p>게시 대기 중인 글입니다. 잠시 후 다시 확인해 주세요.</p>
  if (status.data.status === 'failed') return <p className="error">게시에 실패한 글입니다.</p>

  const post = status.data.post
  return (
    <article>
      <h1>{post.title}</h1>
      <p className="meta">
        {post.author_nickname} · {new Date(post.created_at).toLocaleString('ko-KR')}
      </p>
      <div className="body">{post.body}</div>
      {me && me.id === post.author_id && (
        <>
          {remove.isError && <p className="error" role="alert">{errorMessage(remove.error)}</p>}
          <button className="danger" onClick={() => remove.mutate()} disabled={remove.isPending}>삭제</button>
        </>
      )}
    </article>
  )
}
