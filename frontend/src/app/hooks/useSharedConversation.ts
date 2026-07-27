"use client"

import useSWR from "swr"
import { pingman } from "../utils/pingman"
import type { SharedConversation } from "../types"

const API_BASE = "/api/gideon/v1/chat"

export default function useSharedConversation(conversationId: string) {
  const { data, error, isLoading } = useSWR<SharedConversation>(
    `${API_BASE}/shared/${conversationId}`,
    async (url: string) => {
      const res = await pingman.get<SharedConversation>(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.data
    },
    { revalidateOnFocus: false }
  )

  return {
    conversation: data?.conversation ?? null,
    messages: data?.messages ?? [],
    access: data?.access ?? null,
    loading: isLoading,
    error,
  }
}
