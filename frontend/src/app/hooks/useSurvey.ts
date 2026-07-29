"use client"

import useSWR from "swr"
import { useCallback } from "react"
import { pingman } from "../utils/pingman"
import type { Survey } from "../types"

const PENDING_PATH = "/api/gideon/v1/surveys/pending"

interface PendingResponse {
  survey: Survey | null
}

export default function useSurvey() {
  const { data, error, isLoading, mutate } = useSWR<PendingResponse>(
    PENDING_PATH,
    async (url: string) => {
      const res = await pingman.get<PendingResponse>(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.data
    },
    { revalidateOnFocus: false }
  )

  const submit = useCallback(
    async (slug: string, answers: Record<string, unknown>) => {
      const res = await pingman.post(`/api/gideon/v1/surveys/${slug}/respond`, { answers })
      if (!res.ok) {
        const detail = (res.data as { detail?: string } | undefined)?.detail
        throw new Error(detail || `HTTP ${res.status}`)
      }
      await mutate({ survey: null }, { revalidate: false })
    },
    [mutate]
  )

  const dismiss = useCallback(
    async (slug: string) => {
      const res = await pingman.post(`/api/gideon/v1/surveys/${slug}/dismiss`, {})
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await mutate({ survey: null }, { revalidate: false })
    },
    [mutate]
  )

  return {
    survey: data?.survey || null,
    loading: isLoading,
    error,
    submit,
    dismiss,
  }
}
