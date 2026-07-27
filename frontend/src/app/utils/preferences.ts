"use client"

export type SoundMode = "always" | "unfocused" | "mute"
export type NotificationMode = "always" | "unfocused" | "off"

const SOUND_KEY = "gideon:pref:sound"
const NOTIF_KEY = "gideon:pref:notification"

const SOUND_DEFAULT: SoundMode = "unfocused"
const NOTIF_DEFAULT: NotificationMode = "unfocused"

function isSoundMode(v: string | null): v is SoundMode {
  return v === "always" || v === "unfocused" || v === "mute"
}

function isNotificationMode(v: string | null): v is NotificationMode {
  return v === "always" || v === "unfocused" || v === "off"
}

export function getSoundMode(): SoundMode {
  if (typeof window === "undefined") return SOUND_DEFAULT
  const v = window.localStorage.getItem(SOUND_KEY)
  return isSoundMode(v) ? v : SOUND_DEFAULT
}

export function setSoundMode(mode: SoundMode): void {
  if (typeof window === "undefined") return
  window.localStorage.setItem(SOUND_KEY, mode)
}

export function getNotificationMode(): NotificationMode {
  if (typeof window === "undefined") return NOTIF_DEFAULT
  const v = window.localStorage.getItem(NOTIF_KEY)
  return isNotificationMode(v) ? v : NOTIF_DEFAULT
}

export function setNotificationMode(mode: NotificationMode): void {
  if (typeof window === "undefined") return
  window.localStorage.setItem(NOTIF_KEY, mode)
}

// True when the tab is in the background or the window doesn't have focus.
export function isUnfocused(): boolean {
  if (typeof document === "undefined") return false
  if (document.visibilityState === "hidden") return true
  if (typeof document.hasFocus === "function" && !document.hasFocus()) return true
  return false
}

export function shouldPlaySound(): boolean {
  const mode = getSoundMode()
  if (mode === "mute") return false
  if (mode === "always") return true
  return isUnfocused()
}

export function shouldShowNotification(): boolean {
  const mode = getNotificationMode()
  if (mode === "off") return false
  if (mode === "always") return true
  return isUnfocused()
}
