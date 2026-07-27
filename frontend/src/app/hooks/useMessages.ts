"use client"

import useSWR from "swr"
import { pingman } from "../utils/pingman"
import type { Message } from "../types"

const API_BASE = "/api/gideon/v1/chat"

export default function useMessages(conversationId: string | null) {
  const { data, error, isLoading, mutate } = useSWR<Message[]>(
    conversationId ? `${API_BASE}/conversations/${conversationId}/messages` : null,
    async (url: string) => {
      const res = await pingman.get<Message[]>(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.data
    },
    {
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
      dedupingInterval: 5000,
    }
  )

  return {
    messages: data || [],
    loading: isLoading,
    error,
    mutate,
  }
}
