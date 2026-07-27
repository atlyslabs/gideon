import type { Todo } from "../../types"

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

export const PRIORITY_COLORS: Record<string, string> = {
  low: "#94a3b8",
  medium: "#f59e0b",
  high: "#f97316",
  urgent: "#ef4444",
}

export const PRIORITY_LABELS: Record<string, string> = {
  none: "No priority",
  low: "Low",
  medium: "Medium",
  high: "High",
  urgent: "Urgent",
}

export const PRIORITY_CYCLE: Todo["priority"][] = ["none", "low", "medium", "high", "urgent"]

export const PRIORITY_ORDER: Record<string, number> = {
  urgent: 4,
  high: 3,
  medium: 2,
  low: 1,
  none: 0,
}

export const DEFAULT_WIDTH = 320
export const MIN_WIDTH = 280
export const MAX_WIDTH = 600

export type Tab = "todo" | "emails" | "meetings" | "console"

export const TAB_LABELS: Record<Tab, string> = {
  todo: "To Do",
  emails: "Emails",
  meetings: "Meetings",
  console: "Console",
}

export const AVATAR_PALETTE = [
  { bg: "rgba(99,102,241,0.35)", text: "#a5b4fc" },
  { bg: "rgba(139,92,246,0.35)", text: "#c4b5fd" },
  { bg: "rgba(59,130,246,0.35)", text: "#93c5fd" },
  { bg: "rgba(6,182,212,0.35)", text: "#67e8f9" },
  { bg: "rgba(16,185,129,0.35)", text: "#6ee7b7" },
  { bg: "rgba(245,158,11,0.35)", text: "#fcd34d" },
  { bg: "rgba(239,68,68,0.35)", text: "#fca5a5" },
  { bg: "rgba(236,72,153,0.35)", text: "#f9a8d4" },
]

/** Default colors for meeting dots when no color is provided from GCal */
export const MEETING_DOT_COLORS = [
  "#6366f1", "#8b5cf6", "#3b82f6", "#06b6d4",
  "#10b981", "#f59e0b", "#ef4444", "#ec4899",
]

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Optional base URL for weather background videos, e.g. a CDN or a folder you
 * drop into `public/`. No videos ship with this project, so the panel falls back
 * to a CSS gradient (see `getWeatherGradient`) unless you configure this.
 *
 * Expected filenames at the base: night, cloudy, sunny, rainy, thunderstorm (.mp4).
 */
const WEATHER_VIDEO_BASE = (process.env.NEXT_PUBLIC_WEATHER_VIDEO_BASE || "").replace(/\/$/, "")

function weatherKey(condition: string): string {
  const hour = new Date().getHours()
  const isNight = hour < 6 || hour > 19
  if (isNight) return "night"
  switch (condition) {
    case "clouds":
    case "fog":
    case "snow":
      return "cloudy"
    case "rain":
    case "drizzle":
      return "rainy"
    case "thunderstorm":
      return "thunderstorm"
    default:
      return "sunny"
  }
}

/** Video URL for the condition, or "" when no video base is configured. */
export function getWeatherVideo(condition: string): string {
  if (!WEATHER_VIDEO_BASE) return ""
  return `${WEATHER_VIDEO_BASE}/${weatherKey(condition)}.mp4`
}

/** CSS gradient used as the weather backdrop when no video is configured. */
export function getWeatherGradient(condition: string): string {
  switch (weatherKey(condition)) {
    case "night":
      return "linear-gradient(160deg, #0f172a 0%, #1e293b 55%, #334155 100%)"
    case "cloudy":
      return "linear-gradient(160deg, #64748b 0%, #94a3b8 55%, #cbd5e1 100%)"
    case "rainy":
      return "linear-gradient(160deg, #334155 0%, #475569 55%, #64748b 100%)"
    case "thunderstorm":
      return "linear-gradient(160deg, #1e1b4b 0%, #312e81 55%, #4338ca 100%)"
    default:
      return "linear-gradient(160deg, #0284c7 0%, #38bdf8 55%, #7dd3fc 100%)"
  }
}

export function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
}

