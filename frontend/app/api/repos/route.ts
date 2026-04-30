import { NextResponse } from 'next/server'

const BACKEND = process.env.BACKEND_URL

export async function GET() {
  if (!BACKEND) {
    return NextResponse.json({ error: 'BACKEND_URL is not configured' }, { status: 500 })
  }

  const upstream = await fetch(`${BACKEND}/repos`)
  const data = await upstream.json()
  return NextResponse.json(data, { status: upstream.status })
}
