import { ApiError, api } from './client'
import type { User } from './types'

export const authApi = {
  signup: (input: { email: string; password: string; nickname: string }) =>
    api.request<User>('/api/auth/signup', { method: 'POST', body: input }),
  login: (input: { email: string; password: string }) =>
    api.request<User>('/api/auth/login', { method: 'POST', body: input }),
  logout: () => api.request<void>('/api/auth/logout', { method: 'POST' }),
  me: async (): Promise<User | null> => {
    try {
      return await api.request<User>('/api/auth/me')
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return null
      throw err
    }
  },
  deleteAccount: (password: string) => api.request<void>('/api/auth/me', { method: 'DELETE', body: { password } }),
}
