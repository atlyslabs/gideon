"use client"

import { useState } from "react"
import { motion } from "framer-motion"
import { ArrowRight, Loader2, Sparkles } from "lucide-react"
import { getIntegrationIcon } from "../../utils/integrationIcons"

interface DescribeTaskStepProps {
  connectedProviders: string[]
  onSubmit: (description: string) => Promise<void>
  onSkip: () => void
}

const EXAMPLES = [
  "Every morning, check my calendar and Slack me a summary of today's meetings",
  "At the end of each week, compile my Linear issues into a status update",
  "Check my Gmail every afternoon and draft replies to important emails",
]

export default function DescribeTaskStep({
  connectedProviders,
  onSubmit,
  onSkip,
}: DescribeTaskStepProps) {
  const [description, setDescription] = useState("")
  const [submitting, setSubmitting] = useState(false)

  const handleSubmit = async () => {
    if (!description.trim() || submitting) return
    setSubmitting(true)
    try {
      await onSubmit(description.trim())
    } catch {
      setSubmitting(false)
    }
  }

  return (
    <div className="relative px-6 pt-12 pb-20 flex-1 flex items-start justify-center">
      <div className="max-w-3xl mx-auto w-full">
        <motion.div
          className="rounded-2xl p-8 md:p-12"
          style={{
            background: "linear-gradient(135deg, rgba(59,130,246,0.12) 0%, rgba(59,130,246,0.04) 100%)",
            border: "1px solid rgba(59,130,246,0.2)",
            backdropFilter: "blur(24px)",
            boxShadow: "0 0 60px rgba(59,130,246,0.08), inset 0 1px 0 rgba(255,255,255,0.05)",
          }}
          initial={{ opacity: 0, y: 30 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5 }}
        >
          <div className="text-center mb-6">
            <div className="inline-flex items-center justify-center w-10 h-10 rounded-full mb-3" style={{ background: "rgba(59,130,246,0.15)" }}>
              <Sparkles size={20} className="text-blue-400" />
            </div>
            <h2 className="text-2xl font-bold text-white mb-2">
              What would you like to automate?
            </h2>
            <p className="text-white/40 text-sm max-w-md mx-auto">
              Describe a repetitive task and Gideon will create a workflow for it.
            </p>
          </div>

          {/* Connected tools */}
          {connectedProviders.length > 0 && (
            <div className="flex items-center justify-center gap-2 mb-6">
              <span className="text-[12px] text-white/30 mr-1">Your tools:</span>
              {connectedProviders.map((p) => (
                <span key={p} className="shrink-0">{getIntegrationIcon(p, 18)}</span>
              ))}
            </div>
          )}

          {/* Text area */}
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="e.g., Every morning, check my calendar and Slack me a summary of today's meetings"
            rows={3}
            className="w-full px-4 py-3 rounded-xl text-sm leading-relaxed resize-none transition-colors focus:outline-none focus:ring-1 focus:ring-blue-500/30"
            style={{
              background: "rgba(255,255,255,0.05)",
              border: "1px solid rgba(255,255,255,0.1)",
              color: "var(--text-primary)",
            }}
            disabled={submitting}
          />

          {/* Quick examples */}
          <div className="mt-3 flex flex-wrap gap-2">
            {EXAMPLES.map((ex) => (
              <button
                key={ex}
                onClick={() => setDescription(ex)}
                className="px-3 py-1.5 rounded-lg text-[11px] transition-colors"
                style={{
                  background: "rgba(255,255,255,0.04)",
                  border: "1px solid rgba(255,255,255,0.06)",
                  color: "rgba(255,255,255,0.35)",
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.borderColor = "rgba(59,130,246,0.3)"
                  e.currentTarget.style.color = "rgba(255,255,255,0.6)"
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.borderColor = "rgba(255,255,255,0.06)"
                  e.currentTarget.style.color = "rgba(255,255,255,0.35)"
                }}
              >
                {ex}
              </button>
            ))}
          </div>

          {/* Actions */}
          <div className="mt-8 flex items-center justify-between">
            <button
              onClick={onSkip}
              className="text-[13px] transition-colors"
              style={{ color: "rgba(255,255,255,0.3)" }}
              onMouseEnter={(e) => { e.currentTarget.style.color = "rgba(255,255,255,0.6)" }}
              onMouseLeave={(e) => { e.currentTarget.style.color = "rgba(255,255,255,0.3)" }}
            >
              Skip
            </button>
            <motion.button
              onClick={handleSubmit}
              disabled={!description.trim() || submitting}
              className="inline-flex items-center gap-1.5 px-6 py-2.5 rounded-full text-sm font-medium transition-all duration-150"
              style={{
                background: description.trim() && !submitting ? "rgba(255,255,255,0.1)" : "rgba(255,255,255,0.03)",
                color: description.trim() && !submitting ? "white" : "rgba(255,255,255,0.25)",
                border: "1px solid rgba(255,255,255,0.1)",
                cursor: description.trim() && !submitting ? "pointer" : "not-allowed",
              }}
              whileHover={description.trim() && !submitting ? { scale: 1.02 } : {}}
              whileTap={description.trim() && !submitting ? { scale: 0.98 } : {}}
            >
              {submitting ? (
                <>
                  <Loader2 size={14} className="animate-spin" />
                  Creating...
                </>
              ) : (
                <>
                  Create workflow
                  <ArrowRight size={14} />
                </>
              )}
            </motion.button>
          </div>
        </motion.div>
      </div>
    </div>
  )
}
