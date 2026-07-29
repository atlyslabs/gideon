"use client"

/*
 * Memory Graph — default Memory page.
 *
 * Data source: GET /memories/graph returns { nodes, edges } from Graphiti
 * (self-hosted Neo4j, per-user group_id scoping).
 *
 * Top-right "Add memory" button opens a modal — submit posts to
 * /memories/graph/add which ingests in the background (LLM extraction takes
 * 1-5s). We revalidate after 6s so new nodes appear automatically.
 *
 * Delete: per-fact trash is handled inside GraphCanvas's focus panel;
 * "Forget this entirely" removes an entity + all its facts.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import useSWR from "swr"
import { ArrowLeft, Loader2, Network, Plus, X } from "lucide-react"
import { pingman } from "../utils/pingman"
import GraphCanvas from "./GraphCanvas"

// ---------------------------------------------------------------------------
// Types — shape of the /memories/graph response.
// Keep these in sync with routers/memory_router.py::MemoryGraphResponse.
// ---------------------------------------------------------------------------

export type NodeKind = "person" | "project" | "company" | "preference" | "topic" | "other"

export type GraphNode = {
  id: string
  name: string
  summary: string
  labels: string[]
  kind: NodeKind
  degree: number
}

export type GraphEdge = {
  id: string
  source: string
  target: string
  fact: string
  created_at: string
}

export type MemoryGraph = {
  nodes: GraphNode[]
  edges: GraphEdge[]
}

// ---------------------------------------------------------------------------
// Page shell
// ---------------------------------------------------------------------------

export default function MemoryGraphPage({ onBack }: { onBack: () => void }) {
  const { data, error, isLoading, mutate } = useSWR<MemoryGraph>(
    "/api/gideon/v1/memories/graph",
    async (url: string) => {
      const res = await pingman.get<MemoryGraph>(url)
      if (!res.ok) throw new Error("Failed to load memory graph")
      return res.data
    },
    { revalidateOnFocus: false, dedupingInterval: 60_000 },
  )

  const [focusedId, setFocusedId] = useState<string | null>(null)
  const [showAdd, setShowAdd] = useState(false)
  const revalidateTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const stats = useMemo(() => {
    if (!data) return { nodes: 0, edges: 0 }
    return { nodes: data.nodes.length, edges: data.edges.length }
  }, [data])

  const onDeleteFact = useCallback(async (edgeId: string) => {
    try {
      const res = await pingman.delete(`/api/gideon/v1/memories/${edgeId}`)
      if (!res.ok) {
        console.warn("memory: delete fact returned non-ok", { edgeId, status: res.status })
        return
      }
      mutate()
    } catch (err) {
      console.warn("memory: delete fact threw", { edgeId, err })
    }
  }, [mutate])

  const onDeleteNode = useCallback(async (nodeId: string) => {
    try {
      const res = await pingman.delete(`/api/gideon/v1/memories/graph/node/${nodeId}`)
      if (!res.ok) {
        console.warn("memory: delete node returned non-ok", { nodeId, status: res.status })
        return
      }
      setFocusedId(null)
      mutate()
    } catch (err) {
      console.warn("memory: delete node threw", { nodeId, err })
    }
  }, [mutate])

  // After submitting a new memory, Graphiti does LLM extraction off the
  // request path (~1-5s). Re-fetch once after 6s so new nodes appear without
  // the user having to refresh.
  const scheduleRevalidate = useCallback(() => {
    if (revalidateTimerRef.current) clearTimeout(revalidateTimerRef.current)
    revalidateTimerRef.current = setTimeout(() => { mutate() }, 6000)
  }, [mutate])

  useEffect(() => {
    return () => {
      if (revalidateTimerRef.current) clearTimeout(revalidateTimerRef.current)
    }
  }, [])

  return (
    <div
      className="absolute inset-0 flex flex-col"
      style={{ background: "var(--background)", color: "var(--text-primary)" }}
    >
      {/* Header — slim, overlays the canvas */}
      <div
        className="flex items-center justify-between px-5 py-3 z-10"
        style={{ borderBottom: "1px solid var(--border-subtle)", background: "rgba(0,0,0,0.4)", backdropFilter: "blur(8px)" }}
      >
        <div className="flex items-center gap-3">
          <button
            onClick={onBack}
            className="flex items-center gap-1.5 text-sm hover:opacity-80"
            style={{ color: "var(--text-secondary)" }}
          >
            <ArrowLeft size={14} />
            <span>Back</span>
          </button>
          <div className="h-4 w-px" style={{ background: "var(--border)" }} />
          <div className="flex items-center gap-2">
            <Network size={14} style={{ color: "var(--accent)" }} />
            <span className="text-sm font-medium">Memory</span>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <div className="text-xs" style={{ color: "var(--text-tertiary)" }}>
            {isLoading ? "Loading…" : `${stats.nodes} entities · ${stats.edges} facts`}
          </div>
          <button
            onClick={() => setShowAdd(true)}
            className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-full hover:opacity-90 transition-colors"
            style={{
              background: "var(--accent-soft)",
              color: "var(--accent-hover)",
              border: "1px solid var(--accent)",
            }}
          >
            <Plus size={12} />
            <span>Add memory</span>
          </button>
        </div>
      </div>

      {/* Canvas region — fills the rest of the viewport */}
      <div className="relative flex-1 overflow-hidden">
        {isLoading && (
          <div className="absolute inset-0 flex items-center justify-center">
            <Loader2 size={20} className="animate-spin" style={{ color: "var(--accent)" }} />
          </div>
        )}

        {error && (
          <div className="absolute inset-0 flex items-center justify-center">
            <div
              className="px-4 py-3 rounded-md text-sm"
              style={{ background: "var(--surface-elevated)", border: "1px solid var(--border)", color: "var(--text-secondary)" }}
            >
              Couldn&apos;t load the memory graph. Try again in a moment.
            </div>
          </div>
        )}

        {!isLoading && !error && data && data.nodes.length === 0 && (
          <EmptyState onAdd={() => setShowAdd(true)} />
        )}

        {!isLoading && !error && data && data.nodes.length > 0 && (
          <GraphCanvas
            graph={data}
            focusedId={focusedId}
            onFocus={setFocusedId}
            onDeleteFact={onDeleteFact}
            onDeleteNode={onDeleteNode}
          />
        )}
      </div>

      {showAdd && (
        <AddMemoryModal
          onClose={() => setShowAdd(false)}
          onSubmitted={scheduleRevalidate}
        />
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Empty state — shown when Graphiti has no entities yet for this user.
// ---------------------------------------------------------------------------

function EmptyState({ onAdd }: { onAdd: () => void }) {
  return (
    <div className="absolute inset-0 flex items-center justify-center">
      <div className="max-w-sm text-center px-6">
        <Network size={28} className="mx-auto mb-3" style={{ color: "var(--text-tertiary)" }} />
        <div className="text-sm mb-1" style={{ color: "var(--text-primary)" }}>
          Nothing here yet
        </div>
        <div className="text-xs leading-relaxed mb-4" style={{ color: "var(--text-tertiary)" }}>
          Gideon builds this map as you chat. Keep using it for a few days and
          you&apos;ll start to see people, projects, and patterns surface here.
        </div>
        <button
          onClick={onAdd}
          className="text-xs px-3 py-1.5 rounded-full hover:opacity-90"
          style={{ background: "var(--accent-soft)", color: "var(--accent-hover)", border: "1px solid var(--accent)" }}
        >
          Add your first memory
        </button>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// AddMemoryModal — textarea + submit. Fires POST /memories/graph/add which
// returns 202 immediately; the backend ingests in the background.
// ---------------------------------------------------------------------------

function AddMemoryModal({
  onClose,
  onSubmitted,
}: {
  onClose: () => void
  onSubmitted: () => void
}) {
  const [text, setText] = useState("")
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async () => {
    if (submitting) return
    const content = text.trim()
    if (!content) return
    setSubmitting(true)
    setError(null)
    try {
      const res = await pingman.post("/api/gideon/v1/memories/graph/add", { content })
      if (!res.ok) {
        setError("Couldn't queue that memory. Please try again.")
        setSubmitting(false)
        return
      }
      onSubmitted()
      onClose()
    } catch {
      setError("Couldn't queue that memory. Please try again.")
      setSubmitting(false)
    }
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Escape") { onClose(); return }
    if ((e.key === "Enter") && (e.metaKey || e.ctrlKey)) { e.preventDefault(); submit() }
  }

  return (
    <div
      className="absolute inset-0 z-50 flex items-center justify-center"
      style={{ background: "rgba(0,0,0,0.5)", backdropFilter: "blur(4px)" }}
      onClick={onClose}
    >
      <div
        className="w-full max-w-lg mx-4 rounded-xl p-5"
        style={{ background: "var(--surface-card)", border: "1px solid var(--border)" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-3">
          <div className="text-sm font-medium" style={{ color: "var(--text-primary)" }}>
            Add memory
          </div>
          <button onClick={onClose} className="p-1 rounded hover:opacity-80" style={{ color: "var(--text-tertiary)" }}>
            <X size={14} />
          </button>
        </div>
        <div className="text-xs mb-3" style={{ color: "var(--text-tertiary)" }}>
          Tell Gideon something to remember. It&apos;ll process and add it in the background.
        </div>
        <textarea
          autoFocus
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="e.g. Ana is the design lead on the billing project."
          rows={5}
          className="w-full text-sm p-3 rounded-md resize-y outline-none"
          style={{
            background: "var(--surface-elevated)",
            border: "1px solid var(--border)",
            color: "var(--text-primary)",
            fontFamily: "var(--font-body)",
          }}
        />
        {error && (
          <div className="text-xs mt-2" style={{ color: "#f87171" }}>
            {error}
          </div>
        )}
        <div className="flex items-center justify-between mt-4">
          <div className="text-[11px]" style={{ color: "var(--text-tertiary)" }}>
            <kbd style={{ fontFamily: "var(--font-mono)", padding: "1px 5px", border: "1px solid var(--border)", borderRadius: 3 }}>⌘ ↵</kbd>
            <span className="ml-2">to submit</span>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={onClose}
              className="text-xs px-3 py-1.5 rounded-md"
              style={{ color: "var(--text-secondary)", background: "transparent", border: "1px solid var(--border)" }}
            >
              Cancel
            </button>
            <button
              onClick={submit}
              disabled={!text.trim() || submitting}
              className="text-xs px-3 py-1.5 rounded-md disabled:opacity-50"
              style={{
                background: "var(--accent)",
                color: "white",
                border: "1px solid var(--accent)",
              }}
            >
              {submitting ? "Queuing…" : "Add to memory"}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