export function formatDate(iso: string): string {
  const d = new Date(iso)
  const now = new Date()
  const diffMs = now.getTime() - d.getTime()
  const diffMin = Math.floor(diffMs / 60000)
  if (diffMin < 1) return "now"
  if (diffMin < 60) return `${diffMin}m ago`
  const diffHr = Math.floor(diffMin / 60)
  if (diffHr < 24) return `${diffHr}h ago`
  return d.toLocaleDateString([], { month: "short", day: "numeric" })
}

export function sortTodos(todos: Todo[]): Todo[] {
  const uncompleted = todos
    .filter((t) => !t.completed)
    .sort((a, b) => {
      const pDiff = (PRIORITY_ORDER[b.priority] || 0) - (PRIORITY_ORDER[a.priority] || 0)
      if (pDiff !== 0) return pDiff
      return new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
    })
  const completed = todos
    .filter((t) => t.completed)
    .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
  return [...uncompleted, ...completed]
}

export function getSnoozeOptions(): { label: string; value: string }[] {
  const now = new Date()

  const laterToday = new Date(now.getTime() + 4 * 60 * 60 * 1000)
  const sameDay = laterToday.getDate() === now.getDate() && laterToday.getMonth() === now.getMonth()

  const tomorrow = new Date(now)
  tomorrow.setDate(tomorrow.getDate() + 1)
  tomorrow.setHours(9, 0, 0, 0)

  const nextMonday = new Date(now)
  const dayOfWeek = nextMonday.getDay()
  const daysUntilMonday = dayOfWeek === 0 ? 1 : 8 - dayOfWeek
  nextMonday.setDate(nextMonday.getDate() + daysUntilMonday)
  nextMonday.setHours(9, 0, 0, 0)

  const options: { label: string; value: string }[] = []
  if (sameDay) {
    options.push({ label: "Later today", value: laterToday.toISOString() })
  }
  options.push({ label: "Tomorrow", value: tomorrow.toISOString() })
  options.push({ label: "Next week", value: nextMonday.toISOString() })
  return options
}

export function formatSnoozeDate(iso: string): string {
  const target = new Date(iso)
  const now = new Date()
  const diffMs = target.getTime() - now.getTime()

  if (diffMs <= 0) return "Now"

  const diffMin = Math.floor(diffMs / 60000)
  if (diffMin < 60) return `in ${diffMin}m`
  const diffHr = Math.floor(diffMin / 60)
  if (diffHr < 24) return `in ${diffHr}h`

  const tomorrow = new Date(now)
  tomorrow.setDate(tomorrow.getDate() + 1)
  if (target.toDateString() === tomorrow.toDateString()) {
    return `Tomorrow ${target.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", hour12: true })}`
  }

  return target.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })
}

export function formatMeetingTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
  })
}

export function hashString(str: string): number {
  let hash = 0
  for (let i = 0; i < str.length; i++) {
    const char = str.charCodeAt(i)
    hash = ((hash << 5) - hash) + char
    hash |= 0
  }
  return Math.abs(hash)
}

export function getAvatarColor(email: string): { bg: string; text: string } {
  return AVATAR_PALETTE[hashString(email) % AVATAR_PALETTE.length]
}

export function requestNotificationPermission(): void {
  if ("Notification" in window && Notification.permission === "default") {
    Notification.requestPermission()
  }
}

export function fireCompletionNotification(todoText: string): void {
  if ("Notification" in window && Notification.permission === "granted") {
    new Notification("Gideon finished working", {
      body: `Task: ${todoText}`,
      icon: "/logo.png",
    })
  }
}

// Deep link to a Slack thread. The `team` parameter is optional: with it the
// link opens in a specific workspace, without it Slack uses whichever one the
// desktop app currently has active. Set NEXT_PUBLIC_SLACK_TEAM_ID to your
// workspace id (the T0... value) if your users belong to more than one.
export function slackThreadHref(channelId: string, threadTs: string): string {
  const team = process.env.NEXT_PUBLIC_SLACK_TEAM_ID
  const teamParam = team ? `team=${encodeURIComponent(team)}&` : ""
  return `slack://channel?${teamParam}id=${encodeURIComponent(channelId)}&message=${encodeURIComponent(threadTs)}`
}
