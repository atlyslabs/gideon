"use client"

import { useMemo } from "react"
import useSharedConversation from "../hooks/useSharedConversation"
import {
  buildAttachmentsFromCalls,
  buildToolResultMap,
} from "../utils/messageAttachments"
import ChatMessages from "./chat/ChatMessages"
import type { Message, Row, ToolCall } from "../types"

interface SharedChatViewProps {
  conversationId: string
}

/** Convert API messages to Row[] for ChatMessages.
 *
 * Reuses `buildToolResultMap` + `buildAttachmentsFromCalls` from
 * ../utils/messageAttachments so this read-only path stays in lockstep
 * with the live-chat history path in useChatStream.
 *
 * Note: shared chats are auth-gated (see the 403 handling below), so the
 * viewer's bearer token is available — WireframePreview's call to
 * /api/preview/compile will authenticate fine. */
function messagesToRows(messages: Message[]): Row[] {
  const toolResultMap = buildToolResultMap(messages)

  const rows: Row[] = []
  // Roll up tool_calls from intermediate tool-only assistant messages
  // onto the next visible assistant row. The accumulated `allCalls`
  // becomes BOTH the source for attachment cards AND the value of
  // `row.toolCalls` so the "tool done" indicator badges (rendered by
  // ChatMessages) reflect every tool that ran in the round, not just
  // the ones attached to the final assistant message.
  let pendingToolCalls: ToolCall[] = []
  for (const msg of messages) {
    if (msg.role === "tool") continue
    if (msg.role === "user") {
      rows.push({
        id: msg.id,
        role: "user",
        content: msg.content || "",
        files: msg.files ?? undefined,
      })
    } else if (msg.role === "assistant") {
      if (msg.tool_calls?.length && !msg.content?.trim()) {
        pendingToolCalls.push(...msg.tool_calls)
        continue
      }
      const allCalls: ToolCall[] = [...pendingToolCalls, ...(msg.tool_calls || [])]
      rows.push({
        id: msg.id,
        role: "assistant",
        content: msg.content || "",
        toolCalls: allCalls.length ? allCalls : undefined,
        ...buildAttachmentsFromCalls(allCalls, toolResultMap),
      })
      pendingToolCalls = []
    }
  }
  return rows
}

export default function SharedChatView({ conversationId }: SharedChatViewProps) {
  const { conversation, messages, loading, error } = useSharedConversation(conversationId)

  const rows = useMemo(() => messagesToRows(messages), [messages])

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center" style={{ background: "var(--background)" }}>
        <div className="spinner" />
      </div>
    )
  }

  if (error) {
    const is403 = error.message?.includes("403")
    return (
      <div className="min-h-screen flex items-center justify-center p-4" style={{ background: "var(--background)" }}>
        <div className="text-center max-w-sm">
          <div className="text-4xl mb-4">{is403 ? "🔒" : "😵"}</div>
          <h1 className="text-xl font-semibold mb-2" style={{ color: "var(--text-primary)" }}>
            {is403 ? "Access denied" : "Something went wrong"}
          </h1>
          <p className="text-sm" style={{ color: "var(--text-tertiary)" }}>
            {is403
              ? "You don't have permission to view this conversation."
              : "This conversation may have been deleted or is unavailable."}
          </p>
          <a
            href="/"
            className="inline-block mt-4 px-4 py-2 rounded-lg text-sm font-medium transition-colors"
            style={{
              background: "rgba(59,130,246,0.15)",
              color: "#60a5fa",
            }}
          >
            Open Gideon
          </a>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen flex flex-col" style={{ background: "var(--background)" }}>
      {/* Header */}
      <div
        className="shrink-0 flex items-center justify-between px-5 py-3"
        style={{ borderBottom: "1px solid var(--border)" }}
      >
        <div className="flex items-center gap-3 min-w-0">
          <a href="/" className="shrink-0">
            <img src="/logo.png" alt="Gideon" className="w-7 h-7 rounded-full" />
          </a>
          <div className="min-w-0">
            <h1 className="text-sm font-medium truncate" style={{ color: "var(--text-primary)" }}>
              {conversation?.title || "Untitled conversation"}
            </h1>
            <p className="text-xs" style={{ color: "var(--text-tertiary)" }}>
              Shared by {conversation?.owner_name || "Unknown"}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span
            className="text-[10px] font-medium uppercase tracking-wider px-2 py-0.5 rounded-full"
            style={{
              background: "rgba(255,255,255,0.06)",
              color: "var(--text-tertiary)",
              border: "1px solid var(--border)",
            }}
          >
            Read only
          </span>
          <a
            href="/"
            className="px-3 py-1.5 rounded-lg text-xs font-medium transition-colors hover:bg-white/10"
            style={{ color: "var(--text-secondary)" }}
          >
            Open Gideon
          </a>
        </div>
      </div>

      {/* Messages */}
      <div className="flex-1 flex flex-col min-h-0">
        {rows.length === 0 ? (
          <div className="flex-1 flex items-center justify-center">
            <p className="text-sm" style={{ color: "var(--text-tertiary)" }}>
              This conversation has no messages.
            </p>
          </div>
        ) : (
          <ChatMessages
            rows={rows}
            isStreaming={false}
            readOnly
          />
        )}
      </div>

      {/* Footer */}
      <div
        className="shrink-0 px-5 py-3 text-center"
        style={{ borderTop: "1px solid var(--border)" }}
      >
        <p className="text-xs" style={{ color: "var(--text-tertiary)" }}>
          This is a shared read-only view &middot;{" "}
          <a href="/" className="underline hover:no-underline" style={{ color: "var(--text-secondary)" }}>
            Open Gideon
          </a>
        </p>
      </div>
    </div>
  )
}
