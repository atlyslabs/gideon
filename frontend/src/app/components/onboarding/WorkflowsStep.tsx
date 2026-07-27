"use client"

import { useState, useEffect, useRef } from "react"
import { motion } from "framer-motion"
import { ArrowRight, Loader2, Zap, Workflow as WorkflowIcon } from "lucide-react"
import useWorkflows from "../../hooks/useWorkflows"
import { pingman } from "../../utils/pingman"
import { getIntegrationIcon } from "../../utils/integrationIcons"
import type { Workflow } from "../../types"

export default function WorkflowsStep({ onSelectWorkflow, onComplete }: { onSelectWorkflow: (wf: Workflow) => void; onComplete: () => void }) {
  const { workflows, loading, mutate } = useWorkflows()
  const [settling, setSettling] = useState(true)
  const pollCount = useRef(0)

  // Poll until smart onboarding finishes generating workflows
  useEffect(() => {
    const interval = setInterval(async () => {
      pollCount.current += 1
      mutate()

      // Check if smart onboarding is done
      try {
        const res = await pingman.get<{ onboarding_stage: string | null }>("/api/gideon/v1/users/me/settings")
        if (res.ok && res.data?.onboarding_stage === "done") {
          // One final mutate to catch any last-moment workflows
          await mutate()
          clearInterval(interval)
          setSettling(false)
          return
        }
      } catch {}

      // Safety timeout: stop after ~60s
      if (pollCount.current >= 20) {
        clearInterval(interval)
        setSettling(false)
      }
    }, 3000)
    return () => clearInterval(interval)
  }, [mutate])

  const isLoading = loading || settling

  return (
    <div className="relative px-6 pt-12 pb-20 flex-1 flex items-start justify-center">
      <div className="max-w-5xl mx-auto w-full">
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
          <div className="text-center mb-8">
            <h2 className="text-2xl font-bold text-white mb-2">
              Workflows we set up for you
            </h2>
            <p className="text-white/40 text-sm">
              These run automatically on schedule. Click any to review or edit.
            </p>
          </div>

          {isLoading ? (
            <div className="flex justify-center py-12">
              <Loader2 size={24} className="animate-spin text-blue-400" />
            </div>
          ) : workflows.length === 0 ? (
            <div className="text-center py-12">
              <WorkflowIcon size={32} className="mx-auto mb-3 text-white/20" />
              <p className="text-white/40 text-sm">No workflows created yet — you can set them up later.</p>
            </div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {workflows.map((wf, idx) => (
                <motion.button
                  key={wf.id}
                  onClick={() => onSelectWorkflow(wf)}
                  className="text-left rounded-xl p-4 transition-all"
                  style={{
                    background: "rgba(255,255,255,0.05)",
                    border: "1px solid rgba(255,255,255,0.08)",
                  }}
                  onMouseEnter={(e) => { e.currentTarget.style.borderColor = "rgba(59,130,246,0.3)"; e.currentTarget.style.background = "rgba(255,255,255,0.08)" }}
                  onMouseLeave={(e) => { e.currentTarget.style.borderColor = "rgba(255,255,255,0.08)"; e.currentTarget.style.background = "rgba(255,255,255,0.05)" }}
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: idx * 0.08 }}
                >
                  <div className="flex items-center gap-2 mb-2">
                    {wf.integrations_used.length > 0 ? (
                      wf.integrations_used.slice(0, 3).map((integ) => (
                        <span key={integ} className="shrink-0">{getIntegrationIcon(integ, 16)}</span>
                      ))
                    ) : (
                      <Zap size={14} className="text-blue-400" />
                    )}
                  </div>
                  <p className="text-sm font-medium text-white mb-1">{wf.name}</p>
                  <p className="text-xs text-white/40 line-clamp-2">{wf.description || wf.prompt?.slice(0, 100)}</p>
                </motion.button>
              ))}
            </div>
          )}

          <div className="mt-8 flex justify-end">
            <motion.button
              onClick={onComplete}
              className="inline-flex items-center gap-1.5 px-6 py-2.5 rounded-full text-sm font-medium bg-white/10 text-white hover:bg-white/20 border border-white/10 transition-all duration-150"
              whileHover={{ scale: 1.02 }}
              whileTap={{ scale: 0.98 }}
            >
              Let&apos;s go
              <ArrowRight size={14} />
            </motion.button>
          </div>
        </motion.div>
      </div>
    </div>
  )
}
