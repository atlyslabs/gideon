"use client"

import { useState, useEffect, useCallback, Fragment, Suspense } from "react"
import dynamic from "next/dynamic"
import { preload } from "swr"
import { useSearchParams } from "next/navigation"
import SplashScreen from "./components/SplashScreen"
import useUser from "./hooks/useUser"
import useGideonUser from "./hooks/useGideonUser"
import useIntegrations from "./hooks/useIntegrations"
import useAuthConfig from "./hooks/useAuthConfig"
import { useTokenStore } from "./hooks/useTokenStore"
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

// Integrations this deployment refuses to open without, as a comma-separated
// list of provider keys (e.g. "google,slack"). Empty by default, which means
// no gate: an instance holding nothing but an LLM key goes straight into the
// app. That default matters — a self-hosted instance has no Google or Slack
// OAuth client to connect to, so a hardcoded gate would strand it on a screen
// whose buttons cannot succeed.
const REQUIRED_INTEGRATIONS = (process.env.NEXT_PUBLIC_REQUIRED_INTEGRATIONS || "")
  .split(",")
  .map((provider) => provider.trim().toLowerCase())
  .filter(Boolean)

// Display names for the gate screen. Anything absent falls back to its
// capitalized provider key, so a new provider needs no change here.
const PROVIDER_LABELS: Record<string, string> = {
  google: "Google",
  slack: "Slack",
  notion: "Notion",
  linear: "Linear",
  github: "GitHub",
}

function providerLabel(provider: string): string {
  return PROVIDER_LABELS[provider] || provider.charAt(0).toUpperCase() + provider.slice(1)
}

/** "Google", "Google and Slack", "Google, Slack and Notion" */
function joinLabels(providers: string[]): string {
  const labels = providers.map(providerLabel)
  if (labels.length <= 1) return labels[0] || ""
  return `${labels.slice(0, -1).join(", ")} and ${labels[labels.length - 1]}`
}

function HomeInner() {
  const { loading, loggedIn, user } = useUser()
  const { singleUser, loading: authConfigLoading } = useAuthConfig()
  const setAuthToken = useTokenStore((state) => state.setAuthToken)
  const authToken = useTokenStore((state) => state.authToken)
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

  // single_user mode has no identity provider and no login step — the backend
  // resolves every request to the one local owner and ignores the header
  // entirely. The rest of the app still keys off a token being present, so
  // seed a placeholder to unblock it. This is not a credential and grants
  // nothing: it is never validated, and the backend would behave identically
  // with no header at all.
  useEffect(() => {
    if (singleUser && !authToken) {
      setAuthToken("single-user")
    }
  }, [singleUser, authToken, setAuthToken])

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

  const connectedProviders = new Set(
    integrations.filter((i) => i.is_active).map((i) => i.provider)
  )
  const missingRequired = REQUIRED_INTEGRATIONS.filter((p) => !connectedProviders.has(p))
  const requiredConnected = missingRequired.length === 0

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

  // Always show spinner until client has mounted (prevents hydration mismatch).
  // Also wait on the auth mode: deciding between "login screen" and "no login
  // at all" before the answer arrives would flash a sign-in card at a
  // single_user instance that has nothing to sign in to.
  if (!mounted || loading || authConfigLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center" style={{ background: "var(--background)" }}>
        <div className="spinner" />
      </div>
    )
  }

  // Not logged in → login card. Skipped entirely in single_user mode, where
  // there is no provider to authenticate against.
  if (!loggedIn && !singleUser) {
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
              {/* Rendering the button without a client id throws inside
                  @react-oauth/google ("must be used within
                  GoogleOAuthProvider") and takes the whole app down with a
                  blank client-side exception. Say what is missing instead. */}
              {process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID ? (
                <>
                  <GoogleLoginButton />
                  <p className="mt-4 text-xs text-center" style={{ color: "var(--text-tertiary)" }}>
                    Sign in with your Google account
                  </p>
                </>
              ) : (
                <p className="text-xs text-center leading-relaxed" style={{ color: "var(--text-tertiary)" }}>
                  This instance runs <code>AUTH_MODE=oidc</code> but no
                  <code> NEXT_PUBLIC_GOOGLE_CLIENT_ID</code> was set at build time,
                  so there is no sign-in method. Set it and rebuild the frontend,
                  or run the backend with <code>AUTH_MODE=single_user</code>.
                </p>
              )}
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
                  {`Gideon needs ${joinLabels(REQUIRED_INTEGRATIONS)} to work`}
                </p>
              </div>
              <div className="flex flex-col gap-3" style={{ padding: "24px 40px 40px" }}>
                {REQUIRED_INTEGRATIONS.map((provider) => {
                  const connected = connectedProviders.has(provider)
                  return (
                    <Fragment key={provider}>
                      <button
                        onClick={() => handleConnectProvider(provider)}
                        disabled={connected || connectingSlack}
                        className="w-full flex items-center justify-between px-4 py-3 rounded-xl text-sm font-medium transition-all duration-150"
                        style={{
                          background: connected ? "rgba(34, 197, 94, 0.1)" : "rgba(255,255,255,0.08)",
                          border: connected ? "1px solid rgba(34, 197, 94, 0.3)" : "1px solid var(--border)",
                          color: connected ? "#22c55e" : "var(--text-primary)",
                        }}
                      >
                        <span>{providerLabel(provider)}</span>
                        {connected ? (
                          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12" /></svg>
                        ) : (
                          <span style={{ color: "var(--text-secondary)" }}>Connect</span>
                        )}
                      </button>
                      {provider === "slack" && !connected && (
                        <p className="text-[10px] text-center -mt-1" style={{ color: "var(--text-tertiary)" }}>
                          Sign in to your Slack workspace when prompted
                        </p>
                      )}
                    </Fragment>
                  )
                })}
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
