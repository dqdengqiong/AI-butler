import { appConfig } from '@/config'

export class ApiError extends Error {
  constructor(
    readonly statusCode: number,
    readonly responseBody: unknown,
    readonly code = 'API_ERROR',
    readonly retryable = false,
  ) {
    super(readErrorMessage(responseBody) ?? `API request failed with status ${statusCode}`)
    this.name = 'ApiError'
  }
}

interface ErrorEnvelope {
  error?: { code?: unknown; message?: unknown; retryable?: unknown }
}

function readErrorMessage(value: unknown): string | null {
  if (typeof value !== 'object' || value === null) return null
  const envelope = value as ErrorEnvelope
  return typeof envelope.error?.message === 'string' ? envelope.error.message : null
}

function readErrorCode(value: unknown): string {
  if (typeof value !== 'object' || value === null) return 'API_ERROR'
  const envelope = value as ErrorEnvelope
  return typeof envelope.error?.code === 'string' ? envelope.error.code : 'API_ERROR'
}

export interface RequestOptions {
  path: string
  method?: UniApp.RequestOptions['method'] | 'PATCH'
  data?: UniApp.RequestOptions['data']
  accessToken?: string | null
  headers?: Record<string, string>
}

export function request<T>(options: RequestOptions): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json', ...options.headers }
  if (options.accessToken) {
    headers.Authorization = `Bearer ${options.accessToken}`
  }

  return new Promise<T>((resolve, reject) => {
    uni.request({
      url: `${appConfig.apiBaseUrl}${options.path}`,
      method: (options.method ?? 'GET') as UniApp.RequestOptions['method'],
      data: options.data,
      header: headers,
      success(response) {
        if (response.statusCode >= 200 && response.statusCode < 300) {
          resolve(response.data as T)
          return
        }
        const retryable =
          typeof response.data === 'object' &&
          response.data !== null &&
          (response.data as ErrorEnvelope).error?.retryable === true
        reject(
          new ApiError(response.statusCode, response.data, readErrorCode(response.data), retryable),
        )
      },
      fail(error) {
        reject(new Error(error.errMsg || 'network request failed'))
      },
    })
  })
}
