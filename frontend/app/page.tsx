'use client'

import { useState, useEffect, useRef, useCallback } from 'react'
import { motion } from 'motion/react'
import Hls from 'hls.js'
import Lenis from 'lenis'
import { ArrowRight } from 'lucide-react'

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

interface EmailReviewResponse {
  pr_url: string
  recipient: string
  inbox_id: string | null
  inbox_address: string | null
  message_id: string | null
  thread_id: string | null
  warnings_sent: number
  subject: string
  dry_run: boolean
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
  'externalLink' | 'chevronRight' | 'back' | 'terminal' | 'browser' | 'github' | 'plus' | 'x' | 'mail'

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
  mail:         <><rect x="2" y="4" width="20" height="16" rx="2"/><polyline points="22,6 12,13 2,6"/></>,
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
  return (
    <nav className="fixed top-0 left-0 right-0 z-50 bg-transparent" style={{ padding: '16px 24px' }}>
      <div className="flex items-center justify-between">
        <LandingLogo/>
        <div className="hidden md:flex items-center gap-8" style={{ fontFamily: 'var(--font-instrument-sans, sans-serif)' }}>
          <a href="#how" className="text-sm font-medium text-white/80 hover:text-white transition-colors">Product</a>
          <a href="#install" className="text-sm font-medium text-white/80 hover:text-white transition-colors">Install</a>
          <a href="#demo" className="text-sm font-medium text-white/80 hover:text-white transition-colors">Demo</a>
          <a href="https://github.com/ShivamSinghNow/ScarTissueOpenClawHackathon" target="_blank" rel="noopener" className="text-sm font-medium text-white/80 hover:text-white transition-colors">GitHub</a>
        </div>
        <div className="flex items-center gap-4" style={{ fontFamily: 'var(--font-instrument-sans, sans-serif)' }}>
          <button onClick={onLaunch}
            className="bg-white text-black rounded-full font-semibold transition-all hover:shadow-[0_0_20px_rgba(255,255,255,0.25)]"
            style={{ padding: '10px 20px', fontSize: 14, fontFamily: 'var(--font-instrument-sans, sans-serif)' }}>
            Get Started
          </button>
        </div>
      </div>
    </nav>
  )
}

/* ══════════════════════════════════════════════════════════
   LANDING — hero
══════════════════════════════════════════════════════════ */

const HERO_VIDEO_SRC = 'https://stream.mux.com/T6oQJQ02cQ6N01TR6iHwZkKFkbepS34dkkIc9iukgy400g.m3u8'
const HERO_POSTER = 'https://images.unsplash.com/photo-1647356191320-d7a1f80ca777?crop=entropy&cs=tinysrgb&fit=max&fm=jpg&ixid=M3w3Nzg4Nzd8MHwxfHNlYXJjaHwxfHxhYnN0cmFjdCUyMGRhcmslMjB0ZWNobm9sb2d5JTIwbmV1cmFsJTIwbmV0d29ya3xlbnwxfHx8fDE3Njg5NzIyNTV8MA&ixlib=rb-4.1.0&q=80&w=1080'

