import { useQuery } from '@tanstack/react-query'
import { authApi } from '../api/auth'

export const ME_KEY = ['me'] as const

export function useMe() {
  return useQuery({ queryKey: ME_KEY, queryFn: authApi.me, staleTime: 60_000, retry: false })
}
