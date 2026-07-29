"use client"

import { useMemo } from "react"
import useSWR from "swr"
import type { HubMeeting } from "../types"
import { pingman } from "../utils/pingman"

/**
 * Shared hook for the "next upcoming meeting" surface (chat banner).
 * Uses the same SWR key as the hub's meetings fetch, so both dedupe into one request.
 */
export function useNextMeeting(): HubMeeting | null {
  const { data } = useSWR<{ items?: HubMeeting[] }>(
    "/api/gideon/v1/hub/meetings",
    async (url: string) => {
      const res = await pingman.get<{ items?: HubMeeting[] }>(url)
      if (!res.ok) return { items: [] }
      return res.data
    },
    { refreshInterval: 5 * 60_000 },
  )

  return useMemo(() => {
    const items = data?.items
    if (!Array.isArray(items) || items.length === 0) return null
    const now = Date.now()
    const upcoming = items
      .filter((m) => new Date(m.end_time).getTime() > now)
      .sort((a, b) => new Date(a.start_time).getTime() - new Date(b.start_time).getTime())
    return upcoming[0] || null
  }, [data])
}
