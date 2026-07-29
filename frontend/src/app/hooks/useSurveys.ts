"use client"

import useSWR from "swr"
import { useCallback, useState } from "react"
import { pingman } from "../utils/pingman"
import type {
  OrgBreakdown,
  SurveyAdmin,
  SurveyAnalysis,
  SurveyListItem,
  SurveyResponseRow,
} from "../types"

const ADMIN_BASE = "/api/gideon/v1/surveys/admin"


// ---------------------------------------------------------------------------
// List
// ---------------------------------------------------------------------------

export function useSurveysList() {
  const { data, error, isLoading, mutate } = useSWR<{ surveys: SurveyListItem[] }>(
    `${ADMIN_BASE}/list`,
    async (url: string) => {
      const res = await pingman.get<{ surveys: SurveyListItem[] }>(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.data
    },
    { revalidateOnFocus: true }
  )

  const create = useCallback(
    async (payload: { slug: string; title: string; intro_text?: string }) => {
      const res = await pingman.post(`${ADMIN_BASE}/create`, payload)
      if (!res.ok) {
        const detail = (res.data as { detail?: string } | undefined)?.detail
        throw new Error(detail || `HTTP ${res.status}`)
      }
      await mutate()
      return res.data as { id: string; slug: string }
    },
    [mutate]
  )

  return {
    surveys: data?.surveys || [],
    loading: isLoading,
    error,
    mutate,
    create,
  }
}


// ---------------------------------------------------------------------------
// Single survey (admin view)
// ---------------------------------------------------------------------------

export function useSurveyAdmin(slug: string | null, options?: { refreshInterval?: number }) {
  const { data, error, isLoading, mutate } = useSWR<SurveyAdmin>(
    slug ? `${ADMIN_BASE}/${slug}` : null,
    async (url: string) => {
      const res = await pingman.get<SurveyAdmin>(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.data
    },
    {
      revalidateOnFocus: true,
      refreshInterval: options?.refreshInterval,
    }
  )

  const update = useCallback(
    async (patch: Partial<Pick<SurveyAdmin, "title" | "intro_text" | "eligibility" | "questions">>) => {
      if (!slug) throw new Error("slug required")
      const res = await pingman.patch(`${ADMIN_BASE}/${slug}`, patch)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await mutate()
      return res.data as SurveyAdmin
    },
    [slug, mutate]
  )

  const setStatus = useCallback(
    async (status: "draft" | "active" | "closed") => {
      if (!slug) throw new Error("slug required")
      const res = await pingman.patch(`${ADMIN_BASE}/${slug}/status`, { status })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await mutate()
    },
    [slug, mutate]
  )

  const getOrCreateConversation = useCallback(async () => {
    if (!slug) throw new Error("slug required")
    const res = await pingman.post<{ conversation_id: string; user_id: string }>(
      `${ADMIN_BASE}/${slug}/conversation`,
      {}
    )
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    await mutate()
    return res.data
  }, [slug, mutate])

  return {
    survey: data,
    loading: isLoading,
    error,
    mutate,
    update,
    setStatus,
    getOrCreateConversation,
  }
}


// ---------------------------------------------------------------------------
// Responses + analysis
// ---------------------------------------------------------------------------

export function useSurveyResponses(slug: string | null) {
  const { data, error, isLoading, mutate } = useSWR<{
    survey: { slug: string; title: string; questions: unknown[] }
    responses: SurveyResponseRow[]
  }>(
    slug ? `${ADMIN_BASE}/${slug}/responses` : null,
    async (url: string) => {
      const res = await pingman.get<{
        survey: { slug: string; title: string; questions: unknown[] }
        responses: SurveyResponseRow[]
      }>(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.data
    },
    { revalidateOnFocus: true }
  )

  return {
    responses: data?.responses || [],
    loading: isLoading,
    error,
    mutate,
  }
}

export function useSurveyAnalysis(slug: string | null) {
  const { data, error, isLoading, mutate } = useSWR<{
    analysis: SurveyAnalysis
    generated_at: string | null
    cached: boolean
  }>(
    slug ? `${ADMIN_BASE}/${slug}/analysis` : null,
    async (url: string) => {
      const res = await pingman.get<{
        analysis: SurveyAnalysis
        generated_at: string | null
        cached: boolean
      }>(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.data
    },
    { revalidateOnFocus: false }
  )

  // SWR's `isLoading` only reflects the FIRST fetch; subsequent revalidations
  // (which `refresh()` triggers via `mutate()`) leave it false. The BE
  // analysis endpoint is synchronous (Sonnet + per-question stats, typically
  // 5-15s for a real response set), so we need our own in-flight flag to
  // show the user that the click registered and work is happening.
  const [refreshing, setRefreshing] = useState(false)

  const refresh = useCallback(async () => {
    if (!slug) throw new Error("slug required")
    setRefreshing(true)
    try {
      const res = await pingman.get(`${ADMIN_BASE}/${slug}/analysis?refresh=true`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await mutate()
    } finally {
      setRefreshing(false)
    }
  }, [slug, mutate])

  return {
    analysis: data?.analysis || null,
    generatedAt: data?.generated_at || null,
    loading: isLoading,
    refreshing,
    error,
    mutate,
    refresh,
  }
}


// ---------------------------------------------------------------------------
// Org breakdown
// ---------------------------------------------------------------------------

export function useOrgBreakdown() {
  const { data, error, isLoading } = useSWR<OrgBreakdown>(
    `${ADMIN_BASE}/org-breakdown`,
    async (url: string) => {
      const res = await pingman.get<OrgBreakdown>(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.data
    },
    { revalidateOnFocus: false }
  )
  return { breakdown: data, loading: isLoading, error }
}
