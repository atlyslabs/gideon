"use client"

import { useEffect, useState } from "react"
import { useParams, useRouter } from "next/navigation"
import dynamic from "next/dynamic"
import useUser from "../../hooks/useUser"
import useGideonUser from "../../hooks/useGideonUser"
import { useTokenStore } from "../../hooks/useTokenStore"
import { pingman } from "../../utils/pingman"
import SharedChatView from "../../components/SharedChatView"

const AppLayout = dynamic(() => import("../../components/Layout"), { ssr: false })
const OnboardingFlow = dynamic(() => import("../../components/OnboardingFlow"), { ssr: false })

export default function ChatPage() {
  const params = useParams()
  const router = useRouter()
  const conversationId = params?.id as string
  const { loading: authLoading, loggedIn, user } = useUser()
  const { gideonUser, loading: gideonLoading, completeOnboarding } = useGideonUser()
  const authToken = useTokenStore((s) => s.authToken)
  const [accessType, setAccessType] = useState<string | null>(null)
  const [checking, setChecking] = useState(true)

  // Redirect to login if not authenticated
  useEffect(() => {
    if (!authLoading && !loggedIn) {
      router.push(`/login?redirect=/chat/${conversationId}`)
    }
  }, [authLoading, loggedIn, conversationId, router])

  // Check access once authenticated
  useEffect(() => {
    if (!loggedIn || !authToken || !conversationId) return

    async function checkAccess() {
      try {
        const res = await pingman.get<{ has_access: boolean; access_type: string }>(
          `/api/gideon/v1/chat/shared/${conversationId}/access`
        )
        if (res.ok) {
          setAccessType(res.data.access_type)
        } else {
          setAccessType("none")
        }
      } catch {
        setAccessType("none")
      } finally {
        setChecking(false)
      }
    }

    checkAccess()
  }, [loggedIn, authToken, conversationId, router])

  // Loading states
  if (authLoading || checking) {
    return (
      <div className="min-h-screen flex items-center justify-center" style={{ background: "var(--background)" }}>
        <div className="spinner" />
      </div>
    )
  }

  // Not logged in (redirect is happening)
  if (!loggedIn) return null

  // Conversation no longer exists (deleted by owner)
  if (accessType === "deleted") {
    return (
      <div className="min-h-screen flex items-center justify-center p-4" style={{ background: "var(--background)" }}>
        <div className="text-center max-w-sm">
          <div className="text-4xl mb-4">🗑️</div>
          <h1 className="text-xl font-semibold mb-2" style={{ color: "var(--text-primary)" }}>
            Conversation no longer exists
          </h1>
          <p className="text-sm" style={{ color: "var(--text-tertiary)" }}>
            This chat has been deleted and is no longer available.
          </p>
          <a
            href="/"
            className="inline-block mt-4 px-4 py-2 rounded-lg text-sm font-medium transition-colors"
            style={{ background: "rgba(59,130,246,0.15)", color: "#60a5fa" }}
          >
            Open Gideon
          </a>
        </div>
      </div>
    )
  }

  // Access denied (exists but user can't view)
  if (accessType === "none") {
    return (
      <div className="min-h-screen flex items-center justify-center p-4" style={{ background: "var(--background)" }}>
        <div className="text-center max-w-sm">
          <div className="text-4xl mb-4">🔒</div>
          <h1 className="text-xl font-semibold mb-2" style={{ color: "var(--text-primary)" }}>
            Access denied
          </h1>
          <p className="text-sm" style={{ color: "var(--text-tertiary)" }}>
            You don&apos;t have permission to view this conversation.
          </p>
          <a
            href="/"
            className="inline-block mt-4 px-4 py-2 rounded-lg text-sm font-medium transition-colors"
            style={{ background: "rgba(59,130,246,0.15)", color: "#60a5fa" }}
          >
            Open Gideon
          </a>
        </div>
      </div>
    )
  }

  // Owner → render full app directly (no redirect, no flash)
  if (accessType === "owner") {
    const needsOnboarding = !gideonLoading && gideonUser && !gideonUser.onboarding_completed
    const firstName = user?.first_name || gideonUser?.name?.split(" ")[0] || ""

    if (needsOnboarding) {
      return <OnboardingFlow firstName={firstName} onComplete={async () => { await completeOnboarding().catch(() => {}); router.refresh() }} />
    }

    return (
      <div className="h-screen">
        <AppLayout initialConversationId={conversationId} />
      </div>
    )
  }

  // Shared view (shared_link or shared_email)
  return <SharedChatView conversationId={conversationId} />
}
