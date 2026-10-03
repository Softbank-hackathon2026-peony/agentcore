import { ApiError } from './client'

const MESSAGES: Record<string, string> = {
  INVALID_CREDENTIALS: '이메일 또는 비밀번호가 올바르지 않습니다.',
  TOO_MANY_ATTEMPTS: '로그인 시도가 너무 많습니다. 잠시 후 다시 시도하세요.',
  RATE_LIMITED: '요청이 너무 많습니다. 잠시 후 다시 시도하세요.',
  QUEUE_FULL: '지금 글이 너무 많이 몰리고 있습니다. 잠시 후 다시 시도하세요.',
  UNAVAILABLE: '서비스가 일시적으로 불안정합니다. 잠시 후 다시 시도하세요.',
  EMAIL_TAKEN: '이미 가입된 이메일입니다.',
  NICKNAME_TAKEN: '이미 사용 중인 닉네임입니다.',
  FORBIDDEN: '권한이 없거나 비밀번호가 올바르지 않습니다.',
  UNAUTHORIZED: '로그인이 필요합니다.',
  NOT_FOUND: '찾을 수 없습니다.',
  VALIDATION_ERROR: '입력값을 확인해 주세요.',
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return MESSAGES[err.code] ?? `오류가 발생했습니다 (${err.status}).`
  return '네트워크 오류가 발생했습니다.'
}
