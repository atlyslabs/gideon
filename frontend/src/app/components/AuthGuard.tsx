"use client"

import { useEffect } from "react"
import { useRouter } from "next/navigation"
import useUser from "../hooks/useUser"

export default function AuthGuard({ children }: { children: React.ReactNode }) {
  const { loading, loggedIn } = useUser()
  const router = useRouter()

  useEffect(() => {
    if (!loading && !loggedIn) {
      router.push("/login")
    }
  }, [loggedIn, loading, router])

  if (loading || !loggedIn) {
    return (
      <div className="min-h-screen flex items-center justify-center" style={{ background: "var(--background)" }}>
        <div className="spinner" />
      </div>
    )
  }

  return <>{children}</>
}
