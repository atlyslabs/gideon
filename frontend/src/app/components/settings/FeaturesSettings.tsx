"use client"

import { useCallback } from "react"
import { SlidersHorizontal } from "lucide-react"
import type { UserSettings } from "../../hooks/useSettings"

interface FeaturesSettingsProps {
  settings: UserSettings | undefined
  updateSettings: (payload: Record<string, unknown>) => Promise<unknown>
}

export default function FeaturesSettings({
  settings,
  updateSettings,
}: FeaturesSettingsProps) {
  const handleFeatureToggle = useCallback(
    async (field: string, value: boolean) => {
      if (!settings) return
      try {
        await updateSettings({ [field]: value } as Record<string, boolean>)
      } catch {
        // silent -- SWR will revalidate
      }
    },
    [settings, updateSettings]
  )

  return (
    <div className="max-w-[640px] mx-auto py-8 px-8">
      {/* Section header */}
      <div className="flex items-center gap-3 mb-6">
        <div
          className="w-10 h-10 rounded-xl flex items-center justify-center"
          style={{ background: "var(--surface-elevated)" }}
        >
          <SlidersHorizontal size={20} style={{ color: "var(--text-secondary)" }} />
        </div>
        <div>
          <h1
            className="text-[16px] font-semibold"
            style={{ color: "var(--text-primary)" }}
          >
            Features
          </h1>
          <p
            className="text-[13px] mt-0.5"
            style={{ color: "var(--text-secondary)" }}
          >
            Enable or disable background agents.
          </p>
        </div>
      </div>

      {/* Card */}
      <div
        className="rounded-xl overflow-hidden"
        style={{ border: "1px solid var(--border)" }}
      >
        {/* Section title */}
        <div
          className="px-6 pt-5 pb-3"
        >
          <p
            className="text-[13px] font-semibold uppercase tracking-wide"
            style={{ color: "var(--text-secondary)" }}
          >
            Background Agents
          </p>
        </div>

        {/* Toggles */}
        {(() => {
          const items = [
            { field: "feature_action_items", label: "Action Items", description: "Pulls action items from your apps and flags what's urgent.", defaultOn: true },
            { field: "feature_email_drafts", label: "Email Drafts", description: "Auto-generates reply drafts for your recent emails.", defaultOn: true },
            { field: "feature_meeting_prep", label: "Meeting Prep", description: "Generates preparation notes for your upcoming meetings.", defaultOn: true },
            { field: "feature_dev_console", label: "Dev Console", description: "Right-panel tab with your GitHub PR queue and Linear issues. Off by default.", defaultOn: false },
          ] as const
          return items.map((item, idx) => {
            const stored = settings?.[item.field]
            const value = typeof stored === "boolean" ? stored : item.defaultOn
            return (
            <div
              key={item.field}
              className="px-6 py-4"
              style={idx < items.length - 1 ? { borderBottom: "1px solid var(--border-subtle)" } : undefined}
            >
              <div className="flex items-center justify-between gap-4">
                <div>
                  <p
                    className="text-[14px] font-medium"
                    style={{ color: "var(--text-primary)" }}
                  >
                    {item.label}
                  </p>
                  <p
                    className="text-[12px] mt-0.5"
                    style={{ color: "var(--text-secondary)" }}
                  >
                    {item.description}
                  </p>
                </div>
                <button
                  role="switch"
                  aria-checked={value}
                  onClick={() => handleFeatureToggle(item.field, !value)}
                  className="relative shrink-0 rounded-full transition-colors duration-200 ease-in-out"
                  style={{
                    width: 44,
                    height: 24,
                    background: value ? "#7c3aed" : "#333",
                  }}
                >
                  <span
                    className="block rounded-full bg-white shadow transition-transform duration-200 ease-in-out"
                    style={{
                      width: 18,
                      height: 18,
                      position: "absolute",
                      top: 3,
                      left: 3,
                      transform: value ? "translateX(20px)" : "translateX(0)",
                    }}
                  />
                </button>
              </div>
            </div>
            )
          })
        })()}
      </div>
    </div>
  )
}
