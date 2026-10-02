import type { PostStatus } from '../api/types'

export type PendingState = 'pending' | 'delayed' | 'failed'

export interface PendingPost {
  id: string
  title: string
  body: string
  author_nickname: string
  submittedAt: number
  state: PendingState
}

export const STATUS_CHECK_AFTER_MS = 30_000

export const PENDING_LABEL: Record<PendingState, string> = {
  pending: '게시 중…',
  delayed: '지연 중',
  failed: '게시 실패',
}

export function upsertPending(list: PendingPost[], post: PendingPost): PendingPost[] {
  return [post, ...list.filter((p) => p.id !== post.id)]
}

export function visiblePending(list: PendingPost[], serverIds: Set<string>): PendingPost[] {
  return list.filter((p) => !serverIds.has(p.id))
}

export function dueForStatusCheck(list: PendingPost[], now: number): PendingPost[] {
  return list.filter((p) => p.state !== 'failed' && now - p.submittedAt >= STATUS_CHECK_AFTER_MS)
}

export function applyStatus(list: PendingPost[], id: string, status: PostStatus['status']): PendingPost[] {
  if (status === 'published') return list.filter((p) => p.id !== id)
  const state: PendingState = status === 'failed' ? 'failed' : 'delayed'
  return list.map((p) => (p.id === id ? { ...p, state } : p))
}
