"use client"

import useSWR from "swr"
import { useCallback } from "react"
import { pingman } from "../utils/pingman"
import type { Integration } from "../types"

const API_BASE = "/api/gideon/v1/integrations"

interface AuthURLResponse {
  auth_url: string
}

export default function useIntegrations() {
  const { data, error, isLoading, mutate } = useSWR<Integration[]>(
    API_BASE,
    async (url: string) => {
      const res = await pingman.get<Integration[]>(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.data
    }
  )

  const getAuthUrl = useCallback(async (provider: string): Promise<string> => {
    const res = await pingman.get<AuthURLResponse>(`${API_BASE}/${provider}/auth-url`)
    if (!res.ok) throw new Error(`Failed to get auth URL for ${provider}`)
    return res.data.auth_url
  }, [])

  const handleCallback = useCallback(
    async (provider: string, code: string) => {
      const res = await pingman.get(`${API_BASE}/${provider}/callback`, {
        params: { code },
      })
      if (!res.ok) throw new Error(`OAuth callback failed for ${provider}`)
      await mutate()
      return res.data
    },
    [mutate]
  )

  const disconnect = useCallback(
    async (integrationId: string) => {
      const res = await pingman.delete(`${API_BASE}/${integrationId}`)
      if (!res.ok) throw new Error("Failed to disconnect integration")
      await mutate()
    },
    [mutate]
  )

  const requestIntegration = useCallback(async (message: string) => {
    const res = await pingman.post(`${API_BASE}/request`, { message })
    if (!res.ok) throw new Error("Failed to submit integration request")
    return res.data
  }, [])

  return {
    integrations: data || [],
    loading: isLoading,
    error,
    mutate,
    getAuthUrl,
    handleCallback,
    disconnect,
    requestIntegration,
  }
}
