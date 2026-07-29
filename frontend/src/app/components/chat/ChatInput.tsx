"use client"

import { useState, useRef, useCallback, useEffect } from "react"
import { ArrowUp, Square, Paperclip, X, Loader2, FileText, Image as ImageIcon } from "lucide-react"
import type { MentionItem } from "../../types"
import { pingman } from "../../utils/pingman"
import { getIntegrationIcon } from "../../utils/integrationIcons"
import MentionDropdown from "./MentionDropdown"
import SlashCommandDropdown from "./SlashCommandDropdown"
import type { SlashCommand } from "../../hooks/useSlashCommands"
import useIsMobile from "../../hooks/useIsMobile"

interface AttachedFile {
  id: string
  filename: string
  size_bytes: number
  is_image: boolean
  uploading?: boolean
}

const MAX_FILE_SIZE = 10 * 1024 * 1024 // 10 MB
const MAX_FILES = 5

const ALLOWED_EXTENSIONS = new Set([
  ".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".log",
  ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".java", ".rb", ".rs", ".sql",
  ".sh", ".css", ".html", ".htm", ".svg", ".toml", ".ini", ".cfg",
  ".c", ".cpp", ".h", ".swift", ".kt", ".php",
  ".pdf", ".docx", ".doc", ".rtf",
  ".xlsx", ".xls",
  ".pptx",
  ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
])

interface ChatInputProps {
  onSend: (message: string, mentions?: MentionItem[], options?: { fileIds?: string[]; files?: { filename: string; ext: string }[]; autoMode?: boolean }) => void
  onStop?: () => void
  disabled: boolean
  connectedProviders?: string[]
  inline?: boolean
  conversationId?: string | null
  autoMode?: boolean
  onAutoModeChange?: (on: boolean) => void
  /** Predicted next-reply ghost text. When set and the editor is empty, shown
   *  as placeholder with a Tab pill; Tab accepts and inserts it. */
  suggestion?: string | null
  onSuggestionConsumed?: () => void
}

/** Block-level tags that contentEditable uses to wrap each new line. iOS
 *  Safari and mobile Chrome insert <div> (sometimes <p>) on Enter instead of
 *  <br>, so the walker MUST treat their boundaries as newlines or messages
 *  arrive on the server with all newlines collapsed (mobile bug, May 2026). */
const BLOCK_TAGS = new Set(["DIV", "P", "LI", "BLOCKQUOTE", "PRE", "UL", "OL", "H1", "H2", "H3", "H4", "H5", "H6"])

/** Extract plain text + mention metadata from contentEditable div.
 *  Recursive — nested <div> wrappers (Enter on mobile) emit newlines. */
function extractContent(el: HTMLElement): { text: string; mentions: MentionItem[] } {
  const mentions: MentionItem[] = []
  let text = ""

  const walk = (node: Node) => {
    for (const child of node.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) {
        text += child.textContent || ""
        continue
      }
      if (!(child instanceof HTMLElement)) continue

      if (child.dataset.mentionId) {
        // It's a mention pill — extract metadata
        const mention: MentionItem = {
          id: child.dataset.mentionId,
          category: child.dataset.mentionCategory as any,
          label: child.dataset.mentionLabel || child.textContent || "",
          url: child.dataset.mentionUrl || undefined,
          icon: child.dataset.mentionIcon || undefined,
        }
        mentions.push(mention)
        text += `@${mention.label}`
        continue
      }

      if (child.tagName === "BR") {
        text += "\n"
        continue
      }

      if (BLOCK_TAGS.has(child.tagName)) {
        // Insert a newline at the start of a new block, unless we're at the
        // very beginning or the previous char is already a newline. Then
        // descend so nested mention pills / BRs are still recognized.
        if (text && !text.endsWith("\n")) text += "\n"
        walk(child)
        continue
      }

      // Inline non-mention element (e.g. <span>) — recurse so inner nodes
      // (including BR / mentions) are processed.
      walk(child)
    }
  }

  walk(el)
  return { text: text.trim(), mentions }
}

