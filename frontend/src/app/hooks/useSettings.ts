"use client"

import useSWR from "swr"
import { useCallback } from "react"
import { pingman } from "../utils/pingman"

const API_PATH = "/api/gideon/v1/users/me/settings"

export interface UserSettings {
  name: string
  email: string
  timezone: string
  city: string | null
  role: string | null
  department: string | null
  preferred_communication: string
  background_info: string | null
  feature_action_items: boolean
  feature_email_drafts: boolean
  feature_meeting_prep: boolean
  feature_dev_console: boolean
}

interface PatchPayload {
  name?: string
  timezone?: string
  city?: string
  role?: string
  department?: string
  background_info?: string
  preferred_communication?: string
  feature_action_items?: boolean
  feature_email_drafts?: boolean
  feature_meeting_prep?: boolean
  feature_dev_console?: boolean
}

export default function useSettings() {
  const { data, error, isLoading, mutate } = useSWR<UserSettings>(
    API_PATH,
    async (url: string) => {
      const res = await pingman.get<UserSettings>(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.data
    }
  )

  const updateSettings = useCallback(
    async (payload: PatchPayload) => {
      const res = await pingman.patch<UserSettings>(API_PATH, payload)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await mutate(res.data, { revalidate: false })
      return res.data
    },
    [mutate]
  )

  return {
    settings: data,
    loading: isLoading,
    error,
    mutate,
    updateSettings,
  }
}
