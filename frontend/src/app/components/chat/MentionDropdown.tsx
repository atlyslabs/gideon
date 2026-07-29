"use client"

import { useState, useEffect, useRef, useCallback } from "react"
import { createPortal } from "react-dom"
import { ChevronRight, ArrowLeft, Loader2, Users, Wrench } from "lucide-react"
import type { MentionItem, MentionCategory } from "../../types"
import { getIntegrationIcon } from "../../utils/integrationIcons"
import useMentionSearch from "../../hooks/useMentionSearch"

interface MentionDropdownProps {
  anchorRef: React.RefObject<HTMLElement | null>
  query: string
  onSelect: (item: MentionItem) => void
  onClose: () => void
  onClearQuery: () => void
  onKeyDown: (e: React.KeyboardEvent) => void
  connectedProviders?: string[]
}

interface CategoryDef {
  id: MentionCategory
  label: string
  icon: string | null // null = use custom icon
  requiresQuery: boolean
}

const CATEGORIES: CategoryDef[] = [
  { id: "collaborator", label: "Collaborators", icon: null, requiresQuery: false },
  { id: "linear", label: "Linear", icon: "linear", requiresQuery: true },
  { id: "notion", label: "Notion", icon: "notion", requiresQuery: true },
  { id: "slack", label: "Slack", icon: "slack", requiresQuery: true },
  { id: "google_drive", label: "Google Drive", icon: "google_drive", requiresQuery: true },
  { id: "google_docs", label: "Google Docs", icon: "google_docs", requiresQuery: true },
  { id: "google_sheets", label: "Google Sheets", icon: "google_sheets", requiresQuery: true },
  { id: "google_slides", label: "Google Slides", icon: "google_slides", requiresQuery: true },
  { id: "gmail", label: "Gmail", icon: "gmail", requiresQuery: false },
  { id: "google_calendar", label: "Google Calendar", icon: "google_calendar", requiresQuery: false },
]

