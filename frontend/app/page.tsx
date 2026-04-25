'use client'

import { useState, useEffect, useRef, useCallback } from 'react'

/* ══════════════════════════════════════════════════════════
   TYPES
══════════════════════════════════════════════════════════ */

type View = 'landing' | 'empty' | 'loading' | 'results'
type Severity = 'low' | 'medium' | 'high'

interface Incident {
  commit_sha: string
  commit_message: string
  commit_date: string
  author: string
  files_changed: string[]
  functions_changed: string[]
  fix_diff: string
  buggy_parent_sha: string
  issue_refs: number[]
  symptom_summary: string | null
}

interface Warning {
  pr_file: string
  pr_hunk: string           // the @@ header line only
  matched_incident: Incident | null
  severity: Severity
  explanation: string
  confidence: number        // 0.0–1.0
  proposed_fix: string | null
}

interface ReviewResponse {
  pr_url: string
  pr_repo: string
  upstream_repo: string | null
  pr_title: string
  pr_author: string
  warnings: Warning[]
  total_warnings: number
  duration_seconds: number
}

interface PostToGithubResponse {
  pr_url: string
  review_url: string | null
  total_comments: number
  summary_comment: string
}

interface IndexedRepo {
  repo: string
  incidents: number
  status: 'indexed' | 'indexing' | 'error'
  last_indexed: string | null
  max_commits?: number
  error?: string
}

/* ══════════════════════════════════════════════════════════
   CONSTANTS & UTILITIES
══════════════════════════════════════════════════════════ */

const EXAMPLE_PRS = [
  'https://github.com/langchain-ai/langchain/pull/24817',
  'https://github.com/langchain-ai/langchain/pull/23991',
  'https://github.com/langchain-ai/langchain/pull/22104',
]

const LOADING_STEPS = [
  'Fetching PR diff…',
  'Searching scar tissue index…',
  'Cross-referencing codebase…',
  'Reviewing with Claude…',
]

const REPO_RE = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/

function normalizeRepo(repo: string): string {
  return repo.trim().replace(/^https:\/\/github\.com\//, '').replace(/\/$/, '')
}

function repoFromPrUrl(url: string): string | null {
  const match = url.match(/github\.com\/([^/\s]+)\/([^/\s]+)\/pull\/\d+/)
  return match ? `${match[1]}/${match[2]}` : null
}

function formatRelativeTime(value: string | null): string {
  if (!value) return 'unknown'
  const then = new Date(value).getTime()
  if (Number.isNaN(then)) return 'unknown'
  const seconds = Math.max(1, Math.floor((Date.now() - then) / 1000))
  if (seconds < 60) return 'just now'
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? '' : 's'} ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} hour${hours === 1 ? '' : 's'} ago`
  const days = Math.floor(hours / 24)
  return `${days} day${days === 1 ? '' : 's'} ago`
}

function estimateIndexMinutes(maxCommits: number): number {
  return Math.max(1, Math.ceil(maxCommits / 300))
}

const SEV: Record<Severity, { label: string; dot: string; text: string; bg: string; border: string }> = {
  high:   { label: 'HIGH',   dot: '#ef4444', text: '#ef4444', bg: 'rgba(239,68,68,.08)',   border: 'rgba(239,68,68,.2)' },
  medium: { label: 'MEDIUM', dot: '#f59e0b', text: '#f59e0b', bg: 'rgba(245,158,11,.08)', border: 'rgba(245,158,11,.2)' },
  low:    { label: 'LOW',    dot: '#6a9a6a', text: '#6a9a6a', bg: 'rgba(100,150,100,.07)', border: 'rgba(100,150,100,.18)' },
}

const MCP_CONFIGS: Record<string, { lang: string; path: string; content: string }> = {
  claude: {
    lang: 'json',
    path: '~/.claude/mcp.json',
    content: `{
  "mcpServers": {
    "scartissue": {
      "command": "scartissue-mcp",
      "env": {
        "ANTHROPIC_API_KEY": "sk-ant-...",
        "NIA_API_KEY": "nia_...",
        "GITHUB_TOKEN": "ghp_...",
        "CHROMA_PERSIST_DIR": "/absolute/path/to/scartissue/backend/chroma_db"
      }
    }
  }
}`,
  },
  codex: {
    lang: 'toml',
    path: '~/.codex/mcp_servers.toml',
    content: `[scartissue]
command = "scartissue-mcp"

