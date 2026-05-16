# GBrain API Spike Report — SCA-5

**Date:** 2026-05-16  
**Verdict: GO — proceed with Approach B (Live Immune System)**

---

## Installation

- **Method:** `~/.claude/skills/gstack/bin/gstack-gbrain-install` (cloned garrytan/gbrain from GitHub, pinned to v0.18.2 commit `08b3698`)
- **Install path:** `~/gbrain`
- **Version:** `gbrain 0.18.2`
- **Engine:** PGLite local (no cloud, no server — brain at `~/.gbrain/brain.pglite`)

```bash
gbrain --version   # → gbrain 0.18.2
gbrain doctor --json  # → status: "warnings" (expected on fresh empty brain — connection + schema OK)
```

The `warnings` status on doctor is expected: no embeddings yet (empty brain), no skills directory wired. Core checks (`connection`, `schema_version`) are `ok`.

---

## API Surface (confirmed)

### Write
```bash
gbrain put <slug> <<'EOF'
---
title: "..."
tags: [scartissue, bugpattern]
confidence: 0.9
source_commit: abc123
learned_from: live_warning
---

## Body content here
EOF
```

- Slug must be unique, kebab-case. Use `bugpattern-{uuid4}` to guarantee uniqueness.
- YAML frontmatter fields are **preserved and returned** on `gbrain get`.
- No `--metadata` JSON flag exists — structured data goes in YAML frontmatter.
- **Write latency p50: ~437ms**

### Search
```bash
gbrain query "LLMChain input validation missing"   # hybrid RRF (~250ms)
gbrain search "LLMChain input validation"           # keyword tsvector (~476ms)
```

Output format per result:
```
[0.9998] bugpattern-llmchain-input-variables-missing -- ## BugPattern\ntrigger_condition: ...
```

**Search latency p50: ~250ms (hybrid query)** — well within the 3s timeout budget. Synchronous call in the agent loop is safe.

### Read
```bash
gbrain get <slug>   # returns full markdown page with YAML frontmatter
```

---

## Metadata: YAML frontmatter (not JSON)

GBrain does **not** have a `--metadata` JSON flag. All structured fields are stored as YAML frontmatter keys in the page content. They are:
- Preserved verbatim on write
- Returned in full on `gbrain get`
- Searchable semantically via `gbrain query`

BugPattern fields are serialized as frontmatter (`confidence`, `source_commit`, `source_repo`, `learned_from`, `affected_symbols`, `pattern_id`) plus a structured markdown body for semantic searchability.

---

## Test Results

| Test | Result | Notes |
|------|--------|-------|
| `gbrain put bugpattern-llmchain-...` | ✅ PASS | slug created, chunks: 1 |
| `gbrain search "LLMChain input validation"` | ✅ PASS | score 0.9998, correct page |
| `gbrain query "LLMChain input validation missing"` | ✅ PASS | score 1.0000, correct page |
| `gbrain get bugpattern-llmchain-...` | ✅ PASS | full YAML frontmatter returned |
| YAML frontmatter metadata | ✅ PASS | all fields preserved |
| Write latency | ✅ 437ms | under 3s threshold |
| Search latency (hybrid) | ✅ 243ms | well under 3s threshold |

---

## Surprises / Gotchas

1. **Wrong npm package.** `bun install -g gbrain` installs an unrelated graph neural network library. The correct gbrain is `garrytan/gbrain` on GitHub — must use the gstack installer or clone directly.

2. **Command is `gbrain put`, not `gbrain put_page`.** The docs and gstack guide reference `put_page` but the actual CLI command is `put <slug>`.

3. **`gbrain query` (hybrid) is faster than `gbrain search` (keyword)** — 243ms vs 476ms in testing. Use `query` in the agent loop.

4. **Doctor shows "warnings" on fresh install.** Expected — no embeddings, no skills directory. Not an error condition. Connection and schema are OK.

5. **Binary not on PATH after bun install.** The `bun link` step in the gstack installer puts the binary at `~/gbrain/node_modules/.bin/gbrain`, not `~/.bun/bin/gbrain`. The client resolves this automatically.

---

## env vars added to backend/.env

```
GBRAIN_ORG_ID=scartissue-gbrain-hackathon
GBRAIN_PGLITE_PATH=~/.gbrain/brain.pglite
```

No API key needed for PGLite local engine.

---

## Verdict

**GO on Approach B.** GBrain write + search both work, latency is well within the 3s agent-loop timeout, and YAML frontmatter gives us full structured metadata. The `gbrain_client.py` implementation is complete in `backend/app/services/gbrain_client.py`.

Next: SCA-10 (add `learn_from_incident` tool to reviewer.py) and SCA-8 (add `search_with_gbrain()` to scar_index.py) — both unblocked.
