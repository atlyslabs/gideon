"use client"

export const dynamic = "force-dynamic"

import { useEffect, Suspense } from "react"
import { useRouter, useSearchParams } from "next/navigation"
import dynamicImport from "next/dynamic"
import { useTokenStore } from "../hooks/useTokenStore"
import useUser from "../hooks/useUser"

const GoogleLoginButton = dynamicImport(
  () => import("../components/GoogleLoginButton"),
  {
    ssr: false,
    loading: () => (
      <div className="w-full px-4 py-3 text-center" style={{ color: "var(--text-secondary)" }}>
        Loading...
      </div>
    ),
  }
)

function LoginInner() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const rawRedirect = searchParams.get("redirect")
  // Only allow internal paths — block absolute URLs and protocol-relative URLs
  const redirect = rawRedirect && rawRedirect.startsWith("/") && !rawRedirect.startsWith("//") ? rawRedirect : "/"
  const { loggedIn } = useUser()
  const logOut = useTokenStore((store) => store.logOut)

  useEffect(() => {
    if (loggedIn) {
      router.push(redirect)
    } else {
      logOut()
    }
  }, [loggedIn, router, logOut, redirect])

  return (
    <div
      className="min-h-screen flex items-center justify-center p-4"
      style={{ background: "var(--background)" }}
    >
      <div className="relative w-full max-w-sm">
        {/* Card */}
        <div
          className="rounded-2xl overflow-hidden"
          style={{
            background: "var(--surface)",
            border: "1px solid var(--border)",
          }}
        >
          {/* Header */}
          <div
            className="text-center"
            style={{ padding: "48px 40px 32px" }}
          >
            {/* Gideon logo glow */}
            <div
              className="inline-flex items-center justify-center w-20 h-20 rounded-2xl mb-5"
              style={{
                background: "rgba(59, 130, 246, 0.1)",
                border: "1px solid rgba(59, 130, 246, 0.2)",
                boxShadow: "0 0 30px rgba(59, 130, 246, 0.15), 0 0 60px rgba(59, 130, 246, 0.05)",
              }}
            >
              <span
                className="text-3xl font-bold"
                style={{
                  color: "#60a5fa",
                  fontFamily: "monospace",
                  textShadow: "0 0 10px rgba(59, 130, 246, 0.5)",
                }}
              >
                G
              </span>
            </div>

            <h1
              className="text-2xl font-bold tracking-[0.2em]"
              style={{
                color: "var(--text-primary)",
                fontFamily: "monospace",
              }}
            >
              GIDEON
            </h1>
            <p
              className="mt-2 text-sm"
              style={{ color: "var(--text-secondary)" }}
            >
              AI Work Assistant
            </p>
          </div>

          {/* Login button */}
          <div style={{ padding: "0 40px 40px" }}>
            <GoogleLoginButton />
            <p
              className="mt-4 text-xs text-center"
              style={{ color: "var(--text-tertiary)" }}
            >
              Sign in with your Google account
            </p>
          </div>
        </div>
      </div>
    </div>
  )
}

export default function LoginPage() {
  return (
    <Suspense fallback={
      <div className="min-h-screen flex items-center justify-center" style={{ background: "var(--background)" }}>
        <div className="spinner" />
      </div>
    }>
      <LoginInner />
    </Suspense>
  )
}
