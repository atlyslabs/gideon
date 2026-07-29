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

const AUTH_API_URL = process.env.NEXT_PUBLIC_AUTH_API_URL || ""
const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080"

/** Gideon's own profile row, shaped like the identity service's `/me` user. */
function fromGideonProfile(profile: any): User {
  const name: string = profile.name || ""
  const [first, ...rest] = name.split(" ")
  return {
    id: profile.id,
    first_name: first || null,
    last_name: rest.length ? rest.join(" ") : null,
    email: profile.email || "",
    username: (profile.email || "").split("@")[0],
    google_id: "",
    phone_number: "",
    created_at: profile.created_at || "",
    updated_at: profile.updated_at || "",
    roles: profile.roles || [],
    access: [],
    associated_teams: {},
    slack_id: null,
  }
}

export default function useUser(): UseUserReturn {
  const authToken = useTokenStore((state) => state.authToken)

  const { data, mutate, error, isLoading } = useSWR<CurrentUserResponse>(
    authToken ? "/me" : null,
    async (path: string) => {
      // No external identity service configured — the AUTH_MODE=single_user
      // default. Concatenating an empty base would make this a relative fetch
      // against the Next.js server, which has no /me route, so it 404s and the
      // app renders with no name or avatar. Gideon's own profile row is the
      // identity in that case.
      const url = AUTH_API_URL
        ? AUTH_API_URL + path
        : `${API_BASE}/api/gideon/v1/users/me`

      const response = await fetch(url, {
        headers: { Authorization: `Bearer ${authToken}` },
      })

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`)
      }

      const body = await response.json()
      return AUTH_API_URL ? body : { user: fromGideonProfile(body) }
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