function HeroSection({ onLaunch }: { onLaunch: () => void }) {
  const videoRef = useRef<HTMLVideoElement>(null)

  useEffect(() => {
    const video = videoRef.current
    if (!video) return

    if (Hls.isSupported()) {
      const hls = new Hls()
      hls.loadSource(HERO_VIDEO_SRC)
      hls.attachMedia(video)
      hls.on(Hls.Events.MANIFEST_PARSED, () => {
        video.play().catch(() => { /* autoplay blocked */ })
      })
      return () => { hls.destroy() }
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = HERO_VIDEO_SRC
      const onMeta = () => video.play().catch(() => { /* autoplay blocked */ })
      video.addEventListener('loadedmetadata', onMeta)
      return () => video.removeEventListener('loadedmetadata', onMeta)
    }
  }, [])

  return (
    <section
      className="relative w-full min-h-screen text-white overflow-hidden"
      style={{ backgroundColor: '#000000', fontFamily: 'var(--font-instrument-sans, sans-serif)' }}
    >
      {/* Background video */}
      <video
        ref={videoRef}
        muted
        loop
        playsInline
        poster={HERO_POSTER}
        className="absolute inset-0 w-full h-full object-cover"
        style={{ opacity: 0.6 }}
      />

      {/* Black overlay over video for readability */}
      <div className="absolute inset-0 bg-black/60 backdrop-blur-[2px]"/>

      {/* Decorative red gradients (red+black palette, swapped from blue) */}
      <div
        className="absolute pointer-events-none mix-blend-screen"
        style={{
          top: '-20%', left: '20%', width: 600, height: 600, borderRadius: '50%',
          background: 'rgba(239,68,68,0.20)',
          filter: 'blur(120px)',
        }}
      />
      <div
        className="absolute pointer-events-none mix-blend-screen"
        style={{
          bottom: '-10%', right: '20%', width: 500, height: 500, borderRadius: '50%',
          background: 'rgba(140,20,20,0.22)',
          filter: 'blur(120px)',
        }}
      />

      {/* Subtle grid for depth (matches existing scartissue aesthetic) */}
      <div
        className="absolute inset-0 pointer-events-none"
        style={{
          backgroundImage: 'linear-gradient(rgba(255,255,255,0.02) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,0.02) 1px,transparent 1px)',
          backgroundSize: '48px 48px',
        }}
      />

      {/* Content */}
      <div className="relative z-10 mx-auto flex flex-col items-center text-center px-6 max-w-5xl"
        style={{ marginTop: 130, paddingBottom: 80, gap: 44 }}>

        {/* Pre-headline (Instrument Serif) */}
        <motion.p
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6 }}
          className="text-3xl sm:text-5xl text-white"
          style={{
            fontFamily: 'var(--font-instrument-serif, serif)',
            fontStyle: 'italic',
            lineHeight: 1.1,
            letterSpacing: '-0.01em',
            fontSize: 'clamp(28px, 4.5vw, 48px)',
            margin: 0,
          }}
        >
          Every codebase remembers its bugs.
        </motion.p>

        {/* Main headline (Instrument Sans, massive) */}
        <motion.h1
          initial={{ opacity: 0, scale: 0.9 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ delay: 0.2, duration: 0.6 }}
          className="font-semibold tracking-tighter"
          style={{
            fontFamily: 'var(--font-instrument-sans, sans-serif)',
            fontSize: 'clamp(60px, 11vw, 136px)',
            lineHeight: 0.9,
            margin: 0,
            backgroundImage: 'linear-gradient(to bottom, #ffffff, #ffffff 55%, #ffd0d0)',
            WebkitBackgroundClip: 'text',
            backgroundClip: 'text',
            color: 'transparent',
            WebkitTextFillColor: 'transparent',
          }}
        >
          Now your agent<br/>does too.
        </motion.h1>

        {/* Subheadline */}
        <motion.p
          initial={{ opacity: 0 }}
          animate={{ opacity: 0.7 }}
          transition={{ delay: 0.4, duration: 0.6 }}
          className="text-white max-w-xl"
          style={{
            fontFamily: 'var(--font-instrument-sans, sans-serif)',
            fontSize: 'clamp(16px, 1.6vw, 20px)',
            lineHeight: 1.65,
            margin: 0,
          }}
        >
          ScarTissue indexes your repo&apos;s history of fixes and warns when a PR is about
          to reintroduce a regression your team has already paid for.
        </motion.p>

        {/* CTAs */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.6, duration: 0.5 }}
          className="flex flex-col sm:flex-row gap-6 items-center"
        >
          {/* Primary: white pill with red arrow circle */}
          <button
            onClick={onLaunch}
            className="group flex items-center rounded-full transition-all hover:shadow-[0_0_20px_rgba(255,255,255,0.3)] hover:scale-105"
            style={{
              background: '#ffffff',
              padding: '8px 8px 8px 24px',
              fontFamily: 'var(--font-instrument-sans, sans-serif)',
            }}
          >
            <span style={{ color: '#0a0400', fontWeight: 500, fontSize: 18, marginRight: 16 }}>
              Open Web Interface
            </span>
            <span
              className="flex items-center justify-center rounded-full transition-colors"
              style={{
                width: 40, height: 40,
                background: '#ef4444',
              }}
              onMouseEnter={e => (e.currentTarget.style.background = '#dc3838')}
              onMouseLeave={e => (e.currentTarget.style.background = '#ef4444')}
            >
              <ArrowRight size={20} color="#fff" strokeWidth={2}/>
            </span>
          </button>

          {/* Secondary: text link with arrow */}
          <button
            onClick={() => document.getElementById('install')?.scrollIntoView({ behavior: 'smooth' })}
            className="group flex items-center gap-2 text-white/70 hover:text-white transition-colors backdrop-blur-sm hover:bg-white/5 rounded-full"
            style={{
              padding: '10px 18px',
              fontFamily: 'var(--font-instrument-sans, sans-serif)',
              fontSize: 16,
            }}
          >
            Install MCP Server
            <ArrowRight size={16} className="group-hover:translate-x-1 transition-transform" strokeWidth={2}/>
          </button>
        </motion.div>

        {/* Proof line */}
        <motion.p
          initial={{ opacity: 0 }}
          animate={{ opacity: 0.45 }}
          transition={{ delay: 0.85, duration: 0.5 }}
          className="text-white"
          style={{
            fontFamily: 'var(--font-fira-code, monospace)',
            fontSize: 12,
            letterSpacing: '0.01em',
            margin: 0,
          }}
        >
          indexed across langchain-ai/langchain&nbsp;&nbsp;·&nbsp;&nbsp;encode/httpx&nbsp;&nbsp;·&nbsp;&nbsp;catches regressions before they merge
        </motion.p>
      </div>

      {/* Bottom fade so the next section blends into pure black */}
      <div
        className="absolute bottom-0 left-0 right-0 pointer-events-none"
        style={{
          height: 200,
          background: 'linear-gradient(to bottom, transparent, #0a0a0a)',
        }}
      />
    </section>
  )
}

/* ══════════════════════════════════════════════════════════
   LANDING — how it works
══════════════════════════════════════════════════════════ */

function SectionHeading({ eyebrow, title, kicker }: { eyebrow?: string; title: string; kicker?: string }) {
  return (
    <div style={{ textAlign: 'center', marginBottom: 48 }}>
      {eyebrow && (
        <motion.div
          initial={{ opacity: 0, y: 12 }}
          whileInView={{ opacity: 0.55, y: 0 }}
          viewport={{ once: true, margin: '-80px' }}
          transition={{ duration: 0.5 }}
          style={{ fontFamily: 'var(--font-fira-code, monospace)', fontSize: 12, letterSpacing: '0.18em', textTransform: 'uppercase', color: '#ef4444', marginBottom: 14 }}>
          {eyebrow}
        </motion.div>
      )}
      <motion.h2
        initial={{ opacity: 0, y: 16 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true, margin: '-80px' }}
        transition={{ duration: 0.6 }}
        style={{
          fontFamily: 'var(--font-instrument-serif, serif)',
          fontStyle: 'italic',
          fontSize: 'clamp(36px, 5.5vw, 64px)',
          lineHeight: 1.05,
          letterSpacing: '-0.02em',
          color: '#ffffff',
          margin: 0,
        }}>
        {title}
      </motion.h2>
      {kicker && (
        <motion.p
          initial={{ opacity: 0 }}
          whileInView={{ opacity: 0.55 }}
          viewport={{ once: true, margin: '-80px' }}
          transition={{ delay: 0.15, duration: 0.6 }}
          style={{
            fontFamily: 'var(--font-instrument-sans, sans-serif)',
            fontSize: 'clamp(15px, 1.4vw, 18px)',
            color: '#ffffff',
            marginTop: 18,
            maxWidth: 560,
            marginLeft: 'auto',
            marginRight: 'auto',
            lineHeight: 1.6,
          }}>
          {kicker}
        </motion.p>
      )}
    </div>
  )
}

