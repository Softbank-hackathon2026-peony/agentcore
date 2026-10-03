import { Route, Routes } from 'react-router-dom'
import { LoginPage } from './auth/LoginPage'
import { MePage } from './auth/MePage'
import { RequireAuth } from './auth/RequireAuth'
import { SignupPage } from './auth/SignupPage'
import { ListPage } from './board/ListPage'
import { PendingPostsProvider } from './board/PendingPostsContext'
import { PostPage } from './board/PostPage'
import { WritePage } from './board/WritePage'
import { Layout } from './ui/Layout'
import { ToastProvider } from './ui/Toast'

export function App() {
  return (
    <ToastProvider>
      <PendingPostsProvider>
        <Layout>
          <Routes>
            <Route path="/" element={<ListPage />} />
            <Route path="/posts/:id" element={<PostPage />} />
            <Route path="/write" element={<RequireAuth><WritePage /></RequireAuth>} />
            <Route path="/login" element={<LoginPage />} />
            <Route path="/signup" element={<SignupPage />} />
            <Route path="/me" element={<RequireAuth><MePage /></RequireAuth>} />
            <Route path="*" element={<p>페이지를 찾을 수 없습니다.</p>} />
          </Routes>
        </Layout>
      </PendingPostsProvider>
    </ToastProvider>
  )
}
