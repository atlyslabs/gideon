/**
 * Shared attachment reconstruction for chat history.
 *
 * `Row` is the FE's rendering shape; the BE persists a flat stream of
 * `Message` records (role: user | assistant | tool). Going from messages
 * to rows requires:
 *
 *   1. A tool_use_id → result map (so an assistant message's `tool_calls`
 *      can be paired with the matching `tool` message's result),
 *   2. A walker that converts each result-bearing tool call into the
 *      right Row attachment slot (dataTables / charts / drafts / previews
 *      / codeFiles).
 *
 * Both the live-chat history-load path (`useChatStream`) and the read-
 * only shared-chat path (`SharedChatView`) need this. Before this module
 * existed, the logic was copy-pasted in two places and drifted (the
 * shared view was already missing `code_artifact → codeFiles`). Keeping
 * the single implementation here.
 *
 * Streaming concerns (`activeTools`, partial frames, `pendingThinking`,
 * etc.) live in `useChatStream` — they are not history reconstruction.
 */

import type { Message, Row, ToolCall } from "../types"

/**
 * Walk all messages once and build `tool_use_id → result` so the
 * assistant-row pass can attach the right result to each tool call.
 */
export function buildToolResultMap(messages: Message[]): Map<string, unknown> {
  const map = new Map<string, unknown>()
  for (const msg of messages) {
    if (msg.role === "tool" && msg.tool_results?.tool_use_id) {
      map.set(msg.tool_results.tool_use_id, msg.tool_results.result)
    }
  }
  return map
}

type AttachmentBuckets = Pick<
  Row,
  "dataTables" | "charts" | "drafts" | "previews" | "codeFiles"
>

/**
 * Reduce a list of tool calls into the Row's attachment slots. Pass the
 * result map from `buildToolResultMap`. Tool calls with no result, an
 * `error` result, or a shape that doesn't match any handler are skipped
 * — they still appear as "done" indicator badges via `Row.toolCalls`,
 * but they don't produce inline attachment cards.
 */
export function buildAttachmentsFromCalls(
  // `ToolCall` covers normal tool_use blocks. `code_artifact` entries are
  // FE-only metadata appended by the streaming path (no Anthropic side,
  // no tool_use_id), so we accept the looser shape here.
  calls: (ToolCall | { type?: string; [k: string]: unknown })[],
  toolResultMap: Map<string, unknown>,
): AttachmentBuckets {
  const dataTables: NonNullable<Row["dataTables"]> = []
  const charts: NonNullable<Row["charts"]> = []
  const drafts: NonNullable<Row["drafts"]> = []
  const previews: NonNullable<Row["previews"]> = []
  const codeFiles: NonNullable<Row["codeFiles"]> = []

  for (const tc of calls) {
    // Sandbox-generated files uploaded to Drive — FE-only metadata
    // (no tool_use_id, no Anthropic-side meaning). Rebuild the chip.
    if ((tc as { type?: string }).type === "code_artifact" && (tc as { drive_url?: string }).drive_url) {
      const artifact = tc as {
        filename: string
        mime: string
        drive_url: string
        drive_file_id: string
        size_bytes: number
      }
      codeFiles.push({
        filename: artifact.filename,
        mime: artifact.mime,
        drive_url: artifact.drive_url,
        drive_file_id: artifact.drive_file_id,
        size_bytes: artifact.size_bytes,
      })
      continue
    }

    const call = tc as ToolCall
    if (!call?.id) continue
    const result = toolResultMap.get(call.id) as
      | { error?: unknown; [k: string]: unknown }
      | undefined
    if (!result || typeof result !== "object" || result.error) continue

    if (
      call.name === "query_analytics" &&
      (result as { columns?: unknown }).columns &&
      Array.isArray((result as { rows?: unknown[] }).rows)
    ) {
      const r = result as {
        columns: unknown
        rows: unknown[]
        row_count?: number
        sql?: string
      }
      dataTables.push({
        columns: r.columns as never,
        rows: r.rows as never,
        row_count: r.row_count ?? r.rows.length,
        display_limit: 100,
        sql: r.sql ?? "",
      })
    } else if (call.name === "query_grafana_panel" && (result as { query_results?: unknown }).query_results) {
      const r = result as {
        query_results: Record<string, { fields?: unknown[]; rows?: unknown[] }>
        panel_title?: string
        time_range?: unknown
        active_filters?: unknown
      }
      for (const ref of Object.keys(r.query_results)) {
        const frame = r.query_results[ref]
        if (frame?.fields && frame.fields.length >= 2 && frame.rows && frame.rows.length) {
          charts.push({
            title: r.panel_title ?? "",
            fields: frame.fields as never,
            rows: (frame.rows as unknown[]).slice(0, 500) as never,
            time_range: r.time_range as never,
            active_filters: (r.active_filters ?? {}) as never,
          })
          break // first frame only, matches streaming behavior
        }
      }
    } else if (
      (call.name === "draft_email" ||
        call.name === "reply_to_email" ||
        call.name === "update_draft") &&
      (result as { draft_id?: unknown }).draft_id
    ) {
      const r = result as {
        draft_id: string
        to?: string
        subject?: string
        body?: string
        cc?: string
        bcc?: string
      }
      // Dedupe by `draft_id` — `draft_email` followed by N×`update_draft`
      // for the same draft should render as a SINGLE card showing the
      // latest state, not N+1 stacked cards. Each tool result carries
      // the full draft state at that point, so the last write wins.
      // Matches the streaming path's draft-merge behavior in
      // useChatStream (`findIndex` + replace).
      const next = {
        draft_id: r.draft_id,
        to: r.to ?? "",
        subject: r.subject ?? "",
        body: r.body ?? "",
        cc: r.cc ?? "",
        bcc: r.bcc ?? "",
      }
      const existing = drafts.findIndex((d) => d.draft_id === r.draft_id)
      if (existing >= 0) {
        drafts[existing] = next
      } else {
        drafts.push(next)
      }
    } else if (
      call.name === "generate_preview" &&
      (result as { type?: string }).type === "preview" &&
      ((result as { framework?: string }).framework === "react" ||
        (result as { framework?: string }).framework === "html") &&
      typeof (result as { code?: unknown }).code === "string" &&
      ((result as { code: string }).code).trim()
    ) {
      const r = result as { framework: "react" | "html"; code: string; description?: string }
      previews.push({
        framework: r.framework,
        code: r.code,
        description: r.description ?? "",
      })
    }
  }

  return {
    dataTables: dataTables.length ? dataTables : undefined,
    charts: charts.length ? charts : undefined,
    drafts: drafts.length ? drafts : undefined,
    previews: previews.length ? previews : undefined,
    codeFiles: codeFiles.length ? codeFiles : undefined,
  }
}