function HowItWorks() {
  const steps = [
    { icon: 'gitBranch' as IconName,   num: '01', title: 'Index',  desc: "Mine your repo's full git history. Every fix commit becomes a data point in your scar tissue index." },
    { icon: 'search' as IconName,      num: '02', title: 'Review', desc: 'Paste any PR URL. ScarTissue cross-references every hunk against known regression patterns.' },
    { icon: 'shieldAlert' as IconName, num: '03', title: 'Warn',   desc: 'Receive targeted warnings with matched prior commits, severity ratings, and proposed fixes.' },
  ]
  return (
    <section id="how" style={{ padding: '90px 32px 70px', maxWidth: 1140, margin: '0 auto', position: 'relative' }}>
      <SectionHeading eyebrow="How it works" title="Three steps. No setup ceremony." kicker="Point ScarTissue at any GitHub repo. Within minutes you have a regression-detection layer riding on top of every PR your team opens."/>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 20 }}>
        {steps.map((s, i) => (
          <motion.div key={s.title}
            initial={{ opacity: 0, y: 24 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true, margin: '-100px' }}
            transition={{ delay: i * 0.1, duration: 0.55 }}
            className="reveal-card"
            style={{
              background: 'linear-gradient(180deg, rgba(255,255,255,.025) 0%, rgba(255,255,255,.005) 100%)',
              border: '1px solid rgba(255,255,255,.06)',
              borderRadius: 16,
              padding: 28,
              position: 'relative',
              overflow: 'hidden',
              minHeight: 240,
              fontFamily: 'var(--font-instrument-sans, sans-serif)',
            }}>
            <div style={{ position: 'absolute', top: 20, right: 22, fontFamily: 'var(--font-fira-code, monospace)', fontSize: 11, color: 'rgba(239,68,68,.4)', letterSpacing: '0.1em' }}>{s.num}</div>
            <div style={{ width: 44, height: 44, borderRadius: 12, background: 'rgba(239,68,68,.10)', border: '1px solid rgba(239,68,68,.22)', display: 'flex', alignItems: 'center', justifyContent: 'center', marginBottom: 22 }}>
              <Icon name={s.icon} size={18} stroke="#ef4444" strokeWidth={1.6}/>
            </div>
            <h3 style={{ fontSize: 22, fontWeight: 600, color: '#ffffff', letterSpacing: '-0.015em', margin: '0 0 10px' }}>{s.title}</h3>
            <p style={{ fontSize: 14, color: 'rgba(255,255,255,.55)', lineHeight: 1.65, margin: 0 }}>{s.desc}</p>
            <div style={{ position: 'absolute', bottom: 0, left: 0, right: 0, height: 2, background: 'linear-gradient(90deg, transparent 0%, rgba(239,68,68,.5) 50%, transparent 100%)', opacity: 0.6 }}/>
          </motion.div>
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

  const copy = () => {
    navigator.clipboard?.writeText(MCP_CONFIGS[tab].content)
    setCopied(true); setTimeout(() => setCopied(false), 2000)
  }
  useEffect(() => setCopied(false), [tab])

  const tabs = [
    { id: 'claude' as const, label: 'Claude Code' },
    { id: 'codex'  as const, label: 'Codex CLI' },
    { id: 'gemini' as const, label: 'Gemini CLI' },
  ]

  return (
    <section id="install" style={{ padding: '70px 32px', maxWidth: 1140, margin: '0 auto', position: 'relative' }}>
      <SectionHeading eyebrow="Install" title="Wire it up in under a minute." kicker="Drop the MCP config into your agent of choice. ScarTissue becomes a tool your model can call from inside any chat or PR review."/>
      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1.2fr) minmax(0, 1fr)', gap: 32, alignItems: 'stretch' }}>
        {/* Left: tabbed config */}
        <motion.div
          initial={{ opacity: 0, y: 24 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: '-100px' }}
          transition={{ duration: 0.55 }}
          style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
          <div style={{ display: 'flex', gap: 4, padding: 4, background: 'rgba(255,255,255,.03)', border: '1px solid rgba(255,255,255,.06)', borderRadius: 10, alignSelf: 'flex-start', marginBottom: 16, fontFamily: 'var(--font-instrument-sans, sans-serif)' }}>
            {tabs.map(t => (
              <button key={t.id} onClick={() => setTab(t.id)}
                style={{
                  background: tab === t.id ? 'rgba(239,68,68,.14)' : 'transparent',
                  border: tab === t.id ? '1px solid rgba(239,68,68,.3)' : '1px solid transparent',
                  color: tab === t.id ? '#ffffff' : 'rgba(255,255,255,.55)',
                  borderRadius: 7,
                  padding: '7px 14px',
                  fontSize: 13,
                  fontWeight: 500,
                  cursor: 'pointer',
                  transition: 'all .15s',
                }}>
                {t.label}
              </button>
            ))}
          </div>
          <div style={{ flex: 1, background: '#050505', border: '1px solid rgba(255,255,255,.08)', borderRadius: 14, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '12px 16px', background: 'rgba(255,255,255,.02)', borderBottom: '1px solid rgba(255,255,255,.05)' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontFamily: 'var(--font-fira-code, monospace)', fontSize: 12, color: 'rgba(255,255,255,.45)' }}>
                <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#ef4444' }}/>
                {MCP_CONFIGS[tab].path}
              </div>
              <button onClick={copy}
                style={{
                  background: copied ? 'rgba(34,197,94,.12)' : 'rgba(255,255,255,.05)',
                  border: copied ? '1px solid rgba(34,197,94,.3)' : '1px solid rgba(255,255,255,.08)',
                  borderRadius: 6,
                  padding: '5px 10px',
                  fontSize: 12,
                  color: copied ? '#6fcf7f' : 'rgba(255,255,255,.65)',
                  fontFamily: 'var(--font-instrument-sans, sans-serif)',
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  gap: 6,
                  transition: 'all .15s',
                }}>
                <Icon name={copied ? 'check' : 'copy'} size={12} stroke="currentColor"/>
                {copied ? 'copied' : 'copy'}
              </button>
            </div>
            <pre style={{ margin: 0, padding: 20, fontFamily: 'var(--font-fira-code, monospace)', fontSize: 12.5, lineHeight: 1.75, overflowX: 'auto', whiteSpace: 'pre', flex: 1 }}>
              {MCP_CONFIGS[tab].content.split('\n').map((line, i) => (
                <span key={i} style={{ display: 'block', color: colorJsonLine(line, MCP_CONFIGS[tab].lang) }}>{line}</span>
              ))}
            </pre>
          </div>
          <p style={{ fontSize: 13, color: 'rgba(255,255,255,.4)', marginTop: 14, lineHeight: 1.65, fontFamily: 'var(--font-instrument-sans, sans-serif)' }}>
            Prerequisites: clone ScarTissue, run <code style={{ fontFamily: 'var(--font-fira-code, monospace)', color: 'rgba(255,255,255,.7)' }}>uv pip install -e .</code> from <code style={{ fontFamily: 'var(--font-fira-code, monospace)', color: 'rgba(255,255,255,.7)' }}>backend/</code>, then populate <code style={{ fontFamily: 'var(--font-fira-code, monospace)', color: 'rgba(255,255,255,.7)' }}>backend/.env</code>. The <code style={{ fontFamily: 'var(--font-fira-code, monospace)', color: 'rgba(255,255,255,.7)' }}>scartissue-mcp</code> command becomes available globally after install.
          </p>
        </motion.div>

        {/* Right: launch the web interface */}
        <motion.div
          initial={{ opacity: 0, y: 24 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: '-100px' }}
          transition={{ delay: 0.1, duration: 0.55 }}
          style={{ display: 'flex', flexDirection: 'column', fontFamily: 'var(--font-instrument-sans, sans-serif)' }}>
          <div style={{ flex: 1, background: 'linear-gradient(180deg, rgba(239,68,68,.06) 0%, rgba(239,68,68,.0) 100%)', border: '1px solid rgba(239,68,68,.18)', borderRadius: 14, padding: '24px 22px', display: 'flex', flexDirection: 'column' }}>
            <div style={{ fontFamily: 'var(--font-fira-code, monospace)', fontSize: 11, letterSpacing: '0.18em', textTransform: 'uppercase', color: 'rgba(239,68,68,.65)', marginBottom: 10 }}>Or skip the install</div>
            <h3 style={{ fontFamily: 'var(--font-instrument-serif, serif)', fontStyle: 'italic', fontSize: 28, lineHeight: 1.1, color: '#ffffff', margin: '0 0 12px' }}>
              Use the web interface.
            </h3>
            <p style={{ fontSize: 14, color: 'rgba(255,255,255,.6)', lineHeight: 1.65, margin: 0, flex: 1 }}>
              Paste any GitHub PR URL and watch ScarTissue cross-reference every hunk against your indexed history in real time. No CLI, no local install.
            </p>
            <button
              onClick={onLaunch}
              className="group flex items-center rounded-full transition-all hover:shadow-[0_0_20px_rgba(255,255,255,0.25)] hover:scale-105"
              style={{
                background: '#ffffff',
                padding: '8px 8px 8px 22px',
                fontFamily: 'var(--font-instrument-sans, sans-serif)',
                marginTop: 22,
                alignSelf: 'flex-start',
              }}>
              <span style={{ color: '#0a0400', fontWeight: 500, fontSize: 16, marginRight: 14 }}>Launch App</span>
              <span
                className="flex items-center justify-center rounded-full"
                style={{ width: 36, height: 36, background: '#ef4444', transition: 'background .15s' }}
                onMouseEnter={e => (e.currentTarget.style.background = '#dc3838')}
                onMouseLeave={e => (e.currentTarget.style.background = '#ef4444')}>
                <ArrowRight size={18} color="#fff" strokeWidth={2}/>
              </span>
            </button>
          </div>
        </motion.div>
      </div>
    </section>
  )
}

/* ══════════════════════════════════════════════════════════
   LANDING — where you work
══════════════════════════════════════════════════════════ */

function WhereYouWork() {
  const cols = [
    { iconName: 'terminal' as IconName, title: 'Your terminal',    desc: 'Native MCP integration for Claude Code, Codex CLI, and Gemini CLI. Ask your agent to review any PR — it returns warnings, severity, and proposed fixes inline.',  status: 'Available',    avail: true },
    { iconName: 'browser'  as IconName, title: 'Web interface',    desc: 'Paste any PR URL and watch ScarTissue cross-reference every hunk against the indexed history in real time. Post warnings back to GitHub in one click.',         status: 'Available',    avail: true },
    { iconName: 'github'   as IconName, title: 'GitHub bot',       desc: 'Automatically reviews every PR your team opens and posts warnings as inline review comments. Zero-config once installed on your org.',                          status: 'Coming soon',  avail: false },
  ]
  return (
    <section style={{ padding: '70px 32px 90px', maxWidth: 1140, margin: '0 auto', position: 'relative' }}>
      <SectionHeading eyebrow="Surface area" title="Runs where you work." kicker="Same engine, three places. Whether you live in a terminal, a browser, or GitHub itself, ScarTissue meets you there."/>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 20 }}>
        {cols.map((c, i) => (
          <motion.div key={c.title}
            initial={{ opacity: 0, y: 24 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true, margin: '-100px' }}
            transition={{ delay: i * 0.1, duration: 0.55 }}
            style={{
              background: c.avail ? 'linear-gradient(180deg, rgba(255,255,255,.025) 0%, rgba(255,255,255,.005) 100%)' : 'rgba(255,255,255,.015)',
              border: '1px solid rgba(255,255,255,.06)',
              borderRadius: 16,
              padding: 28,
              fontFamily: 'var(--font-instrument-sans, sans-serif)',
              position: 'relative',
              overflow: 'hidden',
              minHeight: 240,
              opacity: c.avail ? 1 : 0.7,
            }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 22 }}>
              <div style={{ width: 44, height: 44, borderRadius: 12, background: c.avail ? 'rgba(239,68,68,.10)' : 'rgba(255,255,255,.04)', border: c.avail ? '1px solid rgba(239,68,68,.22)' : '1px solid rgba(255,255,255,.08)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                <Icon name={c.iconName} size={18} stroke={c.avail ? '#ef4444' : 'rgba(255,255,255,.35)'} strokeWidth={1.6}/>
              </div>
              <span style={{
                fontSize: 10,
                fontWeight: 700,
                letterSpacing: '0.18em',
                textTransform: 'uppercase',
                padding: '4px 10px',
                borderRadius: 4,
                background: c.avail ? 'rgba(34,197,94,.10)' : 'rgba(255,255,255,.04)',
                border: c.avail ? '1px solid rgba(34,197,94,.25)' : '1px solid rgba(255,255,255,.08)',
                color: c.avail ? '#6fcf7f' : 'rgba(255,255,255,.45)',
              }}>{c.status}</span>
            </div>
            <h3 style={{ fontSize: 22, fontWeight: 600, color: '#ffffff', letterSpacing: '-0.015em', margin: '0 0 10px' }}>{c.title}</h3>
            <p style={{ fontSize: 14, color: 'rgba(255,255,255,.55)', lineHeight: 1.65, margin: 0 }}>{c.desc}</p>
            {c.avail && <div style={{ position: 'absolute', bottom: 0, left: 0, right: 0, height: 2, background: 'linear-gradient(90deg, transparent 0%, rgba(239,68,68,.5) 50%, transparent 100%)', opacity: 0.6 }}/>}
          </motion.div>
        ))}
      </div>
    </section>
  )
}

