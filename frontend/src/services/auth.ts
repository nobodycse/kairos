import request from './request'

export interface LoginRequest {
  username: string
  password: string
}

export interface LoginResponse {
  access_token: string
  token_type: 'bearer'
  expires_in: number
}

/** POST /api/v1/auth/login（design.md §3.1） */
export function login(data: LoginRequest): Promise<LoginResponse> {
  return request.post('/auth/login', data) as Promise<LoginResponse>
}
