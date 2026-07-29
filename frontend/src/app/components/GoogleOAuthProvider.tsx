"use client"

import { GoogleOAuthProvider as GoogleProvider } from "@react-oauth/google"
import { useEffect, useState } from "react"

export default function GoogleOAuthProvider({ children }: { children: React.ReactNode }) {
  const [mounted, setMounted] = useState(false)
  const googleClientId = process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID || ""

  // No client id is the normal case for AUTH_MODE=single_user, where there is
  // no Google sign-in at all — so this is not an error condition and must not
  // be logged as one. The login screen explains it when oidc mode actually
  // needs it.
  useEffect(() => {
    setMounted(true)
  }, [])

  if (!mounted || !googleClientId) {
    return <>{children}</>
  }

  return (
    <GoogleProvider clientId={googleClientId}>
      {children}
    </GoogleProvider>
  )
}
