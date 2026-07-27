"use client"

import { useState, useEffect, useCallback, Suspense } from "react"
import dynamic from "next/dynamic"
import { preload } from "swr"
import { useSearchParams } from "next/navigation"
import SplashScreen from "./components/SplashScreen"
import useUser from "./hooks/useUser"
import useGideonUser from "./hooks/useGideonUser"
import useIntegrations from "./hooks/useIntegrations"
import { pingman } from "./utils/pingman"
import AppLayout from "./components/Layout"

const GoogleLoginButton = dynamic(() => import("./components/GoogleLoginButton"), {
  ssr: false,
  loading: () => (
    <div className="w-full px-4 py-3 text-center" style={{ color: "var(--text-secondary)" }}>
      Loading...
    </div>
  ),
})

const OnboardingFlow = dynamic(() => import("./components/OnboardingFlow"), {
  ssr: false,
})

function HomeInner() {
  const { loading, loggedIn, user } = useUser()
  const { gideonUser, loading: gideonLoading, completeOnboarding } = useGideonUser()
  const { integrations, loading: integrationsLoading, getAuthUrl, mutate: mutateIntegrations } = useIntegrations()
  const searchParams = useSearchParams()
  const openConversationId = searchParams.get("open")
  const [splashDone, setSplashDone] = useState(() => {
    if (typeof window !== "undefined") {
      return localStorage.getItem("gideon_splash_shown") === "1"
    }
    return false
  })
  const [onboardingDone, setOnboardingDone] = useState(false)
  const [mounted, setMounted] = useState(false)
  const [connectingSlack, setConnectingSlack] = useState(false)

  useEffect(() => { setMounted(true) }, [])

  // Prefetch all hub data as soon as user is logged in (during splash)
  useEffect(() => {
    if (!loggedIn) return
    const fetcher = async (url: string) => {
      const res = await pingman.get(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.data
    }
    preload("/api/gideon/v1/hub/todos", fetcher)
    preload("/api/gideon/v1/hub/suggestions", fetcher)
    preload("/api/gideon/v1/hub/emails", fetcher)
    preload("/api/gideon/v1/hub/meetings", fetcher)
    preload("/api/gideon/v1/chat/conversations", fetcher)
  }, [loggedIn])

  // If Gideon user data shows onboarding already completed, skip it
  useEffect(() => {
    if (gideonUser?.onboarding_completed) {
      setOnboardingDone(true)
    }
  }, [gideonUser])

  const handleOnboardingComplete = useCallback(async () => {
    try {
      await completeOnboarding()
    } catch {
      // Best-effort; proceed even if the API call fails
    }
    setOnboardingDone(true)
  }, [completeOnboarding])

  const slackConnected = integrations.some((i) => i.provider === "slack" && i.is_active)
  const googleConnected = integrations.some((i) => i.provider === "google" && i.is_active)
  const requiredConnected = slackConnected && googleConnected

  const handleConnectProvider = useCallback(async (provider: string) => {
    try {
      setConnectingSlack(true)
      // Open popup synchronously to avoid browser blocking
      const popup = window.open("about:blank", "_blank", "width=600,height=700,left=200,top=100")
      if (!popup) {
        setConnectingSlack(false)
        return
      }
      const authUrl = await getAuthUrl(provider)
      popup.location.href = authUrl
      const pollTimer = setInterval(async () => {
        if (popup.closed) {
          clearInterval(pollTimer)
          setConnectingSlack(false)
          await mutateIntegrations()
        }
      }, 500)
    } catch {
      setConnectingSlack(false)
    }
  }, [getAuthUrl, mutateIntegrations])

  // Always show spinner until client has mounted (prevents hydration mismatch)
  if (!mounted || loading) {
    return (
      <div className="min-h-screen flex items-center justify-center" style={{ background: "var(--background)" }}>
        <div className="spinner" />
      </div>
    )
  }

  // Not logged in → login card
  if (!loggedIn) {
    return (
      <div
        className="min-h-screen flex items-center justify-center p-4"
        style={{ background: "var(--background)" }}
      >
        <div className="relative w-full max-w-sm">
          <div
            className="rounded-2xl overflow-hidden"
            style={{
              background: "var(--surface)",
              border: "1px solid var(--border)",
            }}
          >
            <div className="text-center" style={{ padding: "48px 40px 32px" }}>
              <img
                src="/logo.png"
                alt="Gideon"
                className="w-20 h-20 rounded-full mx-auto mb-5"
                style={{
                  boxShadow: "0 0 30px rgba(59, 130, 246, 0.15), 0 0 60px rgba(59, 130, 246, 0.05)",
                }}
              />
              <h1
                className="text-2xl font-bold tracking-[0.2em]"
                style={{ color: "var(--text-primary)", fontFamily: "var(--font-orbitron)" }}
              >
                GIDEON
              </h1>
              <p className="mt-2 text-sm" style={{ color: "var(--text-secondary)" }}>
                Move Faster. Do More.
              </p>
            </div>
            <div style={{ padding: "0 40px 40px" }}>
              <GoogleLoginButton />
              <p className="mt-4 text-xs text-center" style={{ color: "var(--text-tertiary)" }}>
                Sign in with your Google account
              </p>
            </div>
          </div>
        </div>
      </div>
    )
  }

  // Logged in → splash, then onboarding (if needed), then integration gate, then app
  const needsOnboarding = !onboardingDone && !gideonLoading && gideonUser && !gideonUser.onboarding_completed
  const showOnboarding = splashDone && needsOnboarding
  const pastOnboarding = onboardingDone || gideonLoading || (gideonUser?.onboarding_completed === true)
  const needsIntegrations = splashDone && pastOnboarding && !integrationsLoading && !requiredConnected
  const showApp = splashDone && pastOnboarding && (requiredConnected || integrationsLoading)

  const firstName = user?.first_name || gideonUser?.name?.split(" ")[0] || ""

  return (
    <>
      {!splashDone && <SplashScreen onComplete={() => { localStorage.setItem("gideon_splash_shown", "1"); setSplashDone(true) }} />}

      {showOnboarding && (
        <OnboardingFlow
          firstName={firstName}
          onComplete={handleOnboardingComplete}
        />
      )}

      {needsIntegrations && (
        <div
          className="min-h-screen flex items-center justify-center p-4"
          style={{ background: "var(--background)" }}
        >
          <div className="w-full max-w-sm">
            <div
              className="rounded-2xl overflow-hidden"
              style={{ background: "var(--surface)", border: "1px solid var(--border)" }}
            >
              <div className="text-center" style={{ padding: "48px 40px 16px" }}>
                <img
                  src="/logo.png"
                  alt="Gideon"
                  className="w-16 h-16 rounded-full mx-auto mb-4"
                  style={{ boxShadow: "0 0 30px rgba(59, 130, 246, 0.15)" }}
                />
                <h2 className="text-lg font-semibold" style={{ color: "var(--text-primary)" }}>
                  Connect required integrations
                </h2>
                <p className="mt-1 text-sm" style={{ color: "var(--text-secondary)" }}>
                  Gideon needs Google and Slack to work
                </p>
              </div>
              <div className="flex flex-col gap-3" style={{ padding: "24px 40px 40px" }}>
                <button
                  onClick={() => handleConnectProvider("google")}
                  disabled={googleConnected || connectingSlack}
                  className="w-full flex items-center justify-between px-4 py-3 rounded-xl text-sm font-medium transition-all duration-150"
                  style={{
                    background: googleConnected ? "rgba(34, 197, 94, 0.1)" : "rgba(255,255,255,0.08)",
                    border: googleConnected ? "1px solid rgba(34, 197, 94, 0.3)" : "1px solid var(--border)",
                    color: googleConnected ? "#22c55e" : "var(--text-primary)",
                  }}
                >
                  <span>Google</span>
                  {googleConnected ? (
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12" /></svg>
                  ) : (
                    <span style={{ color: "var(--text-secondary)" }}>Connect</span>
                  )}
                </button>
                <button
                  onClick={() => handleConnectProvider("slack")}
                  disabled={slackConnected || connectingSlack}
                  className="w-full flex items-center justify-between px-4 py-3 rounded-xl text-sm font-medium transition-all duration-150"
                  style={{
                    background: slackConnected ? "rgba(34, 197, 94, 0.1)" : "rgba(255,255,255,0.08)",
                    border: slackConnected ? "1px solid rgba(34, 197, 94, 0.3)" : "1px solid var(--border)",
                    color: slackConnected ? "#22c55e" : "var(--text-primary)",
                  }}
                >
                  <span>Slack</span>
                  {slackConnected ? (
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12" /></svg>
                  ) : (
                    <span style={{ color: "var(--text-secondary)" }}>Connect</span>
                  )}
                </button>
                {!slackConnected && (
                  <p className="text-[10px] text-center -mt-1" style={{ color: "var(--text-tertiary)" }}>
                    Sign in to your Slack workspace when prompted
                  </p>
                )}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Always render AppLayout when logged in so SWR starts fetching during splash */}
      <div
        className={`h-screen transition-opacity duration-300 ${
          showApp ? "opacity-100" : "opacity-0 pointer-events-none absolute inset-0"
        }`}
      >
        <AppLayout initialConversationId={openConversationId} />
      </div>
    </>
  )
}

export default function Home() {
  return (
    <Suspense fallback={
      <div className="min-h-screen flex items-center justify-center" style={{ background: "var(--background)" }}>
        <div className="spinner" />
      </div>
    }>
      <HomeInner />
    </Suspense>
  )
}
