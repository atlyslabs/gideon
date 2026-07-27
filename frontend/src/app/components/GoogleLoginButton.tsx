"use client"

import { useGoogleLogin } from "@react-oauth/google"
import { useRouter } from "next/navigation"
import toast from "react-hot-toast"
import { useTokenStore } from "../hooks/useTokenStore"
import useUser from "../hooks/useUser"
import { getIntegrationIcon } from "../utils/integrationIcons"

export default function GoogleLoginButton() {
  const router = useRouter()
  const { mutate } = useUser()
  const setAuthToken = useTokenStore((store) => store.setAuthToken)
  const logOut = useTokenStore((store) => store.logOut)

  const login = useGoogleLogin({
    flow: "auth-code",
    onSuccess: async (tokenResponse) => {
      try {
        const authApiUrl =
          process.env.NEXT_PUBLIC_AUTH_API_URL || ""

        // Exchange Google auth code for JWT
        const response = await fetch(authApiUrl + "/auth/google", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ code: tokenResponse.code }),
        })

        if (response.ok) {
          const { token } = await response.json()
          setAuthToken(token)

          // Verify user
          const userResponse = await fetch(authApiUrl + "/me", {
            headers: { Authorization: `Bearer ${token}` },
          })

          if (userResponse.ok) {
            await mutate()
            toast.success("Welcome to Gideon")
          } else {
            logOut()
            toast.error("Failed to verify user permissions")
          }
        } else {
          toast.error("Failed to login")
        }
      } catch (error) {
        console.error("Login error:", error)
        toast.error("Failed to login")
      }
    },
    onError: () => {
      toast.error("Login failed")
    },
  })

  return (
    <button
      onClick={() => login()}
      className="w-full px-6 py-3.5 font-medium text-white rounded-xl transition-all duration-200 hover:scale-[1.02] active:scale-[0.98]"
      style={{
        background: "linear-gradient(135deg, #1e3a5f 0%, #0c1929 100%)",
        border: "1px solid rgba(59, 130, 246, 0.2)",
        boxShadow: "0 0 20px rgba(59, 130, 246, 0.1)",
      }}
    >
      <span className="flex items-center justify-center gap-3">
        {getIntegrationIcon("google_login", 20)}
        Sign in with Google
      </span>
    </button>
  )
}
