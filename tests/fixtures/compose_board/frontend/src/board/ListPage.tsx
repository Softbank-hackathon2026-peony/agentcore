import { useInfiniteQuery, useMutation } from '@tanstack/react-query'
import { useEffect, useMemo } from 'react'
import { Link } from 'react-router-dom'
import { boardApi } from '../api/board'
import { errorMessage } from '../api/messages'
import { newId } from '../lib/id'
import { POSTS_KEY } from './keys'
import { PENDING_LABEL, visiblePending, type PendingPost } from './pending'
import { usePendingPosts } from './PendingPostsContext'

export const POLL_INTERVAL_MS = 5000

export function ListPage() {
  const { pendings, prune, replace } = usePendingPosts()
  const posts = useInfiniteQuery({
    queryKey: POSTS_KEY,
    queryFn: ({ pageParam }) => boardApi.list(pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
    // 여러 페이지를 펼친 상태에서 폴링하면 모든 페이지를 다시 불러오므로 첫 페이지만 볼 때만 폴링한다.
    refetchInterval: (query) => ((query.state.data?.pages.length ?? 1) > 1 ? false : POLL_INTERVAL_MS),
    refetchIntervalInBackground: false,
  })

  const firstPageIds = useMemo(
    () => new Set(posts.data?.pages[0]?.items.map((p) => p.id) ?? []),
    [posts.data],
  )
  useEffect(() => prune(firstPageIds), [firstPageIds, prune])

  // 실패한 글의 예전 Idempotency-Key로는 실패한 id가 그대로 돌아오므로 새 키로 다시 보낸다.
  const retry = useMutation({
    mutationFn: async (post: PendingPost) => {
      const res = await boardApi.create({ title: post.title, body: post.body }, newId())
      return { old: post, id: res.id }
    },
    onSuccess: ({ old, id }) => replace(old.id, { ...old, id, submittedAt: Date.now(), state: 'pending' }),
  })

  const items = posts.data?.pages.flatMap((page) => page.items) ?? []

  return (
    <section>
      <h1>게시판</h1>
      {posts.isPending && <p>불러오는 중…</p>}
      {posts.isError && <p className="error" role="alert">{errorMessage(posts.error)}</p>}
      {retry.isError && <p className="error" role="alert">{errorMessage(retry.error)}</p>}
      <ul className="posts">
        {visiblePending(pendings, firstPageIds).map((p) => (
          <li key={p.id} className="post pending">
            <span className="title">{p.title}</span>
            <span className="badge">{PENDING_LABEL[p.state]}</span>
            {p.state === 'failed' && (
              <button onClick={() => retry.mutate(p)} disabled={retry.isPending}>다시 시도</button>
            )}
            <span className="meta">{p.author_nickname}</span>
          </li>
        ))}
        {items.map((post) => (
          <li key={post.id} className="post">
            <Link to={`/posts/${post.id}`} className="title">{post.title}</Link>
            <span className="meta">
              {post.author_nickname} · {new Date(post.created_at).toLocaleString('ko-KR')}
            </span>
          </li>
        ))}
      </ul>
      {posts.hasNextPage && (
        <button onClick={() => posts.fetchNextPage()} disabled={posts.isFetchingNextPage}>더 보기</button>
      )}
    </section>
  )
}
