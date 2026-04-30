import { proxyBackend } from '../_backend'

export const maxDuration = 300

export async function GET() {
  return proxyBackend('/repos')
}