/* ══════════════════════════════════════════════════════════
   LANDING — demo section (static illustration)
══════════════════════════════════════════════════════════ */

function DemoSection() {
  const prLines = [
    { sign: ' ', code: '    async def on_llm_end(self, response, **kwargs):',                  signC: 'rgba(255,255,255,.25)', codeC: 'rgba(255,255,255,.45)', bg: 'transparent', marker: false },
    { sign: ' ', code: '        coros = []',                                                   signC: 'rgba(255,255,255,.25)', codeC: 'rgba(255,255,255,.45)', bg: 'transparent', marker: false },
    { sign: ' ', code: '        for handler in self.handlers:',                                signC: 'rgba(255,255,255,.25)', codeC: 'rgba(255,255,255,.45)', bg: 'transparent', marker: false },
    { sign: '−', code: '            try:',                                                     signC: '#ef4444',                codeC: '#d88080',               bg: 'rgba(239,68,68,.08)', marker: false },
    { sign: '−', code: '                coros.append(handler.on_llm_end(response, **kwargs))', signC: '#ef4444',                codeC: '#d88080',               bg: 'rgba(239,68,68,.08)', marker: false },
    { sign: '−', code: '            finally:',                                                 signC: '#ef4444',                codeC: '#d88080',               bg: 'rgba(239,68,68,.08)', marker: false },
    { sign: '−', code: '                await handler.aclose()',                               signC: '#ef4444',                codeC: '#f0a0a0',               bg: 'rgba(239,68,68,.16)', marker: true },
    { sign: '+', code: '            coros.append(handler.on_llm_end(response, **kwargs))',     signC: '#6fcf7f',                codeC: '#80c890',               bg: 'rgba(48,160,72,.10)', marker: true },
  ]

  return (
    <section id="demo" style={{ padding: '70px 32px', maxWidth: 1140, margin: '0 auto', position: 'relative' }}>
      <SectionHeading eyebrow="Live example" title="A regression caught before it shipped." kicker="A real pattern from langchain-ai/langchain. The PR removed the cleanup path. ScarTissue matched it to the original fix and emitted a high-confidence warning."/>
      <motion.div
        initial={{ opacity: 0, y: 24 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true, margin: '-120px' }}
        transition={{ duration: 0.6 }}
        style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1.3fr) minmax(0, 1fr)', gap: 0, background: '#050505', border: '1px solid rgba(255,255,255,.08)', borderRadius: 16, overflow: 'hidden', alignItems: 'stretch', boxShadow: '0 30px 100px -40px rgba(239,68,68,.18)' }}>
        {/* Left: PR diff */}
        <div style={{ borderRight: '1px solid rgba(255,255,255,.06)', display: 'flex', flexDirection: 'column' }}>
          <div style={{ padding: '14px 18px', background: 'rgba(255,255,255,.02)', borderBottom: '1px solid rgba(255,255,255,.05)', display: 'flex', alignItems: 'center', gap: 10, fontFamily: 'var(--font-instrument-sans, sans-serif)' }}>
            <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#ef4444' }}/>
            <span style={{ fontSize: 12, fontWeight: 600, color: 'rgba(255,255,255,.5)', letterSpacing: '0.18em', textTransform: 'uppercase' }}>PR hunk</span>
            <span style={{ marginLeft: 'auto', fontFamily: 'var(--font-fira-code, monospace)', fontSize: 12, color: 'rgba(255,255,255,.4)' }}>langchain/callbacks/manager.py</span>
          </div>
          <div style={{ fontFamily: 'var(--font-fira-code, monospace)', fontSize: 13, padding: '12px 0', flex: 1 }}>
            {prLines.map((l, i) => (
              <div key={i} style={{ display: 'flex', alignItems: 'stretch', minHeight: 24, background: l.bg }}>
                <div style={{ width: 3, flexShrink: 0, background: '#ef4444', opacity: l.marker ? 0.85 : 0, boxShadow: l.marker ? '0 0 8px rgba(239,68,68,.5)' : undefined }}/>
                <div style={{ fontSize: 12, color: 'rgba(255,255,255,.18)', padding: '0 12px', minWidth: 44, textAlign: 'right', lineHeight: '24px', userSelect: 'none' }}>{40 + i}</div>
                <div style={{ width: 16, textAlign: 'center', lineHeight: '24px', flexShrink: 0, color: l.signC, fontSize: 13 }}>{l.sign}</div>
                <div style={{ padding: '0 10px', lineHeight: '24px', whiteSpace: 'pre', color: l.codeC, fontSize: 12.5, overflow: 'hidden', textOverflow: 'ellipsis' }}>{l.code}</div>
              </div>
            ))}
          </div>
        </div>
        {/* Right: warning card */}
        <div style={{ padding: 28, display: 'flex', alignItems: 'center', background: 'linear-gradient(180deg, rgba(239,68,68,.04) 0%, transparent 80%)' }}>
          <motion.div
            initial={{ opacity: 0, x: 20 }}
            whileInView={{ opacity: 1, x: 0 }}
            viewport={{ once: true, margin: '-120px' }}
            transition={{ delay: 0.3, duration: 0.5 }}
            style={{ width: '100%', background: '#0a0a0a', border: '1px solid rgba(239,68,68,.3)', borderRadius: 12, padding: '20px 22px', position: 'relative', overflow: 'hidden', fontFamily: 'var(--font-instrument-sans, sans-serif)' }}>
            <div style={{ position: 'absolute', left: 0, top: 0, bottom: 0, width: 3, background: '#ef4444', boxShadow: '0 0 12px rgba(239,68,68,.4)' }}/>
            <div style={{ display: 'inline-flex', alignItems: 'center', gap: 6, background: 'rgba(239,68,68,.10)', border: '1px solid rgba(239,68,68,.25)', borderRadius: 5, padding: '3px 9px', marginBottom: 14 }}>
              <span style={{ width: 6, height: 6, borderRadius: '50%', background: '#ef4444' }}/>
              <span style={{ fontSize: 10, fontWeight: 700, letterSpacing: '0.18em', color: '#ef4444' }}>HIGH</span>
            </div>
            <div style={{ fontSize: 17, fontWeight: 600, color: '#ffffff', lineHeight: 1.3, marginBottom: 10, letterSpacing: '-0.01em' }}>
              Async iterator cleanup removed — resource leak under task cancellation
            </div>
            <div style={{ fontFamily: 'var(--font-fira-code, monospace)', fontSize: 12, color: 'rgba(255,255,255,.4)', marginBottom: 14 }}>
              langchain/callbacks/manager.py<span style={{ color: 'rgba(255,255,255,.25)' }}>:350</span>
            </div>
            <div style={{ fontSize: 14, color: 'rgba(255,255,255,.6)', lineHeight: 1.65, marginBottom: 16 }}>
              Removing the try/finally block that called handler.aclose() mirrors the pattern that caused file handles and HTTP connections to leak in the v0.0.318 regression.
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, paddingTop: 12, borderTop: '1px solid rgba(255,255,255,.06)' }}>
              <Icon name="gitBranch" size={13} stroke="rgba(255,255,255,.45)"/>
              <span style={{ fontFamily: 'var(--font-fira-code, monospace)', fontSize: 12, color: '#ef4444' }}>c891de3</span>
              <span style={{ color: 'rgba(255,255,255,.2)' }}>·</span>
              <span style={{ flex: 1, overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis', fontSize: 12.5, color: 'rgba(255,255,255,.5)' }}>fix: ensure astream cleanup on generator cancellation</span>
            </div>
          </motion.div>
        </div>
      </motion.div>
    </section>
  )
}

