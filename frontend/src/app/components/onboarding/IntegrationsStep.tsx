"use client"

import { motion } from "framer-motion"
import { Check, ArrowRight, Loader2 } from "lucide-react"
import CityPicker from "../CityPicker"
import { ONBOARDING_INTEGRATIONS, IntegrationCardConfig } from "./constants"

interface IntegrationsStepProps {
  connectingProvider: string | null
  settingUp: boolean
  allRequiredConnected: boolean
  requiredProviders: string[]
  city: string
  setCity: (city: string) => void
  role: string
  setRole: (role: string) => void
  department: string
  setDepartment: (department: string) => void
  linkedinUrl: string
  setLinkedinUrl: (url: string) => void
  isProviderConnected: (provider: string) => boolean
  handleConnect: (provider: IntegrationCardConfig) => void
  handleGoogleConnect: () => void
  handleContinue: () => void
}

export default function IntegrationsStep({
  connectingProvider,
  settingUp,
  allRequiredConnected,
  requiredProviders,
  city,
  setCity,
  role,
  setRole,
  department,
  setDepartment,
  linkedinUrl,
  setLinkedinUrl,
  isProviderConnected,
  handleConnect,
  handleGoogleConnect,
  handleContinue,
}: IntegrationsStepProps) {
  return (
    <div className="relative px-6 pt-12 pb-20">
      <div className="max-w-3xl mx-auto">
        <motion.div
          className="rounded-2xl p-8 md:p-12"
          style={{
            background: "linear-gradient(135deg, rgba(59,130,246,0.12) 0%, rgba(59,130,246,0.04) 100%)",
            border: "1px solid rgba(59,130,246,0.2)",
            backdropFilter: "blur(24px)",
            boxShadow: "0 0 60px rgba(59,130,246,0.08), inset 0 1px 0 rgba(255,255,255,0.05)",
          }}
          initial={{ opacity: 0, y: 30 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, delay: 0.6 }}
        >
          {/* Heading */}
          <div className="text-center mb-10">
            <h2 className="text-2xl md:text-3xl font-bold text-white mb-3">
              Set up in under a minute
            </h2>
            <p className="text-white/50 text-sm md:text-base">
              Connect your tools so Gideon can start working for you.
            </p>
          </div>

          {/* Primary CTA */}
          <div className="flex justify-center mb-10">
            <motion.button
              onClick={handleGoogleConnect}
              disabled={connectingProvider === "google"}
              className="inline-flex items-center gap-2.5 px-8 py-3.5 rounded-full bg-white text-gray-900 text-sm font-semibold shadow-xl shadow-black/10 hover:shadow-2xl hover:shadow-black/15 transition-all duration-200 disabled:opacity-60"
              whileHover={{ scale: 1.03 }}
              whileTap={{ scale: 0.98 }}
            >
              {connectingProvider === "google" ? (
                <Loader2 size={16} className="animate-spin" />
              ) : isProviderConnected("google") ? (
                <>
                  <Check size={16} className="text-green-600" />
                  Google Connected
                </>
              ) : (
                <>
                  Connect Google Workspace
                  <ArrowRight size={16} />
                </>
              )}
            </motion.button>
          </div>

          {/* Integration grid */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {ONBOARDING_INTEGRATIONS.map((provider, idx) => {
              const connected = provider.alwaysConnected || isProviderConnected(provider.oauthProvider)
              const connecting = connectingProvider === provider.oauthProvider

              return (
                <motion.div
                  key={provider.id}
                  className={`flex items-center gap-3.5 rounded-xl px-4 py-3.5 transition-all duration-200 ${
                    connected
                      ? "bg-white/15 ring-1 ring-green-400/30"
                      : "bg-white/5 hover:bg-white/10"
                  }`}
                  initial={{ opacity: 0, y: 12 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.3, delay: 0.7 + idx * 0.06 }}
                >
                  <div
                    className={`w-10 h-10 rounded-lg flex items-center justify-center shrink-0 ${
                      provider.darkIcon ? "bg-white/10" : "bg-white/15"
                    }`}
                  >
                    {provider.icon}
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium text-white">{provider.name}</span>
                      {connected && !requiredProviders.includes(provider.oauthProvider) ? (
                        <span className="px-1.5 py-0.5 rounded text-[9px] font-semibold uppercase tracking-wider bg-green-500/20 text-green-400">Connected</span>
                      ) : requiredProviders.includes(provider.oauthProvider) ? (
                        <span className="px-1.5 py-0.5 rounded text-[9px] font-semibold uppercase tracking-wider bg-blue-500/20 text-blue-400">Required</span>
                      ) : (
                        <span className="px-1.5 py-0.5 rounded text-[9px] font-semibold uppercase tracking-wider bg-white/5 text-white/30">Optional</span>
                      )}
                    </div>
                    <div className="text-xs text-white/40">{provider.description}</div>
                    {!connected && provider.connectHint && (
                      <div className="text-[10px] text-white/30">{provider.connectHint}</div>
                    )}
                  </div>
                  {connected ? (
                    <div className="flex items-center gap-1 text-xs font-medium text-green-400">
                      <Check size={14} />
                    </div>
                  ) : (
                    <button
                      onClick={() => handleConnect(provider)}
                      disabled={connecting}
                      className="px-3.5 py-1.5 rounded-lg text-xs font-medium bg-white/10 text-white/80 hover:bg-white/20 hover:text-white transition-all duration-150 disabled:opacity-50"
                    >
                      {connecting ? (
                        <Loader2 size={12} className="animate-spin" />
                      ) : (
                        "Connect"
                      )}
                    </button>
                  )}
                </motion.div>
              )
            })}
          </div>

          {/* About you */}
          <motion.div
            className="mt-8 space-y-3"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.3, delay: 1.2 }}
          >
            <div className="flex items-center gap-3">
              <label className="text-sm text-white/60 whitespace-nowrap w-28 shrink-0">Your city</label>
              <CityPicker
                value={city}
                onChange={setCity}
                variant="dark"
              />
            </div>
            <div className="flex items-center gap-3">
              <label className="text-sm text-white/60 whitespace-nowrap w-28 shrink-0">Role</label>
              <input
                type="text"
                value={role}
                onChange={(e) => setRole(e.target.value)}
                placeholder="e.g. Product Manager"
                className="flex-1 px-3 py-2 rounded-lg text-sm text-white placeholder:text-white/30 outline-none"
                style={{ background: "rgba(255,255,255,0.08)", border: "1px solid rgba(255,255,255,0.1)" }}
              />
            </div>
            <div className="flex items-center gap-3">
              <label className="text-sm text-white/60 whitespace-nowrap w-28 shrink-0">Department</label>
              <input
                type="text"
                value={department}
                onChange={(e) => setDepartment(e.target.value)}
                placeholder="e.g. Engineering, Operations"
                className="flex-1 px-3 py-2 rounded-lg text-sm text-white placeholder:text-white/30 outline-none"
                style={{ background: "rgba(255,255,255,0.08)", border: "1px solid rgba(255,255,255,0.1)" }}
              />
            </div>
            <div className="flex items-center gap-3">
              <label className="text-sm text-white/60 whitespace-nowrap w-28 shrink-0">LinkedIn</label>
              <input
                type="url"
                value={linkedinUrl}
                onChange={(e) => setLinkedinUrl(e.target.value)}
                placeholder="linkedin.com/in/username (optional)"
                className="flex-1 px-3 py-2 rounded-lg text-sm text-white placeholder:text-white/30 outline-none"
                style={{ background: "rgba(255,255,255,0.08)", border: "1px solid rgba(255,255,255,0.1)" }}
              />
            </div>
          </motion.div>

          {/* Continue + footer */}
          <div className="mt-6 flex flex-col items-end gap-2">
            {!allRequiredConnected && (
              <p className="text-xs text-white/40">
                Connect all required integrations to continue
              </p>
            )}
            <motion.button
              onClick={handleContinue}
              disabled={!allRequiredConnected || settingUp}
              className={`inline-flex items-center gap-1.5 px-6 py-2.5 rounded-full text-sm font-medium transition-all duration-150 border ${
                allRequiredConnected && !settingUp
                  ? "bg-white/10 text-white hover:bg-white/20 border-white/10"
                  : "bg-white/5 text-white/30 border-white/5 cursor-not-allowed"
              }`}
              whileHover={allRequiredConnected && !settingUp ? { scale: 1.02 } : {}}
              whileTap={allRequiredConnected && !settingUp ? { scale: 0.98 } : {}}
            >
              {settingUp ? (
                <>
                  <Loader2 size={14} className="animate-spin" />
                  Setting up your workspace...
                </>
              ) : (
                <>
                  Continue
                  <ArrowRight size={14} />
                </>
              )}
            </motion.button>
          </div>

          <div className="mt-6 text-center">
            <p className="text-[11px] text-white/30">
              You are always in control. Critical actions need your approval by default.
            </p>
          </div>
        </motion.div>
      </div>
    </div>
  )
}
