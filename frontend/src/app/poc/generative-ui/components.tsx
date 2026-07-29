"use client"

import type { ReactNode } from "react"
import { Plane, ArrowRight } from "lucide-react"

// ---- tokens (mirror the catalog the model sees) -----------------------------

const SPACING = { xs: 4, sm: 8, md: 16, lg: 24, xl: 32 } as const
const RADIUS = { sm: 6, md: 10, lg: 14, pill: 999 } as const

type Spacing = keyof typeof SPACING
type Radius = keyof typeof RADIUS
type Tone = "neutral" | "primary" | "success" | "danger" | "warning" | "muted"

const TONE_BG: Record<Tone, string> = {
  neutral: "rgba(255,255,255,0.06)",
  primary: "rgba(124, 58, 237, 0.15)",
  success: "rgba(34, 197, 94, 0.15)",
  danger: "rgba(239, 68, 68, 0.15)",
  warning: "rgba(245, 158, 11, 0.15)",
  muted: "rgba(255,255,255,0.04)",
}
const TONE_FG: Record<Tone, string> = {
  neutral: "var(--text-primary)",
  primary: "#a78bfa",
  success: "#4ade80",
  danger: "#f87171",
  warning: "#fbbf24",
  muted: "var(--text-tertiary)",
}

// ---- layout primitives ------------------------------------------------------

export function Stack({
  direction,
  gap = "md",
  align,
  justify,
  wrap,
  children,
}: {
  direction: "row" | "col"
  gap?: Spacing
  align?: "start" | "center" | "end" | "stretch"
  justify?: "start" | "center" | "end" | "between"
  wrap?: boolean
  children?: ReactNode
}) {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: direction === "row" ? "row" : "column",
        gap: SPACING[gap],
        alignItems: align ?? (direction === "row" ? "center" : "stretch"),
        justifyContent:
          justify === "between"
            ? "space-between"
            : justify === "center"
              ? "center"
              : justify === "end"
                ? "flex-end"
                : "flex-start",
        flexWrap: wrap ? "wrap" : "nowrap",
        minWidth: 0,
      }}
    >
      {children}
    </div>
  )
}

export function Card({
  padding = "md",
  tone = "surface",
  radius = "lg",
  children,
}: {
  padding?: Spacing
  tone?: "surface" | "elevated"
  radius?: Radius
  children?: ReactNode
}) {
  return (
    <div
      style={{
        background: tone === "elevated" ? "var(--surface-elevated)" : "var(--surface-card)",
        border: "1px solid var(--border)",
        borderRadius: RADIUS[radius],
        padding: SPACING[padding],
      }}
    >
      {children}
    </div>
  )
}

// ---- text primitives --------------------------------------------------------

export function Heading({ level, text }: { level: 1 | 2 | 3; text: string }) {
  const size = level === 1 ? 28 : level === 2 ? 20 : 16
  const weight = 600
  return (
    <div style={{ color: "var(--text-primary)", fontSize: size, fontWeight: weight, lineHeight: 1.2 }}>
      {text}
    </div>
  )
}

export function Text({
  variant = "body",
  tone = "neutral",
  text,
}: {
  variant?: "body" | "caption" | "label"
  tone?: Tone
  text: string
}) {
  const size = variant === "caption" ? 12 : variant === "label" ? 13 : 14
  const weight = variant === "label" ? 500 : 400
  const color = tone === "neutral" ? "var(--text-primary)" : TONE_FG[tone]
  return <div style={{ color, fontSize: size, fontWeight: weight, lineHeight: 1.4 }}>{text}</div>
}

export function Pill({ label, tone = "neutral" }: { label: string; tone?: Tone }) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        padding: "4px 10px",
        borderRadius: RADIUS.pill,
        background: TONE_BG[tone],
        color: TONE_FG[tone],
        fontSize: 12,
        fontWeight: 500,
        whiteSpace: "nowrap",
      }}
    >
      {label}
    </span>
  )
}

// ---- flight primitives ------------------------------------------------------

export function AirportTime({
  code,
  city,
  time,
  date,
  terminal,
}: {
  code: string
  city: string
  time: string
  date?: string
  terminal?: string
}) {
  return (
    <div style={{ minWidth: 90 }}>
      <div style={{ color: "var(--text-primary)", fontSize: 22, fontWeight: 600, lineHeight: 1.1 }}>
        {time}
      </div>
      <div style={{ color: "var(--text-secondary)", fontSize: 13, fontWeight: 500, marginTop: 2 }}>
        {code} <span style={{ color: "var(--text-tertiary)" }}>· {city}</span>
      </div>
      {(date || terminal) && (
        <div style={{ color: "var(--text-tertiary)", fontSize: 11, marginTop: 4 }}>
          {[date, terminal && `Terminal ${terminal}`].filter(Boolean).join(" · ")}
        </div>
      )}
    </div>
  )
}

