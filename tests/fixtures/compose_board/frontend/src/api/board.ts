import { api } from './client'
import type { PostPage, PostStatus } from './types'

export const boardApi = {
  list: (cursor?: string | null) =>
    api.request<PostPage>(cursor ? `/api/board/posts?cursor=${encodeURIComponent(cursor)}` : '/api/board/posts'),
  get: (id: string) => api.request<PostStatus>(`/api/board/posts/${encodeURIComponent(id)}`),
  create: (input: { title: string; body: string }, idempotencyKey: string) =>
    api.request<{ id: string; status: 'pending' }>('/api/board/posts', {
      method: 'POST',
      body: input,
      headers: { 'Idempotency-Key': idempotencyKey },
    }),
  remove: (id: string) => api.request<void>(`/api/board/posts/${encodeURIComponent(id)}`, { method: 'DELETE' }),
}
