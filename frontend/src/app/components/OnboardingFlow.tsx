"use client"

import { useState, useCallback, useMemo, useRef, useEffect } from "react"
import { motion } from "framer-motion"
import useIntegrations from "../hooks/useIntegrations"
import { pingman } from "../utils/pingman"
import { htmlToMarkdown } from "../utils/htmlToMarkdown"
import WorkflowDetailPage from "./WorkflowDetailPage"
import {
  FeatureShowcaseStep,
  IntegrationsStep,
  ProfileReviewStep,
  ImportMemoriesStep,
  DescribeTaskStep,
  WorkflowsStep,
  DEFAULT_REQUIRED,
  ONBOARDING_INTEGRATIONS,
} from "./onboarding"
import type { IntegrationCardConfig } from "./onboarding"

interface OnboardingFlowProps {
  firstName: string
  onComplete: () => void
}

export default function OnboardingFlow({ firstName, onComplete }: OnboardingFlowProps) {
  const [step, setStep] = useState<1 | 2 | 3 | 4 | 5 | 6>(1)
  const [activeTab, setActiveTab] = useState("ticket-lookup")
  const [connectingProvider, setConnectingProvider] = useState<string | null>(null)
  const [settingUp, setSettingUp] = useState(false)
  const [city, setCity] = useState("")
  const [role, setRole] = useState("")
  const [department, setDepartment] = useState("")
  const [linkedinUrl, setLinkedinUrl] = useState("")
  const [requiredProviders, setRequiredProviders] = useState<string[]>(DEFAULT_REQUIRED)
  const [backgroundInfo, setBackgroundInfo] = useState("")
  const [profileLoading, setProfileLoading] = useState(false)
  const [profileReady, setProfileReady] = useState(false)
  const [onboardingStage, setOnboardingStage] = useState<string | null>(null)
  const [learnedSkills, setLearnedSkills] = useState<{ id: string; name: string; description: string; memory_content: string | null }[]>([])
  const [selectedWorkflowId, setSelectedWorkflowId] = useState<string | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const profileRef = useRef<HTMLDivElement>(null)
  const skillRefs = useRef<Record<string, HTMLDivElement | null>>({})
  const onboardingTriggered = useRef(false)
  const { integrations, getAuthUrl, mutate: mutateIntegrations } = useIntegrations()

  useEffect(() => {
    pingman.get<{ required: string[] }>("/api/gideon/v1/users/me/required-integrations")
      .then((res) => {
        if (res.ok && res.data?.required) {
          setRequiredProviders(res.data.required)
        }
      })
      .catch(() => {})
  }, [])


  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current)
    }
  }, [])

  const isProviderConnected = useCallback(
    (oauthProvider: string) => {
      return integrations.some((i) => i.provider === oauthProvider && i.is_active)
    },
    [integrations]
  )

  const allRequiredConnected = useMemo(
    () => requiredProviders.every((p) => p === "grafana" || isProviderConnected(p)),
    [isProviderConnected, requiredProviders]
  )

  const handleConnect = useCallback(
    async (provider: IntegrationCardConfig) => {
      try {
        setConnectingProvider(provider.oauthProvider)
        const authUrl = await getAuthUrl(provider.oauthProvider)
        const popup = window.open(authUrl, "_blank", "width=600,height=700,left=200,top=100")
        const pollTimer = setInterval(async () => {
          if (popup?.closed) {
            clearInterval(pollTimer)
            setConnectingProvider(null)
            await mutateIntegrations()
          }
        }, 500)
      } catch {
        setConnectingProvider(null)
      }
    },
    [getAuthUrl, mutateIntegrations]
  )

  const handleGoogleConnect = useCallback(() => {
    const googleProvider = ONBOARDING_INTEGRATIONS.find((p) => p.oauthProvider === "google")
    if (googleProvider) handleConnect(googleProvider)
  }, [handleConnect])

  const handleContinue = useCallback(async () => {
    if (!allRequiredConnected || settingUp) return
    setSettingUp(true)

    const settingsPayload: Record<string, string> = {}
    if (city.trim()) settingsPayload.city = city.trim()
    if (role.trim()) settingsPayload.role = role.trim()
    if (department.trim()) settingsPayload.department = department.trim()
    if (linkedinUrl.trim()) settingsPayload.linkedin_url = linkedinUrl.trim()
    if (Object.keys(settingsPayload).length > 0) {
      try { await pingman.patch("/api/gideon/v1/users/me/settings", settingsPayload) } catch {}
    }

    setStep(3)
    setProfileLoading(true)

    if (!onboardingTriggered.current) {
      onboardingTriggered.current = true
      pingman.post("/api/gideon/v1/users/me/profile-research").catch(() => {})
      pingman.post("/api/gideon/v1/users/me/smart-onboarding", {}).catch(() => {})

      pingman.post("/api/gideon/v1/skills/initialize-defaults", {}).then(async (res) => {
        if (res.ok && Array.isArray((res.data as any)?.skills)) {
          const skills = (res.data as any).skills as { id: string; name: string }[]
          for (const skill of skills) {
            pingman.post(`/api/gideon/v1/skills/${skill.id}/learn`, {}).catch(() => {})
          }
        }
      }).catch(() => {})
    }

    let profileDone = false
    let skillsDone = false

    pollRef.current = setInterval(async () => {
      try {
        if (!profileDone) {
          const settingsRes = await pingman.get<{ background_info: string | null; background_generated: boolean; onboarding_stage: string | null }>(
            "/api/gideon/v1/users/me/settings"
          )
          if (settingsRes.ok) {
            setOnboardingStage(settingsRes.data?.onboarding_stage || null)
            const hasProfile = !!settingsRes.data?.background_info
            const isDone = !!settingsRes.data?.background_generated
            // Wait for actual profile content, OR accept that generation
            // finished with nothing (stage=done means no more data coming)
            if (hasProfile || (isDone && settingsRes.data?.onboarding_stage === "done")) {
              profileDone = true
              setBackgroundInfo(settingsRes.data?.background_info || "")
            }
          }
        }

        if (!skillsDone) {
          const skillsRes = await pingman.get<{ skills: { name: string; description: string; memory_content: string | null; status: string }[] }>(
            "/api/gideon/v1/skills"
          )
          if (skillsRes.ok) {
            const skills = skillsRes.data?.skills || []
            const allLearned = skills.length > 0 && skills.every((s) => s.memory_content || s.status === "ready")
            if (allLearned) {
              skillsDone = true
              setLearnedSkills(skills.map((s: any) => ({ id: s.id, name: s.name, description: s.description || "", memory_content: s.memory_content })))
            }
          }
        }

        if (profileDone && skillsDone) {
          if (pollRef.current) clearInterval(pollRef.current)
          setProfileLoading(false)
          setProfileReady(true)
        }
      } catch {
        // keep polling
      }
    }, 3000)

    setTimeout(() => {
      if (pollRef.current) clearInterval(pollRef.current)
      setProfileLoading(false)
      setProfileReady(true)
    }, 300000)
  }, [allRequiredConnected, settingUp, city, role, department, linkedinUrl])

  const saveProfileAndSkills = useCallback(() => {
    if (profileRef.current) {
      const editedMarkdown = htmlToMarkdown(profileRef.current)
      if (editedMarkdown.trim()) {
        pingman.patch("/api/gideon/v1/users/me/settings", { background_info: editedMarkdown.trim() }).catch(() => {})
      }
    } else if (backgroundInfo.trim()) {
      pingman.patch("/api/gideon/v1/users/me/settings", { background_info: backgroundInfo.trim() }).catch(() => {})
    }

    for (const skill of learnedSkills) {
      if (skill.id) {
        const el = skillRefs.current[skill.id]
        const md = el ? htmlToMarkdown(el) : skill.memory_content
        if (md?.trim()) {
          pingman.patch(`/api/gideon/v1/skills/${skill.id}`, { memory_content: md.trim() }).catch(() => {})
        }
      }
    }
  }, [backgroundInfo, learnedSkills])

  const connectedProviders = useMemo(
    () => integrations.filter((i) => i.is_active).map((i) => i.provider),
    [integrations]
  )

  return (
    <motion.div
      className="fixed inset-0 z-50 overflow-y-auto"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.5 }}
    >
      <div className={`min-h-screen bg-black ${step >= 3 ? "flex flex-col" : ""}`}>

        {step === 1 && (
          <FeatureShowcaseStep
            firstName={firstName}
            activeTab={activeTab}
            setActiveTab={setActiveTab}
            onNext={() => setStep(2)}
          />
        )}

        {step === 2 && (
          <IntegrationsStep
            connectingProvider={connectingProvider}
            settingUp={settingUp}
            allRequiredConnected={allRequiredConnected}
            requiredProviders={requiredProviders}
            city={city}
            setCity={setCity}
            role={role}
            setRole={setRole}
            department={department}
            setDepartment={setDepartment}
            linkedinUrl={linkedinUrl}
            setLinkedinUrl={setLinkedinUrl}
            isProviderConnected={isProviderConnected}
            handleConnect={handleConnect}
            handleGoogleConnect={handleGoogleConnect}
            handleContinue={handleContinue}
          />
        )}

        {step === 3 && (
          <ProfileReviewStep
            profileLoading={profileLoading}
            onboardingStage={onboardingStage}
            backgroundInfo={backgroundInfo}
            learnedSkills={learnedSkills}
            profileRef={profileRef}
            skillRefs={skillRefs}
            onNext={() => { saveProfileAndSkills(); setStep(4) }}
          />
        )}

        {step === 4 && (
          <ImportMemoriesStep onComplete={() => setStep(5)} onSkip={() => setStep(5)} />
        )}

        {step === 5 && (
          <DescribeTaskStep
            connectedProviders={connectedProviders}
            onSubmit={async (description) => {
              const resp = await pingman.post("/api/gideon/v1/workflows/from-description", {
                description,
                connected_integrations: connectedProviders,
              })
              if (!resp.ok) {
                throw new Error("Failed to generate workflow")
              }
              setStep(6)
            }}
            onSkip={() => setStep(6)}
          />
        )}

        {step === 6 && (
          <>
          {selectedWorkflowId ? (
            <div className="flex-1 flex">
              <WorkflowDetailPage
                workflowId={selectedWorkflowId}
                onBack={() => setSelectedWorkflowId(null)}
              />
            </div>
          ) : (
            <WorkflowsStep onSelectWorkflow={(wf) => setSelectedWorkflowId(wf.id)} onComplete={onComplete} />
          )}
          </>
        )}
      </div>
    </motion.div>
  )
}
