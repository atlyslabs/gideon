"use client"

import { AlertTriangle, ArrowLeft } from "lucide-react"
import type { Integration } from "../../types"
import type { ProviderConfig } from "./providerConfig"

interface IntegrationDetailProps {
  provider: ProviderConfig
  integration: Integration | undefined
  isConnected: boolean
  hasError: boolean
  isDisconnecting: boolean
  isConnecting: boolean
  // Set of OAuth provider keys (e.g. "google", "slack"). Passed in from the
  // parent because it now lives on the BE manifest (used to live as a static
  // export from `providerConfig.ts`).
  oauthProviders: Set<string>
  onBack: () => void
  onConnect: (oauthProvider: string) => void
  onDisconnect: (integration: Integration) => void
  apiKeyForm?: React.ReactNode
}

export default function IntegrationDetail({
  provider,
  integration,
  isConnected,
  hasError,
  isDisconnecting,
  isConnecting,
  oauthProviders,
  onBack,
  onConnect,
  onDisconnect,
  apiKeyForm,
}: IntegrationDetailProps) {
  const OAUTH_PROVIDERS = oauthProviders
  return (
    <div className="flex-1 flex flex-col min-w-0 rounded-xl overflow-hidden" style={{ background: "var(--surface)" }}>
      <div className="flex-1 overflow-y-auto">
        <div className="max-w-[720px] mx-auto px-6 py-8">
          {/* Back */}
          <button
            onClick={onBack}
            className="flex items-center gap-2 text-[13px] mb-6 transition-colors"
            style={{ color: "var(--text-secondary)" }}
            onMouseEnter={(e) => { e.currentTarget.style.color = "var(--text-primary)" }}
            onMouseLeave={(e) => { e.currentTarget.style.color = "var(--text-secondary)" }}
          >
            <ArrowLeft size={14} />
            All integrations
          </button>

          {/* Header row */}
          <div className="flex items-center justify-between mb-6">
            <div className="flex items-center gap-3">
              <div className="w-12 h-12 rounded-xl flex items-center justify-center" style={provider.darkIcon
                ? { background: "linear-gradient(to bottom right, #2a2a2a, #1e1e1e)", border: "1px solid rgba(255,255,255,0.08)" }
                : { background: "linear-gradient(to bottom right, #fff, #e8e8e8)", border: "1px solid rgba(0,0,0,0.06)" }
              }>
                {provider.icon}
              </div>
              <div>
                <h1 className="text-[16px] font-semibold" style={{ color: "var(--text-primary)" }}>
                  {provider.name}
                </h1>
                <p className="text-[13px]" style={{ color: "var(--text-secondary)" }}>
                  {provider.description}
                </p>
              </div>
            </div>
            {provider.comingSoon ? (
              <span className="px-4 py-2 rounded-full text-[13px] font-medium" style={{ background: "rgba(255,255,255,0.06)", color: "var(--text-tertiary)" }}>
                Coming Soon
              </span>
            ) : isConnected ? (
              <button
                onClick={() => integration && onDisconnect(integration)}
                disabled={isDisconnecting}
                className="px-5 py-2 rounded-full text-[13px] font-medium transition-colors disabled:opacity-50"
                style={{ background: "rgba(239,68,68,0.15)", color: "#EF4444" }}
              >
                {isDisconnecting ? "Disconnecting..." : "Disconnect"}
              </button>
            ) : provider.apiKeyConnect ? (
              !isConnected && <span className="px-4 py-2 rounded-full text-[13px] font-medium" style={{ background: "rgba(255,255,255,0.06)", color: "var(--text-tertiary)" }}>
                Enter API key below
              </span>
            ) : OAUTH_PROVIDERS.has(provider.oauthProvider) ? (
              <div className="flex flex-col items-end gap-1">
                <button
                  onClick={() => onConnect(provider.oauthProvider)}
                  disabled={isConnecting}
                  className="px-5 py-2 rounded-full text-[13px] font-medium transition-colors disabled:opacity-50"
                  style={{ background: "var(--accent)", color: "#000" }}
                >
                  {isConnecting ? "Connecting..." : "Connect"}
                </button>
                {provider.connectHint && (
                  <p className="text-[10px]" style={{ color: "var(--text-tertiary)" }}>{provider.connectHint}</p>
                )}
              </div>
            ) : (
              <span className="px-4 py-2 rounded-full text-[13px] font-medium" style={{ background: "rgba(255,255,255,0.06)", color: "var(--text-tertiary)" }}>
                Auto-configured
              </span>
            )}
          </div>

          {/* Hero banner */}
          <div
            className="rounded-xl overflow-hidden mb-1"
            style={{ background: provider.heroColor, height: 180, position: "relative" }}
          >
            <div className="absolute inset-0 flex items-center justify-center" style={{ opacity: 0.25 }}>
              <div className="w-20 h-20 rounded-2xl flex items-center justify-center" style={{ background: "rgba(255,255,255,0.2)", transform: "scale(2.5)" }}>
                {provider.icon}
              </div>
            </div>
            <div className="absolute" style={{ left: "15%", top: "50%", transform: "translateY(-50%) scale(1.2)", opacity: 0.4 }}>
              <div className="w-14 h-14 rounded-xl flex items-center justify-center" style={{ background: "rgba(255,255,255,0.15)" }}>
                {provider.icon}
              </div>
            </div>
            <div className="absolute" style={{ right: "12%", top: "50%", transform: "translateY(-50%) scale(1.3)", opacity: 0.35 }}>
              <div className="w-16 h-16 rounded-xl flex items-center justify-center" style={{ background: "rgba(255,255,255,0.15)" }}>
                {provider.icon}
              </div>
            </div>
          </div>

          {/* Connection info bar */}
          {integration && (isConnected || hasError) && (
            <div
              className="flex items-center gap-8 px-5 py-3 rounded-b-xl mb-6"
              style={{
                background: "var(--surface-card)",
                border: hasError
                  ? "1px solid rgba(239,68,68,0.3)"
                  : "1px solid var(--border)",
                borderTop: "none",
              }}
            >
              <div>
                <p className="text-[11px] font-medium" style={{ color: "var(--text-tertiary)" }}>Connected</p>
                <p className="text-[13px]" style={{ color: "var(--text-primary)" }}>
                  {integration.extra_data?.team_name || integration.extra_data?.workspace_name || "\u2014"}
                </p>
              </div>
              <div>
                <p className="text-[11px] font-medium" style={{ color: "var(--text-tertiary)" }}>Date</p>
                <p className="text-[13px]" style={{ color: "var(--text-primary)" }}>
                  {integration.connected_at
                    ? new Date(integration.connected_at).toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" })
                    : "\u2014"}
                </p>
              </div>
              <div>
                <p className="text-[11px] font-medium" style={{ color: "var(--text-tertiary)" }}>Status</p>
                {hasError ? (
                  <p className="text-[13px] flex items-center gap-1.5" style={{ color: "#EF4444" }}>
                    <AlertTriangle size={12} />
                    Needs attention
                  </p>
                ) : (
                  <p className="text-[13px] flex items-center gap-1.5" style={{ color: "var(--text-primary)" }}>
                    <span className="w-1.5 h-1.5 rounded-full" style={{ background: "#10B981" }} />
                    Active
                  </p>
                )}
              </div>
            </div>
          )}
          {/* Error detail banner */}
          {integration && hasError && integration.last_error && (
            <div
              className="rounded-xl px-4 py-3 mb-6 flex items-start gap-3"
              style={{ background: "rgba(239,68,68,0.08)", border: "1px solid rgba(239,68,68,0.2)" }}
            >
              <AlertTriangle size={16} className="shrink-0 mt-0.5" style={{ color: "#EF4444" }} />
              <div className="flex-1 min-w-0">
                <p className="text-[13px] font-medium mb-1" style={{ color: "#EF4444" }}>
                  {!integration.is_active ? "Disconnected due to error" : "Background sync failing"}
                </p>
                <p className="text-[12px]" style={{ color: "var(--text-secondary)" }}>
                  {integration.last_error}
                  {integration.last_error_at && (
                    <span style={{ color: "var(--text-tertiary)" }}>
                      {" "}\u00b7 {new Date(integration.last_error_at).toLocaleDateString("en-US", { month: "short", day: "numeric" })}
                    </span>
                  )}
                </p>
                {OAUTH_PROVIDERS.has(provider.oauthProvider) && (
                  <button
                    onClick={() => onConnect(provider.oauthProvider)}
                    disabled={isConnecting}
                    className="mt-2 px-4 py-1.5 rounded-lg text-[12px] font-medium transition-colors"
                    style={{ background: "var(--accent)", color: "#000" }}
                  >
                    {isConnecting ? "Reconnecting..." : "Reconnect"}
                  </button>
                )}
              </div>
            </div>
          )}
          {/* API key connect form */}
          {!isConnected && provider.apiKeyConnect && apiKeyForm}
          {!isConnected && !provider.apiKeyConnect && !hasError && <div className="mb-6" />}

          {/* Data safe card */}
          <div
            className="rounded-xl px-5 py-4 mb-8 flex items-center gap-4"
            style={{ background: "var(--surface-card)", border: "1px solid var(--border)" }}
          >
            <div className="flex-1">
              <h3 className="text-[14px] font-semibold mb-1" style={{ color: "var(--text-primary)" }}>
                {provider.dataSafeTitle}
              </h3>
              <p className="text-[13px] leading-relaxed" style={{ color: "var(--text-secondary)" }}>
                {provider.dataSafeDescription}
              </p>
            </div>
            <div className="shrink-0 w-16 h-16 rounded-full flex items-center justify-center" style={{ background: "rgba(255,255,255,0.04)", border: "2px solid rgba(255,255,255,0.08)" }}>
              <div className="w-6 h-6 rounded-full" style={{ background: "rgba(255,255,255,0.1)", border: "2px solid rgba(255,255,255,0.15)" }} />
            </div>
          </div>

          {/* Capabilities */}
          <div className="mb-8">
            <h3 className="text-[15px] font-semibold mb-3" style={{ color: "var(--text-primary)" }}>
              Capabilities
            </h3>
            <div className="flex flex-wrap gap-2">
              {provider.capabilities.map((cap) => (
                <span
                  key={cap}
                  className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-[13px]"
                  style={{ background: "rgba(255,255,255,0.04)", border: "1px solid var(--border)", color: "var(--text-secondary)" }}
                >
                  <span className="w-4 h-4 flex items-center justify-center shrink-0" style={{ opacity: 0.6 }}>
                    {provider.icon}
                  </span>
                  {cap}
                </span>
              ))}
            </div>
          </div>

          {/* Overview */}
          <div className="mb-6">
            <h3 className="text-[15px] font-semibold mb-2" style={{ color: "var(--text-primary)" }}>
              {provider.overviewTitle}
            </h3>
            <p className="text-[14px] leading-relaxed" style={{ color: "var(--text-secondary)" }}>
              {provider.overviewText}
            </p>
          </div>

          {/* Feature section */}
          <div className="mb-8">
            <h3 className="text-[15px] font-semibold mb-2" style={{ color: "var(--text-primary)" }}>
              {provider.featureTitle}
            </h3>
            <p className="text-[14px] leading-relaxed" style={{ color: "var(--text-secondary)" }}>
              {provider.featureText}
            </p>
          </div>
        </div>
      </div>
    </div>
  )
}
