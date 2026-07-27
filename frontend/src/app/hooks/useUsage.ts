"use client"

import useSWR from "swr"
import { pingman } from "../utils/pingman"

export interface DailyEntry {
  date: string
  group: string
  input_tokens: number
  output_tokens: number
  requests: number
}

export interface UsageBreakdown {
  // model or use_case or user fields
  model?: string
  use_case?: string
  name?: string
  email?: string
  user_id?: string
  input_tokens: number
  output_tokens: number
  requests: number
}

export interface UsageData {
  total_input_tokens: number
  total_output_tokens: number
  total_cache_read_tokens: number
  total_cache_creation_tokens: number
  total_web_searches: number
  total_requests: number
  by_use_case: UsageBreakdown[]
  by_model: UsageBreakdown[]
  by_user: UsageBreakdown[]
  by_day: DailyEntry[]
  year: number
  month: number
  group_by: string
}

export type GroupBy = "model" | "use_case" | "user"

export default function useUsage(year: number, month: number, groupBy: GroupBy = "model") {
  const {
    data,
    error,
    isLoading: loading,
    mutate,
  } = useSWR<UsageData>(
    `/api/gideon/v1/users/admin/usage?year=${year}&month=${month}&group_by=${groupBy}`,
    async (url: string) => {
      const res = await pingman.get<UsageData>(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.data
    },
    { revalidateOnFocus: false, refreshInterval: 60_000 }
  )

  return { data: data ?? null, loading, error, mutate }
}
