"use client"

import { useState, useRef, useEffect, useCallback } from "react"
import { createPortal } from "react-dom"
import { X, Link2, Check, Search, Users } from "lucide-react"
import useShares from "../../hooks/useShares"
import { pingman } from "../../utils/pingman"
import type { MentionItem } from "../../types"

interface ShareDialogProps {
  conversationId: string
  onClose: () => void
}

interface PersonInfo {
  name: string
  avatar_url?: string
}

export default function ShareDialog({ conversationId, onClose }: ShareDialogProps) {
  const { linkShared, sharedWithEmails, loading, updateShare } = useShares(conversationId)
  const [copied, setCopied] = useState(false)
  const [searchQuery, setSearchQuery] = useState("")
  const [searchResults, setSearchResults] = useState<MentionItem[]>([])
  const [searching, setSearching] = useState(false)
  const searchDebounce = useRef<ReturnType<typeof setTimeout> | null>(null)
  const dialogRef = useRef<HTMLDivElement>(null)
  const searchInputRef = useRef<HTMLDivElement>(null)
  const [dropdownPos, setDropdownPos] = useState({ top: 0, left: 0, width: 0 })
  // Local cache: email → {name, avatar_url} for display in shared-with list
  const [peopleInfo, setPeopleInfo] = useState<Record<string, PersonInfo>>({})

  // On mount, fetch collaborator info for already-shared emails
  useEffect(() => {
    if (sharedWithEmails.length === 0) return
    // Fetch info for emails we don't already have
    const missing = sharedWithEmails.filter((e) => !peopleInfo[e])
    if (missing.length === 0) return

    // Search each missing email to get name/avatar
    Promise.all(
      missing.map(async (email) => {
        try {
          const res = await pingman.get<{ items: MentionItem[] }>(
            `/api/gideon/v1/mentions/search?query=${encodeURIComponent(email)}&category=collaborator&limit=1`
          )
          if (res.ok && res.data?.items?.length > 0) {
            const item = res.data.items[0]
            return { email, name: item.label, avatar_url: item.avatar_url }
          }
        } catch { /* ignore */ }
        return { email, name: email.split("@")[0], avatar_url: undefined }
      })
    ).then((results) => {
      const newInfo: Record<string, PersonInfo> = {}
      for (const r of results) {
        newInfo[r.email] = { name: r.name, avatar_url: r.avatar_url }
      }
      setPeopleInfo((prev) => ({ ...prev, ...newInfo }))
    })
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sharedWithEmails])

  // Close on click outside
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (dialogRef.current && !dialogRef.current.contains(e.target as Node)) {
        onClose()
      }
    }
    document.addEventListener("mousedown", handler)
    return () => document.removeEventListener("mousedown", handler)
  }, [onClose])

  // Close on Escape
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose()
    }
    document.addEventListener("keydown", handler)
    return () => document.removeEventListener("keydown", handler)
  }, [onClose])

  const shareUrl = typeof window !== "undefined"
    ? `${window.location.origin}/chat/${conversationId}`
    : `/chat/${conversationId}`

  const handleCopyLink = useCallback(() => {
    navigator.clipboard.writeText(shareUrl)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }, [shareUrl])

  const handleToggleLink = useCallback(async () => {
    await updateShare({ link_shared: !linkShared })
  }, [linkShared, updateShare])

  const handleSearchPeople = useCallback((query: string) => {
    setSearchQuery(query)
    if (searchDebounce.current) clearTimeout(searchDebounce.current)
    if (!query.trim()) {
      setSearchResults([])
      setSearching(false)
      return
    }
    setSearching(true)
    searchDebounce.current = setTimeout(async () => {
      try {
        const res = await pingman.get<{ items: MentionItem[] }>(
          `/api/gideon/v1/mentions/search?query=${encodeURIComponent(query)}&category=collaborator&limit=10`
        )
        if (res.ok) {
          const items = (res.data?.items || []).filter(
            (item) => !sharedWithEmails.includes(item.subtitle?.toLowerCase() || "")
          )
          setSearchResults(items)
          // Update dropdown position
          if (searchInputRef.current) {
            const rect = searchInputRef.current.getBoundingClientRect()
            setDropdownPos({ top: rect.bottom + 4, left: rect.left, width: rect.width })
          }
        }
      } catch {
        // ignore
      } finally {
        setSearching(false)
      }
    }, 200)
  }, [sharedWithEmails])

  const handleAddPerson = useCallback(async (item: MentionItem) => {
    const email = (item.subtitle || item.id).toLowerCase().trim()
    if (!email || sharedWithEmails.includes(email)) return
    // Cache person info locally
    setPeopleInfo((prev) => ({
      ...prev,
      [email]: { name: item.label, avatar_url: item.avatar_url },
    }))
    await updateShare({ shared_with_emails: [...sharedWithEmails, email] })
    setSearchQuery("")
    setSearchResults([])
  }, [sharedWithEmails, updateShare])

  const handleRemovePerson = useCallback(async (email: string) => {
    await updateShare({
      shared_with_emails: sharedWithEmails.filter((e) => e !== email),
    })
  }, [sharedWithEmails, updateShare])

  const dialog = (
    <div className="fixed inset-0 z-[9999] flex items-center justify-center" style={{ background: "rgba(0,0,0,0.6)" }}>
      <div
        ref={dialogRef}
        className="w-full max-w-md rounded-2xl"
        style={{
          background: "var(--surface)",
          border: "1px solid var(--border)",
        }}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4" style={{ borderBottom: "1px solid var(--border)" }}>
          <h2 className="text-sm font-semibold" style={{ color: "var(--text-primary)" }}>
            Share conversation
          </h2>
          <button onClick={onClose} className="p-1 rounded-lg hover:bg-white/10 transition-colors">
            <X size={16} style={{ color: "var(--text-tertiary)" }} />
          </button>
        </div>

        <div className="px-5 py-4 space-y-5">
          {/* Link sharing toggle */}
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2.5">
              <div className="w-8 h-8 rounded-lg flex items-center justify-center" style={{ background: "rgba(255,255,255,0.06)" }}>
                <Link2 size={14} style={{ color: "var(--text-secondary)" }} />
              </div>
              <div>
                <div className="text-sm" style={{ color: "var(--text-primary)" }}>Anyone with the link</div>
                <div className="text-xs" style={{ color: "var(--text-tertiary)" }}>Anyone logged into Gideon can view</div>
              </div>
            </div>
            <button
              onClick={handleToggleLink}
              disabled={loading}
              className="relative w-10 h-5 rounded-full transition-colors"
              style={{
                background: linkShared ? "rgba(59,130,246,0.8)" : "rgba(255,255,255,0.1)",
              }}
            >
              <div
                className="absolute top-0.5 w-4 h-4 rounded-full transition-all"
                style={{
                  background: "white",
                  left: linkShared ? "22px" : "2px",
                }}
              />
            </button>
          </div>

          {/* Copy link */}
          <div
            className="flex items-center gap-2 rounded-lg px-3 py-2"
            style={{ background: "rgba(255,255,255,0.04)", border: "1px solid var(--border)" }}
          >
            <span className="flex-1 text-xs truncate" style={{ color: "var(--text-secondary)" }}>
              {shareUrl}
            </span>
            <button
              onClick={handleCopyLink}
              className="flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-medium transition-colors shrink-0"
              style={{
                background: copied ? "rgba(34,197,94,0.15)" : "rgba(59,130,246,0.15)",
                color: copied ? "#22c55e" : "#60a5fa",
              }}
            >
              {copied ? <><Check size={12} /> Copied</> : "Copy link"}
            </button>
          </div>

          {/* Divider */}
          <div className="flex items-center gap-3">
            <div className="flex-1 h-px" style={{ background: "var(--border)" }} />
            <span className="text-[10px] uppercase tracking-wider" style={{ color: "var(--text-tertiary)" }}>
              Or share with specific people
            </span>
            <div className="flex-1 h-px" style={{ background: "var(--border)" }} />
          </div>

          {/* People search */}
          <div
            ref={searchInputRef}
            className="flex items-center gap-2 rounded-lg px-3 py-2"
            style={{ background: "rgba(255,255,255,0.04)", border: "1px solid var(--border)" }}
          >
            <Search size={14} style={{ color: "var(--text-tertiary)" }} />
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => handleSearchPeople(e.target.value)}
              onFocus={() => {
                // Recalculate dropdown position when input is focused
                if (searchInputRef.current) {
                  const rect = searchInputRef.current.getBoundingClientRect()
                  setDropdownPos({ top: rect.bottom + 4, left: rect.left, width: rect.width })
                }
              }}
              placeholder="Search by name or email..."
              className="flex-1 bg-transparent text-sm outline-none"
              style={{ color: "var(--text-primary)" }}
            />
          </div>

          {/* Shared with list */}
          {sharedWithEmails.length > 0 && (
            <div>
              <div className="text-xs font-medium mb-2" style={{ color: "var(--text-tertiary)" }}>
                Shared with
              </div>
              <div className="space-y-1">
                {sharedWithEmails.map((email) => {
                  const info = peopleInfo[email]
                  return (
                    <div
                      key={email}
                      className="flex items-center justify-between px-3 py-2 rounded-lg"
                      style={{ background: "rgba(255,255,255,0.03)" }}
                    >
                      <div className="flex items-center gap-2.5 min-w-0">
                        {info?.avatar_url ? (
                          <img src={info.avatar_url} className="w-6 h-6 rounded-full shrink-0" alt="" />
                        ) : (
                          <div className="w-6 h-6 rounded-full flex items-center justify-center shrink-0" style={{ background: "rgba(255,255,255,0.08)" }}>
                            <Users size={12} style={{ color: "var(--text-tertiary)" }} />
                          </div>
                        )}
                        <div className="min-w-0">
                          {info?.name && info.name !== email && (
                            <div className="text-sm truncate" style={{ color: "var(--text-primary)" }}>{info.name}</div>
                          )}
                          <div className="text-xs truncate" style={{ color: info?.name ? "var(--text-tertiary)" : "var(--text-primary)" }}>{email}</div>
                        </div>
                      </div>
                      <button
                        onClick={() => handleRemovePerson(email)}
                        className="p-1 rounded hover:bg-white/10 transition-colors shrink-0"
                      >
                        <X size={12} style={{ color: "var(--text-tertiary)" }} />
                      </button>
                    </div>
                  )
                })}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )

  // Search results dropdown — rendered via portal so it's not clipped by dialog
  const dropdown = searchResults.length > 0 && dropdownPos.width > 0 ? createPortal(
    <div
      className="fixed rounded-lg overflow-hidden max-h-52 overflow-y-auto shadow-2xl"
      style={{
        top: dropdownPos.top,
        left: dropdownPos.left,
        width: dropdownPos.width,
        background: "rgba(24,24,24,0.98)",
        border: "1px solid rgba(255,255,255,0.1)",
        backdropFilter: "blur(16px)",
        zIndex: 10000,
      }}
      onMouseDown={(e) => e.stopPropagation()} // prevent dialog close-on-outside-click
    >
      {searchResults.map((item) => (
        <button
          key={item.id}
          onClick={() => handleAddPerson(item)}
          className="w-full flex items-center gap-2.5 px-3 py-2.5 text-left hover:bg-white/[0.08] transition-colors"
        >
          {item.avatar_url ? (
            <img src={item.avatar_url} className="w-7 h-7 rounded-full shrink-0" alt="" />
          ) : (
            <div className="w-7 h-7 rounded-full flex items-center justify-center shrink-0" style={{ background: "rgba(255,255,255,0.08)" }}>
              <Users size={12} style={{ color: "var(--text-tertiary)" }} />
            </div>
          )}
          <div className="flex-1 min-w-0">
            <div className="text-sm truncate" style={{ color: "var(--text-primary)" }}>{item.label}</div>
            {item.subtitle && <div className="text-xs truncate" style={{ color: "var(--text-tertiary)" }}>{item.subtitle}</div>}
          </div>
        </button>
      ))}
    </div>,
    document.body
  ) : null

  return (
    <>
      {createPortal(dialog, document.body)}
      {dropdown}
    </>
  )
}
