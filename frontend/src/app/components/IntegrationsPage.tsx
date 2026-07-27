"use client"

import { useState, useCallback, useEffect, useRef } from "react"
import { Loader2, Search, Send } from "lucide-react"
import toast from "react-hot-toast"
import useIntegrations from "../hooks/useIntegrations"
import type { Integration } from "../types"
import { pingman } from "../utils/pingman"
import useIntegrationCards from "../hooks/useIntegrationCards"
import type { ProviderConfig } from "./integrations/providerConfig"
import IntegrationDetail from "./integrations/IntegrationDetail"
import { Section, IntegrationCard } from "./integrations/IntegrationCard"
import WhatsAppLinkCard from "./settings/WhatsAppLinkCard"

interface IntegrationsPageProps {
  onBack: () => void
}

export default function IntegrationsPage({ onBack }: IntegrationsPageProps) {
  const { integrations, loading, mutate, getAuthUrl, disconnect, requestIntegration } = useIntegrations()
  const { providers: ALL_PROVIDERS, oauthProviders: OAUTH_PROVIDERS } = useIntegrationCards()
  const [connectingProvider, setConnectingProvider] = useState<string | null>(null)
  const [disconnectingId, setDisconnectingId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [search, setSearch] = useState("")
  const [selectedProvider, setSelectedProvider] = useState<ProviderConfig | null>(null)
  const [requestMessage, setRequestMessage] = useState("")
  const [requestSubmitting, setRequestSubmitting] = useState(false)
  // API key connect form (Recruitee)
  const [apiKeyInput, setApiKeyInput] = useState("")
  const [companyIdInput, setCompanyIdInput] = useState("")
  const [apiKeyConnecting, setApiKeyConnecting] = useState(false)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [])

  const isProviderConnected = useCallback(
    (oauthProvider: string): boolean => integrations.some((i) => i.provider === oauthProvider && i.is_active),
    [integrations]
  )

  const getIntegrationForProvider = useCallback(
    (oauthProvider: string): Integration | undefined =>
      integrations.find((i) => i.provider === oauthProvider && i.is_active) ||
      integrations.find((i) => i.provider === oauthProvider),
    [integrations]
  )

  const hasProviderError = useCallback(
    (oauthProvider: string): boolean => {
      const integration = getIntegrationForProvider(oauthProvider)
      if (!integration) return false
      return integration.error_count > 0 || !!integration.last_error
    },
    [getIntegrationForProvider]
  )

  const handleConnect = useCallback(
    async (oauthProvider: string) => {
      setError(null)
      setConnectingProvider(oauthProvider)
      try {
        const authUrl = await getAuthUrl(oauthProvider)
        const oauthWindow = window.open(authUrl, `${oauthProvider}_oauth`, "width=600,height=700")
        pollRef.current = setInterval(async () => {
          if (!oauthWindow || oauthWindow.closed) {
            if (pollRef.current) clearInterval(pollRef.current)
            pollRef.current = null
            await mutate()
            setConnectingProvider(null)
          }
        }, 1000)
      } catch {
        setError(`Failed to connect ${oauthProvider}. Please try again.`)
        setConnectingProvider(null)
      }
    },
    [getAuthUrl, mutate]
  )

  const handleApiKeyConnect = useCallback(
    async () => {
      if (!apiKeyInput.trim() || !companyIdInput.trim()) return
      setError(null)
      setApiKeyConnecting(true)
      try {
        const res = await pingman.post("/api/gideon/v1/integrations/recruitee/connect", {
          api_key: apiKeyInput.trim(),
          company_id: companyIdInput.trim(),
        })
        if (!res.ok) {
          throw new Error("Failed to connect Recruitee. Please check your API key and company ID.")
        }
        await mutate()
        setApiKeyInput("")
        setCompanyIdInput("")
        toast.success("Recruitee connected!")
      } catch (e: any) {
        setError(e.message || "Failed to connect Recruitee.")
      } finally {
        setApiKeyConnecting(false)
      }
    },
    [apiKeyInput, companyIdInput, mutate]
  )

  const handleDisconnect = useCallback(
    async (integration: Integration) => {
      setError(null)
      setDisconnectingId(integration.id)
      try {
        await disconnect(integration.id)
        setSelectedProvider(null)
      } catch {
        setError(`Failed to disconnect. Please try again.`)
      } finally {
        setDisconnectingId(null)
      }
    },
    [disconnect]
  )

  const handleRequestSubmit = useCallback(async () => {
    if (!requestMessage.trim()) return
    setRequestSubmitting(true)
    try {
      await requestIntegration(requestMessage.trim())
      toast.success("Request sent!")
      setRequestMessage("")
    } catch {
      toast.error("Failed to send request. Please try again.")
    } finally {
      setRequestSubmitting(false)
    }
  }, [requestMessage, requestIntegration])

  // Detail view
  if (selectedProvider) {
    const isConnected = isProviderConnected(selectedProvider.oauthProvider)
    const integration = getIntegrationForProvider(selectedProvider.oauthProvider)
    const isDisconnecting = disconnectingId === integration?.id
    const isConnecting = connectingProvider === selectedProvider.oauthProvider

    const apiKeyForm = !isConnected && selectedProvider.apiKeyConnect ? (
      <div
        className="rounded-xl px-5 py-4 mb-6"
        style={{ background: "var(--surface-card)", border: "1px solid var(--border)" }}
      >
        <p className="text-[13px] font-medium mb-3" style={{ color: "var(--text-primary)" }}>
          Connect with API Key
        </p>
        <p className="text-[12px] mb-4" style={{ color: "var(--text-secondary)" }}>
          Generate a personal API token in Recruitee: Settings &gt; Apps and plugins &gt; Personal API tokens. Your company ID is in the URL when logged into Recruitee.
        </p>
        <div className="flex flex-col gap-2 mb-3">
          <input
            type="password"
            placeholder="API Key"
            value={apiKeyInput}
            onChange={(e) => setApiKeyInput(e.target.value)}
            className="w-full px-3 py-2 rounded-lg text-[13px] outline-none"
            style={{ background: "var(--surface)", border: "1px solid var(--border)", color: "var(--text-primary)" }}
          />
          <input
            type="text"
            placeholder="Company ID (e.g. 123456)"
            value={companyIdInput}
            onChange={(e) => setCompanyIdInput(e.target.value)}
            className="w-full px-3 py-2 rounded-lg text-[13px] outline-none"
            style={{ background: "var(--surface)", border: "1px solid var(--border)", color: "var(--text-primary)" }}
          />
        </div>
        <button
          onClick={handleApiKeyConnect}
          disabled={apiKeyConnecting || !apiKeyInput.trim() || !companyIdInput.trim()}
          className="px-5 py-2 rounded-full text-[13px] font-medium transition-colors disabled:opacity-50"
          style={{ background: "var(--accent)", color: "#000" }}
        >
          {apiKeyConnecting ? "Connecting..." : "Connect"}
        </button>
      </div>
    ) : undefined

    return (
      <IntegrationDetail
        provider={selectedProvider}
        integration={integration}
        isConnected={isConnected}
        hasError={hasProviderError(selectedProvider.oauthProvider)}
        isDisconnecting={isDisconnecting}
        isConnecting={isConnecting}
        oauthProviders={OAUTH_PROVIDERS}
        onBack={() => setSelectedProvider(null)}
        onConnect={handleConnect}
        onDisconnect={handleDisconnect}
        apiKeyForm={apiKeyForm}
      />
    )
  }

  // List view
  const filtered = ALL_PROVIDERS.filter(
    (p) =>
      p.name.toLowerCase().includes(search.toLowerCase()) ||
      p.description.toLowerCase().includes(search.toLowerCase())
  )

  // `comingSoon` is the overriding bucket: a card flagged coming-soon never
  // appears in errored/connected/available even if its provider is otherwise
  // a real OAuth provider (e.g. meta_ads). Without this guard, meta_ads
  // would show up in both Available AND Coming Soon.
  const errored = filtered.filter((p) => !p.comingSoon && hasProviderError(p.oauthProvider) && OAUTH_PROVIDERS.has(p.oauthProvider))
  const connected = filtered.filter((p) => !p.comingSoon && isProviderConnected(p.oauthProvider) && !hasProviderError(p.oauthProvider) && OAUTH_PROVIDERS.has(p.oauthProvider))
  const available = filtered.filter((p) => !p.comingSoon && !isProviderConnected(p.oauthProvider) && !hasProviderError(p.oauthProvider) && OAUTH_PROVIDERS.has(p.oauthProvider))
  const internal = filtered.filter((p) => !OAUTH_PROVIDERS.has(p.oauthProvider) && !p.comingSoon)
  const comingSoon = filtered.filter((p) => p.comingSoon)

  return (
    <div className="flex-1 flex flex-col min-w-0 rounded-xl overflow-hidden" style={{ background: "var(--surface)" }}>
      <div className="flex-1 overflow-y-auto">
        {/* Header */}
        <div className="pt-12 pb-6 text-center">
          <h1 className="text-[28px] font-semibold" style={{ color: "var(--text-primary)" }}>
            Integrations
          </h1>
          <p className="text-[14px] mt-2" style={{ color: "var(--text-secondary)" }}>
            Connect the tools you want to use with Gideon.
          </p>
        </div>

        {/* Search */}
        <div className="max-w-[640px] mx-auto px-6 mb-8">
          <div
            className="flex items-center gap-3 px-4 py-3 rounded-xl"
            style={{ background: "var(--surface-card)", border: "1px solid var(--border)" }}
          >
            <Search size={16} style={{ color: "var(--text-tertiary)" }} />
            <input
              type="text"
              placeholder="Search for integration"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="flex-1 bg-transparent text-[14px] outline-none"
              style={{ color: "var(--text-primary)" }}
            />
          </div>
        </div>

        {error && (
          <div className="max-w-[960px] mx-auto px-6 mb-4">
            <div className="px-4 py-3 rounded-lg text-[13px]" style={{ background: "rgba(239,68,68,0.1)", border: "1px solid rgba(239,68,68,0.2)", color: "var(--error)" }}>
              {error}
            </div>
          </div>
        )}

        {loading ? (
          <div className="flex items-center justify-center py-20"><div className="spinner" /></div>
        ) : (
          <div className="max-w-[960px] mx-auto px-6 pb-12 space-y-8">
            {/* WhatsApp — bespoke OTP linking (not an OAuth manifest provider).
                Self-hides until the WhatsApp number is provisioned. */}
            <WhatsAppLinkCard />
            {errored.length > 0 && (
              <Section title="Needs Attention" error>
                {errored.map((p) => {
                  const integration = getIntegrationForProvider(p.oauthProvider)
                  return (
                    <IntegrationCard
                      key={p.id}
                      provider={p}
                      buttonLabel="Fix"
                      buttonVariant="error"
                      errorCount={integration?.error_count}
                      onAction={() => setSelectedProvider(p)}
                    />
                  )
                })}
              </Section>
            )}

            {connected.length > 0 && (
              <Section title="Connected">
                {connected.map((p) => (
                  <IntegrationCard key={p.id} provider={p} buttonLabel="Manage" buttonVariant="secondary" onAction={() => setSelectedProvider(p)} />
                ))}
              </Section>
            )}

            {available.length > 0 && (
              <Section title="Available">
                {available.map((p) => {
                  const isConnecting = connectingProvider === p.oauthProvider
                  return (
                    <IntegrationCard
                      key={p.id}
                      provider={p}
                      buttonLabel={isConnecting ? "..." : "Connect"}
                      buttonLoading={isConnecting}
                      buttonVariant="primary"
                      onAction={() => handleConnect(p.oauthProvider)}
                    />
                  )
                })}
              </Section>
            )}

            {internal.length > 0 && (
              <Section title="Internal">
                {internal.map((p) => (
                  <IntegrationCard key={p.id} provider={p} buttonLabel="Manage" buttonVariant="secondary" onAction={() => setSelectedProvider(p)} />
                ))}
              </Section>
            )}

            {comingSoon.length > 0 && (
              <Section title="Coming Soon">
                {comingSoon.map((p) => (
                  <IntegrationCard key={p.id} provider={p} buttonLabel="Coming Soon" buttonVariant="muted" onAction={() => setSelectedProvider(p)} />
                ))}
              </Section>
            )}

            {filtered.length === 0 && (
              <div className="text-center py-12">
                <p className="text-[14px]" style={{ color: "var(--text-tertiary)" }}>
                  No integrations match &ldquo;{search}&rdquo;
                </p>
              </div>
            )}

            {/* Request an Integration */}
            <section>
              <h2 className="text-[14px] font-semibold mb-3" style={{ color: "var(--text-primary)" }}>Missing something?</h2>
              <div
                className="rounded-xl p-5"
                style={{ background: "var(--surface-card)", border: "1px solid var(--border)" }}
              >
                <p className="text-[13px] mb-3" style={{ color: "var(--text-secondary)" }}>
                  Let us know what integration you would like to see next.
                </p>
                <div className="flex gap-2">
                  <input
                    type="text"
                    placeholder="Request an integration (e.g. Jira, Asana...)"
                    value={requestMessage}
                    onChange={(e) => setRequestMessage(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter") handleRequestSubmit() }}
                    className="flex-1 bg-transparent text-[13px] outline-none px-3 py-2 rounded-lg"
                    style={{ background: "rgba(255,255,255,0.04)", border: "1px solid var(--border)", color: "var(--text-primary)" }}
                  />
                  <button
                    onClick={handleRequestSubmit}
                    disabled={requestSubmitting || !requestMessage.trim()}
                    className="flex items-center gap-1.5 px-4 py-2 rounded-lg text-[12px] font-medium transition-colors disabled:opacity-50"
                    style={{ background: "var(--accent)", color: "#000" }}
                  >
                    {requestSubmitting ? <Loader2 size={13} className="animate-spin" /> : <Send size={13} />}
                    Submit
                  </button>
                </div>
              </div>
            </section>
          </div>
        )}
      </div>
    </div>
  )
}