/** Insert a mention pill span at the current cursor position */
function insertMentionPill(item: MentionItem) {
  const sel = window.getSelection()
  if (!sel || sel.rangeCount === 0) return

  const range = sel.getRangeAt(0)

  // Create the pill element
  const pill = document.createElement("span")
  pill.contentEditable = "false"
  pill.dataset.mentionId = item.id
  pill.dataset.mentionCategory = item.category
  pill.dataset.mentionLabel = item.label
  if (item.url) pill.dataset.mentionUrl = item.url
  if (item.icon) pill.dataset.mentionIcon = item.icon
  pill.className = "inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md text-xs font-medium mx-0.5 align-baseline"
  pill.style.cssText = "background: rgba(59,130,246,0.15); color: #60a5fa; border: 1px solid rgba(59,130,246,0.25); user-select: all; cursor: default; display: inline-flex; vertical-align: baseline; line-height: 1.4;"
  pill.textContent = `@${item.label}`

  // Insert pill
  range.deleteContents()
  range.insertNode(pill)

  // Move cursor after the pill
  const space = document.createTextNode("\u00A0") // non-breaking space
  pill.after(space)
  range.setStartAfter(space)
  range.setEndAfter(space)
  sel.removeAllRanges()
  sel.addRange(range)
}

/** Delete the @... text before cursor (typed while dropdown was open) */
function deleteAtQuery(el: HTMLElement) {
  const sel = window.getSelection()
  if (!sel || sel.rangeCount === 0) return

  const range = sel.getRangeAt(0)
  const node = range.startContainer
  if (node.nodeType !== Node.TEXT_NODE) return

  const text = node.textContent || ""
  const cursorPos = range.startOffset

  // Find the @ before cursor
  const beforeCursor = text.slice(0, cursorPos)
  const atIdx = beforeCursor.lastIndexOf("@")
  if (atIdx === -1) return

  // Delete from @ to cursor
  node.textContent = text.slice(0, atIdx) + text.slice(cursorPos)

  // Restore cursor
  const newRange = document.createRange()
  const newPos = Math.min(atIdx, (node.textContent || "").length)
  newRange.setStart(node, newPos)
  newRange.setEnd(node, newPos)
  sel.removeAllRanges()
  sel.addRange(newRange)
}


