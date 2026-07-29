"use client"

import { useEffect, useRef, useState } from "react"
import { pingman } from "../utils/pingman"

/**
 * Predicted-next-reply ghost text shown in the chat input after Gideon
 * finishes responding. The user can press Tab to accept.
 *
 * Triggers a single fetch each time `isStreaming` transitions from true → false
 * for the active conversation. Bails out (and clears) when the conversation
 * changes or when the user has typed anything.
 */
export default function useSuggestedReply(
  conversationId: string | null,
  isStreaming: boolean,
) {
  const [suggestion, setSuggestion] = useState<string | null>(null)
  const wasStreamingRef = useRef(false)
  const fetchedForRef = useRef<string | null>(null)

  // Clear when conversation switches. Also reset `wasStreamingRef` — otherwise
  // switching away from a conversation that was actively streaming carries
  // `wasStreamingRef.current = true` into the new conversation, and the next
  // effect tick reads `justFinished = true` against the newly-selected conv
  // (which never streamed in this hook instance), firing a bogus fetch.
  useEffect(() => {
    setSuggestion(null)
    fetchedForRef.current = null
    wasStreamingRef.current = isStreaming
    // Intentionally only re-run on conversationId change. Including isStreaming
    // here would reset suggestion mid-stream.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId])

  useEffect(() => {
    const justFinished = wasStreamingRef.current && !isStreaming
    wasStreamingRef.current = isStreaming

    if (!justFinished || !conversationId) return
    // De-dupe: the streaming flag can flicker on re-renders.
    const key = `${conversationId}:${Date.now()}`
    fetchedForRef.current = key

    let cancelled = false
    ;(async () => {
      // Small delay so the final assistant message is committed server-side
      // and the GET sees a stable trailing pair.
      await new Promise((r) => setTimeout(r, 400))
      if (cancelled) return
      try {
        const res = await pingman.get<{ suggestion: string | null }>(
          `/api/gideon/v1/chat/conversations/${conversationId}/suggested-reply`,
        )
        if (cancelled) return
        if (fetchedForRef.current !== key) return // user kicked off another turn already
        if (res.ok && res.data?.suggestion) {
          setSuggestion(res.data.suggestion)
        }
      } catch {
        /* swallow — ghost text is purely advisory */
      }
    })()

    return () => {
      cancelled = true
    }
  }, [isStreaming, conversationId])

  return { suggestion, clearSuggestion: () => setSuggestion(null) }
}
