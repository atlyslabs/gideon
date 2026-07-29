"use client"

import { useState, useMemo } from "react"
import { ArrowLeft, Search, FileText, Image as ImageIcon, Sheet, FileSpreadsheet, Presentation, Code, ExternalLink, MessageSquare } from "lucide-react"
import useSWR from "swr"
import { pingman } from "../utils/pingman"

interface FilesPageProps {
  onBack: () => void
  onOpenConversation?: (id: string) => void
}

interface FileItem {
  id: string
  filename: string
  mime_type: string
  size_bytes: number
  is_image: boolean
  drive_url: string | null
  conversation_id: string | null
  conversation_title: string | null
  created_at: string
}

/* -- Type config -- */

const FILE_TYPE_ICONS: Record<string, { icon: typeof FileText; color: string }> = {
  "application/pdf": { icon: FileText, color: "#EF4444" },
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": { icon: FileSpreadsheet, color: "#22C55E" },
  "application/vnd.google-apps.spreadsheet": { icon: Sheet, color: "#22C55E" },
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document": { icon: FileText, color: "#3B82F6" },
  "application/vnd.google-apps.document": { icon: FileText, color: "#3B82F6" },
  "application/vnd.openxmlformats-officedocument.presentationml.presentation": { icon: Presentation, color: "#F59E0B" },
  "text/plain": { icon: Code, color: "#8B5CF6" },
  "text/csv": { icon: FileSpreadsheet, color: "#10B981" },
  "application/json": { icon: Code, color: "#F59E0B" },
}

function getFileIcon(mimeType: string, isImage: boolean) {
  if (isImage) return { icon: ImageIcon, color: "#EC4899" }
  if (mimeType.startsWith("text/") || mimeType.includes("javascript") || mimeType.includes("python")) {
    return { icon: Code, color: "#8B5CF6" }
  }
  return FILE_TYPE_ICONS[mimeType] || { icon: FileText, color: "#6B7280" }
}

