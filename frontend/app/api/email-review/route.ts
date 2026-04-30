import { NextRequest } from 'next/server'
import { proxyBackend } from '../_backend'

export const maxDuration = 300

export async function POST(req: NextRequest) {
  const body = await req.json()
  return proxyBackend('/email-review', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}
