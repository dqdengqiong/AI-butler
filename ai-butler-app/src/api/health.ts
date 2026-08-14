import type { components } from './generated/schema'
import { request } from './client'

export type LiveResponse = components['schemas']['LiveResponse']
export type ReadyResponse = components['schemas']['ReadyResponse']

export function getLive(): Promise<LiveResponse> {
  return request<LiveResponse>({ path: '/health/live' })
}

export function getReady(): Promise<ReadyResponse> {
  return request<ReadyResponse>({ path: '/health/ready' })
}
