"use client"

import useSWR from "swr"
import { pingman } from "../utils/pingman"

interface MentionItem {
  id: string
  avatar_url?: string
}

/**
 * Shared map of (lowercased) email/id -> avatar_url, sourced from the collaborator mention search.
 * SWR-keyed so every caller on the page dedupes into one network request.
 */
export function useAvatarMap(): Record<string, string> {
  const { data } = useSWR<Record<string, string>>(
    "/api/gideon/v1/mentions/search?query=&category=collaborator&limit=500",
    async (url: string) => {
      const res = await pingman.get<{ items?: MentionItem[] }>(url)
      if (!res.ok) return {}
      const map: Record<string, string> = {}
      for (const m of res.data?.items || []) {
        if (m.avatar_url && m.id) map[m.id.toLowerCase()] = m.avatar_url
      }
      return map
    },
    { revalidateOnFocus: false },
  )

  return data || {}
}
