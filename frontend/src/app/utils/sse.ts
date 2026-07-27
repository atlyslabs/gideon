import type { SSEEvent } from "../types"

async function _readSse(
  response: Response,
  onEvent: (event: SSEEvent) => void,
  signal?: AbortSignal,
  onId?: (id: string) => void,
  onActivity?: () => void,
): Promise<void> {
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`)
  }
  const reader = response.body?.getReader()
  if (!reader) throw new Error("No response body")

  const decoder = new TextDecoder()
  let buffer = ""
  // Accumulator for the `id:` line of the current SSE record. A full SSE
  // "event" can span multiple lines before the terminating blank line;
  // `id:` may appear once per record. We surface it just before/after the
  // `data:` line is dispatched so callers can persist it as Last-Event-ID
  // for reconnection.
  let pendingId: string | null = null

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    // Any bytes (including `: keepalive` comments the server sends every ~15s
    // during long tool runs) count as liveness — lets a stale-stream watchdog
    // distinguish "slow but alive" from "connection dead".
    if (value && value.length) onActivity?.()

    buffer += decoder.decode(value, { stream: true })

    let boundary = buffer.indexOf("\n\n")
    while (boundary !== -1) {
      const chunk = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)

      pendingId = null
      for (const line of chunk.split("\n")) {
        if (line.startsWith("id:")) {
          pendingId = line.slice(3).trim()
        } else if (line.startsWith("data: ")) {
          const jsonStr = line.slice(6).trim()
          if (jsonStr === "[DONE]") return

          try {
            const event: SSEEvent = JSON.parse(jsonStr)
            onEvent(event)
          } catch {
            // Skip malformed JSON
          }
        }
      }
      if (pendingId && onId) onId(pendingId)

      boundary = buffer.indexOf("\n\n")
    }
  }
}

/**
 * Parse SSE stream from /chat/send.
 * Simple line-by-line parser — handles chunked data properly.
 */
export async function streamChat(
  url: string,
  body: { message: string; conversation_id?: string; mentions?: any[]; resume?: boolean; file_ids?: string[]; auto_mode?: boolean },
  token: string,
  onEvent: (event: SSEEvent) => void,
  onError: (error: Error) => void,
  signal?: AbortSignal,
  onActivity?: () => void,
) {
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(body),
      signal,
    })
    await _readSse(response, onEvent, signal, undefined, onActivity)
  } catch (error) {
    if (signal?.aborted) return
    onError(error as Error)
  }
}

/**
 * Attach to an existing background chat turn via SSE.
 * Returns `"not-found"` if the turn's Redis meta has expired — callers
 * should fall back to re-fetching /messages in that case. Callers can
 * pass `onId` to receive the latest Redis stream entry id after each SSE
 * record; this is the value to pass back as `lastEventId` on reconnect
 * so replay resumes from that point instead of from the start.
 */
export async function resumeChatStream(
  url: string,
  token: string,
  onEvent: (event: SSEEvent) => void,
  onError: (error: Error) => void,
  signal?: AbortSignal,
  lastEventId?: string,
  onId?: (id: string) => void,
  onActivity?: () => void,
): Promise<"ok" | "not-found"> {
  try {
    const headers: Record<string, string> = { Authorization: `Bearer ${token}` }
    if (lastEventId) headers["Last-Event-ID"] = lastEventId
    const response = await fetch(url, {
      method: "GET",
      headers,
      signal,
    })
    if (response.status === 404) return "not-found"
    await _readSse(response, onEvent, signal, onId, onActivity)
    return "ok"
  } catch (error) {
    if (signal?.aborted) return "ok"
    onError(error as Error)
    return "ok"
  }
}