function formatSize(bytes: number): string {
  if (bytes === 0) return ""
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function formatDate(iso: string): string {
  const d = new Date(iso)
  const now = new Date()
  const diffMs = now.getTime() - d.getTime()
  const diffMins = Math.floor(diffMs / 60000)
  if (diffMins < 60) return `${diffMins}m ago`
  const diffHrs = Math.floor(diffMins / 60)
  if (diffHrs < 24) return `${diffHrs}h ago`
  const diffDays = Math.floor(diffHrs / 24)
  if (diffDays < 7) return `${diffDays}d ago`
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" })
}

/* -- Filter types -- */

type FilterType = "all" | "pdf" | "sheets" | "docs" | "images" | "code"

const FILTERS: { key: FilterType; label: string }[] = [
  { key: "all", label: "All" },
  { key: "pdf", label: "PDFs" },
  { key: "sheets", label: "Sheets" },
  { key: "docs", label: "Docs" },
  { key: "images", label: "Images" },
  { key: "code", label: "Code" },
]

function matchesFilter(file: FileItem, filter: FilterType): boolean {
  if (filter === "all") return true
  const mt = file.mime_type
  if (filter === "pdf") return mt === "application/pdf"
  if (filter === "sheets") return mt.includes("spreadsheet") || mt === "text/csv" || mt.includes("google-apps.spreadsheet")
  if (filter === "docs") return mt.includes("wordprocessing") || mt.includes("google-apps.document") || mt.includes("presentation")
  if (filter === "images") return file.is_image
  if (filter === "code") return mt.startsWith("text/") || mt.includes("json") || mt.includes("javascript") || mt.includes("python")
  return true
}

/* -- Date grouping -- */

interface FileGroup {
  label: string
  files: FileItem[]
}

function groupByDate(files: FileItem[]): FileGroup[] {
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const weekAgo = new Date(today.getTime() - 7 * 86400000)

  const groups: { today: FileItem[]; week: FileItem[]; earlier: FileItem[] } = {
    today: [], week: [], earlier: [],
  }

  for (const f of files) {
    const d = new Date(f.created_at)
    if (d >= today) groups.today.push(f)
    else if (d >= weekAgo) groups.week.push(f)
    else groups.earlier.push(f)
  }

  const result: FileGroup[] = []
  if (groups.today.length) result.push({ label: "Today", files: groups.today })
  if (groups.week.length) result.push({ label: "This Week", files: groups.week })
  if (groups.earlier.length) result.push({ label: "Earlier", files: groups.earlier })
  return result
}

/* -- File row -- */

function FileRow({ file, onOpenConversation }: { file: FileItem; onOpenConversation?: (id: string) => void }) {
  const { icon: Icon, color } = getFileIcon(file.mime_type, file.is_image)
  const ext = file.filename.split(".").pop()?.toUpperCase() || ""

  return (
    <div className="flex items-center gap-3 px-3 py-3 rounded-xl group transition-colors hover:bg-white/[0.04]">
      <div
        className="w-10 h-10 rounded-xl flex items-center justify-center shrink-0"
        style={{ background: `${color}12`, border: `1px solid ${color}20` }}
      >
        <Icon size={20} style={{ color }} />
      </div>

      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className="text-[13px] font-medium truncate" style={{ color: "var(--text-primary)" }}>
            {file.filename}
          </span>
          <span
            className="text-[10px] font-medium px-1.5 py-0.5 rounded shrink-0 uppercase tracking-wide"
            style={{ background: `${color}15`, color }}
          >
            {ext}
          </span>
        </div>
        <div className="flex items-center gap-1.5 mt-1">
          {file.size_bytes > 0 && (
            <span className="text-[11px]" style={{ color: "var(--text-tertiary)" }}>
              {formatSize(file.size_bytes)}
            </span>
          )}
          {file.size_bytes > 0 && (
            <span className="text-[11px]" style={{ color: "var(--text-tertiary)" }}>·</span>
          )}
          <span className="text-[11px]" style={{ color: "var(--text-tertiary)" }}>
            {formatDate(file.created_at)}
          </span>
          {file.conversation_title && (
            <>
              <span className="text-[11px]" style={{ color: "var(--text-tertiary)" }}>·</span>
              <span className="text-[11px] truncate max-w-[180px]" style={{ color: "var(--text-tertiary)" }}>
                {file.conversation_title}
              </span>
            </>
          )}
        </div>
      </div>

      <div className="flex items-center gap-1 opacity-100 md:opacity-0 md:group-hover:opacity-100 transition-opacity">
        {file.conversation_id && onOpenConversation && (
          <button
            onClick={() => onOpenConversation(file.conversation_id!)}
            className="p-1.5 rounded-lg transition-colors"
            style={{ color: "var(--text-tertiary)" }}
            onMouseEnter={e => { e.currentTarget.style.background = "rgba(255,255,255,0.08)"; e.currentTarget.style.color = "var(--text-primary)" }}
            onMouseLeave={e => { e.currentTarget.style.background = "transparent"; e.currentTarget.style.color = "var(--text-tertiary)" }}
            title="Open conversation"
          >
            <MessageSquare size={14} />
          </button>
        )}
        {file.drive_url && (
          <a
            href={file.drive_url}
            target="_blank"
            rel="noopener noreferrer"
            className="p-1.5 rounded-lg transition-colors"
            style={{ color: "var(--text-tertiary)" }}
            onMouseEnter={e => { (e.currentTarget as HTMLElement).style.background = "rgba(255,255,255,0.08)"; (e.currentTarget as HTMLElement).style.color = "var(--text-primary)" }}
            onMouseLeave={e => { (e.currentTarget as HTMLElement).style.background = "transparent"; (e.currentTarget as HTMLElement).style.color = "var(--text-tertiary)" }}
            title="Open in Google Drive"
          >
            <ExternalLink size={14} />
          </a>
        )}
      </div>
    </div>
  )
}

/* -- Main page -- */

export default function FilesPage({ onBack, onOpenConversation }: FilesPageProps) {
  const [search, setSearch] = useState("")
  const [filter, setFilter] = useState<FilterType>("all")

  const { data: files = [], isLoading } = useSWR<FileItem[]>(
    "/api/gideon/v1/chat/files",
    async (url: string) => {
      const res = await pingman.get(url)
      return res.ok ? (res.data as FileItem[]) : []
    },
  )

  const filtered = useMemo(() => {
    let result = files
    if (filter !== "all") result = result.filter(f => matchesFilter(f, filter))
    if (search.trim()) {
      const q = search.toLowerCase()
      result = result.filter(f =>
        f.filename.toLowerCase().includes(q) ||
        f.conversation_title?.toLowerCase().includes(q)
      )
    }
    return result
  }, [files, search, filter])

  const groups = useMemo(() => groupByDate(filtered), [filtered])

  // Count per filter for badges
  const filterCounts = useMemo(() => {
    const counts: Record<FilterType, number> = { all: files.length, pdf: 0, sheets: 0, docs: 0, images: 0, code: 0 }
    for (const f of files) {
      for (const key of ["pdf", "sheets", "docs", "images", "code"] as FilterType[]) {
        if (matchesFilter(f, key)) counts[key]++
      }
    }
    return counts
  }, [files])

  return (
    <div className="flex-1 flex flex-col min-w-0 rounded-xl overflow-hidden" style={{ background: "var(--surface)" }}>
      {/* Header */}
      <div className="shrink-0 px-6 pt-6 pb-4" style={{ borderBottom: "1px solid var(--border)" }}>
        <div className="flex items-center gap-3 mb-4">
          <button
            onClick={onBack}
            className="flex items-center gap-1.5 text-[13px] transition-colors"
            style={{ color: "var(--text-secondary)" }}
            onMouseEnter={e => { e.currentTarget.style.color = "var(--text-primary)" }}
            onMouseLeave={e => { e.currentTarget.style.color = "var(--text-secondary)" }}
          >
            <ArrowLeft size={14} />
            Back
          </button>
        </div>

        <div className="flex items-center justify-between mb-4">
          <h1 className="text-[20px] font-semibold" style={{ color: "var(--text-primary)" }}>
            Files
          </h1>
          {files.length > 0 && (
            <span className="text-[12px] font-medium px-2 py-1 rounded-lg" style={{ background: "var(--surface-elevated)", color: "var(--text-tertiary)" }}>
              {files.length} file{files.length !== 1 ? "s" : ""}
            </span>
          )}
        </div>

        {/* Search */}
        <div className="relative mb-3">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2" style={{ color: "var(--text-tertiary)" }} />
          <input
            type="text"
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Search files..."
            className="w-full pl-9 pr-4 py-2 rounded-xl text-[13px] outline-none"
            style={{
              background: "var(--surface-input)",
              border: "1px solid var(--border)",
              color: "var(--text-primary)",
            }}
          />
        </div>

        {/* Filter chips */}
        <div className="flex gap-1.5 flex-wrap">
          {FILTERS.map(f => {
            const count = filterCounts[f.key]
            if (f.key !== "all" && count === 0) return null
            const active = filter === f.key
            return (
              <button
                key={f.key}
                onClick={() => setFilter(f.key)}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[12px] font-medium transition-colors"
                style={{
                  background: active ? "rgba(124,58,237,0.15)" : "var(--surface-elevated)",
                  border: `1px solid ${active ? "rgba(124,58,237,0.3)" : "var(--border)"}`,
                  color: active ? "#a78bfa" : "var(--text-secondary)",
                }}
              >
                {f.label}
                {f.key !== "all" && (
                  <span className="text-[10px] opacity-60">{count}</span>
                )}
              </button>
            )
          })}
        </div>
      </div>

      {/* File list */}
      <div className="flex-1 overflow-y-auto px-6 py-4">
        {isLoading ? (
          <div className="flex justify-center py-16">
            <div className="spinner" />
          </div>
        ) : filtered.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-16">
            <FileText size={32} className="mb-3" style={{ color: "var(--text-tertiary)" }} />
            <p className="text-[14px]" style={{ color: "var(--text-tertiary)" }}>
              {search || filter !== "all" ? "No files match your search" : "No files yet. Upload files in chat or create Sheets/Docs."}
            </p>
          </div>
        ) : (
          <div className="space-y-6">
            {groups.map(group => (
              <div key={group.label}>
                <h3
                  className="text-[11px] font-medium uppercase tracking-wider mb-2 px-3"
                  style={{ color: "var(--text-tertiary)" }}
                >
                  {group.label}
                </h3>
                <div
                  className="rounded-xl overflow-hidden"
                  style={{ border: "1px solid var(--border)" }}
                >
                  {group.files.map((file, i) => (
                    <div
                      key={file.id}
                      style={i > 0 ? { borderTop: "1px solid var(--border-subtle)" } : undefined}
                    >
                      <FileRow file={file} onOpenConversation={onOpenConversation} />
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
