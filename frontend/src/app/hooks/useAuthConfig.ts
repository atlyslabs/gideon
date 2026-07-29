"use client"

import useSWR from "swr"

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080"

export type AuthMode = "single_user" | "oidc"

interface UseAuthConfigReturn {
  loading: boolean
  /** The backend's AUTH_MODE. Undefined until the first response lands. */
  mode?: AuthMode
  /** True when this instance has no login at all and every request is the owner. */
  singleUser: boolean
}

/**
 * Reads the backend's auth mode from the one unauthenticated endpoint.
 *
 * The frontend cannot infer this locally: in `single_user` mode there is no
 * identity provider, no login screen and no token to hold, so rendering a
 * sign-in button would strand the user on a screen with nothing behind it.
 * The backend owns AUTH_MODE, so it is the only honest source of truth —
 * duplicating it into a NEXT_PUBLIC_ variable would let the two disagree and
 * silently break the instance.
 */
export default function useAuthConfig(): UseAuthConfigReturn {
  const { data, isLoading } = useSWR<{ mode: AuthMode }>(
    `${API_BASE}/api/gideon/v1/auth-config`,
    async (url: string) => {
      const response = await fetch(url)
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      return response.json()
    },
    // The mode cannot change without a backend restart, so never refetch.
    { revalidateOnFocus: false, revalidateOnReconnect: false, shouldRetryOnError: false }
  )

  return {
    loading: isLoading,
    mode: data?.mode,
    singleUser: data?.mode === "single_user",
  }
}
