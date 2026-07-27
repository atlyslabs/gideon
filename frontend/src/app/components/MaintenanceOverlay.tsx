"use client"

import { useEffect, useState } from "react"
import dynamic from "next/dynamic"
import { pingman } from "../utils/pingman"

const GideonHead = dynamic(() => import("./GideonHead"), { ssr: false })

type MaintenancePayload = {
  maintenance: true
  message?: string
  eta?: string | null
}

const DEFAULT_MESSAGE = "Gideon is down for a quick migration. Back in a few minutes."
const POLL_INTERVAL_MS = 15_000

export default function MaintenanceOverlay() {
  const [payload, setPayload] = useState<MaintenancePayload | null>(null)
  const [dots, setDots] = useState(".")

  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent<MaintenancePayload>).detail
      setPayload(detail)
    }
    window.addEventListener("gideon-maintenance", handler)
    return () => window.removeEventListener("gideon-maintenance", handler)
  }, [])

  // Animated dots while we wait.
  useEffect(() => {
    if (!payload) return
    const interval = setInterval(() => {
      setDots((d) => (d.length >= 3 ? "." : d + "."))
    }, 500)
    return () => clearInterval(interval)
  }, [payload])

  // Poll a lightweight endpoint; when it stops returning 503, reload.
  // Self-rescheduling chain (setTimeout after each completion) instead of
  // setInterval so a slow response can't stack overlapping fetches.
  useEffect(() => {
    if (!payload) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null

    const runOnce = async () => {
      try {
        // /users/me returns 401 when unauthed, 200 when authed, 503 during
        // maintenance. Any non-503 status means we're back.
        const res = await pingman.get<unknown>("/api/gideon/v1/users/me")
        if (!cancelled && res.status !== 503) {
          window.location.reload()
          return
        }
      } catch {
        // still down — keep waiting
      }
      if (!cancelled) {
        timer = setTimeout(runOnce, POLL_INTERVAL_MS)
      }
    }

    timer = setTimeout(runOnce, POLL_INTERVAL_MS)
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [payload])

  if (!payload) return null

  const message = payload.message || DEFAULT_MESSAGE
  const eta = payload.eta
    ? new Date(payload.eta).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", hour12: true })
    : null

  return (
    <div
      className="fixed inset-0 z-[9999] flex flex-col items-center justify-center px-6"
      style={{
        background: "rgba(10, 10, 10, 0.96)",
        backdropFilter: "blur(20px)",
        WebkitBackdropFilter: "blur(20px)",
        animation: "maintenanceFadeIn 300ms ease-out",
      }}
    >
      <div className="opacity-80">
        <GideonHead size={240} />
      </div>
      <div
        className="mt-2 text-[11px] uppercase tracking-[0.25em] font-semibold"
        style={{ color: "#a78bfa" }}
      >
        Upgrading
      </div>
      <h1
        className="mt-3 text-[26px] font-semibold text-center"
        style={{ color: "rgba(255,255,255,0.95)" }}
      >
        Back in a flash{dots}
      </h1>
      <p
        className="mt-3 text-[14px] text-center max-w-[440px] leading-relaxed"
        style={{ color: "rgba(255,255,255,0.6)" }}
      >
        {message}
      </p>
      {eta && (
        <p className="mt-2 text-[12px]" style={{ color: "rgba(255,255,255,0.4)" }}>
          Estimated back by {eta}
        </p>
      )}
      <p className="mt-6 text-[11px]" style={{ color: "rgba(255,255,255,0.35)" }}>
        This page will reload automatically when Gideon is back.
      </p>

      <style jsx>{`
        @keyframes maintenanceFadeIn {
          from { opacity: 0; }
          to { opacity: 1; }
        }
      `}</style>
    </div>
  )
}
