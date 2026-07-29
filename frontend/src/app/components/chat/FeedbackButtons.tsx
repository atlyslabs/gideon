"use client"

import { useState, useCallback } from "react"
import { ThumbsUp, ThumbsDown, X } from "lucide-react"
import { pingman } from "../../utils/pingman"

// ---------------------------------------------------------------------------
// Feedback modal + buttons -- thumbs up/down on assistant messages
// ---------------------------------------------------------------------------

function FeedbackModal({ type, onSubmit, onClose }: {
  type: "up" | "down"
  onSubmit: (text: string) => void
  onClose: () => void
}) {
  const [text, setText] = useState("")
  const isPositive = type === "up"

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ background: "rgba(0,0,0,0.6)" }} onClick={onClose}>
      <div
        className="rounded-2xl p-6 w-full max-w-md mx-4"
        style={{ background: "var(--surface-elevated)", border: "1px solid var(--border)" }}
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-lg font-semibold mb-4" style={{ color: "var(--text-primary)" }}>
          Give {isPositive ? "positive" : "negative"} feedback
        </h3>
        <p className="text-[13px] mb-2" style={{ color: "var(--text-secondary)" }}>
          Please provide details: (optional)
        </p>
        <textarea
          autoFocus
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Escape") onClose() }}
          rows={3}
          placeholder={isPositive ? "What was satisfying about this response?" : "What went wrong with this response?"}
          className="w-full rounded-xl px-4 py-3 text-[13px] outline-none resize-none mb-4"
          style={{ background: "var(--surface)", border: "1px solid var(--border)", color: "var(--text-primary)" }}
        />
        <div className="flex justify-end gap-3">
          <button
            onClick={onClose}
            className="px-4 py-2 rounded-lg text-[13px] font-medium transition-colors hover:bg-white/10"
            style={{ color: "var(--text-secondary)", border: "1px solid var(--border)" }}
          >
            Cancel
          </button>
          <button
            onClick={() => onSubmit(text)}
            className="px-4 py-2 rounded-lg text-[13px] font-medium transition-colors"
            style={{ background: "var(--text-primary)", color: "var(--background)" }}
          >
            Submit
          </button>
        </div>
      </div>
    </div>
  )
}

export default function FeedbackButtons({ messageId }: { messageId: string }) {
  const [submitted, setSubmitted] = useState<"up" | "down" | null>(null)
  const [modalType, setModalType] = useState<"up" | "down" | null>(null)

  const submit = useCallback(async (vote: "up" | "down", text?: string) => {
    setSubmitted(vote)
    setModalType(null)
    try {
      await pingman.post(`/api/gideon/v1/chat/messages/${messageId}/feedback`, {
        feedback: vote,
        feedback_text: text || undefined,
      })
    } catch {}
  }, [messageId])

  if (submitted) {
    return (
      <span className="inline-flex items-center gap-1">
        <span className="p-1" style={{ color: submitted === "up" ? "var(--accent)" : "#ef4444" }}>
          {submitted === "up" ? <ThumbsUp size={13} /> : <ThumbsDown size={13} />}
        </span>
        <span className="text-[11px]" style={{ color: "var(--text-tertiary)" }}>Thanks!</span>
      </span>
    )
  }

  return (
    <>
      <span className="inline-flex items-center gap-0.5">
        <button onClick={() => setModalType("up")} className="p-1 rounded transition-colors hover:bg-white/10" style={{ color: "var(--text-tertiary)" }} title="Good response">
          <ThumbsUp size={13} />
        </button>
        <button onClick={() => setModalType("down")} className="p-1 rounded transition-colors hover:bg-white/10" style={{ color: "var(--text-tertiary)" }} title="Bad response">
          <ThumbsDown size={13} />
        </button>
      </span>
      {modalType && (
        <FeedbackModal
          type={modalType}
          onSubmit={(text) => submit(modalType, text)}
          onClose={() => setModalType(null)}
        />
      )}
    </>
  )
}
