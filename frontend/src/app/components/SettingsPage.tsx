"use client"

import { useState } from "react"
import { ArrowLeft, User, SlidersHorizontal, Volume2, Info } from "lucide-react"
import useSettings from "../hooks/useSettings"
import { useTokenStore } from "../hooks/useTokenStore"
import useIsMobile from "../hooks/useIsMobile"
import AccountSettings from "./settings/AccountSettings"
import FeaturesSettings from "./settings/FeaturesSettings"
import PreferencesSettings from "./settings/PreferencesSettings"
import AboutSettings from "./settings/AboutSettings"

interface SettingsPageProps {
  onBack: () => void
}

export default function SettingsPage({ onBack }: SettingsPageProps) {
  const isMobile = useIsMobile()
  const { settings, loading, updateSettings } = useSettings()
  const logOut = useTokenStore((s) => s.logOut)

  const [activeSidebarItem, setActiveSidebarItem] = useState<"user" | "features" | "preferences" | "about">("user")

  const TABS = [
    { id: "user" as const, icon: User, label: "Account" },
    { id: "features" as const, icon: SlidersHorizontal, label: "Features" },
    { id: "preferences" as const, icon: Volume2, label: "Preferences" },
    { id: "about" as const, icon: Info, label: "About" },
  ]

  return (
    <div
      className={`flex-1 flex ${isMobile ? "flex-col" : ""} min-w-0 ${isMobile ? "" : "rounded-xl"} overflow-hidden`}
      style={{ background: "var(--surface)" }}
    >
      {/* -- Desktop: Left sidebar | Mobile: Top tabs -- */}
      {isMobile ? (
        <div className="shrink-0" style={{ borderBottom: "1px solid var(--border)" }}>
          <div className="px-4 pt-3 pb-0">
            <h1 className="text-[18px] font-semibold mb-3" style={{ color: "var(--text-primary)" }}>Settings</h1>
          </div>
          <div className="flex px-2">
            {TABS.map((tab) => (
              <button
                key={tab.id}
                onClick={() => setActiveSidebarItem(tab.id)}
                className="flex-1 flex items-center justify-center gap-1.5 py-2.5 text-[13px] font-medium relative"
                style={{ color: activeSidebarItem === tab.id ? "var(--text-primary)" : "var(--text-tertiary)" }}
              >
                <tab.icon size={15} />
                {tab.label}
                {activeSidebarItem === tab.id && (
                  <div className="absolute bottom-0 left-2 right-2 h-[2px] rounded-full" style={{ background: "#a78bfa" }} />
                )}
              </button>
            ))}
          </div>
        </div>
      ) : (
        <div
          className="w-[200px] shrink-0 flex flex-col py-4 px-3"
          style={{ borderRight: "1px solid var(--border)" }}
        >
          <button
            onClick={onBack}
            className="flex items-center gap-2 px-3 py-2 rounded-lg text-[13px] mb-4 transition-colors"
            style={{ color: "var(--text-secondary)" }}
            onMouseEnter={(e) => { e.currentTarget.style.background = "var(--surface-elevated)" }}
            onMouseLeave={(e) => { e.currentTarget.style.background = "transparent" }}
          >
            <ArrowLeft size={16} />
            <span>Back</span>
          </button>
          {TABS.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveSidebarItem(tab.id)}
              className="flex items-center gap-3 px-3 py-2 rounded-lg text-[13px] font-medium transition-colors"
              style={{
                background: activeSidebarItem === tab.id ? "var(--surface-elevated)" : "transparent",
                color: activeSidebarItem === tab.id ? "var(--text-primary)" : "var(--text-secondary)",
              }}
            >
              <tab.icon size={16} />
              <span>{tab.label}</span>
            </button>
          ))}
        </div>
      )}

      {/* -- Content area -- */}
      <div className="flex-1 overflow-y-auto">
        {loading ? (
          <div className="flex items-center justify-center py-20">
            <div className="spinner" />
          </div>
        ) : activeSidebarItem === "user" ? (
          <AccountSettings
            settings={settings}
            updateSettings={updateSettings}
            logOut={logOut}
          />
        ) : activeSidebarItem === "features" ? (
          <FeaturesSettings
            settings={settings}
            updateSettings={updateSettings}
          />
        ) : activeSidebarItem === "preferences" ? (
          <PreferencesSettings />
        ) : (
          <AboutSettings />
        )}
      </div>
    </div>
  )
}
