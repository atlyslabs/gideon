"use client"

import { useCallback, useEffect, useState } from "react"
import { Loader2, Check } from "lucide-react"
import { pingman } from "../../utils/pingman"
import { getIntegrationIcon } from "../../utils/integrationIcons"

interface LinkStatus {
  linked: boolean
  configured: boolean
  phone_masked?: string
}

type Step = "idle" | "code"

export default function WhatsAppLinkCard() {
  const [status, setStatus] = useState<LinkStatus | null>(null)
  const [step, setStep] = useState<Step>("idle")
  const [phone, setPhone] = useState("")
  const [code, setCode] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const loadStatus = useCallback(async () => {
    const res = await pingman.get<LinkStatus>("/api/gideon/v1/whatsapp/link")
    if (res.ok) setStatus(res.data)
  }, [])

  useEffect(() => {
    loadStatus()
  }, [loadStatus])

  // Hidden until the WhatsApp number is provisioned (config gate) — no point
  // showing a connect flow that can't send a code yet.
  if (!status || !status.configured) return null

  const sendCode = async () => {
    setBusy(true)
    setError(null)
    try {
      const res = await pingman.post("/api/gideon/v1/whatsapp/link/start", { phone })
      if (res.ok) setStep("code")
      else setError((res.data as { detail?: string })?.detail || "Could not send the code.")
    } finally {
      setBusy(false)
    }
  }

  const verify = async () => {
    setBusy(true)
    setError(null)
    try {
      const res = await pingman.post("/api/gideon/v1/whatsapp/link/verify", { phone, code })
      if (res.ok) {
        setStep("idle")
        setPhone("")
        setCode("")
        await loadStatus()
      } else {
        setError((res.data as { detail?: string })?.detail || "That code is invalid or expired.")
      }
    } finally {
      setBusy(false)
    }
  }

  const unlink = async () => {
    setBusy(true)
    try {
      await pingman.delete("/api/gideon/v1/whatsapp/link")
      await loadStatus()
    } finally {
      setBusy(false)
    }
  }

  const inputStyle: React.CSSProperties = {
    background: "var(--surface-elevated)",
    border: "1px solid var(--border)",
    color: "var(--text-primary)",
  }

  return (
    <div
      className="rounded-xl px-5 py-4"
      style={{ background: "var(--surface-card)", border: "1px solid var(--border)" }}
    >
      <div className="flex items-center gap-2 mb-1">
        {getIntegrationIcon("whatsapp", 18)}
        <p className="text-[14px] font-medium" style={{ color: "var(--text-primary)" }}>
          WhatsApp
        </p>
      </div>
      <p className="text-[12px] mb-3" style={{ color: "var(--text-tertiary)" }}>
        Chat with Gideon from WhatsApp. Link your number once, then message the Gideon number anytime.
      </p>

      {status.linked ? (
        <div className="flex flex-wrap items-center gap-3">
          <span
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[13px]"
            style={{ background: "rgba(37,211,102,0.12)", color: "#25D366" }}
          >
            <Check size={14} /> Connected · {status.phone_masked}
          </span>
          <button
            onClick={unlink}
            disabled={busy}
            className="px-3 py-1.5 rounded-lg text-[13px] font-medium transition-colors disabled:opacity-50"
            style={{ background: "var(--surface-elevated)", color: "var(--text-secondary)", border: "1px solid var(--border)" }}
          >
            {busy ? <Loader2 size={13} className="animate-spin" /> : "Unlink"}
          </button>
        </div>
      ) : step === "idle" ? (
        <div className="flex flex-wrap items-center gap-2">
          <input
            type="tel"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
            placeholder="+91 98765 43210"
            className="px-3 py-1.5 rounded-lg text-[13px] outline-none w-[180px]"
            style={inputStyle}
          />
          <button
            onClick={sendCode}
            disabled={busy || phone.trim().length < 8}
            className="px-4 py-1.5 rounded-lg text-[13px] font-medium transition-colors disabled:opacity-50"
            style={{ background: "#25D366", color: "#0b1f12" }}
          >
            {busy ? <Loader2 size={13} className="animate-spin" /> : "Send code"}
          </button>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <input
            type="text"
            inputMode="numeric"
            value={code}
            onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
            placeholder="6-digit code"
            className="px-3 py-1.5 rounded-lg text-[13px] outline-none w-[120px] tracking-widest"
            style={inputStyle}
          />
          <button
            onClick={verify}
            disabled={busy || code.length !== 6}
            className="px-4 py-1.5 rounded-lg text-[13px] font-medium transition-colors disabled:opacity-50"
            style={{ background: "#25D366", color: "#0b1f12" }}
          >
            {busy ? <Loader2 size={13} className="animate-spin" /> : "Verify"}
          </button>
          <button
            onClick={() => {
              setStep("idle")
              setCode("")
              setError(null)
            }}
            className="px-3 py-1.5 rounded-lg text-[13px]"
            style={{ color: "var(--text-tertiary)" }}
          >
            Change number
          </button>
        </div>
      )}

      {error && (
        <p className="text-[12px] mt-2" style={{ color: "#ef4444" }}>
          {error}
        </p>
      )}
    </div>
  )
}
