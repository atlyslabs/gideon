"use client"

import { useState, useRef, useCallback } from "react"
import { pingman } from "../utils/pingman"
import type { MentionItem } from "../types"

const API_PATH = "/api/gideon/v1/mentions/search"

export default function useMentionSearch() {
  const [results, setResults] = useState<MentionItem[]>([])
  const [loading, setLoading] = useState(false)
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const search = useCallback((query: string, category: string) => {
    if (debounceRef.current) clearTimeout(debounceRef.current)

    if (!query && !["collaborator", "tool", "gmail", "google_calendar"].includes(category)) {
      setResults([])
      setLoading(false)
      return
    }

    setLoading(true)

    const delay = query ? 150 : 0  // instant for initial loads, 150ms for typing
    debounceRef.current = setTimeout(async () => {
      try {
        const res = await pingman.get<{ items: MentionItem[]; has_more: boolean }>(
          `${API_PATH}?query=${encodeURIComponent(query)}&category=${encodeURIComponent(category)}&limit=10`
        )

        if (res.ok) {
          setResults(res.data?.items || [])
        } else {
          setResults([])
        }
      } catch {
        // Failed
      } finally {
        setLoading(false)
      }
    }, delay)
  }, [])

  const clear = useCallback(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current)
    setResults([])
    setLoading(false)
  }, [])

  return { results, loading, search, clear }
}
