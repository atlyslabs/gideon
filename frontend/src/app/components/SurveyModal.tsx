"use client"

import { useCallback, useMemo, useState } from "react"
import { Loader2, X } from "lucide-react"
import type { Survey, SurveyQuestion } from "../types"

interface SurveyModalProps {
  survey: Survey
  onSubmit: (slug: string, answers: Record<string, unknown>) => Promise<void>
  // When true, render a close button so admins can dismiss the preview without
  // answering. End-user (in-app blocking) usage never sets this.
  previewMode?: boolean
  onClose?: () => void
}

function answerHasValue(value: unknown): boolean {
  if (value === null || value === undefined) return false
  if (typeof value === "string") return value.trim() !== ""
  if (Array.isArray(value)) return value.length > 0
  return true
}

export default function SurveyModal({ survey, onSubmit, previewMode, onClose }: SurveyModalProps) {
  const [answers, setAnswers] = useState<Record<string, unknown>>({})
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const setAnswer = useCallback((qid: string, value: unknown) => {
    setAnswers((prev) => ({ ...prev, [qid]: value }))
  }, [])

  const allRequiredAnswered = useMemo(
    () =>
      survey.questions.every(
        (q) => !q.required || answerHasValue(answers[q.id])
      ),
    [survey.questions, answers]
  )

  const handleSubmit = useCallback(async () => {
    if (!allRequiredAnswered) {
      setError("Please answer all required questions.")
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      await onSubmit(survey.slug, answers)
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to submit. Please try again.")
      setSubmitting(false)
    }
  }, [allRequiredAnswered, answers, onSubmit, survey.slug])

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center px-4 py-8"
      style={{ background: "rgba(0,0,0,0.85)", backdropFilter: "blur(8px)" }}
    >
      <div
        className="w-full max-w-[640px] max-h-[90vh] flex flex-col rounded-2xl overflow-hidden"
        style={{
          background: "var(--surface)",
          border: "1px solid rgba(255,255,255,0.1)",
          boxShadow: "0 20px 60px rgba(0,0,0,0.6)",
        }}
      >
        {/* Header. In normal mode there is no close button (surveys are
            unskippable). In previewMode (admin builder), show a close X so
            the admin can exit without filling required fields. */}
        <div
          className="flex items-start justify-between gap-3 px-6 py-5 shrink-0"
          style={{ borderBottom: "1px solid var(--border)" }}
        >
          <div className="flex-1 min-w-0">
            {previewMode && (
              <p className="text-[10px] uppercase tracking-wide mb-1" style={{ color: "var(--text-tertiary)" }}>
                Preview
              </p>
            )}
            <h2 className="text-[18px] font-semibold" style={{ color: "var(--text-primary)" }}>
              {survey.title}
            </h2>
            {survey.intro_text && (
              <p className="text-[13px] mt-1.5" style={{ color: "var(--text-secondary)" }}>
                {survey.intro_text}
              </p>
            )}
          </div>
          {previewMode && onClose && (
            <button
              onClick={onClose}
              aria-label="Close preview"
              className="shrink-0 p-1.5 rounded-lg transition-colors hover:bg-white/5"
              style={{ color: "var(--text-tertiary)" }}
              title="Close preview"
            >
              <X size={16} aria-hidden="true" />
            </button>
          )}
        </div>

        {/* Questions */}
        <div className="flex-1 overflow-y-auto px-6 py-5 space-y-6">
          {survey.questions.map((q, idx) => (
            <QuestionRow
              key={q.id}
              question={q}
              index={idx}
              value={answers[q.id]}
              onChange={(v) => setAnswer(q.id, v)}
            />
          ))}
        </div>

        {/* Footer */}
        <div
          className="flex items-center justify-between gap-3 px-6 py-4 shrink-0"
          style={{ borderTop: "1px solid var(--border)" }}
        >
          {error ? (
            <span className="text-[12px]" style={{ color: "#f87171" }}>
              {error}
            </span>
          ) : (
            <span className="text-[12px]" style={{ color: "var(--text-tertiary)" }}>
              Takes about a minute. Help us improve Gideon.
            </span>
          )}
          <button
            onClick={handleSubmit}
            disabled={!allRequiredAnswered || submitting}
            className="flex items-center gap-2 px-4 py-1.5 rounded-lg text-[13px] font-medium transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            style={{
              background: allRequiredAnswered ? "#5057EA" : "var(--surface-elevated)",
              color: allRequiredAnswered ? "#ffffff" : "var(--text-tertiary)",
            }}
          >
            {submitting && <Loader2 size={13} className="animate-spin" />}
            Submit
          </button>
        </div>
      </div>
    </div>
  )
}


