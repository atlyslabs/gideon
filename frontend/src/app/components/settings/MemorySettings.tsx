"use client"

import { useState, useCallback, useMemo } from "react"
import { Loader2, Brain, X, Plus, Search } from "lucide-react"
import type { Memory } from "../../hooks/useMemories"

const CATEGORY_COLORS: Record<string, { bg: string; text: string }> = {
  general: { bg: "rgba(148, 163, 184, 0.15)", text: "#94a3b8" },
  preference: { bg: "rgba(168, 85, 247, 0.15)", text: "#a855f7" },
  person: { bg: "rgba(59, 130, 246, 0.15)", text: "#3b82f6" },
  project: { bg: "rgba(34, 197, 94, 0.15)", text: "#22c55e" },
  fact: { bg: "rgba(251, 191, 36, 0.15)", text: "#fbbf24" },
}

interface MemorySettingsProps {
  memories: Memory[]
  memoryCount: number
  memoriesLoading: boolean
  createMemory: (payload: { content: string; category?: string }) => Promise<unknown>
  deleteMemory: (id: string) => Promise<unknown>
}

export default function MemorySettings({
  memories,
  memoryCount,
  memoriesLoading,
  createMemory,
  deleteMemory,
}: MemorySettingsProps) {
  const [memorySearch, setMemorySearch] = useState("")
  const [showAddMemory, setShowAddMemory] = useState(false)
  const [newMemoryContent, setNewMemoryContent] = useState("")
  const [newMemoryCategory, setNewMemoryCategory] = useState("general")
  const [addingMemory, setAddingMemory] = useState(false)
  const [deletingId, setDeletingId] = useState<string | null>(null)

  const filteredMemories = useMemo(() => {
    if (!memorySearch.trim()) return memories
    const q = memorySearch.toLowerCase()
    return memories.filter(
      (m) =>
        m.content.toLowerCase().includes(q) ||
        m.category.toLowerCase().includes(q)
    )
  }, [memories, memorySearch])

  const handleAddMemory = useCallback(async () => {
    if (!newMemoryContent.trim() || addingMemory) return
    setAddingMemory(true)
    try {
      await createMemory({
        content: newMemoryContent.trim(),
        category: newMemoryCategory,
      })
      setNewMemoryContent("")
      setNewMemoryCategory("general")
      setShowAddMemory(false)
    } catch {
      // silent
    } finally {
      setAddingMemory(false)
    }
  }, [newMemoryContent, newMemoryCategory, addingMemory, createMemory])

  const handleDeleteMemory = useCallback(
    async (id: string) => {
      setDeletingId(id)
      try {
        await deleteMemory(id)
      } catch {
        // silent
      } finally {
        setDeletingId(null)
      }
    },
    [deleteMemory]
  )

  const formatDate = (dateStr: string | null) => {
    if (!dateStr) return ""
    const d = new Date(dateStr)
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })
  }

  return (
    <div className="max-w-[640px] mx-auto py-8 px-4 md:px-8">
      {/* Section header */}
      <div className="flex items-center gap-3 mb-6">
        <div
          className="w-10 h-10 rounded-xl flex items-center justify-center"
          style={{ background: "var(--surface-elevated)" }}
        >
          <Brain size={20} style={{ color: "var(--text-secondary)" }} />
        </div>
        <div>
          <h1
            className="text-[16px] font-semibold"
            style={{ color: "var(--text-primary)" }}
          >
            Memory
          </h1>
          <p
            className="text-[13px] mt-0.5"
            style={{ color: "var(--text-secondary)" }}
          >
            Things Gideon remembers about you.
          </p>
        </div>
      </div>

      {/* Actions bar: search + add + count */}
      <div className="flex items-center gap-3 mb-4">
        <div className="relative flex-1">
          <Search
            size={14}
            className="absolute left-3 top-1/2 -translate-y-1/2"
            style={{ color: "var(--text-tertiary)" }}
          />
          <input
            type="text"
            placeholder="Search memories..."
            value={memorySearch}
            onChange={(e) => setMemorySearch(e.target.value)}
            className="w-full pl-9 pr-3 py-2 rounded-lg text-[13px]"
            style={{
              background: "var(--surface-elevated)",
              border: "1px solid var(--border)",
              color: "var(--text-primary)",
              outline: "none",
            }}
            onFocus={(e) => {
              e.currentTarget.style.borderColor = "var(--text-tertiary)"
            }}
            onBlur={(e) => {
              e.currentTarget.style.borderColor = "var(--border)"
            }}
          />
        </div>
        <button
          onClick={() => setShowAddMemory(!showAddMemory)}
          className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-[13px] font-medium transition-colors shrink-0"
          style={{
            background: showAddMemory ? "var(--surface-elevated)" : "transparent",
            border: "1px solid var(--border)",
            color: "var(--text-secondary)",
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.borderColor = "var(--text-tertiary)"
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.borderColor = "var(--border)"
          }}
        >
          <Plus size={14} />
          Add Memory
        </button>
        <span
          className="text-[12px] shrink-0 tabular-nums"
          style={{ color: "var(--text-tertiary)" }}
        >
          {memoryCount}/100
        </span>
      </div>

      {/* Add memory inline form */}
      {showAddMemory && (
        <div
          className="rounded-xl p-4 mb-4"
          style={{
            border: "1px solid var(--border)",
            background: "var(--surface-elevated)",
          }}
        >
          <textarea
            placeholder="What should Gideon remember?"
            value={newMemoryContent}
            onChange={(e) => setNewMemoryContent(e.target.value)}
            rows={2}
            className="w-full px-3 py-2 rounded-lg text-[13px] resize-none mb-3"
            style={{
              background: "var(--surface)",
              border: "1px solid var(--border)",
              color: "var(--text-primary)",
              outline: "none",
            }}
            onFocus={(e) => {
              e.currentTarget.style.borderColor = "var(--text-tertiary)"
            }}
            onBlur={(e) => {
              e.currentTarget.style.borderColor = "var(--border)"
            }}
          />
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              {(["general", "preference", "person", "project", "fact"] as const).map((cat) => {
                const colors = CATEGORY_COLORS[cat]
                const isActive = newMemoryCategory === cat
                return (
                  <button
                    key={cat}
                    onClick={() => setNewMemoryCategory(cat)}
                    className="px-2.5 py-1 rounded-md text-[11px] font-medium capitalize transition-all"
                    style={{
                      background: isActive ? colors.bg : "transparent",
                      color: isActive ? colors.text : "var(--text-tertiary)",
                      border: isActive
                        ? `1px solid ${colors.text}33`
                        : "1px solid transparent",
                    }}
                  >
                    {cat}
                  </button>
                )
              })}
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={() => {
                  setShowAddMemory(false)
                  setNewMemoryContent("")
                  setNewMemoryCategory("general")
                }}
                className="px-3 py-1.5 rounded-lg text-[12px] transition-colors"
                style={{ color: "var(--text-tertiary)" }}
              >
                Cancel
              </button>
              <button
                onClick={handleAddMemory}
                disabled={!newMemoryContent.trim() || addingMemory}
                className="px-3 py-1.5 rounded-lg text-[12px] font-medium transition-colors"
                style={{
                  background: newMemoryContent.trim() ? "#7c3aed" : "#333",
                  color: newMemoryContent.trim() ? "#fff" : "var(--text-tertiary)",
                  opacity: addingMemory ? 0.6 : 1,
                }}
              >
                {addingMemory ? "Saving..." : "Save"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Memory list */}
      <div
        className="rounded-xl overflow-hidden"
        style={{ border: "1px solid var(--border)" }}
      >
        {memoriesLoading ? (
          <div className="flex items-center justify-center py-12">
            <Loader2
              size={18}
              className="animate-spin"
              style={{ color: "var(--text-tertiary)" }}
            />
          </div>
        ) : filteredMemories.length === 0 ? (
          <div className="py-12 text-center">
            <Brain
              size={32}
              className="mx-auto mb-3"
              style={{ color: "var(--text-tertiary)", opacity: 0.4 }}
            />
            <p
              className="text-[13px]"
              style={{ color: "var(--text-tertiary)" }}
            >
              {memorySearch
                ? "No memories match your search."
                : "No memories yet. Chat with Gideon and it will remember important things."}
            </p>
          </div>
        ) : (
          filteredMemories.map((memory, idx) => {
            const colors = CATEGORY_COLORS[memory.category] || CATEGORY_COLORS.general
            const isDeleting = deletingId === memory.id
            return (
              <div
                key={memory.id}
                className="group px-5 py-4 flex items-start gap-3 transition-colors"
                style={{
                  borderBottom:
                    idx < filteredMemories.length - 1
                      ? "1px solid var(--border)"
                      : "none",
                  opacity: isDeleting ? 0.4 : 1,
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = "var(--surface-elevated)"
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background = "transparent"
                }}
              >
                {/* Category badge */}
                <span
                  className="shrink-0 px-2 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wider mt-0.5"
                  style={{
                    background: colors.bg,
                    color: colors.text,
                  }}
                >
                  {memory.category}
                </span>

                {/* Content + date */}
                <div className="flex-1 min-w-0">
                  <p
                    className="text-[13px] leading-relaxed"
                    style={{ color: "var(--text-primary)" }}
                  >
                    {memory.content}
                  </p>
                  <p
                    className="text-[11px] mt-1"
                    style={{ color: "var(--text-tertiary)" }}
                  >
                    {formatDate(memory.created_at)}
                    {memory.source !== "manual" && (
                      <span> &middot; via {memory.source}</span>
                    )}
                  </p>
                </div>

                {/* Delete button (visible on hover) */}
                <button
                  onClick={() => handleDeleteMemory(memory.id)}
                  disabled={isDeleting}
                  className="shrink-0 p-1.5 rounded-md opacity-100 md:opacity-0 md:group-hover:opacity-100 transition-opacity"
                  style={{ color: "var(--text-tertiary)" }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.color = "#ef4444"
                    e.currentTarget.style.background = "rgba(239,68,68,0.1)"
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.color = "var(--text-tertiary)"
                    e.currentTarget.style.background = "transparent"
                  }}
                >
                  <X size={14} />
                </button>
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}
