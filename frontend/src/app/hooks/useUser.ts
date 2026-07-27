"use client"

import useSWR from "swr"
import { useTokenStore } from "./useTokenStore"

export interface User {
  id: string
  first_name: string | null
  last_name: string | null
  email: string
  username: string
  google_id: string
  phone_number: string
  created_at: string
  updated_at: string
  roles: string[]
  access: string[]
  associated_teams: Record<string, any>
  slack_id: string | null
}

interface UseUserReturn {
  loading: boolean
  loggedIn: boolean
  user?: User
  mutate: () => void
  error?: any
}

interface CurrentUserResponse {
  user: User
}

export default function useUser(): UseUserReturn {
  const authToken = useTokenStore((state) => state.authToken)

  const { data, mutate, error, isLoading } = useSWR<CurrentUserResponse>(
    authToken ? "/me" : null,
    async (path: string) => {
      const authApiUrl =
        process.env.NEXT_PUBLIC_AUTH_API_URL || ""

      const response = await fetch(authApiUrl + path, {
        headers: { Authorization: `Bearer ${authToken}` },
      })

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`)
      }

      return response.json()
    }
  )

  const loggedOut = !authToken || (error && error.message.includes("401"))

  return {
    loading: isLoading,
    loggedIn: !loggedOut,
    user: data?.user,
    mutate,
    error,
  }
}
