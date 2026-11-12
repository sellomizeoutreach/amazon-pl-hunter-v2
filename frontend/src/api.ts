// API client. Production fetch uses relative /api paths (FastAPI serves the
// built frontend). Dev uses the Vite proxy (see vite.config.ts) or an
// explicit VITE_API_URL. Token is prompted once and kept in localStorage.

const TOKEN_KEY = 'plh_api_token'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token.trim())
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

const API_BASE = (import.meta.env.VITE_API_URL as string | undefined) ?? ''

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken()
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      ...(init.body ? { 'Content-Type': 'application/json' } : {}),
      ...(token ? { 'X-API-Token': token } : {}),
      ...(init.headers ?? {}),
    },
  })
  if (res.status === 401) {
    throw new ApiError(401, 'Unauthorized — the API token is missing or wrong. Re-enter it below.')
  }
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = body.detail ?? body.error ?? detail
    } catch {
      // keep statusText
    }
    throw new ApiError(res.status, typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return res.json() as Promise<T>
}

export async function apiDownload(path: string, filename: string): Promise<void> {
  const token = getToken()
  const res = await fetch(`${API_BASE}${path}`, {
    headers: token ? { 'X-API-Token': token } : {},
  })
  if (res.status === 401) {
    throw new ApiError(401, 'Unauthorized — the API token is missing or wrong. Re-enter it below.')
  }
  if (!res.ok) {
    throw new ApiError(res.status, `Export failed (${res.status} ${res.statusText})`)
  }
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

export function buildQuery(params: Record<string, string | number | undefined>): string {
  const q = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== '') q.set(k, String(v))
  }
  const s = q.toString()
  return s ? `?${s}` : ''
}
