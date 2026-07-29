"use client"

import useSWR from "swr"
import { useCallback, useState, useRef, useEffect } from "react"
import { pingman } from "../utils/pingman"

const API_PATH = "/api/gideon/v1/memories"

export interface Memory {
  id: string
  content: string
  category: string
  source: string
  source_conversation_id: string | null
  is_active: boolean
  created_at: string | null
  updated_at: string | null
}

export interface MemoryListResponse {
  memories: Memory[]
  count: number
}

interface SearchResult {
  content: string
  score: number
  id: string | null
  category: string | null
  created_at: string | null
}

interface SearchResponse {
  results: SearchResult[]
  count: number
}

interface CreatePayload {
  content: string
  category?: string
}

interface UpdatePayload {
  content: string
}

export default function useMemories() {
  const { data, error, isLoading, mutate } = useSWR<MemoryListResponse>(
    API_PATH,
    async (url: string) => {
      const res = await pingman.get<MemoryListResponse>(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.data
    }
  )

  // Search state
  const [searchQuery, setSearchQuery] = useState("")
  const [searchResults, setSearchResults] = useState<SearchResult[] | null>(null)
  const [searching, setSearching] = useState(false)
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Debounced search
  const searchMemories = useCallback(
    (query: string) => {
      setSearchQuery(query)

      if (debounceRef.current) clearTimeout(debounceRef.current)

      if (!query.trim()) {
        setSearchResults(null)
        setSearching(false)
        return
      }

      setSearching(true)
      debounceRef.current = setTimeout(async () => {
        try {
          const res = await pingman.get<SearchResponse>(
            `${API_PATH}/search?q=${encodeURIComponent(query.trim())}`
          )
          if (res.ok) {
            setSearchResults(res.data.results)
          }
        } catch {
          // silent — fall back to showing all
          setSearchResults(null)
        } finally {
          setSearching(false)
        }
      }, 300)
    },
    []
  )

  // Clean up debounce on unmount
  useEffect(() => {
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current)
    }
  }, [])

  const createMemory = useCallback(
    async (payload: CreatePayload) => {
      const res = await pingman.post<Memory>(API_PATH, payload)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await mutate()
      return res.data
    },
    [mutate]
  )

  const updateMemory = useCallback(
    async (memoryId: string, payload: UpdatePayload) => {
      const res = await pingman.patch<Memory>(`${API_PATH}/${memoryId}`, payload)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await mutate()
      return res.data
    },
    [mutate]
  )

  const deleteMemory = useCallback(
    async (memoryId: string) => {
      // Optimistic removal from cache
      const previous = data
      mutate(
        (current) => {
          if (!current) return current
          return {
            ...current,
            memories: current.memories.filter((m) => m.id !== memoryId),
            count: current.count - 1,
          }
        },
        false // don't revalidate yet
      )

      try {
        const res = await pingman.delete(`${API_PATH}/${memoryId}`)
        if (!res.ok) {
          // Revert on failure
          mutate(previous, false)
          throw new Error(`HTTP ${res.status}`)
        }
        // Revalidate to sync
        await mutate()
      } catch (err) {
        mutate(previous, false)
        throw err
      }
    },
    [mutate, data]
  )

  return {
    memories: data?.memories ?? [],
    count: data?.count ?? 0,
    loading: isLoading,
    error,
    mutate,
    createMemory,
    updateMemory,
    deleteMemory,
    // Search
    searchQuery,
    searchMemories,
    searchResults,
    searching,
  }
}
