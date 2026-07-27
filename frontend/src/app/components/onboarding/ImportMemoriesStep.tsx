"use client"

import { useState } from "react"
import { motion } from "framer-motion"
import { pingman } from "../../utils/pingman"
import { IMPORT_PROMPT } from "./constants"

export default function ImportMemoriesStep({ onComplete, onSkip }: { onComplete: () => void; onSkip: () => void }) {
  const [copied, setCopied] = useState(false)
  const [pastedMemory, setPastedMemory] = useState("")
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [importCount, setImportCount] = useState(0)

  const handleCopy = () => {
    navigator.clipboard.writeText(IMPORT_PROMPT)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const [importError, setImportError] = useState("")

  const handleImport = async () => {
    if (!pastedMemory.trim() || saving) return
    setSaving(true)
    setImportError("")
    try {
      const res = await pingman.post<{ imported: number }>("/api/gideon/v1/memories/import", {
        text: pastedMemory.trim(),
      })
      if (res.ok && typeof res.data?.imported === "number") {
        setImportCount(res.data.imported)
        setSaved(true)
        setTimeout(() => onComplete(), 1500)
      } else {
        setImportError("Could not parse any memories from the text. Try a different format.")
        setSaving(false)
      }
    } catch {
      setImportError("Something went wrong. Please try again.")
      setSaving(false)
    }
  }

  return (
    <div className="relative px-6 pt-12 pb-20 flex-1 flex items-start justify-center">
      <div className="max-w-2xl mx-auto w-full">
        <motion.div
          className="rounded-2xl p-8 md:p-10"
          style={{
            background: "linear-gradient(135deg, rgba(59,130,246,0.12) 0%, rgba(59,130,246,0.04) 100%)",
            border: "1px solid rgba(59,130,246,0.2)",
            backdropFilter: "blur(24px)",
            boxShadow: "0 0 60px rgba(59,130,246,0.08), inset 0 1px 0 rgba(255,255,255,0.05)",
          }}
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5 }}
        >
          <h2 className="text-2xl font-bold text-white mb-2">Import your memories</h2>
          <p className="text-sm text-white/50 mb-8">
            Bring context from ChatGPT or other AI assistants so Gideon knows you from day one.
          </p>

          {/* Step 1: Copy prompt */}
          <div className="mb-6">
            <div className="flex items-center gap-2 mb-3">
              <div className="w-6 h-6 rounded-full flex items-center justify-center text-xs font-bold" style={{ background: "rgba(59,130,246,0.3)", color: "#60a5fa" }}>1</div>
              <span className="text-sm font-medium text-white/80">Copy this prompt into a chat with your other AI</span>
            </div>
            <div
              className="relative rounded-xl p-4 text-sm leading-relaxed"
              style={{ background: "rgba(0,0,0,0.3)", border: "1px solid rgba(255,255,255,0.08)", color: "rgba(255,255,255,0.6)" }}
            >
              {IMPORT_PROMPT}
              <button
                onClick={handleCopy}
                className="absolute bottom-3 right-3 flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium transition-colors"
                style={{
                  background: copied ? "rgba(34,197,94,0.2)" : "rgba(255,255,255,0.1)",
                  color: copied ? "#22c55e" : "rgba(255,255,255,0.6)",
                  border: `1px solid ${copied ? "rgba(34,197,94,0.3)" : "rgba(255,255,255,0.1)"}`,
                }}
              >
                {copied ? "Copied!" : "Copy"}
              </button>
            </div>
          </div>

          {/* Step 2: Paste results */}
          <div className="mb-8">
            <div className="flex items-center gap-2 mb-3">
              <div className="w-6 h-6 rounded-full flex items-center justify-center text-xs font-bold" style={{ background: "rgba(59,130,246,0.3)", color: "#60a5fa" }}>2</div>
              <span className="text-sm font-medium text-white/80">Paste the results below</span>
            </div>
            <textarea
              value={pastedMemory}
              onChange={(e) => setPastedMemory(e.target.value)}
              placeholder="Paste your memory details here..."
              rows={6}
              className="w-full rounded-xl p-4 text-sm leading-relaxed resize-none outline-none transition-colors focus:ring-1 focus:ring-blue-500/30"
              style={{
                background: "rgba(0,0,0,0.3)",
                border: "1px solid rgba(255,255,255,0.08)",
                color: "rgba(255,255,255,0.8)",
              }}
            />
          </div>

          {/* Actions */}
          <div className="flex items-center justify-between">
            <button
              onClick={onSkip}
              className="px-4 py-2 rounded-lg text-sm transition-colors"
              style={{ color: "rgba(255,255,255,0.4)" }}
            >
              Skip
            </button>
            <button
              onClick={handleImport}
              disabled={!pastedMemory.trim() || saving || saved}
              className="px-6 py-2.5 rounded-xl text-sm font-semibold transition-all"
              style={{
                background: saved
                  ? "rgba(34,197,94,0.3)"
                  : pastedMemory.trim()
                    ? "rgba(59,130,246,0.8)"
                    : "rgba(255,255,255,0.1)",
                color: saved ? "#22c55e" : pastedMemory.trim() ? "#fff" : "rgba(255,255,255,0.3)",
                cursor: pastedMemory.trim() && !saving ? "pointer" : "default",
              }}
            >
              {saved ? `Imported ${importCount} memories!` : saving ? "Importing..." : "Add to memory"}
            </button>
          </div>
          {importError && (
            <p className="text-xs mt-3 text-center" style={{ color: "#ef4444" }}>{importError}</p>
          )}
        </motion.div>
      </div>
    </div>
  )
}
