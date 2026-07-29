"use client"

import { useState, useRef, useEffect } from "react"
import {
  MessageSquare,
  CheckSquare,
  Link,
  Workflow,
  MoreHorizontal,
  Settings,
  Sparkles,
  FolderOpen,
  BarChart3,
  Search,
  X,
} from "lucide-react"

interface MobileBottomNavProps {
  activePage: string
  onNavigate: (page: string) => void
  isAdmin?: boolean
  integrationErrorCount?: number
}

const MORE_ITEMS = [
  { icon: Search, label: "Search", id: "search" },
  { icon: Settings, label: "Settings", id: "settings" },
  { icon: Sparkles, label: "Skills", id: "skills" },
  { icon: FolderOpen, label: "Files", id: "files" },
  { icon: BarChart3, label: "Usage", id: "usage", adminOnly: true },
]

export default function MobileBottomNav({
  activePage,
  onNavigate,
  isAdmin,
  integrationErrorCount,
}: MobileBottomNavProps) {
  const [moreOpen, setMoreOpen] = useState(false)
  const moreRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!moreOpen) return
    const handler = (e: MouseEvent) => {
      if (moreRef.current && !moreRef.current.contains(e.target as Node)) {
        setMoreOpen(false)
      }
    }
    document.addEventListener("mousedown", handler)
    return () => document.removeEventListener("mousedown", handler)
  }, [moreOpen])

  const isMoreActive = ["settings", "skills", "files", "usage", "memory"].includes(activePage)

  const tabs = [
    { icon: Link, label: "Connect", id: "integrations", badge: integrationErrorCount },
    { icon: CheckSquare, label: "To Do", id: "todo" },
    { icon: MessageSquare, label: "Chat", id: "chat" },
    { icon: Workflow, label: "Workflows", id: "workflows" },
    { icon: MoreHorizontal, label: "More", id: "_more" },
  ]

  return (
    <>
      {/* More menu overlay */}
      {moreOpen && (
        <div ref={moreRef} className="fixed bottom-16 left-0 right-0 z-50 px-3 pb-1">
          <div
            className="rounded-2xl overflow-hidden"
            style={{
              background: "rgba(28,28,30,0.98)",
              border: "1px solid rgba(255,255,255,0.1)",
              boxShadow: "0 -8px 32px rgba(0,0,0,0.5)",
              backdropFilter: "blur(20px)",
            }}
          >
            <div className="flex items-center justify-between px-4 pt-3 pb-2">
              <span className="text-[12px] font-medium uppercase tracking-wider" style={{ color: "var(--text-tertiary)" }}>
                More
              </span>
              <button onClick={() => setMoreOpen(false)} className="p-1 rounded-lg" style={{ color: "var(--text-tertiary)" }}>
                <X size={16} />
              </button>
            </div>
            {MORE_ITEMS.filter((item) => !item.adminOnly || isAdmin).map((item) => (
              <button
                key={item.id}
                onClick={() => {
                  setMoreOpen(false)
                  onNavigate(item.id)
                }}
                className="w-full flex items-center gap-3 px-4 py-3 text-left transition-colors active:bg-white/5"
                style={{
                  color: activePage === item.id ? "#a78bfa" : "var(--text-primary)",
                }}
              >
                <item.icon size={20} />
                <span className="text-[15px]">{item.label}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Bottom tab bar */}
      <div
        className="fixed bottom-0 left-0 right-0 z-40 flex items-end justify-around"
        style={{
          background: "rgba(0,0,0,0.95)",
          borderTop: "1px solid rgba(255,255,255,0.08)",
          backdropFilter: "blur(20px)",
          paddingBottom: "env(safe-area-inset-bottom, 8px)",
        }}
      >
        {tabs.map((tab) => {
          const isActive = tab.id === "_more"
            ? isMoreActive || moreOpen
            : tab.id === "chat"
              ? activePage === "chat" || activePage === "conversations"
              : activePage === tab.id

          return (
            <button
              key={tab.id}
              onClick={() => {
                if (tab.id === "_more") {
                  setMoreOpen((prev) => !prev)
                } else {
                  setMoreOpen(false)
                  onNavigate(tab.id)
                }
              }}
              className="flex flex-col items-center gap-0.5 py-2 px-3 min-w-[64px] transition-colors"
              style={{ color: isActive ? "#a78bfa" : "rgba(255,255,255,0.45)" }}
            >
              <div className="relative">
                <tab.icon size={22} strokeWidth={isActive ? 2.2 : 1.8} />
                {tab.badge != null && tab.badge > 0 && (
                  <span
                    className="absolute -top-1 -right-1.5 min-w-[14px] h-[14px] flex items-center justify-center rounded-full text-[9px] font-bold px-0.5"
                    style={{ background: "#EF4444", color: "#fff" }}
                  >
                    {tab.badge > 9 ? "9+" : tab.badge}
                  </span>
                )}
              </div>
              <span className="text-[10px] font-medium">{tab.label}</span>
            </button>
          )
        })}
      </div>
    </>
  )
}
