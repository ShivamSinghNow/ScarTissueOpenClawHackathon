import { NextResponse } from 'next/server'

const DEFAULT_BACKEND_URL = 'https://shivamsinghnow--scartissue-backend-fastapi-app.modal.run'
const REQUEST_TIMEOUT_MS = 240_000

function backendUrl(): string {
  return (process.env.BACKEND_URL || DEFAULT_BACKEND_URL).replace(/\/$/, '')
}

async function parseUpstreamBody(response: Response): Promise<unknown> {
  const contentType = response.headers.get('content-type') || ''
  if (contentType.includes('application/json')) {
    return response.json()
  }

  const text = await response.text()
  return {
    detail: text || `Backend returned ${response.status} ${response.statusText}`,
  }
}

function errorMessage(error: unknown): string {
  if (error && typeof error === 'object' && 'name' in error && error.name === 'TimeoutError') {
    return 'ScarTissue review is still running. Please try again in a minute.'
  }
  if (error instanceof Error) return error.message
  return String(error)
}

export async function proxyBackend(path: string, init?: RequestInit): Promise<NextResponse> {
  try {
    const upstream = await fetch(`${backendUrl()}${path}`, {
      ...init,
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    })
    const data = await parseUpstreamBody(upstream)
    return NextResponse.json(data, { status: upstream.status })
  } catch (error) {
    return NextResponse.json(
      { detail: errorMessage(error) || 'Unable to reach ScarTissue backend.' },
      { status: 502 },
    )
  }
}
