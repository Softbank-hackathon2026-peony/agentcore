import { MutationCache, QueryClient } from '@tanstack/react-query'
import { ApiError } from './api/client'
import { ME_KEY } from './auth/useMe'

export function isUnauthorized(err: unknown): boolean {
  return err instanceof ApiError && err.status === 401 && err.code === 'UNAUTHORIZED'
}

export function createQueryClient(onUnauthorized: () => void): QueryClient {
  const client: QueryClient = new QueryClient({
    // 429/503 재시도는 api 클라이언트가 이미 하므로 Query 차원의 재시도는 끈다.
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    mutationCache: new MutationCache({
      onError: (err) => {
        if (!isUnauthorized(err)) return
        client.setQueryData(ME_KEY, null)
        onUnauthorized()
      },
    }),
  })
  return client
}
