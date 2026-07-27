"use client"

import { memo, useEffect, useState } from "react"
import { FileText, Download } from "lucide-react"
import ReactMarkdown from "react-markdown"
import type { Row, MentionItem } from "../../types"
import ToolCallIndicator from "./ToolCallIndicator"
import GatheredInfo from "./GatheredInfo"
import StreamingSparkle from "./StreamingSparkle"
import EmailDraftCard from "./EmailDraftCard"
import DataTableCard from "./DataTableCard"
import WireframePreview from "./WireframePreview"
import ChartCard from "./ChartCard"
import { mdPlugins, mdComponents } from "./markdownConfig"
import { parseContentBlocks, StructuredContent, LiveThinkingBlock, TypewriterMarkdown } from "./ContentBlocks"

// Live "what it's doing right now" status for a streaming assistant turn.
//  - A1: when nothing has streamed yet (e.g. the model is building a large
//    tool-call argument, which produces no visible text), show a labeled
//    "Working…" instead of a bare spinner so the turn never looks idle.
//  - A2: after a threshold, add a calm reassurance line so a genuinely slow
//    turn reads as "still going," not "stuck" (which is what makes users
//    retype / send "?"). When thinking / a tool / partial text is already
//    visible, those convey the activity, so we only add the reassurance line.
// Seconds a streaming turn must run before the calm "still going" reassurance
// line appears (A2 above). Tune here rather than in the render logic.
const SLOW_REASSURANCE_THRESHOLD_SECONDS = 12
export function StreamingStatus({ hasSignal }: { hasSignal: boolean }) {
  const [elapsed, setElapsed] = useState(0)
  useEffect(() => {
    const t0 = Date.now()
    const id = setInterval(() => setElapsed(Math.floor((Date.now() - t0) / 1000)), 1000)
    return () => clearInterval(id)
  }, [])
  const slow = elapsed >= SLOW_REASSURANCE_THRESHOLD_SECONDS
  if (!hasSignal) {
    return (
      <div className="flex items-center gap-2 py-1">
        <StreamingSparkle size={30} />
        <span className="text-[13px]" style={{ color: "var(--text-tertiary)" }}>
          {slow ? "Still working, this one's taking a little longer…" : "Working…"}
        </span>
      </div>
    )
  }
  if (slow) {
    return (
      <div className="py-1 text-[13px]" style={{ color: "var(--text-tertiary)" }}>
        Still working, this one's taking a little longer…
      </div>
    )
  }
  return null
}

// ---------------------------------------------------------------------------
// MessageRow — one assistant row (thinking + tools + markdown + attachments).
//
// PERF-CRITICAL: this component is memoized so historical rows do NOT
// re-render (and re-run their ReactMarkdown parse) on every streaming tick
// (30-60fps) of the in-progress row.
//
// The stream store mutates Row objects IN PLACE (`row.content += chunk`), so
// passing the `row` object and comparing fields in a custom comparator would
// be broken — prevProps.row and nextProps.row are the SAME object, so field
// comparisons always look "equal" even after a mutation. Instead we FLATTEN
// the row into primitive/reference props at the parent's render time. React's
// default shallow compare then works correctly:
//  - string/number/bool fields (content, thinking, streaming, …) are
//    snapshotted per parent render, so a mutation shows up as a prop change;
//  - array fields (activeTools, drafts, dataTables, …) are always REASSIGNED
//    (new array identity) by the stream store on change, so reference
//    equality is a faithful change signal.
// Net effect: the live streaming row re-renders every frame (its content
// prop changes), completed/historical rows bail out.
// ---------------------------------------------------------------------------

interface MessageRowProps {
  id: string
  content: string
  /** undefined = DB/history row; true/false = stream-originated row */
  streaming?: boolean
  thinking?: string
  thinkingStreaming?: boolean
  thinkingDuration?: number
  toolCalls?: Row["toolCalls"]
  activeTools?: Row["activeTools"]
  drafts?: Row["drafts"]
  draftSent?: boolean
  codeImages?: Row["codeImages"]
  codeFiles?: Row["codeFiles"]
  charts?: Row["charts"]
  dataTables?: Row["dataTables"]
  previews?: Row["previews"]
  agentStatus?: Row["agentStatus"]
  onSend?: (message: string, mentions?: MentionItem[], options?: { fileIds?: string[]; files?: { filename: string; ext: string }[] }) => void
}

