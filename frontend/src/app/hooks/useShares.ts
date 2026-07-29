"use client"

import useSWR from "swr"
import { pingman } from "../utils/pingman"
import type { ShareSettings } from "../types"

const API_BASE = "/api/gideon/v1/chat"

export default function useShares(conversationId: string | null) {
  const { data, error, isLoading, mutate } = useSWR<ShareSettings>(
    conversationId ? `${API_BASE}/conversations/${conversationId}/share` : null,
    async (url: string) => {
      const res = await pingman.get<ShareSettings>(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.data
    },
    { revalidateOnFocus: false }
  )

  const updateShare = async (settings: Partial<ShareSettings>) => {
    if (!conversationId) return
    const res = await pingman.patch<ShareSettings>(
      `${API_BASE}/conversations/${conversationId}/share`,
      settings
    )
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    await mutate(res.data, false)
    return res.data
  }

  return {
    linkShared: data?.link_shared ?? false,
    sharedWithEmails: data?.shared_with_emails ?? [],
    shareUrl: data?.share_url ?? "",
    loading: isLoading,
    error,
    mutate,
    updateShare,
  }
}
