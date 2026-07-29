"use client"

import { Fragment, useState, useRef, useEffect, useMemo } from "react"
import { ChevronRight, Loader2 } from "lucide-react"
import ReactMarkdown from "react-markdown"
import type { ContentBlock } from "../../types"
import { mdPlugins, mdComponents } from "./markdownConfig"
import { renderStreamVariant, renderNamedVariant } from "./streamingVariants"
import { ScoreCards, RichTable, InsightCards } from "./InsightBlocks"

// Insight blocks (```scorecard / ```rich-table / ```insight-cards) render
// as real components only after the closing fence has streamed in. Until
// then we fall back to raw text in the streaming variant — JSON.parse on a
// half-streamed block would throw, and rendering a half-card would flicker.
// `splitForStreamingRender` finds CLOSED blocks within a streaming prefix
// and returns segments so the parent can render each completed block as its
// component while the still-open trailing fence (if any) stays as text.
const _CLOSED_INSIGHT_FENCE_RE = /```(scorecard|rich-table|insight-cards)\n([\s\S]*?)```/g

type StreamSegment =
  | { kind: "text"; text: string; start: number }
  | { kind: "scorecard"; data: any[]; start: number }
  | { kind: "rich-table"; data: { headers: any[]; rows: any[] }; start: number }
  | { kind: "insight-cards"; data: any[]; start: number }

function splitForStreamingRender(text: string): StreamSegment[] {
  if (!text || text.indexOf("```") < 0) return [{ kind: "text", text, start: 0 }]
  const segments: StreamSegment[] = []
  const re = new RegExp(_CLOSED_INSIGHT_FENCE_RE.source, "g")
  let lastIdx = 0
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) {
    const before = text.slice(lastIdx, m.index)
    if (before) segments.push({ kind: "text", text: before, start: lastIdx })
    const lang = m[1]
    const raw = m[2].trim()
    let parsed: any
    try {
      parsed = JSON.parse(raw)
    } catch {
      // Closed fence but malformed JSON — keep raw text so the user sees what came through.
      segments.push({ kind: "text", text: m[0], start: m.index })
      lastIdx = re.lastIndex
      continue
    }
    if (lang === "scorecard" && Array.isArray(parsed) && parsed.length > 0) {
      segments.push({ kind: "scorecard", data: parsed, start: m.index })
    } else if (
      lang === "rich-table"
      && parsed
      && Array.isArray(parsed.headers)
      && Array.isArray(parsed.rows)
    ) {
      segments.push({ kind: "rich-table", data: parsed, start: m.index })
    } else if (lang === "insight-cards" && Array.isArray(parsed) && parsed.length > 0) {
      segments.push({ kind: "insight-cards", data: parsed, start: m.index })
    } else {
      // Shape didn't validate — fall back to raw text so we never silently swallow content.
      segments.push({ kind: "text", text: m[0], start: m.index })
    }
    lastIdx = re.lastIndex
  }
  const tail = text.slice(lastIdx)
  if (tail) segments.push({ kind: "text", text: tail, start: lastIdx })
  return segments
}

// ---------------------------------------------------------------------------
// Structured content (thinking blocks, tool actions -- for agent mode)
// ---------------------------------------------------------------------------

export function parseContentBlocks(content: string | null): ContentBlock[] | null {
  if (!content) return null
  const t = content.trim()
  if (!t.startsWith("[") && !t.startsWith("{")) return null
  try {
    const p = JSON.parse(t)
    const arr = Array.isArray(p) ? p : [p]
    if (arr.length > 0 && arr[0].type && ["thinking", "tool_action", "text"].includes(arr[0].type)) return arr as ContentBlock[]
    return null
  } catch { return null }
}

