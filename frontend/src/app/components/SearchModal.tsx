"use client"

import { useState, useEffect, useRef, useCallback, useMemo } from "react"
import {
  Search,
  Settings,
  Link,
  Workflow,
  Sparkles,
  MessageSquare,
  CornerDownLeft,
  Plus,
} from "lucide-react"
import type { Conversation } from "../types"

interface SearchModalProps {
  open: boolean
  onClose: () => void
  conversations: Conversation[]
  onNewChat: () => void
  onSelectConversation: (id: string) => void
  onNavigate: (page: string) => void
}

const NAV_ITEMS = [
  { icon: Plus, label: "New Chat", id: "new-chat", showEnter: true },
  { icon: Settings, label: "Settings", id: "settings" },
  { icon: Link, label: "Integrations", id: "integrations" },
  { icon: Workflow, label: "Workflows", id: "workflows" },
  { icon: Sparkles, label: "Skills", id: "skills" },
]

type FlatItem =
  | { type: "nav"; id: string; label: string; icon: typeof Plus; showEnter?: boolean }
  | { type: "header"; label: string }
  | { type: "conv"; id: string; title: string }

function groupConversationsByDate(conversations: Conversation[]) {
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const yesterday = new Date(today.getTime() - 86400000)
  const weekAgo = new Date(today.getTime() - 7 * 86400000)

  const groups: { label: string; items: Conversation[] }[] = []
  const todayItems: Conversation[] = []
  const yesterdayItems: Conversation[] = []
  const weekItems: Conversation[] = []
  const olderItems: Conversation[] = []

  for (const conv of conversations) {
    const d = new Date(conv.updated_at || conv.created_at)
    if (d >= today) todayItems.push(conv)
    else if (d >= yesterday) yesterdayItems.push(conv)
    else if (d >= weekAgo) weekItems.push(conv)
    else olderItems.push(conv)
  }

  if (todayItems.length) groups.push({ label: "Today", items: todayItems })
  if (yesterdayItems.length) groups.push({ label: "Yesterday", items: yesterdayItems })
  if (weekItems.length) groups.push({ label: "This Week", items: weekItems })
  if (olderItems.length) groups.push({ label: "Older", items: olderItems })

  return groups
}

