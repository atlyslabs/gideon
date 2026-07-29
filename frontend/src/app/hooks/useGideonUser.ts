"use client"

import useSWR from "swr"
import { useCallback } from "react"
import { pingman } from "../utils/pingman"

interface GideonUserProfile {
  id: string
  console_auth_id: string
  email: string
  name: string | null
  avatar_url: string | null
  persona_text: string | null
  timezone: string
  preferred_communication: string
  onboarding_completed: boolean
  roles: string[]
  is_survey_admin: boolean
  created_at: string
  updated_at: string
}

export default function useGideonUser() {
  const { data, error, isLoading, mutate } = useSWR<GideonUserProfile>(
    "/api/gideon/v1/users/me",
    async (url: string) => {
      const res = await pingman.get<GideonUserProfile>(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.data
    }
  )

  const completeOnboarding = useCallback(async () => {
    await pingman.patch("/api/gideon/v1/users/me/onboarding-complete")
    await mutate((prev) => prev ? { ...prev, onboarding_completed: true } : prev, false)
  }, [mutate])

  return {
    gideonUser: data,
    loading: isLoading,
    error,
    mutate,
    completeOnboarding,
  }
}
