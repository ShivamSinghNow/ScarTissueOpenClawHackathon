'use client'

import { useState } from 'react'

interface RepoStatus {
  repo: string
  incidents: number
  status: 'indexed' | 'indexing' | 'error'
}

export default function IndexingStatus() {
  const [repos, setRepos] = useState<RepoStatus[]>([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleIndex(e: React.FormEvent) {
    e.preventDefault()
    if (!input.trim()) return
    const repo = input.trim()
    setLoading(true)
    setError(null)
    setRepos((prev) => [...prev.filter((r) => r.repo !== repo), { repo, incidents: 0, status: 'indexing' }])
    try {
      const res = await fetch('/api/index', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ repo }),
      })
      if (!res.ok) throw new Error(`Server error ${res.status}`)
      const data = await res.json()
      setRepos((prev) =>
        prev.map((r) =>
          r.repo === repo
            ? { repo, incidents: data.incidents_found, status: 'indexed' }
            : r
        )
      )
      setInput('')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setRepos((prev) =>
        prev.map((r) => (r.repo === repo ? { ...r, status: 'error' } : r))
      )
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
      <form onSubmit={handleIndex} style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="owner/repo"
          style={{
            backgroundColor: 'var(--color-surface-2)',
            border: '1px solid var(--color-border)',
            borderRadius: '4px',
            padding: '6px 10px',
            fontSize: '12px',
            color: 'var(--color-fg)',
            fontFamily: 'inherit',
            outline: 'none',
            width: '100%',
          }}
        />
        <button
          type="submit"
          disabled={loading || !input.trim()}
          style={{
            backgroundColor: 'var(--color-surface-2)',
            border: '1px solid var(--color-border)',
            borderRadius: '4px',
            padding: '6px',
            fontSize: '12px',
            color: loading ? 'var(--color-fg-muted)' : 'var(--color-fg)',
            fontFamily: 'inherit',
            cursor: loading || !input.trim() ? 'not-allowed' : 'pointer',
            opacity: loading || !input.trim() ? 0.5 : 1,
            transition: 'opacity 0.15s',
          }}
        >
          {loading ? 'Indexing…' : 'Index repo'}
        </button>
      </form>

      {error && (
        <p style={{ fontSize: '11px', color: 'var(--color-warn-high)', margin: 0 }}>{error}</p>
      )}

      {repos.length === 0 ? (
        <p style={{ fontSize: '11px', color: 'var(--color-fg-muted)', margin: 0 }}>
          No repos indexed yet.
        </p>
      ) : (
        <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: '8px' }}>
          {repos.map((r) => (
            <li key={r.repo}>
              <div
                style={{
                  fontSize: '11px',
                  color: 'var(--color-fg)',
                  fontFamily: 'inherit',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
              >
                {r.repo}
              </div>
              <div style={{ fontSize: '10px', color: 'var(--color-fg-muted)' }}>
                {r.status === 'indexing'
                  ? 'indexing…'
                  : r.status === 'error'
                  ? 'error'
                  : `${r.incidents} incidents`}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
