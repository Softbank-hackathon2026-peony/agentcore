export const POSTS_KEY = ['posts'] as const

export function postKey(id: string) {
  return ['post', id] as const
}
