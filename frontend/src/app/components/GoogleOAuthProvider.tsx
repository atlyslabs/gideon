"use client"

import { GoogleOAuthProvider as GoogleProvider } from "@react-oauth/google"
import { useEffect, useState } from "react"

export default function GoogleOAuthProvider({ children }: { children: React.ReactNode }) {
  const [mounted, setMounted] = useState(false)
  const googleClientId = process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID || ""

  useEffect(() => {
    setMounted(true)
    if (!googleClientId) {
      console.error("NEXT_PUBLIC_GOOGLE_CLIENT_ID environment variable is required but not set")
    }
  }, [googleClientId])

  if (!mounted || !googleClientId) {
    return <>{children}</>
  }

  return (
    <GoogleProvider clientId={googleClientId}>
      {children}
    </GoogleProvider>
  )
}
