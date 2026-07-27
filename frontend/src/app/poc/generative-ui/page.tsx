"use client"

import { useState, useCallback } from "react"
import toast from "react-hot-toast"
import { Sparkles, Loader2 } from "lucide-react"
import { pingman } from "../../utils/pingman"
import { useTokenStore } from "../../hooks/useTokenStore"
import { Renderer, type Node } from "./Renderer"

const API_PATH = "/api/gideon/v1/poc/generative-ui"

const SAMPLE_PROMPTS = [
  "Show me flights from Mumbai to London on Dec 15",
  "Bangalore to Tokyo, 3 Jan, business class options",
  "Cheapest flights from Delhi to New York next Friday",
  "Non-stop flights Mumbai → Singapore tomorrow morning",
]

interface ApiResponse {
  tree: Node
  raw: string
}

export default function GenerativeUIPage() {
  const authToken = useTokenStore((s) => s.authToken)
  const [prompt, setPrompt] = useState(SAMPLE_PROMPTS[0])
  const [tree, setTree] = useState<Node | null>(null)
  const [raw, setRaw] = useState<string>("")
  const [loading, setLoading] = useState(false)
  const [showRaw, setShowRaw] = useState(false)

  const submit = useCallback(async () => {
    if (!prompt.trim() || loading) return
    if (!authToken) {
      toast.error("Sign in on the main page first, then come back.")
      return
    }
    setLoading(true)
    setTree(null)
    setRaw("")
    try {
      const res = await pingman.post<ApiResponse>(API_PATH, { prompt })
      if (!res.ok) {
        toast.error(`HTTP ${res.status}`)
        return
      }
      setTree(res.data.tree)
      setRaw(res.data.raw)
    } catch (e) {
      toast.error("Request failed")
      // eslint-disable-next-line no-console
      console.error(e)
    } finally {
      setLoading(false)
    }
  }, [prompt, loading, authToken])

  const handleAction = useCallback((id: string) => {
    toast(`Action: ${id}`, { icon: "🛫" })
  }, [])

  return (
    <div
      style={{
        minHeight: "100vh",
        background: "var(--surface)",
        color: "var(--text-primary)",
        padding: "32px 24px",
      }}
    >
      <div style={{ maxWidth: 920, margin: "0 auto" }}>
        {/* Header */}
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
          <Sparkles size={18} style={{ color: "#a78bfa" }} />
          <h1 style={{ fontSize: 22, fontWeight: 600, margin: 0 }}>Generative Flight Search</h1>
        </div>
        <p style={{ color: "var(--text-secondary)", fontSize: 13, marginTop: 0, marginBottom: 24 }}>
          Claude composes the page from a fixed component catalog. The frontend just renders.
        </p>

        {/* Input */}
        <div
          style={{
            background: "var(--surface-elevated)",
            border: "1px solid var(--border)",
            borderRadius: 12,
            padding: 12,
            marginBottom: 16,
          }}
        >
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => {
              if ((e.metaKey || e.ctrlKey) && e.key === "Enter") submit()
            }}
            placeholder="Describe the flight search you want…"
            rows={2}
            style={{
              width: "100%",
              background: "transparent",
              border: "none",
              outline: "none",
              color: "var(--text-primary)",
              fontSize: 14,
              fontFamily: "inherit",
              resize: "vertical",
            }}
          />
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, marginTop: 8 }}>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              {SAMPLE_PROMPTS.map((p) => (
                <button
                  key={p}
                  type="button"
                  onClick={() => setPrompt(p)}
                  style={{
                    background: "transparent",
                    color: "var(--text-tertiary)",
                    border: "1px solid var(--border)",
                    borderRadius: 999,
                    padding: "4px 10px",
                    fontSize: 11,
                    cursor: "pointer",
                  }}
                >
                  {p}
                </button>
              ))}
            </div>
            <button
              type="button"
              onClick={submit}
              disabled={loading || !prompt.trim()}
              style={{
                background: "#7c3aed",
                color: "#fff",
                border: "none",
                borderRadius: 8,
                padding: "8px 14px",
                fontSize: 13,
                fontWeight: 500,
                cursor: loading ? "not-allowed" : "pointer",
                opacity: loading || !prompt.trim() ? 0.5 : 1,
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                whiteSpace: "nowrap",
              }}
            >
              {loading ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} />}
              {loading ? "Composing…" : "Generate"}
            </button>
          </div>
        </div>

        {/* Render area */}
        {tree && (
          <div style={{ marginBottom: 16 }}>
            <Renderer node={tree} onAction={handleAction} />
          </div>
        )}

        {/* Raw toggle */}
        {raw && (
          <div style={{ marginTop: 24 }}>
            <button
              type="button"
              onClick={() => setShowRaw((s) => !s)}
              style={{
                background: "transparent",
                color: "var(--text-tertiary)",
                border: "1px solid var(--border)",
                borderRadius: 6,
                padding: "4px 10px",
                fontSize: 11,
                cursor: "pointer",
              }}
            >
              {showRaw ? "Hide" : "Show"} raw JSON tree
            </button>
            {showRaw && (
              <pre
                style={{
                  marginTop: 8,
                  padding: 12,
                  background: "var(--surface-elevated)",
                  border: "1px solid var(--border)",
                  borderRadius: 8,
                  fontSize: 11,
                  color: "var(--text-secondary)",
                  overflow: "auto",
                  maxHeight: 400,
                }}
              >
                {raw}
              </pre>
            )}
          </div>
        )}

        {!tree && !loading && (
          <div
            style={{
              padding: 32,
              textAlign: "center",
              color: "var(--text-tertiary)",
              border: "1px dashed var(--border)",
              borderRadius: 12,
              fontSize: 13,
            }}
          >
            Pick a sample prompt or type your own, then hit Generate.
          </div>
        )}
      </div>
    </div>
  )
}