function ThinkingBlock({ content, durationSeconds }: { content: string; durationSeconds?: number }) {
  const [open, setOpen] = useState(false)
  const innerRef = useRef<HTMLDivElement>(null)
  const [height, setHeight] = useState(0)

  useEffect(() => {
    if (open && innerRef.current) {
      setHeight(Math.min(innerRef.current.scrollHeight, 400))
    } else {
      setHeight(0)
    }
  }, [open, content])

  return (
    <div className="mb-2">
      <button onClick={() => setOpen(!open)} className="flex items-center gap-1.5 text-xs py-1 hover:opacity-80" style={{ color: "var(--text-secondary)" }}>
        <span style={{ fontSize: 8, color: "var(--text-tertiary)" }}>{"\u2022"}</span>
        <span>{durationSeconds ? `Thought for ${durationSeconds}s` : "Thought process"}</span>
        <ChevronRight size={12} style={{ transform: open ? "rotate(90deg)" : "rotate(0deg)", transition: "transform 200ms ease" }} />
      </button>
      <div
        style={{
          height,
          opacity: open ? 1 : 0,
          overflow: "hidden",
          transition: "height 250ms cubic-bezier(0.4, 0, 0.2, 1), opacity 200ms ease",
        }}
      >
        <div
          ref={innerRef}
          className="ml-3 pl-3 text-xs leading-relaxed whitespace-pre-wrap"
          style={{ color: "var(--text-tertiary)", fontStyle: "normal", borderLeft: "1px solid var(--border)", maxHeight: 400, overflowY: "auto" }}
        >
          {content}
        </div>
      </div>
    </div>
  )
}

/** Live thinking block -- shown while Claude is actively thinking (streaming) or collapsed after */
export function LiveThinkingBlock({ content, streaming, durationSeconds }: { content: string; streaming: boolean; durationSeconds?: number }) {
  const [open, setOpen] = useState(true)
  const contentRef = useRef<HTMLDivElement>(null)

  // Auto-scroll the thinking content to bottom while streaming
  useEffect(() => {
    if (streaming && contentRef.current) {
      contentRef.current.scrollTop = contentRef.current.scrollHeight
    }
  }, [content, streaming])

  // Auto-collapse when thinking finishes
  useEffect(() => {
    if (!streaming) setOpen(false)
  }, [streaming])

  return (
    <div className="mb-2">
      <button onClick={() => setOpen(!open)} className="flex items-center gap-1.5 text-xs py-1 hover:opacity-80" style={{ color: "var(--text-secondary)" }}>
        {streaming ? (
          <>
            <Loader2 size={12} className="animate-spin" style={{ color: "var(--accent)" }} />
            <span>Thinking...</span>
          </>
        ) : (
          <>
            <span style={{ fontSize: 8, color: "var(--text-tertiary)" }}>{"\u2022"}</span>
            <span>Thought for {durationSeconds ? `${durationSeconds}s` : "a few seconds"}</span>
            <ChevronRight size={12} style={{ transform: open ? "rotate(90deg)" : "rotate(0deg)", transition: "transform 200ms ease" }} />
          </>
        )}
      </button>
      <div
        style={{
          height: open && content ? "auto" : 0,
          maxHeight: open && content ? (streaming ? 200 : 400) : 0,
          opacity: open && content ? 1 : 0,
          overflow: "hidden",
          transition: streaming ? "none" : "max-height 250ms cubic-bezier(0.4, 0, 0.2, 1), opacity 200ms ease",
        }}
      >
        <div
          ref={contentRef}
          className={`ml-3 pl-3 text-xs leading-relaxed${streaming ? "" : " whitespace-pre-wrap"}`}
          style={{
            color: "var(--text-tertiary)",
            fontStyle: "normal",
            borderLeft: "1px solid var(--border)",
            maxHeight: streaming ? 200 : 400,
            overflowY: "auto",
          }}
        >
          {streaming
            ? renderNamedVariant("thinking-dots", { shown: content, fullText: content })
            : content}
        </div>
      </div>
    </div>
  )
}