export default function SearchModal({
  open,
  onClose,
  conversations,
  onNewChat,
  onSelectConversation,
  onNavigate,
}: SearchModalProps) {
  const [query, setQuery] = useState("")
  const [activeIndex, setActiveIndex] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (open) {
      setQuery("")
      setActiveIndex(0)
      setTimeout(() => inputRef.current?.focus(), 50)
    }
  }, [open])

  const filteredNav = useMemo(() => {
    if (!query.trim()) return NAV_ITEMS
    const q = query.toLowerCase()
    return NAV_ITEMS.filter((item) => item.label.toLowerCase().includes(q))
  }, [query])

  const filteredConversations = useMemo(() => {
    if (!query.trim()) return conversations
    const q = query.toLowerCase()
    return conversations.filter((c) => (c.title || "").toLowerCase().includes(q))
  }, [query, conversations])

  const groups = useMemo(
    () => groupConversationsByDate(filteredConversations),
    [filteredConversations]
  )

  // Build flat list of selectable items (headers are not selectable)
  const flatItems = useMemo(() => {
    const items: FlatItem[] = []
    for (const nav of filteredNav) {
      items.push({ type: "nav", id: nav.id, label: nav.label, icon: nav.icon, showEnter: nav.showEnter })
    }
    for (const group of groups) {
      items.push({ type: "header", label: group.label })
      for (const conv of group.items) {
        items.push({ type: "conv", id: conv.id, title: conv.title || "New conversation" })
      }
    }
    return items
  }, [filteredNav, groups])

  // Selectable indices (skip headers)
  const selectableIndices = useMemo(
    () => flatItems.map((item, i) => (item.type !== "header" ? i : -1)).filter((i) => i >= 0),
    [flatItems]
  )

  // Reset active index when results change
  useEffect(() => {
    setActiveIndex(0)
  }, [query])

  const executeItem = useCallback(
    (item: FlatItem) => {
      if (item.type === "nav") {
        if (item.id === "new-chat") onNewChat()
        else onNavigate(item.id)
        onClose()
      } else if (item.type === "conv") {
        onSelectConversation(item.id)
        onClose()
      }
    },
    [onNewChat, onNavigate, onSelectConversation, onClose]
  )

  // Keyboard navigation
  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose()
        return
      }
      if (e.key === "ArrowDown") {
        e.preventDefault()
        setActiveIndex((prev) => {
          const currentPos = selectableIndices.indexOf(prev)
          const nextPos = currentPos < selectableIndices.length - 1 ? currentPos + 1 : 0
          return selectableIndices[nextPos] ?? prev
        })
        return
      }
      if (e.key === "ArrowUp") {
        e.preventDefault()
        setActiveIndex((prev) => {
          const currentPos = selectableIndices.indexOf(prev)
          const nextPos = currentPos > 0 ? currentPos - 1 : selectableIndices.length - 1
          return selectableIndices[nextPos] ?? prev
        })
        return
      }
      if (e.key === "Enter") {
        e.preventDefault()
        const item = flatItems[activeIndex]
        if (item && item.type !== "header") {
          executeItem(item)
        }
        return
      }
    }
    window.addEventListener("keydown", handler)
    return () => window.removeEventListener("keydown", handler)
  }, [open, onClose, activeIndex, selectableIndices, flatItems, executeItem])

  // Scroll active item into view
  useEffect(() => {
    if (!listRef.current) return
    const el = listRef.current.querySelector(`[data-index="${activeIndex}"]`)
    if (el) el.scrollIntoView({ block: "nearest" })
  }, [activeIndex])

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-[9998] flex items-start justify-center pt-[15vh]"
      style={{ background: "rgba(0, 0, 0, 0.6)", backdropFilter: "blur(4px)" }}
      onClick={onClose}
    >
      <div
        className="w-full max-w-[640px] rounded-2xl overflow-hidden flex flex-col"
        style={{
          background: "var(--surface)",
          border: "1px solid var(--border)",
          maxHeight: "60vh",
          boxShadow: "0 24px 64px rgba(0, 0, 0, 0.5)",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Search input */}
        <div
          className="flex items-center gap-3 px-5 py-4"
          style={{ borderBottom: "1px solid var(--border)" }}
        >
          <Search size={18} style={{ color: "var(--text-tertiary)", flexShrink: 0 }} />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search for chats..."
            className="flex-1 bg-transparent text-[15px] outline-none"
            style={{
              color: "var(--text-primary)",
              caretColor: "var(--text-secondary)",
            }}
          />
        </div>

        {/* Results */}
        <div className="flex-1 overflow-y-auto py-2" ref={listRef}>
          {flatItems.map((item, index) => {
            if (item.type === "header") {
              return (
                <p
                  key={`header-${item.label}`}
                  className="px-5 pt-4 pb-2 text-[12px] font-medium"
                  style={{ color: "var(--text-tertiary)" }}
                >
                  {item.label}
                </p>
              )
            }

            const isActive = index === activeIndex
            const Icon = item.type === "nav" ? item.icon : MessageSquare

            return (
              <button
                key={item.type === "nav" ? item.id : `conv-${item.id}`}
                data-index={index}
                onClick={() => executeItem(item)}
                onMouseEnter={() => setActiveIndex(index)}
                className="w-full flex items-center gap-4 px-5 py-3 transition-colors"
                style={{
                  background: isActive ? "var(--surface-elevated)" : "transparent",
                }}
              >
                <Icon
                  size={18}
                  style={{
                    color: item.type === "nav" ? "var(--text-secondary)" : "var(--text-tertiary)",
                  }}
                />
                <span
                  className="text-[15px] truncate"
                  style={{ color: "var(--text-primary)" }}
                >
                  {item.type === "nav" ? item.label : item.title}
                </span>
                {item.type === "nav" && item.showEnter && (
                  <div className="ml-auto">
                    <div
                      className="w-6 h-6 rounded flex items-center justify-center"
                      style={{
                        background: "var(--surface-elevated)",
                        border: "1px solid var(--border)",
                      }}
                    >
                      <CornerDownLeft size={12} style={{ color: "var(--text-tertiary)" }} />
                    </div>
                  </div>
                )}
              </button>
            )
          })}

          {/* Empty state */}
          {flatItems.length === 0 && (
            <p
              className="text-center py-8 text-[14px]"
              style={{ color: "var(--text-tertiary)" }}
            >
              No results found
            </p>
          )}
        </div>
      </div>
    </div>
  )
}