[scartissue.env]
ANTHROPIC_API_KEY = "sk-ant-..."
NIA_API_KEY = "nia_..."
GITHUB_TOKEN = "ghp_..."
CHROMA_PERSIST_DIR = "/absolute/path/to/scartissue/backend/chroma_db"`,
  },
  gemini: {
    lang: 'json',
    path: '~/.gemini/settings.json',
    content: `{
  "mcpServers": {
    "scartissue": {
      "command": "scartissue-mcp",
      "args": [],
      "env": {
        "ANTHROPIC_API_KEY": "sk-ant-...",
        "NIA_API_KEY": "nia_...",
        "GITHUB_TOKEN": "ghp_...",
        "CHROMA_PERSIST_DIR": "/absolute/path/to/scartissue/backend/chroma_db"
      }
    }
  }
}`,
  },
}

function extractLineRange(hunkHeader: string): string {
  const m = hunkHeader.match(/@@ [+-]\d+(?:,\d+)? [+-](\d+)(?:,(\d+))? @@/)
  if (!m) return ''
  const start = parseInt(m[1])
  const len = m[2] ? parseInt(m[2]) : 1
  return len <= 1 ? `${start}` : `${start}–${start + len - 1}`
}

function colorJsonLine(line: string, lang: string): string {
  if (lang === 'toml') {
    if (line.startsWith('#')) return '#404040'
    if (line.startsWith('[')) return '#9090c8'
    if (line.includes('=')) {
      const isVal = /sk-ant|nia_|ghp_|scartissue-mcp/.test(line)
      return isVal ? '#a8d8a8' : '#c8a8a8'
    }
    return '#4a4a5a'
  }
  const isSection = /"mcpServers"|"scartissue"|"env"|"args"/.test(line)
  const isVal = /sk-ant|nia_|ghp_|scartissue-mcp/.test(line)
  return isSection ? '#9090c8' : isVal ? '#a8d8a8' : /"[^"]+":/.test(line) ? '#c8a8a8' : '#4a4a5a'
}

/* ══════════════════════════════════════════════════════════
   SVG ICON
══════════════════════════════════════════════════════════ */

type IconName = 'gitBranch' | 'search' | 'shieldAlert' | 'copy' | 'check' | 'arrowRight' |
  'externalLink' | 'chevronRight' | 'back' | 'terminal' | 'browser' | 'github' | 'plus' | 'x'

const ICON_PATHS: Record<IconName, React.ReactNode> = {
  gitBranch:    <><circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M6 9a9 9 0 0 0 9 9"/><line x1="6" y1="9" x2="6" y2="21"/></>,
  search:       <><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></>,
  shieldAlert:  <><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="M12 8v4M12 16h.01"/></>,
  copy:         <><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></>,
  check:        <path d="M20 6L9 17l-5-5"/>,
  arrowRight:   <><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></>,
  externalLink: <><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></>,
  chevronRight: <polyline points="9 18 15 12 9 6"/>,
  back:         <><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></>,
  terminal:     <><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></>,
  browser:      <><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/><path d="M2 7h20"/></>,
  github:       <path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"/>,
  plus:         <><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></>,
  x:            <><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></>,
}

function Icon({ name, size = 16, stroke = 'currentColor', strokeWidth = 1.5 }: {
  name: IconName; size?: number; stroke?: string; strokeWidth?: number
}) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
      stroke={stroke} strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round">
      {ICON_PATHS[name]}
    </svg>
  )
}

/* ══════════════════════════════════════════════════════════
   LANDING — shared logo
══════════════════════════════════════════════════════════ */

function LandingLogo() {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
        <rect x="1" y="8"    width="16" height="1.8" rx="0.9"  fill="#ef4444" opacity="0.9"/>
        <rect x="1" y="4"   width="10" height="1.1" rx="0.55" fill="#ef4444" opacity="0.4"/>
        <rect x="1" y="13"  width="13" height="1.1" rx="0.55" fill="#ef4444" opacity="0.4"/>
        <rect x="1" y="1.5" width="5"  height="0.8" rx="0.4"  fill="#ef4444" opacity="0.18"/>
        <rect x="1" y="15.7" width="7" height="0.8" rx="0.4"  fill="#ef4444" opacity="0.18"/>
      </svg>
      <span style={{ fontWeight: 600, fontSize: 14, letterSpacing: '-0.025em', color: '#e5e5e5' }}>ScarTissue</span>
    </div>
  )
}

/* ══════════════════════════════════════════════════════════
   LANDING — nav
══════════════════════════════════════════════════════════ */

function LandingNav({ onLaunch }: { onLaunch: () => void }) {
  const navRef = useRef<HTMLElement>(null)
  useEffect(() => {
    const onScroll = () => {
      if (!navRef.current) return
      if (window.scrollY > 20) navRef.current.classList.add('nav-scrolled')
      else navRef.current.classList.remove('nav-scrolled')
    }
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [])
  return (
    <nav ref={navRef} style={{ position: 'fixed', top: 0, left: 0, right: 0, zIndex: 100, background: 'rgba(10,10,10,0.6)', backdropFilter: 'blur(12px)', borderBottom: '1px solid transparent', height: 48, display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '0 32px', transition: 'background .3s, border-color .3s' }}>
      <LandingLogo/>
      <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
        <a href="#install" style={{ fontSize: 13, color: '#555555', textDecoration: 'none', transition: 'color .12s' }}
          onMouseEnter={e => (e.currentTarget.style.color = '#aaa')}
          onMouseLeave={e => (e.currentTarget.style.color = '#555555')}>Install</a>
        <a href="#demo" style={{ fontSize: 13, color: '#555555', textDecoration: 'none', transition: 'color .12s' }}
          onMouseEnter={e => (e.currentTarget.style.color = '#aaa')}
          onMouseLeave={e => (e.currentTarget.style.color = '#555555')}>Demo</a>
        <button className="btn-primary" onClick={onLaunch} style={{ padding: '7px 16px', fontSize: 13, display: 'flex', alignItems: 'center', gap: 6 }}>
          Launch App <Icon name="arrowRight" size={13}/>
        </button>
      </div>
    </nav>
  )
}

/* ══════════════════════════════════════════════════════════
   LANDING — hero
══════════════════════════════════════════════════════════ */

function HeroSection({ onLaunch }: { onLaunch: () => void }) {
  const [scrolled, setScrolled] = useState(false)
  const [mouse, setMouse] = useState({ x: 0, y: 0 })

  useEffect(() => {
    const onScroll = () => { if (window.scrollY > 80) setScrolled(true) }
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [])

  useEffect(() => {
    const onMove = (e: MouseEvent) => setMouse({
      x: (e.clientX / window.innerWidth - .5) * 2,
      y: (e.clientY / window.innerHeight - .5) * 2,
    })
    window.addEventListener('mousemove', onMove, { passive: true })
    return () => window.removeEventListener('mousemove', onMove)
  }, [])

  const px = (amt: number) => ({ transform: `translate(${mouse.x * amt}px,${mouse.y * amt}px)`, transition: 'transform 400ms ease-out' })

  return (
    <section style={{ minHeight: '100vh', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', textAlign: 'center', padding: '80px 32px 60px', position: 'relative', overflow: 'hidden' }}>
      {/* Ambient glows */}
      <div style={{ position: 'absolute', top: '-10%', left: '-5%', width: 500, height: 500, borderRadius: '50%', background: 'radial-gradient(circle,rgba(239,68,68,.07) 0%,transparent 70%)', pointerEvents: 'none', animation: 'glow1 18s ease-in-out infinite' }}/>
      <div style={{ position: 'absolute', bottom: '-15%', right: '-8%', width: 600, height: 600, borderRadius: '50%', background: 'radial-gradient(circle,rgba(239,68,68,.05) 0%,transparent 70%)', pointerEvents: 'none', animation: 'glow2 22s ease-in-out infinite' }}/>
      <div style={{ position: 'absolute', top: '60%', left: '10%', width: 300, height: 300, borderRadius: '50%', background: 'radial-gradient(circle,rgba(239,68,68,.04) 0%,transparent 70%)', pointerEvents: 'none', animation: 'glow3 16s ease-in-out infinite' }}/>
      {/* Grid */}
      <div style={{ position: 'absolute', inset: 0, backgroundImage: 'linear-gradient(rgba(255,255,255,.018) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.018) 1px,transparent 1px)', backgroundSize: '40px 40px', pointerEvents: 'none' }}/>
      {/* Vignette */}
      <div style={{ position: 'absolute', inset: 0, background: 'radial-gradient(ellipse 70% 60% at 50% 50%,transparent 40%,#0a0a0a 100%)', pointerEvents: 'none' }}/>
      {/* Drifting blob */}
      <div className="parallax-layer" style={{ ...px(8), position: 'absolute', top: '30%', left: '50%', marginLeft: -300, marginTop: -300, width: 600, height: 600, borderRadius: '50%', background: 'radial-gradient(circle,rgba(239,68,68,.2) 0%,transparent 65%)', pointerEvents: 'none', animation: 'blobDrift 20s ease-in-out infinite', filter: 'blur(40px)' }}/>

      <div style={{ position: 'relative', maxWidth: 680 }}>
        <div className="parallax-layer hero-badge" style={{ ...px(2), marginBottom: 8, display: 'inline-block' }}>
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, background: '#111111', border: '1px solid #1a1a1a', borderRadius: 5, padding: '4px 12px', fontSize: 11.5, color: '#555555', fontFamily: 'var(--font-fira-code, monospace)' }}>
            <span style={{ width: 5, height: 5, borderRadius: '50%', background: '#ef4444', flexShrink: 0 }}/>
            v0.1.0 — open beta
          </span>
        </div>

        <h1 className="parallax-layer" style={{ ...px(4), fontSize: 'clamp(36px,5vw,64px)', fontWeight: 700, letterSpacing: '-0.04em', lineHeight: 1.1, color: '#e5e5e5', margin: '18px 0 0' }}>
          <span className="hero-line1" style={{ display: 'block' }}>Every codebase</span>
          <span className="hero-line2" style={{ display: 'block' }}>remembers its bugs.</span>
        </h1>
        <p className="parallax-layer hero-sub" style={{ ...px(3), fontSize: 'clamp(16px,2vw,20px)', color: '#555555', marginTop: 14, fontWeight: 400, letterSpacing: '-0.01em' }}>
          Now your agent does too.
        </p>

        <div className="parallax-layer hero-btns" style={{ ...px(3), display: 'flex', gap: 10, justifyContent: 'center', marginTop: 28 }}>
          <button className="btn-primary" onClick={onLaunch} style={{ display: 'flex', alignItems: 'center', gap: 7, padding: '11px 22px', fontSize: 14 }}>
            Open Web Interface <Icon name="arrowRight" size={14}/>
          </button>
          <button className="btn-outline" onClick={() => document.getElementById('install')?.scrollIntoView({ behavior: 'smooth' })} style={{ display: 'flex', alignItems: 'center', gap: 7, padding: '11px 22px', fontSize: 14 }}>
            Install MCP Server
          </button>
        </div>

        <p className="hero-proof" style={{ marginTop: 20, fontSize: 12, color: '#333333', letterSpacing: '0.01em' }}>
          996 incidents indexed across LangChain&nbsp;&nbsp;·&nbsp;&nbsp;3 repos analyzed&nbsp;&nbsp;·&nbsp;&nbsp;catches regressions before they merge
        </p>
      </div>

      <div className={`scroll-indicator${scrolled ? ' hidden' : ''}`} style={{ position: 'absolute', bottom: 32, left: '50%', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4 }}>
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
          <path d="M4 6l4 4 4-4" stroke="#333333" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"/>
        </svg>
      </div>
    </section>
  )
}

/* ══════════════════════════════════════════════════════════
   LANDING — how it works
══════════════════════════════════════════════════════════ */

function HowItWorks() {
  const steps = [
    { icon: 'gitBranch' as IconName,    title: 'Index',  desc: "Mine your repo's full git history. Every fix commit becomes a data point in your scar tissue index." },
    { icon: 'search' as IconName,       title: 'Review', desc: 'Paste any PR URL. ScarTissue cross-references every hunk against known regression patterns.' },
    { icon: 'shieldAlert' as IconName,  title: 'Warn',   desc: 'Receive targeted warnings with matched prior commits, severity ratings, and explanations.' },
  ]
  return (
    <section style={{ padding: '80px 32px', maxWidth: 900, margin: '0 auto' }}>
      <div style={{ textAlign: 'center', marginBottom: 40 }}>
        <h2 style={{ fontSize: 24, fontWeight: 600, letterSpacing: '-0.025em', color: '#e5e5e5', margin: 0 }}>How it works</h2>
      </div>
      <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
        {steps.map((s, i) => (
          <div key={i} className="step-card" style={{ flex: '1 1 240px', minWidth: 200 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
              <div className="step-icon" style={{ width: 30, height: 30, background: 'rgba(239,68,68,.08)', border: '1px solid rgba(239,68,68,.15)', borderRadius: 6, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
                <Icon name={s.icon} size={14} stroke="#ef4444" strokeWidth={1.5}/>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <span style={{ fontFamily: 'var(--font-fira-code, monospace)', fontSize: 10, color: '#333333' }}>{String(i + 1).padStart(2, '0')}</span>
                <span style={{ fontSize: 15, fontWeight: 600, color: '#e5e5e5', letterSpacing: '-0.015em' }}>{s.title}</span>
              </div>
            </div>
            <p style={{ fontSize: 12.5, color: '#555555', lineHeight: 1.65, margin: 0 }}>{s.desc}</p>
          </div>
        ))}
      </div>
    </section>
  )
}

/* ══════════════════════════════════════════════════════════
   LANDING — install section
══════════════════════════════════════════════════════════ */

function InstallSection({ onLaunch }: { onLaunch: () => void }) {
  const [tab, setTab] = useState<'claude' | 'codex' | 'gemini'>('claude')
  const [copied, setCopied] = useState(false)
  const [btnAnim, setBtnAnim] = useState(false)

  const copy = () => {
    navigator.clipboard?.writeText(MCP_CONFIGS[tab].content)
    setBtnAnim(true); setTimeout(() => setBtnAnim(false), 200)
    setCopied(true); setTimeout(() => setCopied(false), 2000)
  }
  useEffect(() => setCopied(false), [tab])

  const tabs = [
    { id: 'claude' as const, label: 'Claude Code' },
    { id: 'codex'  as const, label: 'Codex CLI' },
    { id: 'gemini' as const, label: 'Gemini CLI' },
  ]

  return (
    <section id="install" style={{ padding: '80px 32px', maxWidth: 900, margin: '0 auto' }}>
      <div style={{ textAlign: 'center', marginBottom: 40 }}>
        <h2 style={{ fontSize: 24, fontWeight: 600, letterSpacing: '-0.025em', color: '#e5e5e5', margin: 0 }}>Install</h2>
      </div>
      <div style={{ display: 'flex', gap: 20, flexWrap: 'wrap' }}>
        {/* Left: tabbed config */}
        <div style={{ flex: '1 1 340px', minWidth: 280 }}>
          <div style={{ display: 'flex', borderBottom: '1px solid #1a1a1a', marginBottom: 0 }}>
            {tabs.map(t => (
              <button key={t.id} className={`itab${tab === t.id ? ' itactive' : ''}`} onClick={() => setTab(t.id)}>
                {t.label}
                <div className="itab-bar"/>
              </button>
            ))}
          </div>
          <div style={{ fontFamily: 'var(--font-fira-code, monospace)', fontSize: 11, color: '#444444', background: '#090909', borderLeft: '1px solid #1a1a1a', borderRight: '1px solid #1a1a1a', padding: '9px 14px 7px' }}>
            {MCP_CONFIGS[tab].path}
          </div>
          <div className="code-block" style={{ position: 'relative', borderTopLeftRadius: 0, borderTopRightRadius: 0, borderTop: 'none', marginTop: 0 }} data-language={MCP_CONFIGS[tab].lang}>
            <div className="scanlines"/>
            <button onClick={copy} className={btnAnim ? 'copy-clicked' : ''} style={{ position: 'absolute', top: 10, right: 10, background: '#111111', border: '1px solid #252525', borderRadius: 5, padding: '4px 8px', cursor: 'pointer', color: copied ? '#4faa6a' : '#555555', fontSize: 11, fontFamily: 'var(--font-space-grotesk, sans-serif)', display: 'flex', alignItems: 'center', gap: 4, transition: 'color .15s', zIndex: 2 }}>
              <Icon name={copied ? 'check' : 'copy'} size={11} stroke="currentColor"/>
              {copied ? 'copied' : 'copy'}
            </button>
            <pre style={{ margin: 0, whiteSpace: 'pre', overflowX: 'auto', paddingRight: 60 }}>
              {MCP_CONFIGS[tab].content.split('\n').map((line, i) => (
                <span key={i} style={{ display: 'block', color: colorJsonLine(line, MCP_CONFIGS[tab].lang) }}>{line}</span>
              ))}
            </pre>
          </div>
          <p style={{ fontSize: 11.5, color: '#333333', marginTop: 10, lineHeight: 1.6 }}>Prerequisites: clone ScarTissue, run uv pip install -e . from backend/, then populate backend/.env. The scartissue-mcp command becomes available globally after install.</p>
        </div>

        {/* Right: web interface thumbnail */}
        <div style={{ flex: '1 1 260px', minWidth: 220, display: 'flex', flexDirection: 'column' }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: '#e5e5e5', marginBottom: 12, letterSpacing: '-0.01em' }}>Web Interface</div>
          <div onClick={onLaunch} style={{ background: '#080808', border: '1px solid #1a1a1a', borderRadius: 8, overflow: 'hidden', cursor: 'pointer', flex: 1, minHeight: 160, position: 'relative', transition: 'border-color .12s' }}
            onMouseEnter={e => (e.currentTarget.style.borderColor = '#2a2a2a')}
            onMouseLeave={e => (e.currentTarget.style.borderColor = '#1a1a1a')}>
            <div style={{ padding: '12px 0 0' }}>
              <div style={{ padding: '3px 12px 3px 4px', background: '#0f0f0f', borderBottom: '1px solid #1a1a1a', fontSize: 9, fontFamily: 'var(--font-fira-code, monospace)', color: '#252525' }}>langchain/callbacks/manager.py</div>
              {[
                { bg: 'transparent',           sign: ' ', signC: '#333',    code: '    async def on_llm_end(self, response, **kwargs):', codeC: '#444' },
                { bg: 'rgba(220,55,50,.08)',   sign: '−', signC: '#cc5050', code: '            await handler.aclose()',                  codeC: '#8a6060' },
                { bg: 'rgba(48,160,72,.08)',   sign: '+', signC: '#4faa6a', code: '            coros.append(handler.on_llm_end(...))',   codeC: '#608a60' },
                { bg: 'transparent',           sign: ' ', signC: '#333',    code: '    await asyncio.gather(*coros)',                    codeC: '#444' },
              ].map((l, i) => (
                <div key={i} style={{ display: 'flex', background: l.bg, padding: '0 8px', gap: 4, alignItems: 'center' }}>
                  <span style={{ width: 3, background: '#ef4444', alignSelf: 'stretch', flexShrink: 0, opacity: i === 1 || i === 2 ? 0.5 : 0 }}/>
                  <span style={{ fontFamily: 'var(--font-fira-code, monospace)', fontSize: 9.5, color: l.signC, width: 12, textAlign: 'center', flexShrink: 0 }}>{l.sign}</span>
                  <span style={{ fontFamily: 'var(--font-fira-code, monospace)', fontSize: 9.5, color: l.codeC, whiteSpace: 'pre' }}>{l.code}</span>
                </div>
              ))}
            </div>
            <div style={{ position: 'absolute', right: 8, top: 40, background: '#111111', border: '1px solid rgba(239,68,68,.25)', borderRadius: 5, padding: '7px 9px', width: 140 }}>
              <div style={{ display: 'inline-flex', alignItems: 'center', gap: 3, background: 'rgba(239,68,68,.08)', border: '1px solid rgba(239,68,68,.2)', borderRadius: 3, padding: '1px 5px', marginBottom: 5 }}>
                <span style={{ width: 4, height: 4, borderRadius: '50%', background: '#ef4444' }}/>
                <span style={{ fontSize: 8, fontWeight: 700, color: '#ef4444', letterSpacing: '0.1em' }}>HIGH</span>
              </div>
              <div style={{ fontSize: 8.5, fontWeight: 500, color: '#c8c8d8', lineHeight: 1.4, marginBottom: 4 }}>Async iterator cleanup removed</div>
              <div style={{ fontFamily: 'var(--font-fira-code, monospace)', fontSize: 8, color: '#333333' }}>manager.py:350</div>
            </div>
            <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', opacity: 0, transition: 'opacity .15s', background: 'rgba(10,10,10,.6)' }}
              onMouseEnter={e => (e.currentTarget.style.opacity = '1')}
              onMouseLeave={e => (e.currentTarget.style.opacity = '0')}>
              <span style={{ fontSize: 12, fontWeight: 500, color: '#e5e5e5', display: 'flex', alignItems: 'center', gap: 5 }}>
                <Icon name="externalLink" size={13}/> Launch App
              </span>
            </div>
          </div>
          <button className="btn-primary" onClick={onLaunch} style={{ marginTop: 10, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 7, padding: '10px', fontSize: 13 }}>
            Launch App <Icon name="arrowRight" size={13}/>
          </button>
        </div>
      </div>
    </section>
  )
}

/* ══════════════════════════════════════════════════════════
   LANDING — where you work
══════════════════════════════════════════════════════════ */

function WhereYouWork() {
  const cols = [
    { iconName: 'terminal' as IconName, title: 'Your terminal',    desc: 'Native MCP integration for Claude Code, Codex CLI, and Gemini CLI.',                                          status: 'Available',    avail: true },
    { iconName: 'browser'  as IconName, title: 'Web interface',    desc: 'Paste any PR URL and get instant inline warnings with matched historical commits.',                            status: 'Available',    avail: true },
    { iconName: 'github'   as IconName, title: 'GitHub bot',       desc: 'Automatically reviews PRs and posts warnings as inline comments when opened.',                                  status: 'Coming soon',  avail: false },
  ]
  return (
    <section style={{ padding: '80px 32px', maxWidth: 900, margin: '0 auto' }}>
      <div style={{ textAlign: 'center', marginBottom: 40 }}>
        <h2 style={{ fontSize: 24, fontWeight: 600, letterSpacing: '-0.025em', color: '#e5e5e5', margin: 0 }}>Runs where you work</h2>
        <p style={{ fontSize: 13, color: '#333333', marginTop: 8 }}>Review PRs in your terminal, browser, or directly on GitHub.</p>
      </div>
      <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
        {cols.map((c, i) => (
          <div key={i} className="step-card" style={{ flex: '1 1 240px', minWidth: 200 }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <div className="step-icon" style={{ width: 30, height: 30, background: 'rgba(239,68,68,.08)', border: '1px solid rgba(239,68,68,.15)', borderRadius: 6, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
                  <Icon name={c.iconName} size={14} stroke="#ef4444" strokeWidth={1.5}/>
                </div>
                <span style={{ fontSize: 15, fontWeight: 600, color: '#e5e5e5', letterSpacing: '-0.015em' }}>{c.title}</span>
              </div>
              <span className={c.avail ? 'badge-avail' : 'badge-soon'}>{c.status}</span>
            </div>
            <p style={{ fontSize: 12.5, color: '#555555', lineHeight: 1.65, margin: 0 }}>{c.desc}</p>
          </div>
        ))}
      </div>
    </section>
  )
}

/* ══════════════════════════════════════════════════════════
   LANDING — demo section (static illustration)
══════════════════════════════════════════════════════════ */

function DemoSection() {
  const cardRef = useRef<HTMLDivElement>(null)
  const [cardVisible, setCardVisible] = useState(false)
  useEffect(() => {
    if (!cardRef.current) return
    const obs = new IntersectionObserver(entries => {
      if (entries[0].isIntersecting) { setCardVisible(true); obs.disconnect() }
    }, { threshold: 0.3 })
    obs.observe(cardRef.current)
    return () => obs.disconnect()
  }, [])

  const prLines = [
    { sign: ' ', code: '    async def on_llm_end(self, response, **kwargs):',              signC: '#333',    codeC: '#555', bg: 'transparent', pulse: false, marker: false },
    { sign: ' ', code: '        coros = []',                                               signC: '#333',    codeC: '#555', bg: 'transparent', pulse: false, marker: false },
    { sign: ' ', code: '        for handler in self.handlers:',                            signC: '#333',    codeC: '#555', bg: 'transparent', pulse: false, marker: false },
    { sign: '−', code: '            try:',                                                 signC: '#cc5050', codeC: '#8a6060', bg: 'rgba(220,55,50,.09)', pulse: true, marker: false },
    { sign: '−', code: '                coros.append(handler.on_llm_end(response, **kwargs))', signC: '#cc5050', codeC: '#8a6060', bg: 'rgba(220,55,50,.09)', pulse: true, marker: false },
    { sign: '−', code: '            finally:',                                             signC: '#cc5050', codeC: '#8a6060', bg: 'rgba(220,55,50,.09)', pulse: true, marker: false },
    { sign: '−', code: '                await handler.aclose()',                           signC: '#cc5050', codeC: '#c07070', bg: 'rgba(220,55,50,.13)', pulse: true, marker: true },
    { sign: '+', code: '            coros.append(handler.on_llm_end(response, **kwargs))', signC: '#4faa6a', codeC: '#608a60', bg: 'rgba(48,160,72,.09)', pulse: false, marker: true },
  ]

  return (
    <section id="demo" style={{ padding: '80px 32px', maxWidth: 900, margin: '0 auto' }}>
      <div style={{ textAlign: 'center', marginBottom: 40 }}>
        <h2 style={{ fontSize: 24, fontWeight: 600, letterSpacing: '-0.025em', color: '#e5e5e5', margin: 0 }}>Example warning</h2>
        <p style={{ fontSize: 12.5, color: '#333333', marginTop: 8 }}>A real pattern from langchain-ai/langchain, caught before merge.</p>
      </div>
      <div style={{ display: 'flex', gap: 0, background: '#080808', border: '1px solid #1a1a1a', borderRadius: 10, overflow: 'hidden', alignItems: 'stretch' }}>
        <div style={{ flex: '0 0 55%', minWidth: 0, borderRight: '1px solid #1a1a1a', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
          <div style={{ padding: '10px 14px', background: '#0f0f0f', borderBottom: '1px solid #1a1a1a', display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
            <span style={{ fontSize: 10.5, fontWeight: 600, color: '#333333', letterSpacing: '0.05em', textTransform: 'uppercase' }}>PR Hunk</span>
            <span className="chip" style={{ marginLeft: 'auto' }}>langchain/callbacks/manager.py</span>
          </div>
          <div style={{ fontFamily: 'var(--font-fira-code, monospace)', fontSize: 11.5, flex: 1 }}>
            {prLines.map((l, i) => (
              <div key={i} className={l.pulse ? 'del-pulse' : ''} style={{ display: 'flex', alignItems: 'stretch', minHeight: 22, background: l.pulse ? undefined : (l.bg || 'transparent') }}>
                <div style={{ width: 3, flexShrink: 0, background: '#ef4444', opacity: l.marker ? 0.7 : 0, boxShadow: l.marker ? '0 0 6px rgba(239,68,68,.4)' : undefined }}/>
                <div style={{ fontSize: 11, color: '#252525', padding: '0 8px', minWidth: 36, textAlign: 'right', lineHeight: '22px', userSelect: 'none' }}>{40 + i}</div>
                <div style={{ width: 14, textAlign: 'center', lineHeight: '22px', flexShrink: 0, color: l.signC, fontSize: 12 }}>{l.sign}</div>
                <div style={{ padding: '0 8px', lineHeight: '22px', whiteSpace: 'pre', color: l.codeC, fontSize: 11 }}>{l.code}</div>
              </div>
            ))}
          </div>
        </div>
        <div ref={cardRef} style={{ flex: '0 0 45%', minWidth: 0, padding: 20, display: 'flex', alignItems: 'center', overflow: 'hidden' }}>
          <div className={`demo-card-hidden${cardVisible ? ' demo-card-visible' : ''}`}
            style={{ width: '100%', minWidth: 0, background: '#111111', border: '1px solid rgba(239,68,68,.25)', borderRadius: 8, padding: '14px 15px', position: 'relative', overflow: 'hidden' }}>
            <div style={{ position: 'absolute', left: 0, top: 0, bottom: 0, width: 3, background: '#ef4444', boxShadow: '0 0 8px rgba(239,68,68,.3)' }}/>
            <div style={{ display: 'inline-flex', alignItems: 'center', gap: 5, background: 'rgba(239,68,68,.08)', border: '1px solid rgba(239,68,68,.2)', borderRadius: 4, padding: '2px 7px', marginBottom: 9 }}>
              <span style={{ width: 5, height: 5, borderRadius: '50%', background: '#ef4444', flexShrink: 0 }}/>
              <span style={{ fontSize: 9.5, fontWeight: 700, letterSpacing: '0.1em', color: '#ef4444' }}>HIGH</span>
            </div>
            <div style={{ fontSize: 13, fontWeight: 600, color: '#e0e0e8', lineHeight: 1.4, marginBottom: 7 }}>Async iterator cleanup removed — resource leak under task cancellation</div>
            <div style={{ fontFamily: 'var(--font-fira-code, monospace)', fontSize: 10.5, color: '#333333', marginBottom: 10 }}>langchain/callbacks/manager.py<span style={{ color: '#252525' }}>:350</span></div>
            <div style={{ fontSize: 12, color: '#444444', lineHeight: 1.6, marginBottom: 11 }}>Removing the try/finally block that called handler.aclose() mirrors the pattern that caused file handles and HTTP connections to leak in the v0.0.318 regression.</div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: '#333333' }}>
              <Icon name="gitBranch" size={11} stroke="#333333"/>
              <span style={{ fontFamily: 'var(--font-fira-code, monospace)', fontSize: 10.5, color: '#404040' }}>c891de3</span>
              <span style={{ color: '#252525' }}>·</span>
              <span style={{ flex: 1, overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis' }}>fix: ensure astream cleanup on generator cancellation</span>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}

/* ══════════════════════════════════════════════════════════
   LANDING — footer
══════════════════════════════════════════════════════════ */

function LandingFooter() {
  return (
    <footer style={{ borderTop: '1px solid #1a1a1a', padding: '20px 32px', textAlign: 'center' }}>
      <p style={{ fontSize: 12, color: '#2e2e2e', margin: 0 }}>
        Powered by Claude + Nia&nbsp;&nbsp;·&nbsp;&nbsp;
        <a href="https://github.com/ShivamSinghNow/scartissue" target="_blank" rel="noopener" style={{ color: '#333333', textDecoration: 'none', fontFamily: 'var(--font-fira-code, monospace)', fontSize: 11.5, transition: 'color .12s' }}
          onMouseEnter={e => (e.currentTarget.style.color = '#666')}
          onMouseLeave={e => (e.currentTarget.style.color = '#333333')}>
          github.com/ShivamSinghNow/scartissue
        </a>
      </p>
    </footer>
  )
}

function LandingPage({ onLaunch }: { onLaunch: () => void }) {
  return (
    <div>
      <LandingNav onLaunch={onLaunch}/>
      <div style={{ paddingTop: 48 }}>
        <HeroSection onLaunch={onLaunch}/>
        <div style={{ borderTop: '1px solid #111111' }}/>
        <HowItWorks/>
        <div style={{ borderTop: '1px solid #111111' }}/>
        <InstallSection onLaunch={onLaunch}/>
        <div style={{ borderTop: '1px solid #111111' }}/>
        <DemoSection/>
        <div style={{ borderTop: '1px solid #111111' }}/>
        <WhereYouWork/>
        <LandingFooter/>
      </div>
    </div>
  )
}

/* ══════════════════════════════════════════════════════════
   APP — header
══════════════════════════════════════════════════════════ */

function AppHeader({ view, onHome, indexedRepos, onIndexRepo, onRemoveRepo }: {
  view: View
  onHome: () => void
  indexedRepos: IndexedRepo[]
  onIndexRepo: (repo: string, maxCommits: number) => void
  onRemoveRepo: (repo: string) => void
}) {
  const [open, setOpen] = useState(false)
  const [indexInput, setIndexInput] = useState('')
  const [maxCommits, setMaxCommits] = useState(1000)
  const [indexError, setIndexError] = useState<string | null>(null)
  const dropRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (dropRef.current && !dropRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [])

  const handleIndex = async (e: React.FormEvent) => {
    e.preventDefault()
    const repo = normalizeRepo(indexInput)
    if (!REPO_RE.test(repo)) {
      setIndexError('Use owner/repo format.')
      return
    }
    setIndexError(null)
    onIndexRepo(repo, maxCommits)
    setIndexInput('')
  }

  const totalIndexed = indexedRepos.filter(r => r.status === 'indexed').length

  return (
    <div style={{ position: 'fixed', top: 0, left: 0, right: 0, zIndex: 100, background: 'rgba(10,10,10,0.95)', borderBottom: '1px solid #1a1a1a', height: 44, display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '0 20px' }}>
      <button onClick={onHome} style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0 }}>
        <LandingLogo/>
      </button>
      {view !== 'landing' && (
        <div ref={dropRef} style={{ position: 'relative' }}>
          <button onClick={() => setOpen(o => !o)} style={{ display: 'flex', alignItems: 'center', gap: 5, background: 'transparent', border: '1px solid #1a1a1a', borderRadius: 6, padding: '4px 9px', cursor: 'pointer', color: '#555555', fontSize: 11.5, fontFamily: 'var(--font-space-grotesk, sans-serif)' }}>
            <span style={{ width: 5, height: 5, borderRadius: '50%', background: totalIndexed > 0 ? '#4faa6a' : '#555555', flexShrink: 0, boxShadow: totalIndexed > 0 ? '0 0 4px rgba(79,170,106,.5)' : undefined }}/>
            {totalIndexed} repo{totalIndexed !== 1 ? 's' : ''} indexed
            <Icon name="chevronRight" size={10} stroke="#333333" strokeWidth={1.5}/>
          </button>
          {open && (
            <div style={{ position: 'absolute', right: 0, top: 34, background: '#0f0f0f', border: '1px solid #1a1a1a', borderRadius: 8, padding: '10px 0', width: 380, boxShadow: '0 12px 40px rgba(0,0,0,.8)' }}>
              <div style={{ padding: '0 12px 8px', fontSize: 10, color: '#252525', fontWeight: 600, letterSpacing: '0.1em', textTransform: 'uppercase', borderBottom: '1px solid #1a1a1a', marginBottom: 2 }}>Indexed Repositories</div>
              {indexedRepos.length === 0 ? (
                <div style={{ padding: '10px 12px', fontSize: 11, color: '#333333' }}>No repos indexed yet.</div>
              ) : indexedRepos.map(r => (
                <div key={r.repo} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '8px 12px', gap: 10 }}>
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div style={{ fontFamily: 'var(--font-fira-code, monospace)', fontSize: 11, color: '#c0c0c8', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.repo}</div>
                    <div style={{ fontSize: 10.5, color: '#333333', marginTop: 2, lineHeight: 1.45 }}>
                      {r.status === 'indexing'
                        ? `Indexing ~${r.max_commits ?? 1000} commits, estimated ${estimateIndexMinutes(r.max_commits ?? 1000)} minutes`
                        : r.status === 'error'
                          ? r.error ?? 'Indexing failed'
                          : `${r.incidents} incidents · indexed ${formatRelativeTime(r.last_indexed)}`}
                    </div>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
                    <span className={r.status === 'indexing' ? 'repo-spinner' : ''} style={{ width: 7, height: 7, borderRadius: '50%', background: r.status === 'indexing' ? 'transparent' : r.status === 'error' ? '#f59e0b' : '#4faa6a', border: r.status === 'indexing' ? '1px solid rgba(239,68,68,.3)' : 'none', borderTopColor: r.status === 'indexing' ? '#ef4444' : undefined }}/>
                    <span style={{ fontSize: 10.5, color: r.status === 'indexing' ? '#ef4444' : r.status === 'error' ? '#f59e0b' : '#4faa6a' }}>{r.status}</span>
                    {r.status === 'error' && (
                      <button type="button" onClick={() => onIndexRepo(r.repo, r.max_commits ?? 1000)} style={{ background: 'none', border: 'none', color: '#c89070', cursor: 'pointer', fontSize: 10.5, padding: 0, fontFamily: 'var(--font-space-grotesk, sans-serif)' }}>retry</button>
                    )}
                    <button type="button" aria-label={`Remove ${r.repo}`} onClick={() => onRemoveRepo(r.repo)} style={{ background: 'none', border: 'none', color: '#333333', cursor: 'pointer', padding: 2, display: 'flex' }}>
                      <Icon name="x" size={11} stroke="currentColor"/>
                    </button>
                  </div>
                </div>
              ))}
              <div style={{ borderTop: '1px solid #1a1a1a', marginTop: 4, padding: '10px 12px 0' }}>
                <div style={{ fontSize: 11.5, color: '#a0a0a8', fontWeight: 600, marginBottom: 8 }}>Index new repo</div>
                <form onSubmit={handleIndex} style={{ display: 'grid', gridTemplateColumns: '1fr 84px 58px', gap: 6, alignItems: 'center' }}>
                  <input value={indexInput} onChange={e => setIndexInput(e.target.value)} placeholder="owner/repo"
                    style={{ flex: 1, background: '#0a0a0a', border: '1px solid #1a1a1a', borderRadius: 4, padding: '5px 8px', fontSize: 11, color: '#e5e5e5', fontFamily: 'var(--font-fira-code, monospace)', outline: 'none' }}
                    onFocus={e => (e.currentTarget.style.borderColor = 'rgba(239,68,68,.35)')}
                    onBlur={e => (e.currentTarget.style.borderColor = '#1a1a1a')}/>
                  <input type="number" min={500} max={5000} step={100} value={maxCommits} onChange={e => setMaxCommits(Math.min(5000, Math.max(500, Number(e.target.value) || 1000)))}
                    aria-label="commits"
                    style={{ background: '#0a0a0a', border: '1px solid #1a1a1a', borderRadius: 4, padding: '5px 6px', fontSize: 11, color: '#e5e5e5', fontFamily: 'var(--font-fira-code, monospace)', outline: 'none', width: '100%' }}/>
                  <button type="submit" disabled={!indexInput.trim()} style={{ background: '#ef4444', border: 'none', borderRadius: 4, padding: '6px 8px', cursor: !indexInput.trim() ? 'not-allowed' : 'pointer', opacity: !indexInput.trim() ? 0.45 : 1, color: '#fff', fontSize: 11, fontFamily: 'var(--font-space-grotesk, sans-serif)', transition: 'opacity .15s' }}>
                    Index
                  </button>
                </form>
                <div style={{ fontSize: 10.5, color: '#333333', marginTop: 6 }}>commits (higher = better coverage, longer wait)</div>
                {indexError && <div style={{ fontSize: 10.5, color: '#ef4444', marginTop: 6 }}>{indexError}</div>}
                <div style={{ fontSize: 10.5, color: '#333333', marginTop: 9, lineHeight: 1.45 }}>
                  Tip: In Claude Code, Codex CLI, or Gemini CLI, ask your agent to index any repo directly via the scartissue_index_repo tool.
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/* ══════════════════════════════════════════════════════════
   APP — empty state (PR URL input)
══════════════════════════════════════════════════════════ */

function AppEmpty({ onSubmit }: { onSubmit: (url: string) => void }) {
  const [val, setVal] = useState('')

  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => {
      const text = (e.clipboardData || (window as unknown as { clipboardData: DataTransfer }).clipboardData).getData('text')
      if (/github\.com\/.+\/pull\/\d+/.test(text)) {
        e.preventDefault()
        setVal(text)
        setTimeout(() => onSubmit(text), 300)
      }
    }
    document.addEventListener('paste', onPaste)
    return () => document.removeEventListener('paste', onPaste)
  }, [onSubmit])

  const submit = (e?: React.FormEvent) => {
    e?.preventDefault()
    if (val.trim()) onSubmit(val.trim())
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: 'calc(100vh - 44px)', padding: '0 24px' }}>
      <div style={{ width: '100%', maxWidth: 520 }}>
        <form onSubmit={submit}>
          <div style={{ position: 'relative' }}>
            <input type="text" value={val} onChange={e => setVal(e.target.value)}
              placeholder="Paste any GitHub PR URL…"
              style={{ width: '100%', background: '#0f0f0f', border: '1px solid #1a1a1a', borderRadius: 7, padding: '12px 48px 12px 14px', fontSize: 13.5, fontFamily: 'var(--font-space-grotesk, sans-serif)', color: '#e5e5e5', caretColor: '#ef4444', outline: 'none', transition: 'border-color .1s' }}
              onFocus={e => (e.currentTarget.style.borderColor = 'rgba(239,68,68,.35)')}
              onBlur={e => (e.currentTarget.style.borderColor = '#1a1a1a')}/>
            <button type="submit" disabled={!val.trim()} style={{ position: 'absolute', right: 8, top: '50%', transform: 'translateY(-50%)', background: val.trim() ? '#ef4444' : '#1a1a1a', border: 'none', borderRadius: 5, width: 30, height: 28, cursor: val.trim() ? 'pointer' : 'default', display: 'flex', alignItems: 'center', justifyContent: 'center', transition: 'background .15s' }}>
              <Icon name="arrowRight" size={13} stroke={val.trim() ? '#fff' : '#333333'}/>
            </button>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 10 }}>
            <span style={{ fontSize: 11, color: '#252525', flexShrink: 0 }}>or try</span>
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
              {EXAMPLE_PRS.map(pr => (
                <button key={pr} type="button"
                  onClick={() => { setVal(pr); setTimeout(() => onSubmit(pr), 80) }}
                  style={{ fontFamily: 'var(--font-fira-code, monospace)', fontSize: 10.5, color: '#333333', background: '#0f0f0f', border: '1px solid #1a1a1a', borderRadius: 5, padding: '4px 9px', cursor: 'pointer', transition: 'border-color .1s, color .1s' }}
                  onMouseEnter={e => { e.currentTarget.style.borderColor = '#252525'; e.currentTarget.style.color = '#888' }}
                  onMouseLeave={e => { e.currentTarget.style.borderColor = '#1a1a1a'; e.currentTarget.style.color = '#333333' }}>
                  #{pr.split('/').pop()}
                </button>
              ))}
            </div>
          </div>
        </form>
        <p style={{ marginTop: 28, fontSize: 12, color: '#252525', textAlign: 'center', lineHeight: 1.7 }}>
          Works on any GitHub repo. Index a new repo via the Indexed repos panel,<br/>
          or ask your coding agent to index one directly via MCP.
        </p>
        <div style={{ marginTop: 14, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8 }}>
          <span style={{ display: 'inline-block', background: '#111111', border: '1px solid #1a1a1a', borderRadius: 3, padding: '0 5px', fontFamily: 'var(--font-fira-code, monospace)', fontSize: 10, color: '#333333' }}>⌘V</span>
          <span style={{ fontSize: 11, color: '#1e1e1e' }}>paste any GitHub PR URL to analyze instantly</span>
        </div>
      </div>
    </div>
  )
}

/* ══════════════════════════════════════════════════════════
   APP — loading state (fires the real API call)
══════════════════════════════════════════════════════════ */

function AppLoading({ prUrl, onDone, onError }: {
  prUrl: string
  onDone: (data: ReviewResponse) => void
  onError: (msg: string) => void
}) {
  const [step, setStep] = useState(0)

  useEffect(() => {
    let cancelled = false
    let apiResult: ReviewResponse | null = null
    let animDone = false

    const tryTransition = () => {
      if (apiResult && animDone && !cancelled) onDone(apiResult)
    }

    // Animate steps over ~5s
    const delays = [700, 1400, 1100, 1700]
    let acc = 0
    const timers = delays.map((d, i) => {
      acc += d
      return setTimeout(() => { if (!cancelled) setStep(i + 1) }, acc)
    })
    const animTimer = setTimeout(() => { animDone = true; tryTransition() }, acc + 400)

    // Real API call
    fetch('/api/review', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pr_url: prUrl }),
    })
      .then(async r => {
        if (!r.ok) {
          const body = await r.json().catch(() => ({ detail: `Server error ${r.status}` }))
          throw new Error(body.detail || `Server error ${r.status}`)
        }
        return r.json() as Promise<ReviewResponse>
      })
      .then(data => { apiResult = data; tryTransition() })
      .catch(err => { if (!cancelled) onError(err instanceof Error ? err.message : String(err)) })

    return () => {
      cancelled = true
      timers.forEach(clearTimeout)
      clearTimeout(animTimer)
    }
  }, [prUrl, onDone, onError])

  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: 'calc(100vh - 44px)' }}>
      <div style={{ width: 290 }}>
        {LOADING_STEPS.map((label, i) => (
          <div key={i} className={`pstep ${step > i ? 'done' : step === i ? 'active' : 'pending'}`}
            style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '6px 0', color: step > i ? '#4faa6a' : step === i ? '#ef4444' : '#1e1e1e' }}>
            <div className="sdot" style={{ width: 7, height: 7, borderRadius: '50%', flexShrink: 0 }}/>
            <span style={{ fontSize: 12.5, fontFamily: 'var(--font-fira-code, monospace)' }}>{label}</span>
            {step > i && <Icon name="check" size={11} stroke="#4faa6a" strokeWidth={2}/>}
          </div>
        ))}
        {step === 4 && (
          <p style={{ marginTop: 16, fontSize: 11, color: '#252525', fontFamily: 'var(--font-fira-code, monospace)' }}>Waiting for Claude response…</p>
        )}
      </div>
    </div>
  )
}

/* ══════════════════════════════════════════════════════════
   APP — warning card (real backend data)
══════════════════════════════════════════════════════════ */

function WarningCard({ w, active, onActivate, cardRef, onJumpToDiff }: {
  w: Warning
  active: boolean
  onActivate: () => void
  cardRef: (el: HTMLDivElement | null) => void
  onJumpToDiff: () => void
}) {
  const [expanded, setExpanded] = useState(false)
  const [copied, setCopied] = useState(false)
  const s = SEV[w.severity]
  const lineRange = extractLineRange(w.pr_hunk)
  const inc = w.matched_incident

  const copyMd = () => {
    const sha = inc?.commit_sha?.slice(0, 7) ?? 'unknown'
    const msg = inc?.commit_message?.split('\n')[0] ?? ''
    navigator.clipboard?.writeText(
      `## ScarTissue Warning — ${w.severity.toUpperCase()}\n**${w.explanation}**\n\`${w.pr_file}\`${lineRange ? ` lines ${lineRange}` : ''}\n\nMatched commit: \`${sha}\` "${msg}" (${inc?.author ?? ''}, ${inc ? new Date(inc.commit_date).toLocaleDateString() : ''})\n\nConfidence: ${Math.round(w.confidence * 100)}%`
    )
    setCopied(true); setTimeout(() => setCopied(false), 1800)
  }

  return (
    <div ref={cardRef} className={`wcard ${active ? 'wactive' : ''}`}
      onMouseEnter={onActivate}
      style={{ background: '#0f0f0f', border: `1px solid ${active ? 'rgba(239,68,68,.2)' : '#1a1a1a'}`, borderRadius: 8, padding: '13px 14px' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 7 }}>
        <div style={{ display: 'inline-flex', alignItems: 'center', gap: 5, background: s.bg, border: `1px solid ${s.border}`, borderRadius: 4, padding: '2px 7px' }}>
          <span style={{ width: 5, height: 5, borderRadius: '50%', background: s.dot, flexShrink: 0 }}/>
          <span style={{ fontSize: 9.5, fontWeight: 700, letterSpacing: '0.1em', color: s.text }}>{s.label}</span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 2 }}>
          <button onClick={onJumpToDiff} title="Jump to hunk" style={{ background: 'none', border: 'none', cursor: 'pointer', color: '#2e2e2e', padding: '3px 6px', borderRadius: 4, fontSize: 10.5, fontFamily: 'var(--font-space-grotesk, sans-serif)', display: 'flex', alignItems: 'center', gap: 3, transition: 'color .1s' }}
            onMouseEnter={e => (e.currentTarget.style.color = '#6060b0')}
            onMouseLeave={e => (e.currentTarget.style.color = '#2e2e2e')}>
            <Icon name="arrowRight" size={11} stroke="currentColor"/> diff
          </button>
          <button onClick={copyMd} title="Copy as markdown" style={{ background: 'none', border: 'none', cursor: 'pointer', color: '#2e2e2e', padding: '3px 6px', borderRadius: 4, fontSize: 10.5, fontFamily: 'var(--font-space-grotesk, sans-serif)', display: 'flex', alignItems: 'center', gap: 3, transition: 'color .1s' }}
            onMouseEnter={e => (e.currentTarget.style.color = '#606060')}
            onMouseLeave={e => (e.currentTarget.style.color = '#2e2e2e')}>
            {copied ? <><Icon name="check" size={11} stroke="#4faa6a"/><span style={{ color: '#4faa6a' }}>copied</span></> : <><Icon name="copy" size={11} stroke="currentColor"/>copy</>}
          </button>
        </div>
      </div>

      <div style={{ fontSize: 13, fontWeight: 500, color: '#d8d8e2', lineHeight: 1.45, marginBottom: 6 }}>{w.explanation}</div>
      <div style={{ fontFamily: 'var(--font-fira-code, monospace)', fontSize: 10.5, color: '#333333', marginBottom: 9 }}>
        {w.pr_file}{lineRange && <span style={{ color: '#1e1e1e' }}>:{lineRange}</span>}
      </div>

      {/* Confidence bar */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 9 }}>
        <div style={{ flex: 1, height: 2, background: '#1a1a1a', borderRadius: 1, overflow: 'hidden' }}>
          <div style={{ height: '100%', width: `${Math.round(w.confidence * 100)}%`, background: w.confidence > 0.8 ? '#ef4444' : w.confidence > 0.6 ? '#6a6a5a' : '#3a4a3a', borderRadius: 1 }}/>
        </div>
        <span style={{ fontSize: 10, fontFamily: 'var(--font-fira-code, monospace)', color: '#252525', minWidth: 28 }}>{Math.round(w.confidence * 100)}%</span>
      </div>

      {w.proposed_fix && (
        <div style={{ marginBottom: 9, background: '#0a0a0a', border: '1px solid #1a1a1a', borderRadius: 5, padding: '8px 10px' }}>
          <div style={{ fontSize: 10, fontWeight: 600, color: '#333333', letterSpacing: '0.06em', textTransform: 'uppercase', marginBottom: 4 }}>Suggested fix</div>
          <div style={{ fontSize: 11.5, color: '#5a8a5a', lineHeight: 1.55, fontFamily: 'var(--font-fira-code, monospace)' }}>{w.proposed_fix}</div>
        </div>
      )}

      {inc && (
        <>
          <button onClick={() => setExpanded(e => !e)} style={{ width: '100%', background: 'none', border: 'none', cursor: 'pointer', padding: 0, textAlign: 'left', display: 'flex', alignItems: 'center', gap: 5, color: '#252525', fontSize: 11, fontFamily: 'var(--font-space-grotesk, sans-serif)' }}>
            <Icon name="chevronRight" size={9} stroke="#333333" strokeWidth={1.5}/>
            <span>matched commit</span>
            <span style={{ fontFamily: 'var(--font-fira-code, monospace)', fontSize: 10.5, color: '#383838' }}>{inc.commit_sha.slice(0, 7)}</span>
          </button>
          {expanded && (
            <div style={{ marginTop: 8, background: '#0a0a0a', border: '1px solid #1a1a1a', borderRadius: 6, padding: '10px 11px' }}>
              <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 8, marginBottom: 5 }}>
                <span style={{ fontSize: 11.5, color: '#a0a0b0', lineHeight: 1.5 }}>{inc.commit_message.split('\n')[0]}</span>
                <span style={{ flexShrink: 0, color: '#ef4444', fontSize: 10.5, fontFamily: 'var(--font-fira-code, monospace)', opacity: .7 }}>{inc.commit_sha.slice(0, 7)}</span>
              </div>
              <div style={{ display: 'flex', gap: 14 }}>
                <span style={{ fontSize: 10.5, fontFamily: 'var(--font-fira-code, monospace)', color: '#2e2e2e' }}>{inc.author}</span>
                <span style={{ fontSize: 10.5, color: '#252525' }}>{new Date(inc.commit_date).toLocaleDateString()}</span>
              </div>
              {inc.issue_refs.length > 0 && (
                <div style={{ marginTop: 4, fontSize: 10.5, color: '#2e2e2e', fontFamily: 'var(--font-fira-code, monospace)' }}>
                  Issues: {inc.issue_refs.map(n => `#${n}`).join(', ')}
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  )
}

/* ══════════════════════════════════════════════════════════
   APP — results (split: hunk refs on left, warnings on right)
══════════════════════════════════════════════════════════ */

function AppResults({ data, onReset, indexedRepos, onIndexRepo }: {
  data: ReviewResponse
  onReset: () => void
  indexedRepos: IndexedRepo[]
  onIndexRepo: (repo: string, maxCommits: number) => void
}) {
  const [activeIdx, setActiveIdx] = useState<number | null>(null)
  const [activeFile, setActiveFile] = useState(0)
  const [confirmPost, setConfirmPost] = useState(false)
  const [posting, setPosting] = useState(false)
  const [postResult, setPostResult] = useState<PostToGithubResponse | null>(null)
  const [postError, setPostError] = useState<string | null>(null)

  const cardRefs = useRef<Record<number, HTMLDivElement | null>>({})
  const hunkRefs = useRef<Record<number, HTMLDivElement | null>>({})
  const rightRef = useRef<HTMLDivElement>(null)
  const leftRef  = useRef<HTMLDivElement>(null)
  const fileRefs = useRef<Record<number, HTMLDivElement | null>>({})

  // Group warnings by file for tabs + left panel
  const fileGroups = useCallback(() => {
    const map = new Map<string, number[]>()
    data.warnings.forEach((w, i) => {
      if (!map.has(w.pr_file)) map.set(w.pr_file, [])
      map.get(w.pr_file)!.push(i)
    })
    return Array.from(map.entries()) // [fileName, [warningIdx...]]
  }, [data.warnings])

  const groups = fileGroups()
  // For forks, scar tissue is keyed by the upstream root, so check the upstream
  // first and fall back to the literal PR repo (or URL parse for older payloads).
  const prRepo = data.upstream_repo ?? data.pr_repo ?? repoFromPrUrl(data.pr_url)
  const forkRepo = data.upstream_repo ? data.pr_repo : null
  const indexedRepo = prRepo ? indexedRepos.find(r => r.repo.toLowerCase() === prRepo.toLowerCase() && r.status === 'indexed') : undefined
  const indexingRepo = prRepo ? indexedRepos.find(r => r.repo.toLowerCase() === prRepo.toLowerCase() && r.status === 'indexing') : undefined

  const scrollCardToIdx = useCallback((idx: number) => {
    const el = cardRefs.current[idx]
    if (el && rightRef.current) rightRef.current.scrollTo({ top: el.offsetTop - 12, behavior: 'smooth' })
  }, [])

  const scrollHunkToIdx = useCallback((idx: number) => {
    const el = hunkRefs.current[idx]
    if (el && leftRef.current) leftRef.current.scrollTo({ top: el.offsetTop - 60, behavior: 'smooth' })
  }, [])

  const handleCardActivate = useCallback((idx: number) => setActiveIdx(idx), [])

  const handleJumpToDiff = useCallback((idx: number) => {
    setActiveIdx(idx)
    scrollHunkToIdx(idx)
  }, [scrollHunkToIdx])

  const handleHunkClick = useCallback((idx: number) => {
    setActiveIdx(idx)
    scrollCardToIdx(idx)
  }, [scrollCardToIdx])

  const jumpToFile = (fileIdx: number) => {
    setActiveFile(fileIdx)
    const el = fileRefs.current[fileIdx]
    if (el && leftRef.current) leftRef.current.scrollTo({ top: el.offsetTop - 2, behavior: 'smooth' })
  }

  const handlePostToGithub = useCallback(() => {
    setPosting(true)
    setPostError(null)
    fetch('/api/post-to-github', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pr_url: data.pr_url, warnings: data.warnings }),
    })
      .then(async r => {
        const body = await r.json().catch(() => ({ detail: `Server error ${r.status}` }))
        if (!r.ok) throw new Error(body.detail || `Server error ${r.status}`)
        return body as PostToGithubResponse
      })
      .then(result => {
        setPostResult(result)
        setConfirmPost(false)
      })
      .catch(err => setPostError(err instanceof Error ? err.message : String(err)))
      .finally(() => setPosting(false))
  }, [data.pr_url, data.warnings])

  // j/k keyboard navigation
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (['INPUT', 'TEXTAREA'].includes((e.target as HTMLElement).tagName)) return
      const n = data.warnings.length
      if (n === 0) return
      if (e.key === 'j' || e.key === 'ArrowDown') {
        e.preventDefault()
        setActiveIdx(prev => { const next = Math.min((prev ?? -1) + 1, n - 1); scrollCardToIdx(next); scrollHunkToIdx(next); return next })
      }
      if (e.key === 'k' || e.key === 'ArrowUp') {
        e.preventDefault()
        setActiveIdx(prev => { const next = Math.max((prev ?? n) - 1, 0); scrollCardToIdx(next); scrollHunkToIdx(next); return next })
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [data.warnings.length, scrollCardToIdx, scrollHunkToIdx])

  return (
    <div style={{ height: '100vh', display: 'flex', flexDirection: 'column', paddingTop: 44 }}>
      {/* Meta bar */}
      <div style={{ background: '#0a0a0a', borderBottom: '1px solid #1a1a1a', padding: '0 16px', height: 36, display: 'flex', alignItems: 'center', flexShrink: 0 }}>
        <button onClick={onReset} style={{ background: 'none', border: 'none', cursor: 'pointer', color: '#333333', padding: '0 8px 0 0', display: 'flex', alignItems: 'center', gap: 4, fontSize: 11, marginRight: 10, transition: 'color .1s', fontFamily: 'var(--font-space-grotesk, sans-serif)' }}
          onMouseEnter={e => (e.currentTarget.style.color = '#888')}
          onMouseLeave={e => (e.currentTarget.style.color = '#333333')}>
          <Icon name="back" size={11} stroke="currentColor" strokeWidth={1.4}/>
        </button>
        <div style={{ width: 1, height: 14, background: '#1a1a1a', marginRight: 12 }}/>
        <span style={{ fontSize: 12.5, color: '#b0b0b8', fontWeight: 500, marginRight: 10, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1, minWidth: 0 }}>{data.pr_title}</span>
        <span style={{ fontFamily: 'var(--font-fira-code, monospace)', fontSize: 11, color: '#333333', marginRight: 10, flexShrink: 0 }}>{data.pr_author}</span>
        <div style={{ display: 'flex', alignItems: 'center', gap: 5, flexShrink: 0 }}>
          <span style={{ width: 5, height: 5, borderRadius: '50%', background: data.total_warnings > 0 ? '#ef4444' : '#4faa6a', flexShrink: 0 }}/>
          <span style={{ fontSize: 11, color: data.total_warnings > 0 ? '#ef4444' : '#4faa6a', fontWeight: 500 }}>{data.total_warnings} warning{data.total_warnings !== 1 ? 's' : ''}</span>
        </div>
        <div style={{ width: 1, height: 14, background: '#1a1a1a', margin: '0 10px', flexShrink: 0 }}/>
        <button
          disabled={data.warnings.length === 0 || posting || Boolean(postResult)}
          onClick={() => setConfirmPost(true)}
          title="Requires GITHUB_TOKEN with PR write access."
          className="btn-primary"
          style={{ padding: '6px 11px', fontSize: 11.5, display: 'flex', alignItems: 'center', gap: 5, opacity: data.warnings.length === 0 || postResult ? .45 : 1, cursor: data.warnings.length === 0 || postResult ? 'not-allowed' : 'pointer', flexShrink: 0 }}>
          <Icon name="github" size={12}/>
          {posting ? 'Posting...' : postResult ? 'Already posted' : 'Post to GitHub'}
        </button>
      </div>

      {/* File tabs */}
      <div style={{ background: '#0a0a0a', borderBottom: '1px solid #1a1a1a', padding: '0 16px', height: 33, display: 'flex', alignItems: 'stretch', flexShrink: 0, overflowX: 'auto', gap: 0 }}>
        {groups.map(([fileName, idxs], fi) => (
          <button key={fileName} className={`ftab ${activeFile === fi ? 'ftactive' : ''}`}
            onClick={() => jumpToFile(fi)}
            style={{ background: 'none', border: 'none', padding: '0 12px', cursor: 'pointer', fontSize: 11, fontFamily: 'var(--font-fira-code, monospace)', color: activeFile === fi ? '#e5e5e5' : '#333333', display: 'flex', alignItems: 'center', gap: 5, flexShrink: 0 }}>
            {fileName.split('/').pop()}
            {idxs.length > 0 && (
              <span style={{ background: 'rgba(239,68,68,.1)', border: '1px solid rgba(239,68,68,.2)', borderRadius: 3, padding: '0 4px', fontSize: 9, color: '#ef4444', fontFamily: 'var(--font-space-grotesk, sans-serif)', fontWeight: 600 }}>{idxs.length}</span>
            )}
          </button>
        ))}
      </div>

      {/* Split pane */}
      <div style={{ display: 'flex', flex: 1, overflow: 'hidden' }}>
        {/* Left: hunk references */}
        <div ref={leftRef} style={{ width: '60%', overflow: 'auto', borderRight: '1px solid #1a1a1a' }}>
          {data.warnings.length === 0 ? (
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: '100%', color: indexedRepo ? '#4faa6a' : '#d0a060', fontSize: 13, gap: 10, padding: 24, textAlign: 'center' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <Icon name={indexedRepo ? 'check' : 'shieldAlert'} size={16} stroke={indexedRepo ? '#4faa6a' : '#d0a060'} strokeWidth={2}/>
                {indexedRepo ? `No historical patterns matched. ${indexedRepo.incidents} prior incidents analyzed.` : 'No warnings returned.'}
              </div>
              {indexedRepo && forkRepo && (
                <div style={{ fontSize: 11, color: '#666' }}>
                  PR is from fork {forkRepo} — analyzed against upstream {prRepo}.
                </div>
              )}
              {!indexedRepo && prRepo && (
                <div style={{ background: 'rgba(245,158,11,.06)', border: '1px solid rgba(245,158,11,.18)', borderRadius: 7, padding: '10px 12px', maxWidth: 390 }}>
                  <div style={{ fontSize: 12, color: '#d0a060', lineHeight: 1.5 }}>
                    {forkRepo
                      ? `Upstream ${prRepo} hasn't been indexed yet. Index it to get regression warnings on this PR from fork ${forkRepo}.`
                      : `This repo hasn't been indexed yet. Index ${prRepo} to get regression warnings on this PR.`}
                  </div>
                  <button className="btn-primary" onClick={() => onIndexRepo(prRepo, 1000)} disabled={Boolean(indexingRepo)} style={{ marginTop: 9, padding: '7px 10px', fontSize: 11.5, opacity: indexingRepo ? .55 : 1, cursor: indexingRepo ? 'not-allowed' : 'pointer' }}>
                    {indexingRepo ? 'Indexing...' : 'Index this repo'}
                  </button>
                </div>
              )}
            </div>
          ) : groups.map(([fileName, idxs], fi) => (
            <div key={fileName}>
              {/* Sticky file header */}
              <div ref={el => { fileRefs.current[fi] = el }}
                style={{ padding: '5px 10px', background: '#0a0a0a', borderTop: fi > 0 ? '1px solid #1a1a1a' : 'none', borderBottom: '1px solid #1a1a1a', display: 'flex', alignItems: 'center', gap: 6, position: 'sticky', top: 0, zIndex: 1 }}>
                <Icon name="gitBranch" size={10} stroke="#252525"/>
                <span style={{ fontFamily: 'var(--font-fira-code, monospace)', fontSize: 10.5, color: '#333333' }}>{fileName}</span>
              </div>
              {/* Hunk header rows — one per warning in this file */}
              {idxs.map(wIdx => {
                const w = data.warnings[wIdx]
                const isActive = activeIdx === wIdx
                return (
                  <div key={wIdx} ref={el => { hunkRefs.current[wIdx] = el }}
                    className={`diff-line hunk-header has-marker ${isActive ? 'active-hunk' : ''}`}
                    onClick={() => handleHunkClick(wIdx)}>
                    <div className="gutter-marker"/>
                    <div className="ln"/>
                    <div className="ls hunk-header">@@</div>
                    <div className="lc hunk-header" style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{w.pr_hunk}</div>
                  </div>
                )
              })}
            </div>
          ))}
        </div>

        {/* Right: warning cards */}
        <div ref={rightRef} style={{ width: '40%', overflow: 'auto', padding: 12, display: 'flex', flexDirection: 'column', gap: 8, background: '#0a0a0a' }}>
          {(postResult || postError) && (
            <div style={{ background: postResult ? 'rgba(34,197,94,.06)' : 'rgba(239,68,68,.07)', border: `1px solid ${postResult ? 'rgba(34,197,94,.18)' : 'rgba(239,68,68,.2)'}`, borderRadius: 7, padding: '9px 10px', fontSize: 11.5, color: postResult ? '#6fcf7f' : '#ef4444', display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10 }}>
              {postResult ? (
                <span>
                  Posted {postResult.total_comments} comment{postResult.total_comments !== 1 ? 's' : ''}{postResult.review_url && (
                    <> · <a href={postResult.review_url} target="_blank" rel="noreferrer" style={{ color: '#a8d8a8', textDecoration: 'none' }}>View on GitHub</a></>
                  )}
                </span>
              ) : (
                <>
                  <span>{postError}</span>
                  <button onClick={() => setConfirmPost(true)} style={{ background: 'none', border: 'none', color: '#f0a0a0', cursor: 'pointer', fontSize: 11.5, fontFamily: 'var(--font-space-grotesk, sans-serif)' }}>Retry</button>
                </>
              )}
            </div>
          )}
          {data.warnings.length === 0 ? (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', color: indexedRepo ? '#333333' : '#5a4630', fontSize: 12, textAlign: 'center', lineHeight: 1.5, padding: 18 }}>
              {indexedRepo ? 'No warnings to display.' : 'Index this repo before treating an empty review as a clean result.'}
            </div>
          ) : data.warnings.map((w, i) => (
            <WarningCard key={i} w={w} active={activeIdx === i}
              onActivate={() => handleCardActivate(i)}
              cardRef={el => { cardRefs.current[i] = el }}
              onJumpToDiff={() => handleJumpToDiff(i)}/>
          ))}
          {data.warnings.length > 0 && (
            <div style={{ display: 'flex', justifyContent: 'center', gap: 6, padding: '6px 0 2px', alignItems: 'center' }}>
              {['j', 'k'].map(k => (
                <span key={k} style={{ display: 'inline-block', background: '#0f0f0f', border: '1px solid #1a1a1a', borderRadius: 3, padding: '0 5px', fontFamily: 'var(--font-fira-code, monospace)', fontSize: 10, color: '#252525' }}>{k}</span>
              ))}
              <span style={{ fontSize: 10.5, color: '#1e1e1e' }}>navigate warnings</span>
            </div>
          )}
        </div>
      </div>
      {confirmPost && (
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,.68)', backdropFilter: 'blur(6px)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 200 }}>
          <div style={{ width: 'min(420px, calc(100vw - 32px))', background: '#0f0f0f', border: '1px solid #252525', borderRadius: 10, boxShadow: '0 18px 70px rgba(0,0,0,.5)', padding: 18 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
              <div style={{ width: 28, height: 28, borderRadius: 7, background: 'rgba(239,68,68,.09)', border: '1px solid rgba(239,68,68,.2)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                <Icon name="github" size={14} stroke="#ef4444"/>
              </div>
              <h3 style={{ margin: 0, fontSize: 15, color: '#e5e5e5', letterSpacing: '-0.01em' }}>Post ScarTissue warnings</h3>
            </div>
            <p style={{ margin: '0 0 10px', color: '#a0a0a8', fontSize: 13, lineHeight: 1.5 }}>
              Post {data.warnings.length} warning{data.warnings.length !== 1 ? 's' : ''} as review comments on this GitHub PR?
            </p>
            <p style={{ margin: '0 0 16px', color: '#444444', fontSize: 11.5, lineHeight: 1.45 }}>
              Requires GITHUB_TOKEN with PR write access.
            </p>
            {postError && <div style={{ marginBottom: 12, background: 'rgba(239,68,68,.07)', border: '1px solid rgba(239,68,68,.18)', borderRadius: 6, padding: '8px 9px', color: '#ef4444', fontSize: 11.5 }}>{postError}</div>}
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
              <button className="btn-outline" disabled={posting} onClick={() => setConfirmPost(false)} style={{ padding: '8px 14px', fontSize: 12.5, opacity: posting ? .5 : 1 }}>Cancel</button>
              <button className="btn-primary" disabled={posting} onClick={handlePostToGithub} style={{ padding: '8px 14px', fontSize: 12.5, minWidth: 112, opacity: posting ? .7 : 1 }}>
                {posting ? 'Posting...' : 'Post to GitHub'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

/* ══════════════════════════════════════════════════════════
   ROOT — state machine
══════════════════════════════════════════════════════════ */

export default function Root() {
  const [view, setView]           = useState<View>('landing')
  const [prUrl, setPrUrl]         = useState('')
  const [reviewData, setReviewData] = useState<ReviewResponse | null>(null)
  const [error, setError]         = useState<string | null>(null)
  const [indexedRepos, setIndexedRepos] = useState<IndexedRepo[]>([])

  // Persist view in localStorage so refresh stays in app
  useEffect(() => {
    const saved = localStorage.getItem('st_view')
    if (saved === 'empty') setView('empty')
  }, [])
  useEffect(() => {
    localStorage.setItem('st_view', view === 'landing' ? 'landing' : 'empty')
  }, [view])

  useEffect(() => {
    fetch('/api/repos')
      .then(async r => {
        if (!r.ok) throw new Error(`Server error ${r.status}`)
        return r.json() as Promise<Array<{ repo: string; incidents: number; last_indexed: string | null }>>
      })
      .then(repos => {
        setIndexedRepos(repos.map(r => ({
          repo: r.repo,
          incidents: r.incidents,
          last_indexed: r.last_indexed,
          status: 'indexed',
        })))
      })
      .catch(() => {
        // The app can still review/index when the backend comes up later.
      })
  }, [])

  const handleSubmit = useCallback((url: string) => {
    setPrUrl(url)
    setError(null)
    setReviewData(null)
    setView('loading')
  }, [])

  const handleLoadingDone = useCallback((data: ReviewResponse) => {
    setReviewData(data)
    setView('results')
  }, [])

  const handleLoadingError = useCallback((msg: string) => {
    setError(msg)
    setView('empty')
  }, [])

  const handleIndexRepo = useCallback((repoName: string, maxCommits = 1000) => {
    const repo = normalizeRepo(repoName)
    if (!REPO_RE.test(repo)) return
    setIndexedRepos(prev => {
      const existing = prev.find(r => r.repo.toLowerCase() === repo.toLowerCase())
      if (existing) {
        return prev.map(r => r.repo.toLowerCase() === repo.toLowerCase()
          ? { ...r, repo, status: 'indexing', max_commits: maxCommits, error: undefined }
          : r)
      }
      return [...prev, { repo, incidents: 0, last_indexed: null, status: 'indexing', max_commits: maxCommits }]
    })

    fetch('/api/index', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repo, max_commits: maxCommits }),
    })
      .then(async r => {
        const body = await r.json().catch(() => ({ detail: `Server error ${r.status}` }))
        if (!r.ok) throw new Error(body.detail || `Server error ${r.status}`)
        return body as { repo: string; incidents_found: number; status: string }
      })
      .then(result => {
        setIndexedRepos(prev => prev.map(r => r.repo.toLowerCase() === repo.toLowerCase()
          ? {
              ...r,
              repo: result.repo,
              incidents: result.incidents_found,
              last_indexed: new Date().toISOString(),
              status: 'indexed',
              error: undefined,
            }
          : r))
      })
      .catch(err => {
        setIndexedRepos(prev => prev.map(r => r.repo.toLowerCase() === repo.toLowerCase()
          ? { ...r, status: 'error', error: err instanceof Error ? err.message : String(err) }
          : r))
      })
  }, [])

  const handleRemoveRepo = useCallback((repo: string) => {
    setIndexedRepos(prev => prev.filter(r => r.repo.toLowerCase() !== repo.toLowerCase()))
  }, [])

  const isApp = view !== 'landing'

  return (
    <>
      {view === 'landing' && <LandingPage onLaunch={() => setView('empty')}/>}

      {isApp && (
        <div style={{ height: '100vh', overflow: 'hidden' }}>
          <AppHeader
            view={view}
            onHome={() => setView('landing')}
            indexedRepos={indexedRepos}
            onIndexRepo={handleIndexRepo}
            onRemoveRepo={handleRemoveRepo}
          />
          {view === 'empty' && (
            <>
              {error && (
                <div style={{ position: 'fixed', top: 44, left: 0, right: 0, padding: '8px 20px', background: 'rgba(239,68,68,.08)', borderBottom: '1px solid rgba(239,68,68,.2)', display: 'flex', alignItems: 'center', gap: 8, zIndex: 50 }}>
                  <Icon name="shieldAlert" size={13} stroke="#ef4444"/>
                  <span style={{ fontSize: 12, color: '#ef4444' }}>{error}</span>
                  <button onClick={() => setError(null)} style={{ marginLeft: 'auto', background: 'none', border: 'none', cursor: 'pointer', color: '#ef4444', padding: 2 }}>
                    <Icon name="x" size={12} stroke="currentColor"/>
                  </button>
                </div>
              )}
              <AppEmpty onSubmit={handleSubmit}/>
            </>
          )}
          {view === 'loading' && (
            <AppLoading prUrl={prUrl} onDone={handleLoadingDone} onError={handleLoadingError}/>
          )}
          {view === 'results' && reviewData && (
            <AppResults data={reviewData} onReset={() => setView('empty')} indexedRepos={indexedRepos} onIndexRepo={handleIndexRepo}/>
          )}
        </div>
      )}
    </>
  )
}
