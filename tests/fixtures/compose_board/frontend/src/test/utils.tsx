import { QueryClient } from '@tanstack/react-query'
import { useLocation } from 'react-router-dom'
import type { User } from '../api/types'
import { ME_KEY } from '../auth/useMe'

export const TEST_USER: User = {
  id: '01920000-0000-7000-8000-000000000001',
  email: 'user@example.com',
  nickname: '테스터',
}

export function createTestQueryClient(me?: User | null): QueryClient {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  if (me !== undefined) client.setQueryData(ME_KEY, me)
  return client
}

export function LocationDisplay() {
  const location = useLocation()
  return <div data-testid="location">{location.pathname + location.search}</div>
}
