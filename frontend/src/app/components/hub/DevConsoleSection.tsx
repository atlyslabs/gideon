"use client"

import { useCallback, useEffect, useState } from "react"
import {
  ChevronDown,
  ChevronRight,
  GitMerge,
  GitPullRequest,
  MessageSquare,
  RefreshCw,
  Terminal as TerminalIcon,
} from "lucide-react"
import useSWR from "swr"
import { pingman } from "../../utils/pingman"

// ---------------------------------------------------------------------------
// Types — mirror dev_console_router.py response models
// ---------------------------------------------------------------------------

interface PullRequestRef {
  repo: string
  number: number
  title: string
  url: string
  author: string
  state: string
  draft: boolean
  is_review_request: boolean
  updated_at: string
}

interface GitHubPayload {
  connected: boolean
  login?: string
  review_requests: PullRequestRef[]
  authored: PullRequestRef[]
}

interface LinearIssueRef {
  id: string
  identifier: string
  title: string
  url: string
  state: string
  state_type: string
  priority: number
  team: string
  updated_at: string
}

interface LinearWorkflowState {
  id: string
  name: string
  type: string
  team_id: string
  team_key: string
}

interface LinearPayload {
  connected: boolean
  issues: LinearIssueRef[]
  workflow_states: LinearWorkflowState[]
}

// ---------------------------------------------------------------------------
// Theme — "dev console" aesthetic. Translucent black panel atop the weather
// video, JetBrains-mono everywhere, phosphor-green primary, amber/red for
// state.
// ---------------------------------------------------------------------------

// Solid panel background so the weather video underneath the right-panel
// doesn't bleed through the rows and create the false "alternating row tones"
// effect. The dev console is meant to feel like a self-contained terminal
// pane, not a video overlay.
const C = {
  bg: "#050a08",          // panel chrome (sticky headers + outer container)
  bgRow: "transparent",   // rows inherit panel bg — no per-row video bleed
  border: "rgba(0, 255, 140, 0.18)",
  borderStrong: "rgba(0, 255, 140, 0.35)",
  textPrimary: "#d8ffe8",
  textDim: "rgba(216, 255, 232, 0.55)",
  textVery: "rgba(216, 255, 232, 0.35)",
  green: "#00ff8c",
  greenDim: "rgba(0, 255, 140, 0.7)",
  amber: "#ffb347",
  red: "#ff6b6b",
  blue: "#6bc6ff",
} as const

const MONO = "'JetBrains Mono', 'Fira Code', 'SF Mono', Menlo, Consolas, monospace"

// ---------------------------------------------------------------------------
// Fetchers
// ---------------------------------------------------------------------------

async function gh(url: string): Promise<GitHubPayload> {
  const r = await pingman.get<GitHubPayload>(url)
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return r.data
}

async function lnr(url: string): Promise<LinearPayload> {
  const r = await pingman.get<LinearPayload>(url)
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return r.data
}

// ---------------------------------------------------------------------------
// Bits
// ---------------------------------------------------------------------------

function Prompt({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.18em]" style={{ fontFamily: MONO, color: C.greenDim }}>
      <span style={{ color: C.green }}>$</span>
      <span>{children}</span>
    </div>
  )
}

function StatusDot({ color }: { color: string }) {
  return (
    <span
      className="inline-block w-1.5 h-1.5 rounded-full shrink-0"
      style={{ background: color, boxShadow: `0 0 8px ${color}` }}
    />
  )
}

function linearStateColor(stateType: string): string {
  switch (stateType) {
    case "started":
      return C.amber
    case "completed":
      return C.green
    case "canceled":
      return C.red
    case "triage":
      return C.red
    case "unstarted":
      return C.blue
    default:
      return C.textVery
  }
}