export function StructuredContent({ blocks, agentStatus }: { blocks: ContentBlock[]; agentStatus?: string | null }) {
  return (
    <div>
      {agentStatus === "working" && (
        <div className="flex items-center gap-2 mb-3 text-sm" style={{ color: "var(--text-secondary)" }}>
          <Loader2 size={14} className="animate-spin" style={{ color: "var(--accent)" }} />
          <span>Working on it...</span>
        </div>
      )}
      {blocks.map((b, i) => {
        if (b.type === "thinking") return <ThinkingBlock key={i} content={b.content} durationSeconds={b.duration_seconds} />
        if (b.type === "tool_action") return (
          <div key={i} className="flex items-center gap-2 text-xs py-0.5" style={{ color: "var(--text-tertiary)" }}>
            <span>{"\u2699\uFE0F"}</span><span>{b.summary}</span>
          </div>
        )
        if (b.type === "text") return (
          <div key={i} className="leading-relaxed prose prose-invert prose-sm max-w-none" style={{ fontSize: "var(--font-size-content)", color: "var(--text-primary)" }}>
            <ReactMarkdown remarkPlugins={mdPlugins} components={mdComponents}>{b.content}</ReactMarkdown>
          </div>
        )
        return null
      })}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Typewriter -- reveals streaming text character-by-character at ~300 cps.
// When streaming ends, snaps to full content instantly (no flash).
// Only used for actively-streamed rows; DB-loaded messages render instantly.
// ---------------------------------------------------------------------------

export function TypewriterMarkdown({ fullText, streaming }: { fullText: string; streaming: boolean }) {
  const [displayLen, setDisplayLen] = useState(0)
  const targetRef = useRef(0)
  const rafRef = useRef(0)
  const lastTimeRef = useRef(0)

  targetRef.current = fullText.length

  // rAF loop -- runs only while streaming
  useEffect(() => {
    if (!streaming) return

    const step = (time: number) => {
      if (!lastTimeRef.current) lastTimeRef.current = time
      const dt = time - lastTimeRef.current
      lastTimeRef.current = time

      setDisplayLen((prev) => {
        const target = targetRef.current
        if (prev >= target) return prev
        const advance = Math.max(1, Math.ceil((dt / 1000) * 300))
        return Math.min(prev + advance, target)
      })

      rafRef.current = requestAnimationFrame(step)
    }

    lastTimeRef.current = 0
    rafRef.current = requestAnimationFrame(step)
    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current)
      rafRef.current = 0
    }
  }, [streaming])

  // Snap displayLen to word boundary -- never show partial words.
  let snapLen = displayLen
  if (streaming && snapLen < fullText.length && snapLen > 0) {
    while (snapLen < fullText.length && fullText[snapLen] !== " " && fullText[snapLen] !== "\n") {
      snapLen++
    }
  }

  const shown = streaming ? fullText.slice(0, snapLen) : fullText

  // Memoize the segmentation unconditionally (Rules of Hooks). It's only
  // consumed in the streaming branch below; cheap regex pass either way.
  const segments = useMemo(
    () => (streaming ? splitForStreamingRender(shown) : null),
    [shown, streaming],
  )

  // While streaming: split the visible prefix on CLOSED insight fences so
  // completed scorecard / rich-table / insight-cards blocks render as their
  // components mid-stream instead of as raw JSON. The still-open trailing
  // fence (if any) keeps streaming as text via the active variant.
  // On stream end: snap to ReactMarkdown so the final output is faithful.
  if (streaming && segments) {
    // Only the LAST text segment is the actively-streaming tail; earlier
    // text segments are settled (a closed insight fence sits after them).
    // Variants like `block-fade` need a `fullText` longer than `shown` to
    // know where the trailing edge / cursor lives, so for the tail segment
    // we pass the row's full buffered text starting at this segment's
    // offset. Settled segments pass equal `shown`/`fullText` so no cursor
    // shows up in the middle of a finished section.
    let lastTextIdx = -1
    for (let i = segments.length - 1; i >= 0; i--) {
      if (segments[i].kind === "text") { lastTextIdx = i; break }
    }
    return (
      <div className="leading-relaxed" style={{ fontSize: "var(--font-size-content)", color: "var(--text-primary)" }}>
        {segments.map((seg, i) => {
          if (seg.kind === "scorecard") return <ScoreCards key={`b-${i}`} cards={seg.data} />
          if (seg.kind === "rich-table") return <RichTable key={`b-${i}`} data={seg.data} />
          if (seg.kind === "insight-cards") return <InsightCards key={`b-${i}`} cards={seg.data} />
          const segFullText = i === lastTextIdx ? fullText.slice(seg.start) : seg.text
          return (
            <Fragment key={`t-${i}`}>
              {renderStreamVariant({ shown: seg.text, fullText: segFullText })}
            </Fragment>
          )
        })}
      </div>
    )
  }

  return (
    <div className="leading-relaxed prose prose-invert prose-sm max-w-none" style={{ fontSize: "var(--font-size-content)", color: "var(--text-primary)" }}>
      <ReactMarkdown remarkPlugins={mdPlugins} components={mdComponents}>{shown}</ReactMarkdown>
    </div>
  )
}

