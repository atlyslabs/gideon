"use client"

import { useCallback } from "react"
import useSWR from "swr"
import { pingman } from "../utils/pingman"
import type { Conversation } from "../types"

const API_BASE = "/api/gideon/v1/chat"

// Stable fallback so consumers get a referentially-stable array while the
// SWR request is in flight (a fresh [] every render breaks memoized children).
const EMPTY_CONVERSATIONS: Conversation[] = []

export default function useConversations() {
  const { data, error, isLoading, mutate } = useSWR<Conversation[]>(
    `${API_BASE}/conversations`,
    async (url: string) => {
      const res = await pingman.get<Conversation[]>(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.data
    },
    {
      revalidateOnFocus: false,
      dedupingInterval: 5000,
    }
  )

  // All three actions are useCallback'd on the (stable) SWR mutate so their
  // identity survives re-renders — Layout feeds them into memoized children.
  const createConversation = useCallback(async (): Promise<Conversation> => {
    const res = await pingman.post<Conversation>(`${API_BASE}/conversations`)
    if (!res.ok) throw new Error("Failed to create conversation")
    await mutate()
    return res.data
  }, [mutate])

  const deleteConversation = useCallback(async (id: string) => {
    await pingman.delete(`${API_BASE}/conversations/${id}`)
    await mutate()
  }, [mutate])

  const togglePin = useCallback(async (id: string, nextPinned: boolean) => {
    // Optimistic update — pinned bubbles to the top immediately, order preserved within groups.
    await mutate(
      (current) => {
        if (!current) return current
        const updated = current.map((c) => (c.id === id ? { ...c, is_pinned: nextPinned } : c))
        // Resort: pinned first (by updated_at DESC), then unpinned (by updated_at DESC).
        return [...updated].sort((a, b) => {
          if (a.is_pinned !== b.is_pinned) return a.is_pinned ? -1 : 1
          return new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()
        })
      },
      { revalidate: false },
    )
    try {
      const res = await pingman.patch(`${API_BASE}/conversations/${id}`, { is_pinned: nextPinned })
      if (!res.ok) {
        await mutate()
        throw new Error("Failed to update pin state")
      }
    } catch (err) {
      // Network / timeout path: pingman.patch can reject before we see res.ok.
      // Roll back to server truth and rethrow so the caller sees the failure.
      await mutate()
      throw err
    }
  }, [mutate])

  return {
    conversations: data || EMPTY_CONVERSATIONS,
    loading: isLoading,
    error,
    mutate,
    createConversation,
    deleteConversation,
    togglePin,
  }
}
