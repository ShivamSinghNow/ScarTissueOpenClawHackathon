import { NextRequest, NextResponse } from 'next/server'

const BACKEND = process.env.BACKEND_URL

export async function POST(req: NextRequest) {
  if (!BACKEND) {
    return NextResponse.json({ error: 'BACKEND_URL is not configured' }, { status: 500 })
  }

  const body = await req.json()
  const upstream = await fetch(`${BACKEND}/index`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const data = await upstream.json()
  return NextResponse.json(data, { status: upstream.status })
}