function QuestionRow({
  question,
  index,
  value,
  onChange,
}: {
  question: SurveyQuestion
  index: number
  value: unknown
  onChange: (v: unknown) => void
}) {
  const number = `${index + 1}.`

  return (
    <div>
      <p className="text-[14px] font-medium mb-2" style={{ color: "var(--text-primary)" }}>
        <span style={{ color: "var(--text-tertiary)", marginRight: 6 }}>{number}</span>
        {question.prompt}
        {question.required && (
          <span style={{ color: "#f87171", marginLeft: 4 }}>*</span>
        )}
      </p>

      {question.type === "likert_1_5" && (
        <div className="flex items-center gap-2 mt-2">
          {[1, 2, 3, 4, 5].map((n) => {
            const selected = value === n
            return (
              <button
                key={n}
                onClick={() => onChange(n)}
                className="w-10 h-10 rounded-lg text-[13px] font-medium transition-colors"
                style={{
                  background: selected ? "#5057EA" : "var(--surface-elevated)",
                  color: selected ? "#ffffff" : "var(--text-secondary)",
                  border: `1px solid ${selected ? "#5057EA" : "var(--border)"}`,
                }}
              >
                {n}
              </button>
            )
          })}
          <div className="flex-1 ml-2 flex items-center justify-between text-[11px]" style={{ color: "var(--text-tertiary)" }}>
            <span>Not at all</span>
            <span>A lot</span>
          </div>
        </div>
      )}

      {question.type === "text" && (
        <textarea
          value={(value as string) || ""}
          onChange={(e) => onChange(e.target.value)}
          placeholder={question.placeholder || "Type your answer..."}
          rows={3}
          className="w-full px-3 py-2 rounded-lg text-[13px] resize-none"
          style={{
            background: "var(--surface-elevated)",
            border: "1px solid var(--border)",
            color: "var(--text-primary)",
            outline: "none",
          }}
        />
      )}

      {question.type === "single_select" && (
        <div className="flex flex-col gap-1.5 mt-2">
          {(question.options || []).map((opt) => {
            const selected = value === opt
            return (
              <button
                key={opt}
                onClick={() => onChange(opt)}
                className="text-left px-3 py-2 rounded-lg text-[13px] transition-colors"
                style={{
                  background: selected ? "rgba(80,87,234,0.15)" : "var(--surface-elevated)",
                  color: selected ? "#a5a9f5" : "var(--text-secondary)",
                  border: `1px solid ${selected ? "#5057EA" : "var(--border)"}`,
                }}
              >
                {opt}
              </button>
            )
          })}
        </div>
      )}

      {question.type === "multi_select" && (
        <div className="flex flex-wrap gap-2 mt-2">
          {(question.options || []).map((opt) => {
            const arr = Array.isArray(value) ? (value as string[]) : []
            const selected = arr.includes(opt)
            return (
              <button
                key={opt}
                onClick={() => {
                  const next = selected ? arr.filter((x) => x !== opt) : [...arr, opt]
                  onChange(next)
                }}
                className="px-3 py-1.5 rounded-full text-[12px] transition-colors"
                style={{
                  background: selected ? "rgba(80,87,234,0.15)" : "var(--surface-elevated)",
                  color: selected ? "#a5a9f5" : "var(--text-secondary)",
                  border: `1px solid ${selected ? "#5057EA" : "var(--border)"}`,
                }}
              >
                {opt}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