export function DurationBar({
  durationLabel,
  stops,
  stopLabels,
}: {
  durationLabel: string
  stops: number
  stopLabels?: string[]
}) {
  return (
    <div style={{ flex: 1, minWidth: 120, paddingTop: 4 }}>
      <div style={{ textAlign: "center", color: "var(--text-secondary)", fontSize: 12, marginBottom: 6 }}>
        {durationLabel}
      </div>
      <div style={{ position: "relative", height: 2, background: "var(--border)", borderRadius: 2 }}>
        <Plane
          size={14}
          style={{
            position: "absolute",
            right: -4,
            top: -6,
            color: "var(--text-secondary)",
            transform: "rotate(90deg)",
          }}
        />
        {Array.from({ length: stops }).map((_, i) => {
          const pct = ((i + 1) / (stops + 1)) * 100
          return (
            <div
              key={i}
              style={{
                position: "absolute",
                left: `${pct}%`,
                top: -3,
                width: 8,
                height: 8,
                borderRadius: "50%",
                background: "var(--surface)",
                border: "2px solid var(--text-secondary)",
                transform: "translateX(-50%)",
              }}
            />
          )
        })}
      </div>
      <div
        style={{
          textAlign: "center",
          color: stops === 0 ? "#4ade80" : "var(--text-tertiary)",
          fontSize: 11,
          marginTop: 6,
        }}
      >
        {stops === 0 ? "Non-stop" : `${stops} stop${stops > 1 ? "s" : ""}`}
        {stopLabels && stopLabels.length > 0 ? ` · ${stopLabels.join(", ")}` : ""}
      </div>
    </div>
  )
}

export function AirlineBadge({
  code,
  name,
  flightNumber,
}: {
  code: string
  name: string
  flightNumber: string
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 130 }}>
      <div
        style={{
          width: 36,
          height: 36,
          borderRadius: 8,
          background: "var(--surface-elevated)",
          border: "1px solid var(--border)",
          color: "var(--text-primary)",
          fontSize: 13,
          fontWeight: 600,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          letterSpacing: 0.5,
        }}
      >
        {code}
      </div>
      <div style={{ minWidth: 0 }}>
        <div style={{ color: "var(--text-primary)", fontSize: 13, fontWeight: 500, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
          {name}
        </div>
        <div style={{ color: "var(--text-tertiary)", fontSize: 11, marginTop: 1 }}>
          {flightNumber}
        </div>
      </div>
    </div>
  )
}

const CURRENCY_SYMBOLS: Record<string, string> = {
  INR: "₹",
  USD: "$",
  EUR: "€",
  GBP: "£",
  AED: "AED ",
}

export function PriceTag({
  amount,
  currency,
  perPax,
  note,
}: {
  amount: number
  currency: string
  perPax?: boolean
  note?: string
}) {
  const sym = CURRENCY_SYMBOLS[currency] ?? `${currency} `
  return (
    <div style={{ textAlign: "right", minWidth: 110 }}>
      <div style={{ color: "var(--text-primary)", fontSize: 20, fontWeight: 600 }}>
        {sym}
        {amount.toLocaleString()}
      </div>
      {perPax && (
        <div style={{ color: "var(--text-tertiary)", fontSize: 11, marginTop: 2 }}>per traveller</div>
      )}
      {note && <div style={{ color: "#a78bfa", fontSize: 11, marginTop: 2, fontWeight: 500 }}>{note}</div>}
    </div>
  )
}

export function Button({
  label,
  intent = "primary",
  onClick,
}: {
  label: string
  intent?: "primary" | "secondary" | "ghost"
  onClick?: () => void
}) {
  const base = {
    primary: { bg: "#7c3aed", fg: "#fff", border: "transparent" },
    secondary: { bg: "var(--surface-elevated)", fg: "var(--text-primary)", border: "var(--border)" },
    ghost: { bg: "transparent", fg: "var(--text-secondary)", border: "transparent" },
  }[intent]
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        background: base.bg,
        color: base.fg,
        border: `1px solid ${base.border}`,
        borderRadius: RADIUS.md,
        padding: "8px 14px",
        fontSize: 13,
        fontWeight: 500,
        cursor: "pointer",
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
      }}
    >
      {label}
      {intent === "primary" && <ArrowRight size={14} />}
    </button>
  )
}