function MessageRow({
  content,
  streaming,
  thinking,
  thinkingStreaming,
  thinkingDuration,
  toolCalls,
  activeTools,
  drafts,
  draftSent,
  codeImages,
  codeFiles,
  charts,
  dataTables,
  previews,
  agentStatus,
  onSend,
}: MessageRowProps) {
  // Gathered information: thinking + tool calls grouped
  const hasThinking = !!(thinkingStreaming || thinking)
  const toolItems = [
    ...(toolCalls || []).filter((tc) => tc.id && tc.name && (tc as any).type !== "advisor_tool_result").map((tc) => ({ key: tc.id, name: tc.name, status: "done" as const, summary: undefined as string | undefined, reconnect: undefined as string | undefined })),
    ...(activeTools || []).filter((tc) => !(tc.draft && tc.status === "done")).map((tc, i) => ({ key: `active-${i}`, name: tc.name, status: tc.status, summary: tc.summary, reconnect: tc.reconnect })),
  ]
  const hasToolCalls = toolItems.length > 0

  return (
    <div>
      {(hasThinking || hasToolCalls) && (
        <GatheredInfo streaming={!!streaming} hasContent={!!content}>
          {/* Thinking */}
          {(thinkingStreaming || (streaming !== undefined && thinking)) && (
            <LiveThinkingBlock
              content={thinking || ""}
              streaming={!!thinkingStreaming}
              durationSeconds={thinkingDuration}
            />
          )}
          {streaming === undefined && thinking && (
            <LiveThinkingBlock content={thinking} streaming={false} durationSeconds={thinkingDuration} />
          )}
          {/* Tool calls */}
          {toolItems.map((tc) => (
            <ToolCallIndicator key={tc.key} toolName={tc.name} status={tc.status} summary={tc.summary} reconnect={tc.reconnect} />
          ))}
        </GatheredInfo>
      )}

      {/* Email draft cards (shown outside gathered info) */}
      {activeTools?.map((tc, i) =>
        tc.draft && tc.status === "done" ? (
          <EmailDraftCard key={`active-${i}`} draft={tc.draft} onSendViaChat={onSend} alreadySent={draftSent} />
        ) : null
      )}
      {!activeTools && drafts?.map((d, i) => (
        <EmailDraftCard key={`draft-${i}`} draft={d} onSendViaChat={onSend} alreadySent={draftSent} />
      ))}

      {/* Content */}
      {content ? (() => {
        const blocks = parseContentBlocks(content)
        if (blocks) return <StructuredContent blocks={blocks} agentStatus={agentStatus} />

        // Streaming rows -> typewriter
        if (streaming !== undefined) {
          return <TypewriterMarkdown fullText={content} streaming={!!streaming} />
        }
        // DB rows -> instant render
        return (
          <div className="leading-relaxed prose prose-invert prose-sm max-w-none" style={{ fontSize: "var(--font-size-content)", color: "var(--text-primary)" }}>
            <ReactMarkdown remarkPlugins={mdPlugins} components={mdComponents}>{content}</ReactMarkdown>
          </div>
        )
      })() : null}

      {/* Code execution images */}
      {codeImages?.map((img, i) => (
        <div key={`cimg-${i}`} className="my-3 rounded-xl overflow-hidden" style={{ border: "1px solid var(--border)" }}>
          <img
            src={`data:${img.mime};base64,${img.data}`}
            alt={img.filename || "Generated chart"}
            className="w-full max-w-[600px]"
            style={{ display: "block" }}
          />
        </div>
      ))}

      {/* Code execution files (xlsx / csv / pdf / docx / etc.) auto-uploaded to Drive */}
      {codeFiles?.map((cf, i) => (
        <a
          key={`cfile-${i}`}
          href={cf.drive_url}
          target="_blank"
          rel="noopener noreferrer"
          className="my-3 inline-flex items-center gap-3 px-4 py-3 rounded-xl transition-colors hover:bg-white/[0.04]"
          style={{ border: "1px solid var(--border)", maxWidth: 480, textDecoration: "none" }}
        >
          <FileText size={18} style={{ color: "var(--text-secondary)", flexShrink: 0 }} />
          <div className="flex-1 min-w-0">
            <div className="text-[13px] font-medium truncate" style={{ color: "var(--text-primary)" }}>
              {cf.filename}
            </div>
            <div className="text-[11px] mt-0.5" style={{ color: "var(--text-tertiary)" }}>
              {cf.size_bytes >= 1024 * 1024
                ? `${(cf.size_bytes / (1024 * 1024)).toFixed(1)} MB`
                : `${Math.max(1, Math.round(cf.size_bytes / 1024))} KB`}
              {" · Saved to Drive"}
            </div>
          </div>
          <Download size={14} style={{ color: "var(--text-tertiary)", flexShrink: 0 }} />
        </a>
      ))}

      {/* Charts */}
      {charts?.map((chart, i) => (
        <ChartCard key={`chart-${i}`} data={chart} />
      ))}

      {/* Data tables */}
      {dataTables?.map((dt, i) => (
        <DataTableCard key={`dt-${i}`} data={dt} />
      ))}

      {/* Wireframe / React previews — live compile via /api/preview/compile */}
      {previews?.map((p, i) => (
        <WireframePreview key={`prev-${i}`} payload={p} />
      ))}

      {/* Live status: "Working…" when nothing's streamed yet,
          plus a reassurance line once a turn runs long. */}
      {streaming && (
        <StreamingStatus
          hasSignal={!!(content || thinking || thinkingStreaming || activeTools?.length || hasToolCalls)}
        />
      )}
    </div>
  )
}

// Default shallow compare is intentional — see the perf note above.
export default memo(MessageRow)