export default function MentionDropdown({
  anchorRef,
  query,
  onSelect,
  onClose,
  onClearQuery,
  onKeyDown,
  connectedProviders,
}: MentionDropdownProps) {
  const [selectedCategory, setSelectedCategory] = useState<MentionCategory | null>(null)
  const [activeIndex, setActiveIndex] = useState(0)
  const activeIndexRef = useRef(0)
  const [categorySearchQuery, setCategorySearchQuery] = useState("")
  const searchInputRef = useRef<HTMLInputElement>(null)
  const { results, loading, search, clear } = useMentionSearch()
  // queryAtCategorySelect removed — using categorySearchQuery + searchInputRef instead
  const listRef = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState({ bottom: 0, left: 0, width: 400 })

  // Filter categories to only connected providers
  const availableCategories = CATEGORIES.filter((cat) => {
    if (cat.id === "collaborator") return true
    if (cat.id === "linear") return connectedProviders?.includes("linear")
    if (cat.id === "notion") return connectedProviders?.includes("notion")
    if (cat.id === "slack") return connectedProviders?.includes("slack")
    if (cat.id === "gmail" || cat.id === "google_calendar" || cat.id.startsWith("google_")) return connectedProviders?.includes("google")
    return false
  })

  // Position dropdown above the anchor, clamped to stay within the viewport on mobile
  useEffect(() => {
    if (!anchorRef.current) return
    const computePos = () => {
      const rect = anchorRef.current!.getBoundingClientRect()
      const margin = 8
      const maxWidth = Math.max(240, Math.min(420, window.innerWidth - margin * 2))
      const width = Math.min(rect.width, maxWidth)
      const left = Math.max(margin, Math.min(rect.left, window.innerWidth - width - margin))
      setPos({
        bottom: window.innerHeight - rect.top + 8,
        left,
        width,
      })
    }
    computePos()
    window.addEventListener("resize", computePos)
    return () => window.removeEventListener("resize", computePos)
  }, [anchorRef])

  // When query changes and we're in collaborator mode (no category selected), search collaborators
  useEffect(() => {
    if (!selectedCategory && query) {
      search(query, "collaborator")
    }
  }, [query, selectedCategory, search])

  // When entering a category, clear old results and search fresh
  useEffect(() => {
    if (selectedCategory) {
      clear()
      search("", selectedCategory)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedCategory])

  // When typing in the category search input
  useEffect(() => {
    if (selectedCategory && categorySearchQuery !== undefined) {
      search(categorySearchQuery, selectedCategory)
    }
  }, [categorySearchQuery, selectedCategory, search])

  // Reset active index when results change
  useEffect(() => { setActiveIndex(0); activeIndexRef.current = 0 }, [results, selectedCategory])

  // Scroll active item into view
  useEffect(() => {
    const el = listRef.current?.querySelector(`[data-index="${activeIndex}"]`)
    if (el) el.scrollIntoView({ block: "nearest" })
  }, [activeIndex])

  // When no category selected: show categories (filtered by query) + collaborator results
  const filteredCategories = !selectedCategory
    ? availableCategories.filter((cat) => !query || cat.label.toLowerCase().includes(query.toLowerCase()))
    : []
  const items = selectedCategory ? results : (query ? results : [])
  const showCategories = !selectedCategory
  // Combined items: filtered categories first, then search results
  const combinedCount = showCategories ? filteredCategories.length + items.length : items.length

  // Keep refs in sync for the keyboard handler (avoids stale closures)
  const itemsRef = useRef(items)
  const filteredCategoriesRef = useRef(filteredCategories)
  const combinedCountRef = useRef(combinedCount)
  itemsRef.current = items
  filteredCategoriesRef.current = filteredCategories
  combinedCountRef.current = combinedCount

  // Keyboard handler — called from ChatInput
  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      if (selectedCategory) {
        setSelectedCategory(null)
        clear()
      } else {
        onClose()
      }
      e.preventDefault()
      return
    }

    if (e.key === "ArrowUp") {
      e.preventDefault()
      setActiveIndex((prev) => {
        const next = prev > 0 ? prev - 1 : combinedCountRef.current - 1
        activeIndexRef.current = next
        return next
      })
      return
    }

    if (e.key === "ArrowDown") {
      e.preventDefault()
      setActiveIndex((prev) => {
        const next = prev < combinedCountRef.current - 1 ? prev + 1 : 0
        activeIndexRef.current = next
        return next
      })
      return
    }

    if (e.key === "Enter") {
      e.preventDefault()
      const idx = activeIndexRef.current
      const cats = filteredCategoriesRef.current
      const itms = itemsRef.current
      if (showCategories && idx < cats.length) {
        const cat = cats[idx]
        if (cat) {
          setSelectedCategory(cat.id)
          setCategorySearchQuery("")
          onClearQuery()
          if (!cat.requiresQuery) search("", cat.id)
          setTimeout(() => searchInputRef.current?.focus(), 50)
        }
      } else {
        const itemIdx = showCategories ? idx - cats.length : idx
        if (itms[itemIdx]) onSelect(itms[itemIdx])
      }
      return
    }

    onKeyDown(e)
  }, [showCategories, selectedCategory, onSelect, onClose, onClearQuery, onKeyDown, clear, search])

  // Expose handleKeyDown via a ref pattern — ChatInput will call this
  // We use the onKeyDown prop to pass keyboard events from the parent
  useEffect(() => {
    // Store the handler so ChatInput can call it
    (window as any).__mentionDropdownKeyDown = handleKeyDown
    return () => { delete (window as any).__mentionDropdownKeyDown }
  }, [handleKeyDown])

  const dropdown = (
    <div
      className="fixed z-[9999] rounded-xl overflow-hidden shadow-2xl"
      style={{
        bottom: pos.bottom,
        left: pos.left,
        width: pos.width,
        maxHeight: 360,
        background: "rgba(24,24,24,0.95)",
        border: "1px solid rgba(255,255,255,0.1)",
        backdropFilter: "blur(16px)",
      }}
      onMouseDown={(e) => e.preventDefault()} // prevent textarea blur
    >
      {/* Header with search input when inside a category */}
      {selectedCategory && (
        <div
          className="flex items-center gap-2 px-3 py-2 text-xs"
          style={{ borderBottom: "1px solid rgba(255,255,255,0.08)" }}
        >
          <button
            onClick={() => { setSelectedCategory(null); setCategorySearchQuery(""); clear() }}
            className="flex items-center gap-1 text-white/50 hover:text-white/80 transition-colors shrink-0"
          >
            <ArrowLeft size={12} />
          </button>
          <span className="flex items-center gap-1.5 shrink-0">
            {getIntegrationIcon(
              CATEGORIES.find((c) => c.id === selectedCategory)?.icon || selectedCategory,
              14
            )}
          </span>
          <input
            ref={searchInputRef}
            type="text"
            value={categorySearchQuery}
            onChange={(e) => setCategorySearchQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "ArrowUp") {
                e.preventDefault()
                setActiveIndex((prev) => {
                  const next = prev > 0 ? prev - 1 : itemsRef.current.length - 1
                  activeIndexRef.current = next
                  return next
                })
              } else if (e.key === "ArrowDown") {
                e.preventDefault()
                setActiveIndex((prev) => {
                  const next = prev < itemsRef.current.length - 1 ? prev + 1 : 0
                  activeIndexRef.current = next
                  return next
                })
              } else if (e.key === "Enter" || e.key === "Tab") {
                e.preventDefault()
                const item = itemsRef.current[activeIndexRef.current]
                if (item) onSelect(item)
              } else if (e.key === "Backspace" && !categorySearchQuery) {
                e.preventDefault()
                // Go back to category menu and refocus the editor
                setSelectedCategory(null)
                setCategorySearchQuery("")
                clear()
                // Focus editor so its keydown handler works for next Backspace
                if (anchorRef.current) {
                  const editor = anchorRef.current.querySelector("[contenteditable]") as HTMLElement
                  editor?.focus()
                }
              } else if (e.key === "Escape") {
                e.preventDefault()
                setSelectedCategory(null)
                setCategorySearchQuery("")
                clear()
              }
            }}
            placeholder={`Search ${CATEGORIES.find((c) => c.id === selectedCategory)?.label || ""}...`}
            className="flex-1 bg-transparent text-sm text-white outline-none placeholder:text-white/30"
          />
          {loading && <Loader2 size={12} className="animate-spin text-white/40 shrink-0" />}
        </div>
      )}

      <div ref={listRef} className="overflow-y-auto" style={{ maxHeight: 320 }}>
        {/* Categories (filtered when typing) */}
        {showCategories && filteredCategories.map((cat, idx) => (
          <button
            key={cat.id}
            data-index={idx}
            className="w-full flex items-center gap-3 px-3 py-2.5 text-left transition-colors"
            style={{
              background: idx === activeIndex ? "rgba(255,255,255,0.08)" : "transparent",
            }}
            onMouseEnter={() => setActiveIndex(idx)}
            onClick={() => {
              setSelectedCategory(cat.id)
              if (!cat.requiresQuery) search("", cat.id)
            }}
          >
            <span className="w-7 h-7 rounded-lg flex items-center justify-center shrink-0" style={{ background: "rgba(255,255,255,0.08)" }}>
              {cat.icon ? getIntegrationIcon(cat.icon, 16) : cat.id === "tool" ? <Wrench size={14} className="text-white/60" /> : <Users size={14} className="text-white/60" />}
            </span>
            <span className="flex-1 text-sm text-white">{cat.label}</span>
            {cat.id === "collaborator" ? (
              <span className="text-[10px] text-white/30 px-1.5 py-0.5 rounded" style={{ background: "rgba(255,255,255,0.05)" }}>↵</span>
            ) : (
              <ChevronRight size={14} className="text-white/30" />
            )}
          </button>
        ))}

        {/* Collaborator results (shown alongside categories when typing without selecting a category) */}
        {showCategories && query && items.length > 0 && (
          <>
            {filteredCategories.length > 0 && (
              <div className="px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-white/30" style={{ borderTop: "1px solid rgba(255,255,255,0.06)" }}>
                People
              </div>
            )}
            {items.map((item, idx) => {
              const globalIdx = filteredCategories.length + idx
              return (
                <button
                  key={`${item.id}-${idx}`}
                  data-index={globalIdx}
                  className="w-full flex items-center gap-3 px-3 py-2.5 text-left transition-colors"
                  style={{
                    background: globalIdx === activeIndex ? "rgba(255,255,255,0.08)" : "transparent",
                  }}
                  onMouseEnter={() => setActiveIndex(globalIdx)}
                  onClick={() => onSelect(item)}
                >
                  {item.avatar_url ? (
                    <img src={item.avatar_url} className="w-7 h-7 rounded-full shrink-0" alt="" />
                  ) : (
                    <span className="w-7 h-7 rounded-lg flex items-center justify-center shrink-0" style={{ background: "rgba(255,255,255,0.08)" }}>
                      {getIntegrationIcon(item.icon || item.category, 14)}
                    </span>
                  )}
                  <div className="flex-1 min-w-0">
                    <div className="text-sm text-white truncate">{item.label}</div>
                    {item.subtitle && <div className="text-[11px] text-white/40 truncate">{item.subtitle}</div>}
                  </div>
                  {globalIdx === activeIndex && (
                    <span className="text-[10px] text-white/30 px-1.5 py-0.5 rounded shrink-0" style={{ background: "rgba(255,255,255,0.05)" }}>↵</span>
                  )}
                </button>
              )
            })}
          </>
        )}

        {/* Search results when inside a selected category */}
        {selectedCategory && items.length === 0 && !loading && (
          <div className="px-3 py-6 text-center text-xs text-white/30">
            {query ? "No results found" : (CATEGORIES.find(c => c.id === selectedCategory)?.requiresQuery ? "Type to search..." : "No items found")}
          </div>
        )}

        {selectedCategory && items.map((item, idx) => (
          <button
            key={`${item.id}-${idx}`}
            data-index={idx}
            className="w-full flex items-center gap-3 px-3 py-2.5 text-left transition-colors"
            style={{
              background: idx === activeIndex ? "rgba(255,255,255,0.08)" : "transparent",
            }}
            onMouseEnter={() => setActiveIndex(idx)}
            onClick={() => onSelect(item)}
          >
            {item.avatar_url ? (
              <img src={item.avatar_url} className="w-7 h-7 rounded-full shrink-0" alt="" />
            ) : (
              <span className="w-7 h-7 rounded-lg flex items-center justify-center shrink-0" style={{ background: "rgba(255,255,255,0.08)" }}>
                {getIntegrationIcon(item.icon || item.category, 14)}
              </span>
            )}
            <div className="flex-1 min-w-0">
              <div className="text-sm text-white truncate">{item.label}</div>
              {item.subtitle && <div className="text-[11px] text-white/40 truncate">{item.subtitle}</div>}
            </div>
            {idx === activeIndex && (
              <span className="text-[10px] text-white/30 px-1.5 py-0.5 rounded shrink-0" style={{ background: "rgba(255,255,255,0.05)" }}>↵</span>
            )}
          </button>
        ))}

        {!showCategories && loading && items.length === 0 && (
          <div className="flex items-center justify-center py-6">
            <Loader2 size={16} className="animate-spin text-white/30" />
          </div>
        )}
      </div>
    </div>
  )

  return createPortal(dropdown, document.body)
}
