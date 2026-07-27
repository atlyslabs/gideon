"use client"

import { useEffect, useRef, useState } from "react"
import { pingman } from "../utils/pingman"

const POLL_MS = 60_000
const VERSION_URL = "/api/gideon/v1/version"

/**
 * Polls the backend's /version endpoint every 60s. The backend captures the
 * pod's startup timestamp at import time, so every deploy changes the value.
 *
 * Returns `outdated=true` once the polled version differs from the first
 * response we saw. The caller renders an "update available" banner. The
 * baseline is captured on first poll (not on hook mount) so a temporary
 * /version outage at app start doesn't lock us into a wrong baseline.
 */
export default function useVersionCheck(): { outdated: boolean } {
  const [outdated, setOutdated] = useState(false)
  const baseline = useRef<string | null>(null)

  useEffect(() => {
    let cancelled = false

    async function tick() {
      try {
        const res = await pingman.get<{ version: string }>(VERSION_URL)
        if (!res.ok || cancelled) return
        const v = res.data?.version
        if (!v) return
        if (baseline.current === null) {
          baseline.current = v
          return
        }
        if (v !== baseline.current && !outdated) {
          setOutdated(true)
        }
      } catch {
        // Backend unreachable — skip this tick, don't churn UI.
      }
    }

    tick()  // immediate first poll to establish baseline
    const handle = window.setInterval(tick, POLL_MS)
    return () => {
      cancelled = true
      window.clearInterval(handle)
    }
  }, [outdated])

  return { outdated }
}