function relativeTime(iso: string): string {
  if (!iso) return ""
  const d = new Date(iso)
  const diff = Date.now() - d.getTime()
  const m = Math.floor(diff / 60_000)
  if (m < 1) return "now"
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h`
  const days = Math.floor(h / 24)
  if (days < 30) return `${days}d`
  return d.toLocaleDateString([], { month: "short", day: "numeric" })
}

// ---------------------------------------------------------------------------
// PR row — inline merge + comment composer
// ---------------------------------------------------------------------------

function PRRow({ pr, onAfterAction }: { pr: PullRequestRef; onAfterAction: () => void }) {
  const [composerOpen, setComposerOpen] = useState(false)
  const [comment, setComment] = useState("")
  const [busy, setBusy] = useState<null | "merge" | "comment">(null)
  const [flash, setFlash] = useState<string | null>(null)

  const [owner, name] = pr.repo.split("/")
  const cleanRepo = name || pr.repo

  const submitComment = useCallback(async () => {
    if (!comment.trim()) return
    setBusy("comment")
    try {
      const r = await pingman.post<{ ok: boolean }>(
        `/api/gideon/v1/dev-console/github/pr/${owner}/${name}/${pr.number}/comment`,
        { body: comment }
      )
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      setComment("")
      setComposerOpen(false)
      setFlash("commented")
      setTimeout(() => setFlash(null), 1800)
    } catch (e) {
      setFlash(`× ${e instanceof Error ? e.message : "failed"}`)
      setTimeout(() => setFlash(null), 2800)
    } finally {
      setBusy(null)
    }
  }, [comment, owner, name, pr.number])

  const merge = useCallback(async () => {
    if (!confirm(`Merge ${pr.repo}#${pr.number}?`)) return
    setBusy("merge")
    try {
      const r = await pingman.post<{ ok: boolean; sha: string }>(
        `/api/gideon/v1/dev-console/github/pr/${owner}/${name}/${pr.number}/merge`,
        { method: "squash" }
      )
      if (!r.ok) {
        const errMsg = (r.data as unknown as { detail?: string })?.detail || `HTTP ${r.status}`
        throw new Error(errMsg)
      }
      setFlash("merged ✓")
      setTimeout(() => { setFlash(null); onAfterAction() }, 1500)
    } catch (e) {
      setFlash(`× ${e instanceof Error ? e.message : "merge failed"}`)
      setTimeout(() => setFlash(null), 3500)
    } finally {
      setBusy(null)
    }
  }, [owner, name, pr.number, pr.repo, onAfterAction])

  return (
    <div
      className="px-3 py-2 transition-colors"
      style={{
        borderBottom: `1px solid ${C.border}`,
        background: C.bgRow,
        fontFamily: MONO,
      }}
    >
      <div className="flex items-start gap-2">
        <span style={{ color: pr.draft ? C.textVery : C.green, marginTop: 2 }}>
          <GitPullRequest size={12} />
        </span>
        <div className="flex-1 min-w-0">
          <div className="flex items-baseline gap-1.5 text-[12px]">
            <a
              href={pr.url}
              target="_blank"
              rel="noopener noreferrer"
              className="hover:underline truncate"
              style={{ color: C.textPrimary }}
              title={pr.title}
            >
              {pr.title}
            </a>
          </div>
          <div className="flex items-center gap-2 text-[10.5px] mt-0.5" style={{ color: C.textDim }}>
            <span>{cleanRepo}#{pr.number}</span>
            <span>·</span>
            <span>@{pr.author}</span>
            <span>·</span>
            <span>{relativeTime(pr.updated_at)}</span>
            {pr.draft && (
              <>
                <span>·</span>
                <span style={{ color: C.amber }}>draft</span>
              </>
            )}
            {pr.is_review_request && (
              <>
                <span>·</span>
                <span style={{ color: C.amber }}>review</span>
              </>
            )}
          </div>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          <button
            onClick={() => setComposerOpen(v => !v)}
            disabled={busy !== null}
            title="Comment"
            className="px-1.5 py-1 rounded transition-colors hover:bg-white/10"
            style={{ color: C.textDim, fontFamily: MONO }}
          >
            <MessageSquare size={11} />
          </button>
          {!pr.is_review_request && !pr.draft && (
            <button
              onClick={merge}
              disabled={busy !== null}
              title="Squash and merge"
              className="px-1.5 py-1 rounded transition-colors hover:bg-white/10"
              style={{ color: busy === "merge" ? C.amber : C.greenDim }}
            >
              <GitMerge size={11} />
            </button>
          )}
        </div>
      </div>

      {composerOpen && (
        <div className="mt-2 pl-5">
          <textarea
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder="// type a comment, ⌘+Enter to send"
            rows={2}
            onKeyDown={(e) => {
              if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                e.preventDefault()
                submitComment()
              }
            }}
            className="w-full text-[11.5px] px-2 py-1.5 rounded outline-none resize-none"
            style={{
              fontFamily: MONO,
              background: "rgba(0,0,0,0.55)",
              color: C.textPrimary,
              border: `1px solid ${C.border}`,
            }}
          />
          <div className="flex items-center justify-end gap-2 mt-1">
            <button
              onClick={() => { setComposerOpen(false); setComment("") }}
              className="text-[10.5px] px-2 py-0.5 rounded hover:bg-white/5"
              style={{ color: C.textDim, fontFamily: MONO }}
            >
              esc
            </button>
            <button
              onClick={submitComment}
              disabled={busy !== null || !comment.trim()}
              className="text-[10.5px] px-2 py-0.5 rounded transition-colors disabled:opacity-40"
              style={{
                background: `linear-gradient(180deg, ${C.green}22, ${C.green}11)`,
                color: C.green,
                border: `1px solid ${C.borderStrong}`,
                fontFamily: MONO,
              }}
            >
              {busy === "comment" ? "..." : "send"}
            </button>
          </div>
        </div>
      )}

      {flash && (
        <div className="text-[10.5px] mt-1 pl-5" style={{ color: flash.startsWith("×") ? C.red : C.green, fontFamily: MONO }}>
          {flash}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Linear issue row — inline state dropdown + comment
// ---------------------------------------------------------------------------

function LinearRow({
  issue,
  states,
  onAfterAction,
}: {
  issue: LinearIssueRef
  states: LinearWorkflowState[]
  onAfterAction: () => void
}) {
  const [composerOpen, setComposerOpen] = useState(false)
  const [comment, setComment] = useState("")
  const [busy, setBusy] = useState<null | "state" | "comment">(null)
  const [flash, setFlash] = useState<string | null>(null)
  const [localState, setLocalState] = useState({ name: issue.state, type: issue.state_type })

  // Sync localState when the parent re-fetches and the issue's state changed
  // externally (someone else moved it in Linear). Without this, the row keeps
  // showing the stale state we initialised from on mount because React reuses
  // the component instance across renders (keyed by issue.id).
  useEffect(() => {
    setLocalState({ name: issue.state, type: issue.state_type })
  }, [issue.state, issue.state_type])

  // Linear team key prefix on identifier (e.g. "ENG-123" → "ENG"). Used to
  // scope the workflow-state dropdown to states for THIS team only.
  const teamKey = issue.identifier.split("-")[0] || ""
  const teamStates = states.filter(s => s.team_key === teamKey)

  const setState = useCallback(async (stateId: string) => {
    setBusy("state")
    try {
      const r = await pingman.post<{ ok: boolean; state: { name: string; type: string } }>(
        `/api/gideon/v1/dev-console/linear/issue/${issue.id}/state`,
        { state_id: stateId }
      )
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      setLocalState(r.data.state)
      setFlash("updated ✓")
      setTimeout(() => { setFlash(null); onAfterAction() }, 1500)
    } catch (e) {
      setFlash(`× ${e instanceof Error ? e.message : "failed"}`)
      setTimeout(() => setFlash(null), 2800)
    } finally {
      setBusy(null)
    }
  }, [issue.id, onAfterAction])

  const submitComment = useCallback(async () => {
    if (!comment.trim()) return
    setBusy("comment")
    try {
      const r = await pingman.post<{ ok: boolean }>(
        `/api/gideon/v1/dev-console/linear/issue/${issue.id}/comment`,
        { body: comment }
      )
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      setComment("")
      setComposerOpen(false)
      setFlash("commented")
      setTimeout(() => setFlash(null), 1800)
    } catch (e) {
      setFlash(`× ${e instanceof Error ? e.message : "failed"}`)
      setTimeout(() => setFlash(null), 2800)
    } finally {
      setBusy(null)
    }
  }, [issue.id, comment])

  return (
    <div
      className="px-3 py-2"
      style={{
        borderBottom: `1px solid ${C.border}`,
        background: C.bgRow,
        fontFamily: MONO,
      }}
    >
      <div className="flex items-start gap-2">
        <span style={{ marginTop: 4 }}>
          <StatusDot color={linearStateColor(localState.type)} />
        </span>
        <div className="flex-1 min-w-0">
          <a
            href={issue.url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-[12px] hover:underline truncate block"
            style={{ color: C.textPrimary }}
            title={issue.title}
          >
            {issue.title}
          </a>
          <div className="flex items-center gap-2 text-[10.5px] mt-0.5" style={{ color: C.textDim }}>
            <span>{issue.identifier}</span>
            <span>·</span>
            <span>{issue.team}</span>
            <span>·</span>
            <span>{relativeTime(issue.updated_at)}</span>
          </div>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          <button
            onClick={() => setComposerOpen(v => !v)}
            disabled={busy !== null}
            title="Comment"
            className="px-1.5 py-1 rounded hover:bg-white/10"
            style={{ color: C.textDim }}
          >
            <MessageSquare size={11} />
          </button>
        </div>
      </div>

      {teamStates.length > 0 && (
        <div className="flex items-center gap-1 mt-1.5 pl-5 flex-wrap">
          {teamStates.map(s => {
            const active = s.name === localState.name
            return (
              <button
                key={s.id}
                onClick={() => !active && setState(s.id)}
                disabled={busy !== null || active}
                className="text-[10px] px-1.5 py-0.5 rounded transition-colors"
                style={{
                  background: active ? `${linearStateColor(s.type)}22` : "transparent",
                  border: `1px solid ${active ? `${linearStateColor(s.type)}66` : C.border}`,
                  color: active ? linearStateColor(s.type) : C.textDim,
                  fontFamily: MONO,
                  cursor: active ? "default" : "pointer",
                }}
              >
                {s.name.toLowerCase()}
              </button>
            )
          })}
        </div>
      )}

      {composerOpen && (
        <div className="mt-2 pl-5">
          <textarea
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder="// comment, ⌘+Enter to send"
            rows={2}
            onKeyDown={(e) => {
              if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                e.preventDefault()
                submitComment()
              }
            }}
            className="w-full text-[11.5px] px-2 py-1.5 rounded outline-none resize-none"
            style={{
              fontFamily: MONO,
              background: "rgba(0,0,0,0.55)",
              color: C.textPrimary,
              border: `1px solid ${C.border}`,
            }}
          />
          <div className="flex items-center justify-end gap-2 mt-1">
            <button
              onClick={() => { setComposerOpen(false); setComment("") }}
              className="text-[10.5px] px-2 py-0.5 rounded hover:bg-white/5"
              style={{ color: C.textDim, fontFamily: MONO }}
            >
              esc
            </button>
            <button
              onClick={submitComment}
              disabled={busy !== null || !comment.trim()}
              className="text-[10.5px] px-2 py-0.5 rounded transition-colors disabled:opacity-40"
              style={{
                background: `linear-gradient(180deg, ${C.green}22, ${C.green}11)`,
                color: C.green,
                border: `1px solid ${C.borderStrong}`,
                fontFamily: MONO,
              }}
            >
              {busy === "comment" ? "..." : "send"}
            </button>
          </div>
        </div>
      )}

      {flash && (
        <div className="text-[10.5px] mt-1 pl-5" style={{ color: flash.startsWith("×") ? C.red : C.green, fontFamily: MONO }}>
          {flash}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Section
// ---------------------------------------------------------------------------

function SectionHeader({
  label,
  count,
  loading,
  onRefresh,
}: {
  label: string
  count: number
  loading: boolean
  onRefresh: () => void
}) {
  return (
    <div
      className="flex items-center justify-between px-3 py-1.5 sticky top-0 z-10"
      style={{
        background: C.bg,
        borderBottom: `1px solid ${C.border}`,
        backdropFilter: "blur(6px)",
        fontFamily: MONO,
      }}
    >
      <Prompt>
        {label} <span style={{ color: C.textVery }}>({count})</span>
      </Prompt>
      <button
        onClick={onRefresh}
        disabled={loading}
        className="px-1.5 py-1 rounded hover:bg-white/10 disabled:opacity-40"
        title="Refetch"
        style={{ color: C.greenDim }}
      >
        <RefreshCw size={11} style={{ animation: loading ? "spin 0.8s linear infinite" : "none" }} />
      </button>
    </div>
  )
}

function EmptyLine({ children }: { children: React.ReactNode }) {
  return (
    <div className="px-3 py-3 text-[11px]" style={{ color: C.textVery, fontFamily: MONO }}>
      {children}
    </div>
  )
}

function NotConnectedRow({ provider, label }: { provider: string; label: string }) {
  return (
    <EmptyLine>
      <span style={{ color: C.red }}>×</span>{" "}
      <span>{label} not connected.</span>{" "}
      <a href={`/integrations?connect=${provider}`} className="underline" style={{ color: C.green }}>
        connect →
      </a>
    </EmptyLine>
  )
}

// ---------------------------------------------------------------------------
// Grouping — collapsible sections by repo / team
// ---------------------------------------------------------------------------

/** Group an array of items by a string key. Returns groups sorted by display
 * name. The `keyFn` produces the unique grouping identity (must be globally
 * unique — e.g. full "owner/repo"), `labelFn` produces the human-readable
 * display name (e.g. just "repo"). Two groups can legitimately share a label
 * across different orgs and they stay separate, which is the whole point. */
function groupBy<T>(
  items: T[],
  keyFn: (item: T) => string,
  labelFn: (item: T) => string = keyFn,
): { key: string; label: string; items: T[] }[] {
  const map = new Map<string, { label: string; items: T[] }>()
  for (const item of items) {
    const k = keyFn(item) || "(unknown)"
    const existing = map.get(k)
    if (existing) existing.items.push(item)
    else map.set(k, { label: labelFn(item) || k, items: [item] })
  }
  return Array.from(map.entries())
    .sort((a, b) => a[1].label.localeCompare(b[1].label))
    .map(([key, { label, items }]) => ({ key, label, items }))
}

/** Strip "owner/" from a GitHub repo string for display only. The owner is
 * usually constant across a single user's PRs so the short name reads cleaner,
 * but `groupBy` must still key on the full slug to avoid merging same-name
 * repos from different orgs. */
function shortRepo(repo: string): string {
  const slash = repo.lastIndexOf("/")
  return slash >= 0 ? repo.slice(slash + 1) : repo
}

const COLLAPSED_STORAGE_KEY = "gideon:dev-console:collapsed"

/** Hook: read + write a single collapsed-group flag from localStorage.
 *
 * Storage layout: one JSON object under COLLAPSED_STORAGE_KEY mapping
 * "<sectionId>::<groupKey>" → true (collapsed). Absence = expanded. We keep
 * a single key so a future "expand all" / "collapse all" affordance is one
 * setItem.
 *
 * Read happens in an effect after mount (rather than in the useState
 * initializer) so server-rendered HTML and client first-paint always agree
 * (collapsed=false). After hydration the effect runs once and syncs from
 * localStorage. Users who previously collapsed a group will see a one-frame
 * "expanded → collapsed" transition; that's the trade for guaranteed
 * hydration parity. The dev console only mounts post-auth client-side
 * today, but treating localStorage as client-only keeps this hook safe to
 * use anywhere. */
function useCollapsed(sectionId: string, groupKey: string): [boolean, () => void] {
  const storageKey = `${sectionId}::${groupKey}`
  const [collapsed, setCollapsed] = useState<boolean>(false)

  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(COLLAPSED_STORAGE_KEY)
      if (!raw) return
      const parsed = JSON.parse(raw) as Record<string, boolean>
      if (parsed[storageKey] === true) setCollapsed(true)
    } catch {
      // localStorage unavailable / malformed JSON — leave default (expanded).
    }
  }, [storageKey])

  const toggle = useCallback(() => {
    setCollapsed(prev => {
      const next = !prev
      try {
        const raw = window.localStorage.getItem(COLLAPSED_STORAGE_KEY)
        const parsed: Record<string, boolean> = raw ? JSON.parse(raw) : {}
        if (next) parsed[storageKey] = true
        else delete parsed[storageKey]
        window.localStorage.setItem(COLLAPSED_STORAGE_KEY, JSON.stringify(parsed))
      } catch {
        // localStorage unavailable / quota — fail soft, UI state still works for the session.
      }
      return next
    })
  }, [storageKey])

  return [collapsed, toggle]
}

function CollapsibleGroup({
  sectionId,
  groupKey,
  label,
  count,
  children,
}: {
  sectionId: string
  groupKey: string
  label: string
  count: number
  children: React.ReactNode
}) {
  const [collapsed, toggle] = useCollapsed(sectionId, groupKey)
  return (
    <div>
      <button
        onClick={toggle}
        className="w-full flex items-center gap-1.5 px-3 py-1 text-left transition-colors hover:bg-white/[0.03]"
        style={{ fontFamily: MONO, borderBottom: `1px solid ${C.border}` }}
        aria-expanded={!collapsed}
        title={groupKey !== label ? groupKey : undefined}
      >
        <span style={{ color: C.greenDim }}>
          {collapsed ? <ChevronRight size={10} /> : <ChevronDown size={10} />}
        </span>
        <span className="text-[11px]" style={{ color: C.textPrimary }}>{label}</span>
        <span className="text-[10.5px]" style={{ color: C.textVery }}>({count})</span>
      </button>
      {!collapsed && <div>{children}</div>}
    </div>
  )
}

export default function DevConsoleSection() {
  const { data: ghData, error: ghErr, isLoading: ghLoading, mutate: refetchGh } = useSWR<GitHubPayload>(
    "/api/gideon/v1/dev-console/github",
    gh,
    { revalidateOnFocus: false, refreshInterval: 0 }
  )
  const { data: lnrData, error: lnrErr, isLoading: lnrLoading, mutate: refetchLnr } = useSWR<LinearPayload>(
    "/api/gideon/v1/dev-console/linear",
    lnr,
    { revalidateOnFocus: false, refreshInterval: 0 }
  )

  // Render a "boot" line on mount for flavor.
  const [bootStamp] = useState(() => new Date().toLocaleTimeString([], { hour12: false }))

  return (
    <div
      // min-h-full (not h-full) so the wrapper grows with content. h-full
      // makes the wrapper viewport-sized; rows that overflow into the scroll
      // area then render outside the painted background, and the right-panel
      // weather video bleeds through as teal patches behind transparent rows.
      className="min-h-full flex flex-col"
      style={{ background: C.bg, fontFamily: MONO }}
    >
      {/* Header strip — "boot" line + last-fetched */}
      <div
        className="px-3 py-2 text-[10.5px] flex items-center gap-2"
        style={{ borderBottom: `1px solid ${C.border}`, color: C.greenDim }}
      >
        <TerminalIcon size={12} style={{ color: C.green }} />
        <span style={{ color: C.textVery }}>[{bootStamp}]</span>
        <span>gideon dev-console v1 — github + linear, live</span>
      </div>

      {/* GitHub */}
      <SectionHeader
        label="github pr review queue"
        count={(ghData?.review_requests?.length ?? 0)}
        loading={ghLoading}
        onRefresh={() => refetchGh()}
      />
      {ghErr ? (
        <EmptyLine><span style={{ color: C.red }}>× error:</span> {String(ghErr)}</EmptyLine>
      ) : !ghData ? (
        <EmptyLine>fetching…</EmptyLine>
      ) : !ghData.connected ? (
        <NotConnectedRow provider="github" label="github" />
      ) : ghData.review_requests.length === 0 ? (
        <EmptyLine>// inbox zero — nothing awaiting your review</EmptyLine>
      ) : (
        <div>
          {groupBy(ghData.review_requests, pr => pr.repo, pr => shortRepo(pr.repo)).map(group => (
            <CollapsibleGroup
              key={`gh-review:${group.key}`}
              sectionId="gh-review"
              groupKey={group.key}
              label={group.label}
              count={group.items.length}
            >
              {group.items.map(pr => (
                <PRRow key={`${pr.repo}#${pr.number}`} pr={pr} onAfterAction={() => refetchGh()} />
              ))}
            </CollapsibleGroup>
          ))}
        </div>
      )}

      <SectionHeader
        label="github your open prs"
        count={(ghData?.authored?.length ?? 0)}
        loading={ghLoading}
        onRefresh={() => refetchGh()}
      />
      {ghData?.connected && ghData.authored.length === 0 ? (
        <EmptyLine>// no open PRs authored by you</EmptyLine>
      ) : ghData?.connected ? (
        <div>
          {groupBy(ghData.authored, pr => pr.repo, pr => shortRepo(pr.repo)).map(group => (
            <CollapsibleGroup
              key={`gh-authored:${group.key}`}
              sectionId="gh-authored"
              groupKey={group.key}
              label={group.label}
              count={group.items.length}
            >
              {group.items.map(pr => (
                <PRRow key={`${pr.repo}#${pr.number}-auth`} pr={pr} onAfterAction={() => refetchGh()} />
              ))}
            </CollapsibleGroup>
          ))}
        </div>
      ) : null}

      {/* Linear */}
      <SectionHeader
        label="linear assigned"
        count={(lnrData?.issues?.length ?? 0)}
        loading={lnrLoading}
        onRefresh={() => refetchLnr()}
      />
      {lnrErr ? (
        <EmptyLine><span style={{ color: C.red }}>× error:</span> {String(lnrErr)}</EmptyLine>
      ) : !lnrData ? (
        <EmptyLine>fetching…</EmptyLine>
      ) : !lnrData.connected ? (
        <NotConnectedRow provider="linear" label="linear" />
      ) : lnrData.issues.length === 0 ? (
        <EmptyLine>// no open issues assigned to you</EmptyLine>
      ) : (
        <div>
          {groupBy(lnrData.issues, issue => issue.team).map(group => (
            <CollapsibleGroup
              key={`lnr:${group.key}`}
              sectionId="lnr"
              groupKey={group.key}
              label={group.label}
              count={group.items.length}
            >
              {group.items.map(issue => (
                <LinearRow
                  key={issue.id}
                  issue={issue}
                  states={lnrData.workflow_states}
                  onAfterAction={() => refetchLnr()}
                />
              ))}
            </CollapsibleGroup>
          ))}
        </div>
      )}

      <div style={{ height: 24 }} />
    </div>
  )
}
