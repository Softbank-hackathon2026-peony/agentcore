export interface User {
  id: string
  email: string
  nickname: string
}

export interface Post {
  id: string
  author_id: string | null
  author_nickname: string
  title: string
  body: string
  created_at: string
}

export interface PostPage {
  items: Post[]
  next_cursor: string | null
}

export type PostStatus =
  | { status: 'published'; post: Post }
  | { status: 'pending' }
  | { status: 'failed' }
