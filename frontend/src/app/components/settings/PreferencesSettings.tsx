"use client"

import { useEffect, useState } from "react"
import { Volume2, Bell } from "lucide-react"
import {
  getSoundMode,
  getNotificationMode,
  setSoundMode,
  setNotificationMode,
  type SoundMode,
  type NotificationMode,
} from "../../utils/preferences"

const SOUND_OPTIONS: { value: SoundMode; label: string }[] = [
  { value: "always", label: "Always play sound" },
  { value: "unfocused", label: "Only when tab is not focused" },
  { value: "mute", label: "Mute" },
]

const NOTIF_OPTIONS: { value: NotificationMode; label: string }[] = [
  { value: "always", label: "Always show notifications" },
  { value: "unfocused", label: "Only when tab is not focused" },
  { value: "off", label: "Off" },
]

function RadioRow<T extends string>({
  selected,
  value,
  label,
  onSelect,
}: {
  selected: T
  value: T
  label: string
  onSelect: (v: T) => void
}) {
  const isSelected = selected === value
  return (
    <button
      type="button"
      onClick={() => onSelect(value)}
      className="flex items-center gap-3 py-2 w-full text-left"
    >
      <span
        className="shrink-0 rounded-full flex items-center justify-center"
        style={{
          width: 18,
          height: 18,
          border: `1.5px solid ${isSelected ? "#7c3aed" : "var(--border)"}`,
          background: "transparent",
        }}
      >
        {isSelected && (
          <span
            className="rounded-full"
            style={{ width: 9, height: 9, background: "#7c3aed" }}
          />
        )}
      </span>
      <span className="text-[14px]" style={{ color: "var(--text-primary)" }}>
        {label}
      </span>
    </button>
  )
}

export default function PreferencesSettings() {
  const [sound, setSound] = useState<SoundMode>("unfocused")
  const [notif, setNotif] = useState<NotificationMode>("unfocused")

  useEffect(() => {
    setSound(getSoundMode())
    setNotif(getNotificationMode())
  }, [])

  const handleSound = (v: SoundMode) => {
    setSound(v)
    setSoundMode(v)
  }

  const handleNotif = (v: NotificationMode) => {
    setNotif(v)
    setNotificationMode(v)
    if (v !== "off" && typeof Notification !== "undefined" && Notification.permission === "default") {
      Notification.requestPermission().catch(() => {})
    }
  }

  return (
    <div className="max-w-[640px] mx-auto py-8 px-8">
      <div className="flex items-center gap-3 mb-6">
        <div
          className="w-10 h-10 rounded-xl flex items-center justify-center"
          style={{ background: "var(--surface-elevated)" }}
        >
          <Volume2 size={20} style={{ color: "var(--text-secondary)" }} />
        </div>
        <div>
          <h1 className="text-[16px] font-semibold" style={{ color: "var(--text-primary)" }}>
            Preferences
          </h1>
          <p className="text-[13px] mt-0.5" style={{ color: "var(--text-secondary)" }}>
            Customize sound and notifications.
          </p>
        </div>
      </div>

      <div className="rounded-xl overflow-hidden" style={{ border: "1px solid var(--border)" }}>
        <div className="px-6 py-5">
          <p className="text-[14px] font-medium" style={{ color: "var(--text-primary)" }}>
            Sound
          </p>
          <p className="text-[12px] mt-0.5 mb-3" style={{ color: "var(--text-secondary)" }}>
            Play a notification sound when a response completes.
          </p>
          <div className="flex flex-col">
            {SOUND_OPTIONS.map((opt) => (
              <RadioRow
                key={opt.value}
                selected={sound}
                value={opt.value}
                label={opt.label}
                onSelect={handleSound}
              />
            ))}
          </div>
        </div>

        <div style={{ borderTop: "1px solid var(--border-subtle)" }} />

        <div className="px-6 py-5">
          <div className="flex items-center gap-2 mb-0.5">
            <Bell size={14} style={{ color: "var(--text-secondary)" }} />
            <p className="text-[14px] font-medium" style={{ color: "var(--text-primary)" }}>
              Notifications
            </p>
          </div>
          <p className="text-[12px] mb-3" style={{ color: "var(--text-secondary)" }}>
            Show a browser notification when a response completes.
          </p>
          <div className="flex flex-col">
            {NOTIF_OPTIONS.map((opt) => (
              <RadioRow
                key={opt.value}
                selected={notif}
                value={opt.value}
                label={opt.label}
                onSelect={handleNotif}
              />
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