/* ══════════════════════════════════════════════════════════
   LANDING — footer
══════════════════════════════════════════════════════════ */

function LandingFooter() {
  return (
    <footer style={{ borderTop: '1px solid rgba(255,255,255,.05)', padding: '60px 32px 40px', textAlign: 'center', maxWidth: 1140, margin: '0 auto', fontFamily: 'var(--font-instrument-sans, sans-serif)' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', marginBottom: 14 }}>
        <LandingLogo/>
      </div>
      <p style={{ fontSize: 13, color: 'rgba(255,255,255,.35)', margin: '0 0 24px', fontFamily: 'var(--font-instrument-serif, serif)', fontStyle: 'italic' }}>
        Every codebase remembers its bugs.
      </p>
      <p style={{ marginTop: 0, fontSize: 13, color: 'rgba(255,255,255,.4)', fontFamily: 'var(--font-instrument-serif, serif)', fontStyle: 'italic', letterSpacing: '0.01em' }}>
        built by{' '}
        <a href="https://www.soharshh.com/" target="_blank" rel="noopener"
          style={{ color: 'rgba(255,255,255,.7)', textDecoration: 'none', borderBottom: '1px solid rgba(255,255,255,.2)', transition: 'color .15s, border-color .15s' }}
          onMouseEnter={e => { e.currentTarget.style.color = '#ffffff'; e.currentTarget.style.borderBottomColor = 'rgba(239,68,68,.6)' }}
          onMouseLeave={e => { e.currentTarget.style.color = 'rgba(255,255,255,.7)'; e.currentTarget.style.borderBottomColor = 'rgba(255,255,255,.2)' }}>
          Harsh
        </a>
        {' '}&amp;{' '}
        <a href="https://shivam-singh.dev/" target="_blank" rel="noopener"
          style={{ color: 'rgba(255,255,255,.7)', textDecoration: 'none', borderBottom: '1px solid rgba(255,255,255,.2)', transition: 'color .15s, border-color .15s' }}
          onMouseEnter={e => { e.currentTarget.style.color = '#ffffff'; e.currentTarget.style.borderBottomColor = 'rgba(239,68,68,.6)' }}
          onMouseLeave={e => { e.currentTarget.style.color = 'rgba(255,255,255,.7)'; e.currentTarget.style.borderBottomColor = 'rgba(255,255,255,.2)' }}>
          Shivam
        </a>
      </p>
    </footer>
  )
}

function LandingPage({ onLaunch }: { onLaunch: () => void }) {
  // Buttery smooth wheel/touchpad scrolling. Native CSS scroll-behavior only
  // applies to anchor jumps; Lenis intercepts wheel events for the same
  // ease-out feel on every flick.
  useEffect(() => {
    const lenis = new Lenis({
      duration: 1.1,
      easing: (t: number) => Math.min(1, 1.001 - Math.pow(2, -10 * t)),
      smoothWheel: true,
      lerp: 0.1,
    })
    let raf = 0
    const tick = (time: number) => {
      lenis.raf(time)
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => {
      cancelAnimationFrame(raf)
      lenis.destroy()
    }
  }, [])

  return (
    <div style={{ background: '#000000', color: '#ffffff', overflowX: 'hidden' }}>
      <LandingNav onLaunch={onLaunch}/>
      <HeroSection onLaunch={onLaunch}/>
      {/* Continuous black surface — sections sit on a single canvas with
          drifting ambient glows, a slow grid drift, and vertical scan
          beams so scrolling past the hero never feels like dead black. */}
      <div style={{ background: '#000000', position: 'relative', overflow: 'hidden' }}>
        {/* Drifting ambient red glows (CSS-driven, slow, never sync) */}
        <div className="bg-blob-1"
          style={{ position: 'absolute', top: '6%', left: '-10%', width: 520, height: 520, borderRadius: '50%', background: 'radial-gradient(circle, rgba(239,68,68,.18) 0%, transparent 70%)', pointerEvents: 'none', filter: 'blur(80px)', mixBlendMode: 'screen' }}/>
        <div className="bg-blob-2"
          style={{ position: 'absolute', top: '36%', right: '-14%', width: 580, height: 580, borderRadius: '50%', background: 'radial-gradient(circle, rgba(140,20,20,.20) 0%, transparent 70%)', pointerEvents: 'none', filter: 'blur(90px)', mixBlendMode: 'screen' }}/>
        <div className="bg-blob-3"
          style={{ position: 'absolute', top: '66%', left: '12%', width: 440, height: 440, borderRadius: '50%', background: 'radial-gradient(circle, rgba(239,68,68,.14) 0%, transparent 70%)', pointerEvents: 'none', filter: 'blur(80px)', mixBlendMode: 'screen' }}/>

        {/* Slowly drifting + pulsing grid for ambient depth */}
        <div className="bg-grid bg-grid-pulse"
          style={{
            position: 'absolute', inset: 0, pointerEvents: 'none',
            backgroundImage: 'linear-gradient(rgba(255,255,255,.07) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.07) 1px, transparent 1px)',
            backgroundSize: '60px 60px',
            maskImage: 'radial-gradient(ellipse 80% 50% at 50% 30%, black 30%, transparent 80%)',
            WebkitMaskImage: 'radial-gradient(ellipse 80% 50% at 50% 30%, black 30%, transparent 80%)',
          }}/>

        {/* Vertical scan beams — three offset so one is always crossing the viewport */}
        <div className="bg-scan-beam"
          style={{ position: 'absolute', top: 0, left: '22%', width: 1, height: 200, pointerEvents: 'none', background: 'linear-gradient(to bottom, transparent, rgba(239,68,68,.6), transparent)', boxShadow: '0 0 12px rgba(239,68,68,.4)' }}/>
        <div className="bg-scan-beam delay-1"
          style={{ position: 'absolute', top: 0, left: '64%', width: 1, height: 160, pointerEvents: 'none', background: 'linear-gradient(to bottom, transparent, rgba(239,68,68,.5), transparent)', boxShadow: '0 0 10px rgba(239,68,68,.35)' }}/>
        <div className="bg-scan-beam delay-2"
          style={{ position: 'absolute', top: 0, left: '85%', width: 1, height: 240, pointerEvents: 'none', background: 'linear-gradient(to bottom, transparent, rgba(239,68,68,.45), transparent)', boxShadow: '0 0 10px rgba(239,68,68,.3)' }}/>

        {/* Top-edge fade so the hero's dark bottom blends seamlessly into this canvas */}
        <div style={{ position: 'absolute', top: 0, left: 0, right: 0, height: 120, pointerEvents: 'none', background: 'linear-gradient(to bottom, #000000 0%, transparent 100%)' }}/>

        <div style={{ position: 'relative', zIndex: 1 }}>
          <HowItWorks/>
          <InstallSection onLaunch={onLaunch}/>
          <DemoSection/>
          <WhereYouWork/>
          <LandingFooter/>
        </div>
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
      style={{ background: '#0f0f0f', border: `1px solid ${active ? 'rgba(239,68,68,.2)' : '#1a1a1a'}`, borderRadius: 8, padding: '13px 14px', minWidth: 0, overflow: 'hidden' }}>
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
        <div style={{ marginBottom: 9, background: '#0a0a0a', border: '1px solid #1a1a1a', borderRadius: 5, padding: '8px 10px', minWidth: 0 }}>
          <div style={{ fontSize: 10, fontWeight: 600, color: '#333333', letterSpacing: '0.06em', textTransform: 'uppercase', marginBottom: 4 }}>Suggested fix</div>
          <pre style={{ fontSize: 11.5, color: '#9ec79e', lineHeight: 1.55, fontFamily: 'var(--font-fira-code, monospace)', margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word', overflowWrap: 'anywhere' }}>{w.proposed_fix}</pre>
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

  const [confirmEmail, setConfirmEmail] = useState(false)
  const [emailing, setEmailing] = useState(false)
  const [emailResult, setEmailResult] = useState<EmailReviewResponse | null>(null)
  const [emailError, setEmailError] = useState<string | null>(null)
  const [recipientEmail, setRecipientEmail] = useState('')
  useEffect(() => {
    try {
      const v = window.localStorage.getItem('scartissue:lastEmail')
      if (v) setRecipientEmail(v)
    } catch { /* localStorage may be disabled */ }
  }, [])

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

  // Page is the scroll surface; offset by the sticky stack (44 + 36 + 33 = 113) plus a little breathing room.
  const STICKY_OFFSET = 125
  const scrollWindowToEl = (el: HTMLElement | null) => {
    if (!el) return
    const top = el.getBoundingClientRect().top + window.scrollY - STICKY_OFFSET
    window.scrollTo({ top, behavior: 'smooth' })
  }

  const scrollCardToIdx = useCallback((idx: number) => {
    // Right panel is its own scroll surface; scrollIntoView walks to the nearest scrollable ancestor.
    cardRefs.current[idx]?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, [])

  const scrollHunkToIdx = useCallback((idx: number) => {
    scrollWindowToEl(hunkRefs.current[idx])
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
    scrollWindowToEl(fileRefs.current[fileIdx])
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

  const handleEmail = useCallback(() => {
    if (!recipientEmail || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(recipientEmail)) {
      setEmailError('Enter a valid email address.')
      return
    }
    setEmailing(true)
    setEmailError(null)
    try { window.localStorage.setItem('scartissue:lastEmail', recipientEmail) } catch { /* ignore */ }
    fetch('/api/email-review', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        pr_url: data.pr_url,
        pr_title: data.pr_title,
        pr_repo: data.pr_repo,
        pr_author: data.pr_author,
        recipient_email: recipientEmail,
        warnings: data.warnings,
      }),
    })
      .then(async r => {
        const body = await r.json().catch(() => ({ detail: `Server error ${r.status}` }))
        if (!r.ok) throw new Error(body.detail || `Server error ${r.status}`)
        return body as EmailReviewResponse
      })
      .then(result => {
        setEmailResult(result)
        setConfirmEmail(false)
      })
      .catch(err => setEmailError(err instanceof Error ? err.message : String(err)))
      .finally(() => setEmailing(false))
  }, [data.pr_url, data.pr_title, data.pr_repo, data.pr_author, data.warnings, recipientEmail])

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
    <div style={{ paddingTop: 44 }}>
      {/* Meta bar — sticky below the fixed AppHeader (44px) */}
      <div style={{ background: '#0a0a0a', borderBottom: '1px solid #1a1a1a', padding: '0 16px', height: 36, display: 'flex', alignItems: 'center', position: 'sticky', top: 44, zIndex: 50 }}>
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
          disabled={data.warnings.length === 0 || emailing || Boolean(emailResult)}
          onClick={() => { setEmailError(null); setConfirmEmail(true) }}
          title="Email the PR author the warnings via AgentMail."
          className="btn-outline"
          style={{ padding: '6px 11px', fontSize: 11.5, display: 'flex', alignItems: 'center', gap: 5, opacity: data.warnings.length === 0 || emailResult ? .45 : 1, cursor: data.warnings.length === 0 || emailResult ? 'not-allowed' : 'pointer', flexShrink: 0, marginRight: 6 }}>
          <Icon name="mail" size={12}/>
          {emailing ? 'Sending...' : emailResult ? 'Email sent' : 'Email Author'}
        </button>
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

      {/* File tabs — sticky below the meta bar (44 + 36 = 80) */}
      <div style={{ background: '#0a0a0a', borderBottom: '1px solid #1a1a1a', padding: '0 16px', height: 33, display: 'flex', alignItems: 'stretch', overflowX: 'auto', gap: 0, position: 'sticky', top: 80, zIndex: 40 }}>
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

      {/* Split pane — no internal scrolling; the page itself is the scroll surface */}
      <div style={{ display: 'flex', alignItems: 'flex-start' }}>
        {/* Left: hunk references */}
        <div ref={leftRef} style={{ width: '60%', borderRight: '1px solid #1a1a1a' }}>
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
              {/* Sticky file header — sits below AppHeader (44) + meta bar (36) + file tabs (33) = 113 */}
              <div ref={el => { fileRefs.current[fi] = el }}
                style={{ padding: '5px 10px', background: '#0a0a0a', borderTop: fi > 0 ? '1px solid #1a1a1a' : 'none', borderBottom: '1px solid #1a1a1a', display: 'flex', alignItems: 'center', gap: 6, position: 'sticky', top: 113, zIndex: 30 }}>
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

        {/* Right: warning cards — sticky, with its own scrollbar so the user can scroll through all warnings while the left rail stays in view */}
        <div ref={rightRef} style={{ width: '40%', padding: 12, display: 'flex', flexDirection: 'column', gap: 8, background: '#0a0a0a', position: 'sticky', top: 113, alignSelf: 'flex-start', maxHeight: 'calc(100vh - 113px)', overflowY: 'auto' }}>
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
          {(emailResult || emailError) && (
            <div style={{ background: emailResult ? 'rgba(34,197,94,.06)' : 'rgba(239,68,68,.07)', border: `1px solid ${emailResult ? 'rgba(34,197,94,.18)' : 'rgba(239,68,68,.2)'}`, borderRadius: 7, padding: '9px 10px', fontSize: 11.5, color: emailResult ? '#6fcf7f' : '#ef4444', display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10 }}>
              {emailResult ? (
                <span>Emailed {emailResult.warnings_sent} warning{emailResult.warnings_sent !== 1 ? 's' : ''} to {emailResult.recipient}{emailResult.inbox_address && <> · from <span style={{ fontFamily: 'var(--font-fira-code, monospace)' }}>{emailResult.inbox_address}</span></>}</span>
              ) : (
                <>
                  <span>{emailError}</span>
                  <button onClick={() => setConfirmEmail(true)} style={{ background: 'none', border: 'none', color: '#f0a0a0', cursor: 'pointer', fontSize: 11.5, fontFamily: 'var(--font-space-grotesk, sans-serif)' }}>Retry</button>
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
      {confirmEmail && (
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,.68)', backdropFilter: 'blur(6px)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 200 }}>
          <div style={{ width: 'min(440px, calc(100vw - 32px))', background: '#0f0f0f', border: '1px solid #252525', borderRadius: 10, boxShadow: '0 18px 70px rgba(0,0,0,.5)', padding: 18 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10 }}>
              <div style={{ width: 28, height: 28, borderRadius: 7, background: 'rgba(168,216,168,.09)', border: '1px solid rgba(168,216,168,.2)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                <Icon name="mail" size={14} stroke="#a8d8a8"/>
              </div>
              <h3 style={{ margin: 0, fontSize: 15, color: '#e5e5e5', letterSpacing: '-0.01em' }}>Email PR author</h3>
            </div>
            <p style={{ margin: '0 0 12px', color: '#a0a0a8', fontSize: 13, lineHeight: 1.5 }}>
              Send the {data.warnings.length} warning{data.warnings.length !== 1 ? 's' : ''} on this PR as an HTML email.
            </p>
            <label htmlFor="scartissue-email-input" style={{ display: 'block', fontSize: 11, color: '#888', marginBottom: 5, fontFamily: 'var(--font-space-grotesk, sans-serif)' }}>Recipient email</label>
            <input
              id="scartissue-email-input"
              type="email"
              autoFocus
              required
              placeholder="author@example.com"
              value={recipientEmail}
              onChange={e => { setRecipientEmail(e.target.value); if (emailError) setEmailError(null) }}
              onKeyDown={e => { if (e.key === 'Enter' && !emailing) handleEmail() }}
              style={{ width: '100%', padding: '9px 10px', background: '#0a0a0a', border: '1px solid #252525', borderRadius: 6, color: '#e5e5e5', fontSize: 13, fontFamily: 'var(--font-fira-code, monospace)', outline: 'none', boxSizing: 'border-box' }}
            />
            <p style={{ margin: '10px 0 14px', color: '#444444', fontSize: 11.5, lineHeight: 1.45 }}>
              Powered by <a href="https://agentmail.to" target="_blank" rel="noreferrer" style={{ color: '#777', textDecoration: 'none' }}>AgentMail</a>. Requires AGENTMAIL_API_KEY on the backend.
            </p>
            {emailError && <div style={{ marginBottom: 12, background: 'rgba(239,68,68,.07)', border: '1px solid rgba(239,68,68,.18)', borderRadius: 6, padding: '8px 9px', color: '#ef4444', fontSize: 11.5 }}>{emailError}</div>}
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
              <button className="btn-outline" disabled={emailing} onClick={() => setConfirmEmail(false)} style={{ padding: '8px 14px', fontSize: 12.5, opacity: emailing ? .5 : 1 }}>Cancel</button>
              <button className="btn-primary" disabled={emailing || !recipientEmail} onClick={handleEmail} style={{ padding: '8px 14px', fontSize: 12.5, minWidth: 100, opacity: (emailing || !recipientEmail) ? .55 : 1 }}>
                {emailing ? 'Sending...' : 'Send email'}
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