export default function ChatInput({ onSend, onStop, disabled, connectedProviders = [], inline = false, conversationId, autoMode = false, onAutoModeChange, suggestion, onSuggestionConsumed }: ChatInputProps) {
  const [mentionOpen, setMentionOpen] = useState(false)
  const [mentionQuery, setMentionQuery] = useState("")
  const [slashOpen, setSlashOpen] = useState(false)
  const [slashQuery, setSlashQuery] = useState("")
  const [hasContent, setHasContent] = useState(false)
  const [attachedFiles, setAttachedFiles] = useState<AttachedFile[]>([])
  const [isDragOver, setIsDragOver] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const editorRef = useRef<HTMLDivElement>(null)
  const cardRef = useRef<HTMLDivElement>(null)
  // Timestamp of the last Escape press, for the double-Esc-to-clear gesture.
  const lastEscRef = useRef(0)
  const isMobile = useIsMobile()

  // Auto-focus on mount and when re-enabled
  useEffect(() => {
    if (!disabled && editorRef.current) {
      editorRef.current.focus()
    }
  }, [disabled])

  // --- Draft persistence (survive accidental close/refresh, like ChatGPT) ---
  // The unsent composer content — including mention pills, hence innerHTML, not
  // just text — is mirrored to localStorage on every edit, keyed per
  // conversation (and a shared "new" key for not-yet-created chats). Restored on
  // mount / conversation switch, cleared on send.
  const draftKey = `gideon:chat-draft:${conversationId || "new"}`

  const saveDraft = useCallback(() => {
    if (typeof window === "undefined" || !editorRef.current) return
    const text = editorRef.current.textContent || ""
    try {
      if (text.trim().length > 0) localStorage.setItem(draftKey, editorRef.current.innerHTML)
      else localStorage.removeItem(draftKey)
    } catch { /* localStorage full/blocked — draft persistence is best-effort */ }
  }, [draftKey])

  const clearDraft = useCallback(() => {
    if (typeof window === "undefined") return
    try { localStorage.removeItem(draftKey) } catch { /* best-effort */ }
  }, [draftKey])

  // Wipe the composer text (incl. mention/slash pills) and its saved draft, then
  // refocus. Used by the double-Esc gesture. Leaves attached files in place —
  // the gesture clears what was *typed*, not staged uploads.
  const clearComposer = useCallback(() => {
    if (!editorRef.current) return
    editorRef.current.innerHTML = ""
    clearDraft()
    setHasContent(false)
    setMentionOpen(false)
    setMentionQuery("")
    setSlashOpen(false)
    setSlashQuery("")
    editorRef.current.focus()
  }, [clearDraft])

  // Restore the saved draft for this conversation. Runs on mount and whenever
  // the conversation changes — loading that conversation's draft, or clearing
  // the editor if it now shows a different conversation's leftover text.
  useEffect(() => {
    if (typeof window === "undefined" || !editorRef.current) return
    const editor = editorRef.current
    let saved: string | null = null
    try { saved = localStorage.getItem(draftKey) } catch { /* best-effort */ }

    // Whatever is in the editor belongs to the PREVIOUS conversation and was
    // already mirrored to its own key on every edit, so we can replace it
    // wholesale: load this conversation's draft, or clear the box if it has
    // none. (Gating restore on "editor empty" left the prior conversation's
    // text visible when switching to a chat that also had a draft.)
    editor.innerHTML = saved || ""
    setHasContent((editor.textContent || "").length > 0)
    if (saved && !disabled) {
      // Drop the caret at the end of the restored text.
      const range = document.createRange()
      range.selectNodeContents(editor)
      range.collapse(false)
      const sel = window.getSelection()
      sel?.removeAllRanges()
      sel?.addRange(range)
    }
    // Only re-run when the conversation (key) changes — not on every keystroke.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftKey])

  const handleSend = useCallback(() => {
    if (!editorRef.current || disabled) return
    // Don't send while files are uploading
    if (attachedFiles.some(f => f.uploading)) return
    const { text, mentions } = extractContent(editorRef.current)
    if (!text && attachedFiles.length === 0) return
    const readyFiles = attachedFiles.filter(f => !f.uploading)
    const fileIds = readyFiles.map(f => f.id)
    const fileMeta = readyFiles.map(f => ({ filename: f.filename, ext: f.filename.includes(".") ? f.filename.split(".").pop()!.toUpperCase() : "" }))
    const opts: any = {}
    if (fileIds.length > 0) { opts.fileIds = fileIds; opts.files = fileMeta }
    if (autoMode) opts.autoMode = true
    onSend(text || "(see attached files)", mentions.length > 0 ? mentions : undefined, Object.keys(opts).length > 0 ? opts : undefined)
    editorRef.current.innerHTML = ""
    clearDraft()  // the message is sent — drop the saved draft
    setHasContent(false)
    setAttachedFiles([])
    setMentionOpen(false)
    setMentionQuery("")
    setSlashOpen(false)
    setSlashQuery("")
    onSuggestionConsumed?.()
  }, [disabled, onSend, attachedFiles, autoMode, onSuggestionConsumed, clearDraft])

  const uploadFile = useCallback(async (file: File) => {
    // Validate extension
    const ext = "." + file.name.split(".").pop()?.toLowerCase()
    if (!ALLOWED_EXTENSIONS.has(ext)) return
    if (file.size > MAX_FILE_SIZE) return
    if (attachedFiles.length >= MAX_FILES) return

    const tempId = `uploading-${Date.now()}-${Math.random()}`
    setAttachedFiles(prev => [...prev, { id: tempId, filename: file.name, size_bytes: file.size, is_image: file.type.startsWith("image/"), uploading: true }])

    try {
      const formData = new FormData()
      formData.append("file", file)
      if (conversationId) formData.append("conversation_id", conversationId)

      const res = await pingman.post<any>(
        `/api/gideon/v1/chat/upload${conversationId ? `?conversation_id=${conversationId}` : ""}`,
        formData,
      )
      if (!res.ok) {
        throw new Error(res.data?.detail || `Upload failed: ${res.status}`)
      }
      setAttachedFiles(prev => prev.map(f => f.id === tempId ? { ...res.data, uploading: false } : f))
    } catch {
      setAttachedFiles(prev => prev.filter(f => f.id !== tempId))
    }
  }, [attachedFiles.length, conversationId])

  const handleFileSelect = useCallback((files: FileList | null) => {
    if (!files) return
    const remaining = MAX_FILES - attachedFiles.length
    Array.from(files).slice(0, remaining).forEach(uploadFile)
  }, [attachedFiles.length, uploadFile])

  const handleDragOver = useCallback((e: React.DragEvent) => { e.preventDefault(); setIsDragOver(true) }, [])
  const handleDragLeave = useCallback(() => setIsDragOver(false), [])
  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    setIsDragOver(false)
    handleFileSelect(e.dataTransfer.files)
  }, [handleFileSelect])

  const removeFile = useCallback((fileId: string) => {
    setAttachedFiles(prev => prev.filter(f => f.id !== fileId))
  }, [])

  const handleSlashSelect = useCallback((cmd: SlashCommand) => {
    if (!editorRef.current) return
    const editor = editorRef.current
    // Clear the partial `/<query>` text and insert a styled, non-editable
    // slash token followed by a regular space text node where the user types
    // params. `extractContent` reads the span's textContent, so the BE still
    // sees `/<slug> ...` exactly as before.
    editor.innerHTML = ""
    const token = document.createElement("span")
    token.contentEditable = "false"
    token.dataset.slashCommand = cmd.slug
    token.className = "slash-token"
    token.style.cssText = "font-weight: 600; color: var(--text-primary); user-select: all; cursor: default;"
    token.textContent = `/${cmd.slug}`
    editor.appendChild(token)
    const space = document.createTextNode(" ")
    editor.appendChild(space)

    editor.focus()
    const sel = window.getSelection()
    if (sel) {
      const range = document.createRange()
      range.setStartAfter(space)
      range.setEndAfter(space)
      sel.removeAllRanges()
      sel.addRange(range)
    }
    setSlashOpen(false)
    setSlashQuery("")
    setHasContent(true)
    saveDraft()  // programmatic insert doesn't fire onInput — persist it
  }, [saveDraft])

  const handleClearQuery = useCallback(() => {
    if (editorRef.current) {
      deleteAtQuery(editorRef.current)
      setMentionQuery("")
    }
  }, [])

  const handleMentionSelect = useCallback((item: MentionItem) => {
    if (!editorRef.current) return
    const editor = editorRef.current

    // Focus editor first
    editor.focus()

    // Try to delete @query text — walk text nodes, skip pill internals
    const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT)
    let textNode: Text | null = null
    while (walker.nextNode()) {
      const node = walker.currentNode as Text
      // Skip text inside mention pills
      if ((node.parentElement as HTMLElement)?.dataset?.mentionId) continue
      const atIdx = (node.textContent || "").lastIndexOf("@")
      if (atIdx !== -1) {
        textNode = node
      }
    }
    if (textNode) {
      const text = textNode.textContent || ""
      const atIdx = text.lastIndexOf("@")
      if (atIdx !== -1) {
        // Place cursor after @ to make deleteAtQuery work
        const sel = window.getSelection()
        if (sel) {
          const range = document.createRange()
          range.setStart(textNode, text.length)
          range.setEnd(textNode, text.length)
          sel.removeAllRanges()
          sel.addRange(range)
        }
        deleteAtQuery(editor)
      }
    }

    // Now place cursor at end and insert pill
    const sel = window.getSelection()
    if (sel) {
      const range = document.createRange()
      range.selectNodeContents(editor)
      range.collapse(false)
      sel.removeAllRanges()
      sel.addRange(range)
    }

    insertMentionPill(item)
    setMentionOpen(false)
    setMentionQuery("")
    setHasContent(true)
    saveDraft()  // pill insert doesn't fire onInput — persist incl. pill markup
  }, [saveDraft])

  const acceptSuggestion = useCallback(() => {
    if (!editorRef.current || !suggestion) return
    editorRef.current.innerHTML = ""
    const node = document.createTextNode(suggestion)
    editorRef.current.appendChild(node)
    setHasContent(true)
    saveDraft()  // an accepted suggestion is now unsent content — persist it too
    editorRef.current.focus()
    const sel = window.getSelection()
    if (sel) {
      const range = document.createRange()
      range.selectNodeContents(editorRef.current)
      range.collapse(false)
      sel.removeAllRanges()
      sel.addRange(range)
    }
    onSuggestionConsumed?.()
  }, [suggestion, onSuggestionConsumed, saveDraft])

  const handleKeyDown = (e: React.KeyboardEvent) => {
    // Tab to accept the suggested reply (only when no dropdowns and editor empty).
    if (
      e.key === "Tab"
      && !e.shiftKey
      && !slashOpen
      && !mentionOpen
      && !hasContent
      && suggestion
      && !disabled
    ) {
      e.preventDefault()
      acceptSuggestion()
      return
    }
    // When slash dropdown is open, delegate navigation keys to it.
    if (slashOpen) {
      if (["ArrowUp", "ArrowDown", "Enter", "Escape", "Tab"].includes(e.key)) {
        e.preventDefault()
        const handler = (window as any).__slashDropdownKeyDown
        if (handler) handler(e)
        return
      }
    }

    // When mention dropdown is open, delegate navigation keys
    if (mentionOpen) {
      if (["ArrowUp", "ArrowDown", "Enter", "Escape", "Tab"].includes(e.key)) {
        e.preventDefault()
        const handler = (window as any).__mentionDropdownKeyDown
        if (handler) {
          if (e.key === "Tab") {
            handler({ ...e, key: "Enter", preventDefault: () => {} })
          } else {
            handler(e)
          }
        }
        return
      }
      // Backspace on category menu → check if @ exists, if not close
      if (e.key === "Backspace") {
        // Check if there's an @ in editable text
        let hasAt = false
        if (editorRef.current) {
          const walker = document.createTreeWalker(editorRef.current, NodeFilter.SHOW_TEXT)
          while (walker.nextNode()) {
            const n = walker.currentNode as Text
            if ((n.parentElement as HTMLElement)?.dataset?.mentionId) continue
            if ((n.textContent || "").includes("@")) { hasAt = true; break }
          }
        }
        if (!hasAt) {
          // No @ in editor — close dropdown
          e.preventDefault()
          setMentionOpen(false)
          setMentionQuery("")
          return
        }
        // Has @ — let browser delete it, then close
        if (!mentionQuery) {
          setMentionOpen(false)
          setMentionQuery("")
          return
        }
      }
    }

    // Double-Esc clears the composer. A single Esc is a deliberate no-op so it
    // doesn't fight blur / dropdown-close semantics. When a mention/slash
    // dropdown is open the blocks above already consumed Escape to close it, so
    // this only fires with no dropdown open.
    if (e.key === "Escape") {
      if (!hasContent) return
      e.preventDefault()
      const now = Date.now()
      if (now - lastEscRef.current < 600) {
        lastEscRef.current = 0
        clearComposer()
      } else {
        lastEscRef.current = now
      }
      return
    }

    if (e.key === "Enter" && !e.shiftKey && !isMobile) {
      e.preventDefault()
      handleSend()
    }
  }

  const handleInput = () => {
    if (!editorRef.current) return

    const sel = window.getSelection()
    const text = editorRef.current.textContent || ""
    setHasContent(text.length > 0)
    saveDraft()  // mirror the unsent draft to localStorage on every edit
    // Once the user has typed anything, retire the ghost suggestion entirely
    // for this turn — otherwise clearing the draft mid-edit makes it reappear.
    if (text.length > 0 && suggestion) {
      onSuggestionConsumed?.()
    }

    // Slash command trigger: only when the entire input is `/<...>` with no
    // pills, no extra leading content. Mid-message slashes are not commands.
    const slashMatch = /^\/([a-z0-9-]*)$/i.exec(text)
    if (slashMatch) {
      setSlashQuery(slashMatch[1])
      if (!slashOpen) setSlashOpen(true)
      // Mention dropdown could have been left open from a prior keystroke
      // (e.g. user typed `@x` then deleted everything and started over with
      // `/`). Close it so the slash dropdown doesn't stack on top.
      if (mentionOpen) {
        setMentionOpen(false)
        setMentionQuery("")
      }
      // If the slash dropdown is open, suppress mention detection below.
      return
    } else if (slashOpen) {
      setSlashOpen(false)
      setSlashQuery("")
    }

    if (!sel || sel.rangeCount === 0) return
    const range = sel.getRangeAt(0)
    const node = range.startContainer

    if (node.nodeType === Node.TEXT_NODE) {
      const nodeText = node.textContent || ""
      const cursorPos = range.startOffset
      const beforeCursor = nodeText.slice(0, cursorPos)

      // Find @ trigger
      const atIdx = beforeCursor.lastIndexOf("@")
      if (atIdx !== -1) {
        const charBefore = atIdx > 0 ? beforeCursor[atIdx - 1] : " "
        if (charBefore === " " || charBefore === "\n" || charBefore === "\u00A0" || atIdx === 0) {
          const query = beforeCursor.slice(atIdx + 1)
          setMentionQuery(query)
          if (!mentionOpen) setMentionOpen(true)
          return
        }
      }
    }

    // If we get here and dropdown was open, check if @ was deleted in editable text (not pills)
    if (mentionOpen) {
      let hasAt = false
      const walker = document.createTreeWalker(editorRef.current, NodeFilter.SHOW_TEXT)
      while (walker.nextNode()) {
        const n = walker.currentNode as Text
        if ((n.parentElement as HTMLElement)?.dataset?.mentionId) continue
        if ((n.textContent || "").includes("@")) { hasAt = true; break }
      }
      if (!hasAt) {
        setMentionOpen(false)
        setMentionQuery("")
      }
    }
  }

  // Show placeholder
  const showPlaceholder = !hasContent && !disabled

  const canSend = (hasContent || attachedFiles.length > 0) && !disabled && !attachedFiles.some(f => f.uploading)

  return (
    <div className={inline ? "pt-2" : "px-6 pb-5 pt-3"}>
      <div className={inline ? "" : "max-w-5xl mx-auto"}>
        <div
          ref={cardRef}
          className="rounded-2xl overflow-hidden"
          style={{
            background: "#000000",
            border: isDragOver ? "0.5px solid var(--accent)" : "0.5px solid rgba(255,255,255,0.25)",
            outline: "0.5px solid rgba(255,255,255,0.05)",
            outlineOffset: "-1px",
            boxShadow: "0 -4px 24px rgba(0,0,0,0.5), 0 0 30px rgba(59,130,246,0.1), 0 0 0 1px rgba(59,130,246,0.12), inset 0 0.5px 0 rgba(255,255,255,0.06)",
            transition: "border-color 0.15s",
          }}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
        >
          {/* Attached file cards */}
          {attachedFiles.length > 0 && (
            <div className="flex flex-col gap-1.5 px-4 pt-3">
              {attachedFiles.map(f => {
                const ext = f.filename.includes(".") ? f.filename.split(".").pop()?.toUpperCase() || "" : ""
                return (
                  <div
                    key={f.id}
                    className="flex items-center gap-2 px-2.5 py-2 rounded-lg"
                    style={{ background: "rgba(255,255,255,0.06)", border: "1px solid rgba(255,255,255,0.08)" }}
                  >
                    {f.uploading ? (
                      <Loader2 size={14} className="animate-spin shrink-0" style={{ color: "var(--text-tertiary)" }} />
                    ) : (
                      <FileText size={14} style={{ color: "var(--text-tertiary)", flexShrink: 0 }} />
                    )}
                    <span
                      className="text-[12px] truncate"
                      style={{ color: "var(--text-secondary)", maxWidth: 180 }}
                    >
                      {f.filename}
                    </span>
                    {ext && (
                      <span
                        className="text-[9px] font-medium px-1.5 py-0.5 rounded shrink-0 ml-auto"
                        style={{ background: "rgba(255,255,255,0.08)", color: "var(--text-tertiary)" }}
                      >
                        {ext}
                      </span>
                    )}
                    {!f.uploading && (
                      <button onClick={() => removeFile(f.id)} className="hover:bg-white/10 rounded p-0.5 shrink-0">
                        <X size={10} style={{ color: "var(--text-tertiary)" }} />
                      </button>
                    )}
                  </div>
                )
              })}
            </div>
          )}

          {/* Text area */}
          <div className="px-4 pt-3 pb-1">
            <div className="relative min-w-0">
              {showPlaceholder && suggestion && (
                <div
                  className="absolute inset-0 flex items-center gap-2 pointer-events-none"
                  style={{ fontSize: "var(--font-size-input)", color: "var(--text-tertiary)" }}
                >
                  <span className="truncate">{suggestion}</span>
                  <span
                    className="shrink-0 px-1.5 py-0.5 rounded text-[10px] font-medium leading-none"
                    style={{
                      border: "1px solid rgba(255,255,255,0.15)",
                      color: "rgba(255,255,255,0.45)",
                      background: "rgba(255,255,255,0.03)",
                    }}
                  >
                    Tab
                  </span>
                </div>
              )}
              {showPlaceholder && !suggestion && (
                <div
                  className="absolute inset-0 pointer-events-none"
                  style={{ fontSize: "var(--font-size-input)", color: "var(--text-tertiary)" }}
                >
                  Type and press enter to start chatting...
                </div>
              )}
              <div
                ref={editorRef}
                contentEditable
                suppressContentEditableWarning
                onInput={handleInput}
                onKeyDown={handleKeyDown}
                onPaste={(e) => {
                  const files = e.clipboardData.files
                  if (files.length > 0 && !disabled) {
                    e.preventDefault()
                    handleFileSelect(files)
                    return
                  }
                  e.preventDefault()
                  const text = e.clipboardData.getData("text/plain")
                  document.execCommand("insertText", false, text)
                }}
                className="outline-none whitespace-pre-wrap break-words"
                style={{
                  fontSize: "var(--font-size-input)",
                  color: "var(--text-primary)",
                  maxHeight: "160px",
                  overflowY: "auto",
                  minHeight: "24px",
                }}
              />
            </div>
          </div>

          {/* Toolbar row — clip + auto on left, send on right */}
          <div className="flex items-center justify-between px-3 pb-3 pt-1">
            <div className="flex items-center gap-1.5">
              {/* Paperclip attach */}
              <button
                onClick={() => fileInputRef.current?.click()}
                disabled={disabled || attachedFiles.length >= MAX_FILES}
                className="p-2 rounded-full transition-colors shrink-0 hover:bg-white/10 disabled:opacity-30"
                title="Attach file (max 10 MB)"
              >
                <Paperclip size={18} style={{ color: "var(--text-tertiary)" }} />
              </button>
              <input
                ref={fileInputRef}
                type="file"
                multiple
                className="hidden"
                accept={Array.from(ALLOWED_EXTENSIONS).join(",")}
                onChange={e => { handleFileSelect(e.target.files); e.target.value = "" }}
              />
              {/* Auto mode toggle — neumorphic pill */}
              <button
                onClick={() => onAutoModeChange?.(!autoMode)}
                className="relative flex items-center gap-2 pl-3 pr-1.5 py-1.5 rounded-xl transition-all duration-200"
                style={{
                  background: autoMode
                    ? "linear-gradient(to bottom, #141414 30%, #2a1a3e 70%, #1a1030)"
                    : "linear-gradient(to bottom, #141414, #0c0c0c)",
                  boxShadow: autoMode
                    ? "0 0 0 0.9px rgba(255,255,255,0.15), inset 0 1px 0 rgba(255,255,255,0.06), 0 4px 12px rgba(120,80,200,0.15)"
                    : "0 0 0 0.9px rgba(255,255,255,0.12), inset 0 1px 0 rgba(255,255,255,0.04)",
                  transform: autoMode ? "translateY(-1px)" : "none",
                }}
                title={autoMode ? "Auto mode ON: actions execute without confirmation" : "Auto mode OFF: actions require confirmation"}
              >
                <span
                  className="text-[12px] font-semibold transition-colors duration-150"
                  style={{ color: autoMode ? "rgba(255,255,255,0.9)" : "rgba(255,255,255,0.25)" }}
                >
                  Auto
                </span>
                {/* Circle */}
                <span
                  className="relative flex items-center justify-center w-5 h-5 rounded-full transition duration-200"
                  style={{
                    background: "linear-gradient(to bottom, #131313, #1e1e1e)",
                    boxShadow: "0 0 0 0.9px rgba(255,255,255,0.15), 0 0 4px 1px rgba(255,255,255,0.06) inset",
                  }}
                >
                  <svg
                    width="8" height="8" viewBox="0 0 8 8" fill="none" xmlns="http://www.w3.org/2000/svg"
                    className="transition-opacity duration-150"
                    style={{ opacity: autoMode ? 1 : 0 }}
                  >
                    <path d="M1.04 4.68L2.94 6.79L6.95 1.72" stroke="rgba(255,255,255,0.8)" strokeWidth="1.69" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                </span>
              </button>
            </div>

            <div className="flex items-center gap-2">
              {disabled && onStop ? (
                <button
                  onClick={onStop}
                  className="w-8 h-8 rounded-full flex items-center justify-center transition-colors hover:bg-white/10"
                  style={{
                    background: "var(--surface-elevated)",
                    border: "1px solid var(--border)",
                  }}
                  title="Stop generating"
                >
                  <Square size={12} fill="var(--text-primary)" style={{ color: "var(--text-primary)" }} />
                </button>
              ) : (
                <button
                  onClick={handleSend}
                  disabled={!canSend}
                  className="w-8 h-8 rounded-full flex items-center justify-center transition-all shrink-0"
                  style={{
                    background: canSend ? "var(--text-primary)" : "var(--surface-elevated)",
                    color: canSend ? "var(--background)" : "var(--text-tertiary)",
                    cursor: canSend ? "pointer" : "default",
                    border: canSend ? "none" : "1px solid var(--border)",
                  }}
                >
                  <ArrowUp size={16} strokeWidth={2.5} />
                </button>
              )}
            </div>
          </div>

          {/* Drag overlay */}
          {isDragOver && (
            <div className="px-4 pb-3 text-[12px] text-center" style={{ color: "var(--accent)" }}>
              Drop files here (max 10 MB each, up to 5 files)
            </div>
          )}
        </div>

        {mentionOpen && (
          <MentionDropdown
            anchorRef={cardRef}
            query={mentionQuery}
            onSelect={handleMentionSelect}
            onClose={() => { setMentionOpen(false); setMentionQuery("") }}
            onClearQuery={handleClearQuery}
            onKeyDown={() => {}}
            connectedProviders={connectedProviders}
          />
        )}

        {slashOpen && (
          <SlashCommandDropdown
            anchorRef={cardRef}
            query={slashQuery}
            onSelect={handleSlashSelect}
            onClose={() => { setSlashOpen(false); setSlashQuery("") }}
          />
        )}

        {/* Connected integration icons — static row */}
        {connectedProviders.length > 0 && (() => {
          const allIcons = connectedProviders.flatMap(p =>
            p === "google"
              ? ["gmail", "google_calendar", "google_drive", "google_sheets", "google_docs", "google_slides"]
              : [p]
          )
          return (
            <div className="flex items-center justify-end gap-1.5 mt-2 px-1">
              {allIcons.map((p) => (
                <div
                  key={p}
                  className="w-6 h-6 rounded-md flex items-center justify-center"
                  style={{
                    background: "rgba(255,255,255,0.06)",
                    border: "1px solid rgba(255,255,255,0.08)",
                    opacity: 0.6,
                  }}
                  title={p}
                >
                  {getIntegrationIcon(p, 16)}
                </div>
              ))}
            </div>
          )
        })()}
      </div>
    </div>
  )
}
