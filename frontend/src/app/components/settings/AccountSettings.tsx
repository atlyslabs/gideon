"use client"

import { useState, useCallback, useRef, useEffect } from "react"
import { User, LogOut, Loader2, Trash2 } from "lucide-react"
import { pingman } from "../../utils/pingman"
import ReactMarkdown from "react-markdown"
import CityPicker from "../CityPicker"
import { htmlToMarkdown } from "../../utils/htmlToMarkdown"
import { getIntegrationIcon } from "../../utils/integrationIcons"
import useIsMobile from "../../hooks/useIsMobile"
import type { UserSettings } from "../../hooks/useSettings"

const GmailIcon = getIntegrationIcon("gmail", 18)
const SlackIcon = getIntegrationIcon("slack", 18)

interface AccountSettingsProps {
  settings: UserSettings | undefined
  updateSettings: (payload: Record<string, unknown>) => Promise<unknown>
  logOut: () => void
}

export default function AccountSettings({
  settings,
  updateSettings,
  logOut,
}: AccountSettingsProps) {
  const isMobile = useIsMobile()
  const [username, setUsername] = useState(settings?.name || "")
  const [role, setRole] = useState(settings?.role || "")
  const [saving, setSaving] = useState(false)
  const [savingRole, setSavingRole] = useState(false)
  const [deleteConfirm, setDeleteConfirm] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [deleteInput, setDeleteInput] = useState("")

  useEffect(() => {
    if (settings?.name) setUsername(settings.name)
  }, [settings?.name])
  useEffect(() => {
    setRole(settings?.role || "")
  }, [settings?.role])
  const roleRef = useRef<HTMLInputElement>(null)
  const handleRoleSave = useCallback(async () => {
    if (!settings) return
    const trimmed = role.trim()
    if (trimmed === (settings.role || "")) return
    setSavingRole(true)
    try {
      await updateSettings({ role: trimmed })
    } catch {
      setRole(settings.role || "")
    } finally {
      setSavingRole(false)
    }
  }, [role, settings, updateSettings])
  const handleRoleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter") {
        e.preventDefault()
        roleRef.current?.blur()
      }
    },
    []
  )
  const usernameRef = useRef<HTMLInputElement>(null)
  const backgroundRef = useRef<HTMLDivElement>(null)

  const handleUsernameSave = useCallback(async () => {
    if (!settings || username.trim() === settings.name) return
    setSaving(true)
    try {
      await updateSettings({ name: username.trim() })
    } catch {
      setUsername(settings.name)
    } finally {
      setSaving(false)
    }
  }, [username, settings, updateSettings])

  const handleUsernameKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter") {
        e.preventDefault()
        usernameRef.current?.blur()
      }
    },
    []
  )

  const handleBackgroundSave = useCallback(() => {
    if (!backgroundRef.current) return
    const md = htmlToMarkdown(backgroundRef.current).trim()
    if (md && md !== settings?.background_info) {
      updateSettings({ background_info: md }).catch(() => {})
    }
  }, [settings?.background_info, updateSettings])

  const handleCommunicationToggle = useCallback(
    async (mode: string) => {
      if (!settings || mode === settings.preferred_communication) return
      try {
        await updateSettings({ preferred_communication: mode })
      } catch {
        // silent -- SWR will revalidate
      }
    },
    [settings, updateSettings]
  )

  return (
    <div className={`max-w-[640px] mx-auto py-8 ${isMobile ? "px-4" : "px-8"}`}>
      {/* Section header */}
      <div className="flex items-center gap-3 mb-6">
        <div
          className="w-10 h-10 rounded-xl flex items-center justify-center"
          style={{ background: "var(--surface-elevated)" }}
        >
          <User size={20} style={{ color: "var(--text-secondary)" }} />
        </div>
        <div>
          <h1
            className="text-[16px] font-semibold"
            style={{ color: "var(--text-primary)" }}
          >
            Account Information
          </h1>
          <p
            className="text-[13px] mt-0.5"
            style={{ color: "var(--text-secondary)" }}
          >
            Update your account details.
          </p>
        </div>
      </div>

      {/* Card */}
      <div
        className="rounded-xl overflow-hidden"
        style={{ border: "1px solid var(--border)" }}
      >
        {/* -- Username -- */}
        <div
          className={`${isMobile ? "px-4" : "px-6"} py-5`}
          style={{ borderBottom: "1px solid var(--border)" }}
        >
          <div className={`flex ${isMobile ? "flex-col gap-2" : "flex-row items-start justify-between gap-4"}`}>
            <div className="shrink-0">
              <p
                className="text-[14px] font-medium"
                style={{ color: "var(--text-primary)" }}
              >
                Username
              </p>
              <p
                className="text-[12px] mt-0.5"
                style={{ color: "var(--text-secondary)" }}
              >
                What should we call you?
              </p>
            </div>
            <div className="relative">
              <input
                ref={usernameRef}
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                onBlur={handleUsernameSave}
                onKeyDown={handleUsernameKeyDown}
                className={`${isMobile ? "w-full" : "w-[260px]"} px-3 py-2 rounded-lg text-[13px] transition-colors`}
                style={{
                  background: "var(--surface-elevated)",
                  border: "1px solid var(--border)",
                  color: "var(--text-primary)",
                  outline: "none",
                }}
                onFocus={(e) => {
                  e.currentTarget.style.borderColor = "var(--text-tertiary)"
                }}
                onBlurCapture={(e) => {
                  e.currentTarget.style.borderColor = "var(--border)"
                }}
              />
              {saving && (
                <Loader2
                  size={14}
                  className="animate-spin absolute right-3 top-1/2 -translate-y-1/2"
                  style={{ color: "var(--text-tertiary)" }}
                />
              )}
            </div>
          </div>
        </div>

        {/* -- Email -- */}
        <div
          className={`${isMobile ? "px-4" : "px-6"} py-5`}
          style={{ borderBottom: "1px solid var(--border)" }}
        >
          <div className={`flex ${isMobile ? "flex-col gap-2" : "flex-row items-start justify-between gap-4"}`}>
            <div className="shrink-0">
              <p
                className="text-[14px] font-medium"
                style={{ color: "var(--text-primary)" }}
              >
                Email
              </p>
              <p
                className="text-[12px] mt-0.5"
                style={{ color: "var(--text-secondary)" }}
              >
                Manage the email you use to sign into Gideon.
              </p>
            </div>
            <input
              type="text"
              value={settings?.email || ""}
              readOnly
              className={`${isMobile ? "w-full" : "w-[260px]"} px-3 py-2 rounded-lg text-[13px] cursor-default`}
              style={{
                background: "var(--surface-elevated)",
                border: "1px solid var(--border)",
                color: "var(--text-tertiary)",
                outline: "none",
              }}
            />
          </div>
        </div>

        {/* -- Timezone -- */}
        <div
          className={`${isMobile ? "px-4" : "px-6"} py-5`}
          style={{ borderBottom: "1px solid var(--border)" }}
        >
          <div className={`flex ${isMobile ? "flex-col gap-2" : "flex-row items-start justify-between gap-4"}`}>
            <div className="shrink-0">
              <p
                className="text-[14px] font-medium"
                style={{ color: "var(--text-primary)" }}
              >
                Timezone
              </p>
              <p
                className="text-[12px] mt-0.5"
                style={{ color: "var(--text-secondary)" }}
              >
                Used for scheduling and greetings.
              </p>
            </div>
            <select
              value={settings?.timezone || "UTC"}
              onChange={async (e) => {
                await updateSettings({ timezone: e.target.value })
              }}
              className={`${isMobile ? "w-full" : "w-[260px]"} px-3 py-2 rounded-lg text-[13px] cursor-pointer`}
              style={{
                background: "var(--surface-elevated)",
                border: "1px solid var(--border)",
                color: "var(--text-primary)",
                outline: "none",
              }}
            >
              {Intl.supportedValuesOf("timeZone").map((tz) => (
                <option key={tz} value={tz}>{tz.replace(/_/g, " ")}</option>
              ))}
            </select>
          </div>
        </div>

        {/* -- City -- */}
        <div
          className={`${isMobile ? "px-4" : "px-6"} py-5`}
          style={{ borderBottom: "1px solid var(--border)" }}
        >
          <div className={`flex ${isMobile ? "flex-col gap-2" : "flex-row items-start justify-between gap-4"}`}>
            <div className="shrink-0">
              <p
                className="text-[14px] font-medium"
                style={{ color: "var(--text-primary)" }}
              >
                City
              </p>
              <p
                className="text-[12px] mt-0.5"
                style={{ color: "var(--text-secondary)" }}
              >
                Used for weather and location context.
              </p>
            </div>
            <CityPicker
              value={settings?.city || ""}
              onChange={async (city) => {
                await updateSettings({ city })
              }}
              variant="surface"
            />
          </div>
        </div>

        {/* -- Role -- */}
        <div
          className={`${isMobile ? "px-4" : "px-6"} py-5`}
          style={{ borderBottom: "1px solid var(--border)" }}
        >
          <div className={`flex ${isMobile ? "flex-col gap-2" : "flex-row items-start justify-between gap-4"}`}>
            <div className="shrink-0">
              <p
                className="text-[14px] font-medium"
                style={{ color: "var(--text-primary)" }}
              >
                Role
              </p>
              <p
                className="text-[12px] mt-0.5"
                style={{ color: "var(--text-secondary)" }}
              >
                Your job title. Helps Gideon tailor responses.
              </p>
            </div>
            <div className="relative">
              <input
                ref={roleRef}
                type="text"
                value={role}
                onChange={(e) => setRole(e.target.value)}
                onBlur={handleRoleSave}
                onKeyDown={handleRoleKeyDown}
                placeholder="e.g. Product Manager"
                className={`${isMobile ? "w-full" : "w-[260px]"} px-3 py-2 rounded-lg text-[13px] transition-colors`}
                style={{
                  background: "var(--surface-elevated)",
                  border: "1px solid var(--border)",
                  color: "var(--text-primary)",
                  outline: "none",
                }}
                onFocus={(e) => {
                  e.currentTarget.style.borderColor = "var(--text-tertiary)"
                }}
                onBlurCapture={(e) => {
                  e.currentTarget.style.borderColor = "var(--border)"
                }}
              />
              {savingRole && (
                <Loader2
                  size={14}
                  className="animate-spin absolute right-3 top-1/2 -translate-y-1/2"
                  style={{ color: "var(--text-tertiary)" }}
                />
              )}
            </div>
          </div>
        </div>

        {/* -- Department -- */}
        <div
          className={`${isMobile ? "px-4" : "px-6"} py-5`}
          style={{ borderBottom: "1px solid var(--border)" }}
        >
          <div className={`flex ${isMobile ? "flex-col gap-2" : "flex-row items-start justify-between gap-4"}`}>
            <div className="shrink-0">
              <p
                className="text-[14px] font-medium"
                style={{ color: "var(--text-primary)" }}
              >
                Department
              </p>
              <p
                className="text-[12px] mt-0.5"
                style={{ color: "var(--text-secondary)" }}
              >
                Synced from the company org chart.
              </p>
            </div>
            <input
              type="text"
              value={settings?.department || "—"}
              readOnly
              className={`${isMobile ? "w-full" : "w-[260px]"} px-3 py-2 rounded-lg text-[13px] cursor-default`}
              style={{
                background: "var(--surface-elevated)",
                border: "1px solid var(--border)",
                color: "var(--text-tertiary)",
                outline: "none",
              }}
            />
          </div>
        </div>

        {/* -- Preferred Mode of Communication -- */}
        <div
          className={`${isMobile ? "px-4" : "px-6"} py-5`}
          style={{ borderBottom: "1px solid var(--border)" }}
        >
          <div className={`flex ${isMobile ? "flex-col gap-2" : "flex-row items-start justify-between gap-4"}`}>
            <div className="shrink-0">
              <p
                className="text-[14px] font-medium"
                style={{ color: "var(--text-primary)" }}
              >
                Preferred Mode of Communication
              </p>
              <p
                className="text-[12px] mt-0.5"
                style={{ color: "var(--text-secondary)" }}
              >
                Choose how you&apos;d like Gideon to communicate with you.
              </p>
            </div>
            <div className="flex items-center gap-2">
              {[
                { id: "gmail", label: "Gmail", icon: GmailIcon },
                { id: "slack", label: "Slack", icon: SlackIcon },
              ].map((mode) => {
                const isActive =
                  settings?.preferred_communication === mode.id
                return (
                  <button
                    key={mode.id}
                    onClick={() => handleCommunicationToggle(mode.id)}
                    className="flex items-center gap-2 px-4 py-2 rounded-lg text-[13px] font-medium transition-all"
                    style={{
                      background: isActive
                        ? "var(--surface-elevated)"
                        : "transparent",
                      border: isActive
                        ? "1px solid rgba(255,255,255,0.2)"
                        : "1px solid var(--border)",
                      color: isActive
                        ? "var(--text-primary)"
                        : "var(--text-tertiary)",
                    }}
                    onMouseEnter={(e) => {
                      if (!isActive) {
                        e.currentTarget.style.borderColor =
                          "var(--text-tertiary)"
                        e.currentTarget.style.color =
                          "var(--text-secondary)"
                      }
                    }}
                    onMouseLeave={(e) => {
                      if (!isActive) {
                        e.currentTarget.style.borderColor = "var(--border)"
                        e.currentTarget.style.color =
                          "var(--text-tertiary)"
                      }
                    }}
                  >
                    {mode.icon}
                    {mode.label}
                  </button>
                )
              })}
            </div>
          </div>
        </div>

        {/* -- Background -- */}
        <div
          className={`${isMobile ? "px-4" : "px-6"} py-5`}
          style={{ borderBottom: "1px solid var(--border)" }}
        >
          <div className="mb-3">
            <p
              className="text-[14px] font-medium"
              style={{ color: "var(--text-primary)" }}
            >
              Background
            </p>
            <p
              className="text-[12px] mt-0.5"
              style={{ color: "var(--text-secondary)" }}
            >
              Tell us about yourself. Click to edit.
            </p>
          </div>
          {settings?.background_info ? (
            <div
              ref={backgroundRef}
              contentEditable
              suppressContentEditableWarning
              onBlur={handleBackgroundSave}
              className="rounded-lg p-4 max-h-[400px] overflow-y-auto text-[13px] leading-relaxed prose prose-invert prose-sm max-w-none cursor-text transition-colors focus:outline-none focus:ring-1 focus:ring-blue-500/30"
              style={{
                background: "var(--surface-elevated)",
                border: "1px solid var(--border)",
                color: "var(--text-secondary)",
              }}
            >
              <ReactMarkdown>{settings.background_info}</ReactMarkdown>
            </div>
          ) : (
            <div
              className="rounded-lg p-4 text-[13px]"
              style={{
                background: "var(--surface-elevated)",
                border: "1px solid var(--border)",
              }}
            >
              <div className="flex items-center gap-3 py-4">
                <Loader2
                  size={16}
                  className="animate-spin"
                  style={{ color: "var(--text-tertiary)" }}
                />
                <span style={{ color: "var(--text-tertiary)" }}>
                  Generating your profile...
                </span>
              </div>
            </div>
          )}
        </div>

        {/* -- Logout -- */}
        <div className={`${isMobile ? "px-4" : "px-6"} py-5`} style={{ borderBottom: "1px solid var(--border)" }}>
          <div className={`flex ${isMobile ? "flex-col gap-3" : "flex-row items-center justify-between gap-3"}`}>
            <p
              className="text-[14px] font-medium"
              style={{ color: "var(--text-secondary)" }}
            >
              Logout from this account
            </p>
            <button
              onClick={logOut}
              className="flex items-center gap-2 px-4 py-2 rounded-lg text-[13px] font-medium transition-colors"
              style={{
                background: "#ef4444",
                color: "#ffffff",
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.background = "#dc2626"
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = "#ef4444"
              }}
            >
              <LogOut size={14} />
              Logout
            </button>
          </div>
        </div>

        {/* -- Delete All Data -- */}
        <div className={`${isMobile ? "px-4" : "px-6"} py-5`}>
          {!deleteConfirm ? (
            <div className={`flex ${isMobile ? "flex-col gap-3" : "flex-row items-center justify-between gap-3"}`}>
              <div>
                <p
                  className="text-[14px] font-medium"
                  style={{ color: "var(--text-secondary)" }}
                >
                  Delete all data
                </p>
                <p
                  className="text-[12px] mt-0.5"
                  style={{ color: "var(--text-tertiary)" }}
                >
                  Permanently delete all conversations, memories, workflows, and integrations.
                </p>
              </div>
              <button
                onClick={() => setDeleteConfirm(true)}
                className="flex items-center gap-2 px-4 py-2 rounded-lg text-[13px] font-medium transition-colors shrink-0"
                style={{
                  background: "transparent",
                  border: "1px solid #ef4444",
                  color: "#ef4444",
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = "rgba(239,68,68,0.1)"
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background = "transparent"
                }}
              >
                <Trash2 size={14} />
                Delete
              </button>
            </div>
          ) : (
            <div>
              <p
                className="text-[14px] font-medium mb-1"
                style={{ color: "#ef4444" }}
              >
                Are you sure? This cannot be undone.
              </p>
              <p
                className="text-[12px] mb-3"
                style={{ color: "var(--text-tertiary)" }}
              >
                Type <span style={{ color: "var(--text-primary)", fontWeight: 600 }}>delete</span> to confirm.
              </p>
              <div className="flex flex-wrap items-center gap-3">
                <input
                  type="text"
                  value={deleteInput}
                  onChange={(e) => setDeleteInput(e.target.value)}
                  placeholder="delete"
                  className={`${isMobile ? "w-full" : "w-[160px]"} px-3 py-2 rounded-lg text-[13px]`}
                  style={{
                    background: "var(--surface-elevated)",
                    border: "1px solid #ef4444",
                    color: "var(--text-primary)",
                    outline: "none",
                  }}
                  autoFocus
                />
                <button
                  disabled={deleteInput !== "delete" || deleting}
                  onClick={async () => {
                    setDeleting(true)
                    try {
                      const res = await pingman.delete("/api/gideon/v1/users/me")
                      if (res.ok) {
                        window.history.replaceState(null, "", "/")
                        logOut()
                      } else {
                        setDeleting(false)
                      }
                    } catch {
                      setDeleting(false)
                    }
                  }}
                  className="flex items-center gap-2 px-4 py-2 rounded-lg text-[13px] font-medium transition-colors"
                  style={{
                    background: deleteInput === "delete" && !deleting ? "#ef4444" : "var(--surface-elevated)",
                    color: deleteInput === "delete" && !deleting ? "#ffffff" : "var(--text-tertiary)",
                    cursor: deleteInput === "delete" && !deleting ? "pointer" : "not-allowed",
                  }}
                >
                  {deleting ? <Loader2 size={14} className="animate-spin" /> : <Trash2 size={14} />}
                  {deleting ? "Deleting..." : "Delete Everything"}
                </button>
                <button
                  onClick={() => { setDeleteConfirm(false); setDeleteInput("") }}
                  className="px-4 py-2 rounded-lg text-[13px] font-medium"
                  style={{
                    color: "var(--text-tertiary)",
                    background: "transparent",
                  }}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
