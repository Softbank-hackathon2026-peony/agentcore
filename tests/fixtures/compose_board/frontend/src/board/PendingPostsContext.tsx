import { useQueryClient } from '@tanstack/react-query'
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { boardApi } from '../api/board'
import { ApiError } from '../api/client'
import type { PostStatus } from '../api/types'
import { POSTS_KEY } from './keys'
import { applyStatus, dueForStatusCheck, upsertPending, visiblePending, type PendingPost } from './pending'

interface PendingPostsApi {
  pendings: PendingPost[]
  add: (post: PendingPost) => void
  replace: (oldId: string, post: PendingPost) => void
  prune: (serverIds: Set<string>) => void
}

const PendingPostsContext = createContext<PendingPostsApi | null>(null)

async function fetchStatus(id: string): Promise<PostStatus['status']> {
  try {
    return (await boardApi.get(id)).status
  } catch (err) {
    // pending 키(TTL 1시간)도 DB 행도 없으면 404가 온다. 게시되지 못한 것으로 본다.
    if (err instanceof ApiError && err.status === 404) return 'failed'
    return 'pending'
  }
}

export function PendingPostsProvider({
  children,
  initialPendings = [],
  checkIntervalMs = 5000,
}: {
  children: ReactNode
  initialPendings?: PendingPost[]
  checkIntervalMs?: number
}) {
  const queryClient = useQueryClient()
  const [pendings, setPendings] = useState<PendingPost[]>(initialPendings)
  const latest = useRef(pendings)
  latest.current = pendings

  const add = useCallback((post: PendingPost) => setPendings((list) => upsertPending(list, post)), [])
  const replace = useCallback(
    (oldId: string, post: PendingPost) => setPendings((list) => upsertPending(list.filter((p) => p.id !== oldId), post)),
    [],
  )
  const prune = useCallback(
    (serverIds: Set<string>) =>
      setPendings((list) => {
        const next = visiblePending(list, serverIds)
        return next.length === list.length ? list : next
      }),
    [],
  )

  // 30초가 지나도 목록에 나타나지 않은 글은 서버에 상태를 물어본다.
  useEffect(() => {
    // 재시도 백오프 때문에 한 번의 확인이 간격보다 길어질 수 있으므로, 이전 확인이 끝나기 전에는 새로 시작하지 않는다.
    let running = false
    const timer = setInterval(async () => {
      if (running) return
      running = true
      try {
        for (const post of dueForStatusCheck(latest.current, Date.now())) {
          const status = await fetchStatus(post.id)
          setPendings((list) => applyStatus(list, post.id, status))
          if (status === 'published') void queryClient.invalidateQueries({ queryKey: POSTS_KEY })
        }
      } finally {
        running = false
      }
    }, checkIntervalMs)
    return () => clearInterval(timer)
  }, [checkIntervalMs, queryClient])

  const value = useMemo(() => ({ pendings, add, replace, prune }), [pendings, add, replace, prune])
  return <PendingPostsContext.Provider value={value}>{children}</PendingPostsContext.Provider>
}

export function usePendingPosts(): PendingPostsApi {
  const value = useContext(PendingPostsContext)
  if (!value) throw new Error('usePendingPosts must be used inside PendingPostsProvider')
  return value
}
