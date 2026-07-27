"use client"

import { useState, useEffect, useRef } from "react"
import { Check, ChevronDown } from "lucide-react"
import { getIntegrationIcon, getIntegrationLabel } from "../SkillsPage"

/* -- Pill Component -- */

interface PillProps {
  value: string
  onClick: () => void
  active?: boolean
  minWidth?: string
}

function Pill({ value, onClick, active, minWidth }: PillProps) {
  return (
    <button
      onClick={onClick}
      className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg text-[13px] font-medium transition-colors"
      style={{
        background: active ? "rgba(124,58,237,0.15)" : "var(--surface-elevated)",
        border: `1px solid ${active ? "rgba(124,58,237,0.4)" : "var(--border)"}`,
        color: active ? "#a78bfa" : "var(--text-primary)",
        minWidth: minWidth || "auto",
      }}
    >
      {value}
      <ChevronDown size={12} style={{ opacity: 0.5 }} />
    </button>
  )
}

/* -- Pill Dropdown -- */

interface PillDropdownProps {
  options: { value: string; label: string }[]
  value: string
  onChange: (val: string) => void
  onClose: () => void
}

function PillDropdown({ options, value, onChange, onClose }: PillDropdownProps) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    document.addEventListener("mousedown", handler)
    return () => document.removeEventListener("mousedown", handler)
  }, [onClose])

  return (
    <div
      ref={ref}
      className="absolute top-full left-0 mt-1 rounded-lg py-1 z-50 min-w-[120px]"
      style={{
        background: "var(--surface-elevated)",
        border: "1px solid var(--border)",
        boxShadow: "0 8px 24px rgba(0,0,0,0.4)",
      }}
    >
      {options.map((opt) => (
        <button
          key={opt.value}
          onClick={() => { onChange(opt.value); onClose() }}
          className="w-full text-left px-3 py-1.5 text-[13px] transition-colors flex items-center gap-2"
          style={{
            color: opt.value === value ? "#a78bfa" : "var(--text-primary)",
            background: "transparent",
          }}
          onMouseEnter={(e) => { e.currentTarget.style.background = "var(--surface-hover)" }}
          onMouseLeave={(e) => { e.currentTarget.style.background = "transparent" }}
        >
          {opt.value === value && <Check size={12} />}
          <span className={opt.value === value ? "" : "ml-5"}>{opt.label}</span>
        </button>
      ))}
    </div>
  )
}

/* -- Days-of-week Multi-select Pill --

   Compact multi-select for the schedule_days_of_week filter. Top row holds
   one-tap presets (All / Weekdays / Weekends); below it is a 7-day toggle
   row for custom selections. "All days" persists as null so the scheduler
   skips the weekday filter entirely. */

interface DaysOfWeekPickerProps {
  value: string[] | null
  onChange: (val: string[] | null) => void
  onClose: () => void
}

function DaysOfWeekPicker({ value, onChange, onClose }: DaysOfWeekPickerProps) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    document.addEventListener("mousedown", handler)
    return () => document.removeEventListener("mousedown", handler)
  }, [onClose])

  const selected = new Set(value || [])
  const isAll = !value || value.length === 0 || value.length === 7

  const togglePresetAll = () => onChange(null)
  const togglePresetWeekdays = () => onChange(["MO", "TU", "WE", "TH", "FR"])
  const togglePresetWeekends = () => onChange(["SA", "SU"])

  const toggleDay = (d: string) => {
    const next = new Set(value || [])
    if (next.has(d)) next.delete(d)
    else next.add(d)
    // Empty set -> null (no filter, "all days") instead of "never runs".
    if (next.size === 0 || next.size === 7) {
      onChange(null)
    } else {
      onChange(["MO", "TU", "WE", "TH", "FR", "SA", "SU"].filter((x) => next.has(x)))
    }
  }

  return (
    <div
      ref={ref}
      className="absolute top-full left-0 mt-1 rounded-lg py-2 z-50"
      style={{
        background: "var(--surface-elevated)",
        border: "1px solid var(--border)",
        boxShadow: "0 8px 24px rgba(0,0,0,0.4)",
        minWidth: "260px",
      }}
    >
      <div className="px-2 pb-2 flex gap-1" style={{ borderBottom: "1px solid var(--border)" }}>
        {WEEKDAY_PRESETS.map((p) => {
          const active =
            (p.id === "all" && isAll) ||
            (p.id !== "all" && p.value !== null && value !== null && value.length === p.value.length && p.value.every((d) => selected.has(d)))
          return (
            <button
              key={p.id}
              onClick={() => {
                if (p.id === "all") togglePresetAll()
                else if (p.id === "weekdays") togglePresetWeekdays()
                else if (p.id === "weekends") togglePresetWeekends()
              }}
              className="flex-1 text-[12px] py-1.5 rounded-md transition-colors"
              style={{
                background: active ? "rgba(167,139,250,0.15)" : "transparent",
                color: active ? "#a78bfa" : "var(--text-secondary)",
                border: active ? "1px solid rgba(167,139,250,0.4)" : "1px solid var(--border)",
              }}
            >
              {p.label}
            </button>
          )
        })}
      </div>
      <div className="px-2 pt-2 flex gap-1">
        {[
          { v: "MO", l: "Mon" },
          { v: "TU", l: "Tue" },
          { v: "WE", l: "Wed" },
          { v: "TH", l: "Thu" },
          { v: "FR", l: "Fri" },
          { v: "SA", l: "Sat" },
          { v: "SU", l: "Sun" },
        ].map(({ v, l }) => {
          const active = !isAll && selected.has(v)
          return (
            <button
              key={v}
              onClick={() => toggleDay(v)}
              className="flex-1 text-[12px] py-1.5 rounded-md transition-colors"
              style={{
                background: active ? "rgba(167,139,250,0.15)" : "var(--surface-hover)",
                color: active ? "#a78bfa" : "var(--text-secondary)",
                border: active ? "1px solid rgba(167,139,250,0.4)" : "1px solid transparent",
              }}
            >
              {l}
            </button>
          )
        })}
      </div>
    </div>
  )
}

/* -- Inline Text Input Pill -- */

interface PillInputProps {
  value: string
  onChange: (val: string) => void
  onClose: () => void
  type?: "text" | "number" | "date" | "time"
  width?: string
}

function PillInput({ value, onChange, onClose, type = "text", width }: PillInputProps) {
  const [local, setLocal] = useState(value)
  const ref = useRef<HTMLInputElement>(null)

  useEffect(() => { ref.current?.focus() }, [])

  const commit = () => {
    if (local.trim()) onChange(local.trim())
    onClose()
  }

  return (
    <div className="absolute top-full left-0 mt-1 z-50">
      <input
        ref={ref}
        type={type}
        value={local}
        onChange={(e) => setLocal(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => { if (e.key === "Enter") commit(); if (e.key === "Escape") onClose() }}
        className="px-3 py-1.5 rounded-lg text-[13px] outline-none"
        style={{
          background: "var(--surface-elevated)",
          border: "1px solid rgba(124,58,237,0.4)",
          color: "var(--text-primary)",
          width: width || "120px",
          boxShadow: "0 8px 24px rgba(0,0,0,0.4)",
        }}
      />
    </div>
  )
}

/* -- Until Pill (date picker with "Forever" clear) -- */

export interface UntilPillProps {
  /** ISO datetime string or null for "Forever". */
  value: string | null
  onChange: (iso: string | null) => void
}

// We persist the selected day as end-of-day UTC (23:59:59Z). Both display and
// <input type="date"> round-trips must read the UTC calendar date, otherwise users
// in UTC+ timezones see the next day in the pill (and users in UTC- see the previous).
function formatUntilPill(iso: string | null): string {
  if (!iso) return "Forever"
  const d = new Date(iso)
  if (isNaN(d.getTime())) return "Forever"
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
  return `${months[d.getUTCMonth()]} ${d.getUTCDate()}, ${d.getUTCFullYear()}`
}

function localDateInput(d: Date): string {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, "0")
  const day = String(d.getDate()).padStart(2, "0")
  return `${y}-${m}-${day}`
}

// Convert the stored UTC end-of-day ISO to yyyy-MM-dd for <input type="date">.
function isoToDateInput(iso: string | null): string {
  if (!iso) return localDateInput(new Date())
  const d = new Date(iso)
  if (isNaN(d.getTime())) return localDateInput(new Date())
  const y = d.getUTCFullYear()
  const m = String(d.getUTCMonth() + 1).padStart(2, "0")
  const day = String(d.getUTCDate()).padStart(2, "0")
  return `${y}-${m}-${day}`
}

export function UntilPill({ value, onChange }: UntilPillProps) {
  const [open, setOpen] = useState(false)
  const panelRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener("mousedown", handler)
    return () => document.removeEventListener("mousedown", handler)
  }, [open])

  const handleDate = (dateStr: string) => {
    // Treat as end-of-day UTC so the workflow runs through the selected date.
    if (!dateStr) return
    const iso = new Date(`${dateStr}T23:59:59.000Z`).toISOString()
    onChange(iso)
    setOpen(false)
  }

  return (
    <div className="flex items-center flex-wrap gap-2 text-[14px]" style={{ color: "var(--text-secondary)" }}>
      <span>Until</span>
      <div className="relative">
        <Pill
          value={formatUntilPill(value)}
          onClick={() => setOpen((o) => !o)}
          active={open}
        />
        {open && (
          <div
            ref={panelRef}
            className="absolute top-full left-0 mt-1 z-50 rounded-lg py-2 px-2 flex flex-col gap-2"
            style={{
              background: "var(--surface-elevated)",
              border: "1px solid var(--border)",
              boxShadow: "0 8px 24px rgba(0,0,0,0.4)",
              minWidth: 200,
            }}
          >
            <input
              type="date"
              value={isoToDateInput(value)}
              min={localDateInput(new Date())}
              onChange={(e) => handleDate(e.target.value)}
              className="px-2 py-1.5 rounded-md text-[13px] outline-none"
              style={{
                background: "var(--surface-card)",
                border: "1px solid var(--border)",
                color: "var(--text-primary)",
                colorScheme: "dark",
              }}
            />
            <button
              onClick={() => { onChange(null); setOpen(false) }}
              className="text-left px-2 py-1.5 rounded-md text-[13px] transition-colors"
              style={{
                color: value === null ? "#a78bfa" : "var(--text-secondary)",
                background: "transparent",
              }}
              onMouseEnter={(e) => { e.currentTarget.style.background = "var(--surface-hover)" }}
              onMouseLeave={(e) => { e.currentTarget.style.background = "transparent" }}
            >
              {value === null ? "✓ Forever" : "Clear (run forever)"}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

/* -- Schedule Pill Builder -- */

export interface ScheduleBuilderProps {
  startDate: string
  interval: number
  unit: string
  dayOfWeek: string | null
  daysOfWeek: string[] | null
  time: string
  onChange: (fields: Partial<{
    schedule_start_date: string
    schedule_interval: number
    schedule_unit: string
    schedule_day_of_week: string | null
    schedule_days_of_week: string[] | null
    schedule_time: string
  }>) => void
}

const UNIT_OPTIONS = [
  { value: "minute", label: "minute" },
  { value: "hour", label: "hour" },
  { value: "day", label: "day" },
  { value: "week", label: "week" },
]

// Runs take ~30-90s, so a sub-5-minute cadence would overlap. Mirrors the
// backend MIN_MINUTE_INTERVAL clamp.
const MIN_MINUTE_INTERVAL = 5

// Sub-daily units repeat through the day and have no fixed time-of-day anchor.
const isIntervalUnit = (u: string) => u === "minute" || u === "hour"

const DAY_OPTIONS = [
  { value: "MO", label: "Monday" },
  { value: "TU", label: "Tuesday" },
  { value: "WE", label: "Wednesday" },
  { value: "TH", label: "Thursday" },
  { value: "FR", label: "Friday" },
  { value: "SA", label: "Saturday" },
  { value: "SU", label: "Sunday" },
]

function formatDatePill(dateStr: string): string {
  if (!dateStr) return "Today"
  const d = new Date(dateStr + "T00:00:00")
  if (isNaN(d.getTime())) return "Today"
  const today = new Date()
  if (d.toDateString() === today.toDateString()) return "Today"
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
  return `${months[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()}`
}

function formatTimePill(time24: string): string {
  if (!time24) return "09:00 AM"
  const [h, m] = time24.split(":").map(Number)
  const ampm = h >= 12 ? "PM" : "AM"
  const h12 = h % 12 || 12
  return `${String(h12).padStart(2, "0")}:${String(m).padStart(2, "0")} ${ampm}`
}

// Quick presets for the day-unit weekday filter. "All days" maps to null
// (no filter). "Custom" keeps the existing array as-is so the user can
// open the dropdown and toggle individual days.
const WEEKDAY_PRESETS: { id: string; label: string; value: string[] | null }[] = [
  { id: "all", label: "All days", value: null },
  { id: "weekdays", label: "Weekdays", value: ["MO", "TU", "WE", "TH", "FR"] },
  { id: "weekends", label: "Weekends", value: ["SA", "SU"] },
]

function summarizeDays(days: string[] | null): string {
  if (!days || days.length === 0) return "All days"
  const set = new Set(days)
  const weekdays = ["MO", "TU", "WE", "TH", "FR"]
  const weekends = ["SA", "SU"]
  if (set.size === 7) return "All days"
  if (weekdays.every((d) => set.has(d)) && set.size === 5) return "Weekdays"
  if (weekends.every((d) => set.has(d)) && set.size === 2) return "Weekends"
  // Custom — short labels Mon/Tue/...
  const short: Record<string, string> = { MO: "Mon", TU: "Tue", WE: "Wed", TH: "Thu", FR: "Fri", SA: "Sat", SU: "Sun" }
  const ordered = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"].filter((d) => set.has(d))
  return ordered.map((d) => short[d]).join(", ")
}

export function ScheduleBuilder({ startDate, interval, unit, dayOfWeek, daysOfWeek, time, onChange }: ScheduleBuilderProps) {
  const [openPill, setOpenPill] = useState<string | null>(null)

  return (
    <div className="flex items-center flex-wrap gap-2 text-[14px]" style={{ color: "var(--text-secondary)" }}>
      <span>From</span>
      <div className="relative">
        <Pill
          value={formatDatePill(startDate)}
          onClick={() => setOpenPill(openPill === "date" ? null : "date")}
          active={openPill === "date"}
        />
        {openPill === "date" && (
          <PillInput
            value={startDate || new Date().toISOString().split("T")[0]}
            onChange={(v) => onChange({ schedule_start_date: v })}
            onClose={() => setOpenPill(null)}
            type="date"
            width="160px"
          />
        )}
      </div>

      <span>run every</span>

      <div className="relative">
        <Pill
          value={String(interval)}
          onClick={() => setOpenPill(openPill === "interval" ? null : "interval")}
          active={openPill === "interval"}
          minWidth="40px"
        />
        {openPill === "interval" && (
          <PillInput
            value={String(interval)}
            onChange={(v) => {
              const floor = unit === "minute" ? MIN_MINUTE_INTERVAL : 1
              onChange({ schedule_interval: Math.max(floor, parseInt(v) || floor) })
            }}
            onClose={() => setOpenPill(null)}
            type="number"
            width="80px"
          />
        )}
      </div>

      <div className="relative">
        <Pill
          value={unit + (interval > 1 ? "s" : "")}
          onClick={() => setOpenPill(openPill === "unit" ? null : "unit")}
          active={openPill === "unit"}
        />
        {openPill === "unit" && (
          <PillDropdown
            options={UNIT_OPTIONS}
            value={unit}
            onChange={(v) => {
              const updates: any = { schedule_unit: v }
              if (v !== "week") updates.schedule_day_of_week = null
              // Bump interval up to the minute floor when switching to minutes.
              if (v === "minute" && interval < MIN_MINUTE_INTERVAL) {
                updates.schedule_interval = MIN_MINUTE_INTERVAL
              }
              onChange(updates)
            }}
            onClose={() => setOpenPill(null)}
          />
        )}
      </div>

      {unit === "week" && (
        <>
          <span>on</span>
          <div className="relative">
            <Pill
              value={dayOfWeek || "MO"}
              onClick={() => setOpenPill(openPill === "day" ? null : "day")}
              active={openPill === "day"}
            />
            {openPill === "day" && (
              <PillDropdown
                options={DAY_OPTIONS}
                value={dayOfWeek || "MO"}
                onChange={(v) => onChange({ schedule_day_of_week: v })}
                onClose={() => setOpenPill(null)}
              />
            )}
          </div>
        </>
      )}

      {/* Restrict which weekdays the schedule runs on. Shown for every-day
          (interval 1) and for sub-daily intervals ("every 30 min on weekdays").
          Hidden for every-N-days because "every 3 days on weekdays" is
          ambiguous and would need extra UX to disambiguate. */}
      {((unit === "day" && interval === 1) || isIntervalUnit(unit)) && (
        <>
          <span>on</span>
          <div className="relative">
            <Pill
              value={summarizeDays(daysOfWeek)}
              onClick={() => setOpenPill(openPill === "days" ? null : "days")}
              active={openPill === "days"}
              minWidth="80px"
            />
            {openPill === "days" && (
              <DaysOfWeekPicker
                value={daysOfWeek}
                onChange={(v) => onChange({ schedule_days_of_week: v })}
                onClose={() => setOpenPill(null)}
              />
            )}
          </div>
        </>
      )}

      {/* Time-of-day anchor only applies to day/week schedules. Sub-daily
          intervals (minute/hour) repeat through the day, so no "at <time>". */}
      {!isIntervalUnit(unit) && (
        <>
          <span>at</span>
          <div className="relative">
            <Pill
              value={formatTimePill(time)}
              onClick={() => setOpenPill(openPill === "time" ? null : "time")}
              active={openPill === "time"}
            />
            {openPill === "time" && (
              <PillInput
                value={time || "09:00"}
                onChange={(v) => onChange({ schedule_time: v })}
                onClose={() => setOpenPill(null)}
                type="time"
                width="140px"
              />
            )}
          </div>
        </>
      )}
    </div>
  )
}

/* -- Trigger Pill Builder -- */

const TRIGGER_INTEGRATIONS = [
  { value: "gmail", label: "Gmail" },
  { value: "google_calendar", label: "Google Calendar" },
  { value: "slack", label: "Slack" },
]

const TRIGGER_EVENTS: Record<string, { value: string; label: string }[]> = {
  gmail: [
    { value: "new_email", label: "new email" },
  ],
  google_calendar: [
    { value: "before_event", label: "before event" },
  ],
  slack: [
    { value: "new_message", label: "new message" },
  ],
}

export interface TriggerBuilderProps {
  integration: string | null
  event: string | null
  minutesBefore: number | null
  filter: Record<string, any> | null
  onChange: (fields: Partial<{
    trigger_integration: string | null
    trigger_event: string | null
    trigger_minutes_before: number | null
    trigger_filter: Record<string, any> | null
  }>) => void
}

export function TriggerBuilder({ integration, event, minutesBefore, filter, onChange }: TriggerBuilderProps) {
  const [openPill, setOpenPill] = useState<string | null>(null)
  const integKey = (integration || "gmail").toLowerCase().replace(/\s+/g, "_")
  const events = TRIGGER_EVENTS[integKey] || TRIGGER_EVENTS.gmail
  const isCalendar = integKey === "google_calendar"

  return (
    <div className="flex items-center flex-wrap gap-2 text-[14px]" style={{ color: "var(--text-secondary)" }}>
      <span>Trigger:</span>

      {/* Integration pill */}
      <div className="relative">
        <button
          onClick={() => setOpenPill(openPill === "integration" ? null : "integration")}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[13px] font-medium transition-colors"
          style={{
            background: openPill === "integration" ? "rgba(124,58,237,0.15)" : "var(--surface-elevated)",
            border: `1px solid ${openPill === "integration" ? "rgba(124,58,237,0.4)" : "var(--border)"}`,
            color: openPill === "integration" ? "#a78bfa" : "var(--text-primary)",
          }}
        >
          {getIntegrationIcon(integKey)}
          {getIntegrationLabel(integKey)}
          <ChevronDown size={12} style={{ opacity: 0.5 }} />
        </button>
        {openPill === "integration" && (
          <PillDropdown
            options={TRIGGER_INTEGRATIONS}
            value={integKey}
            onChange={(v) => {
              onChange({
                trigger_integration: v,
                trigger_event: null,
                trigger_minutes_before: v === "google_calendar" ? 15 : null,
              })
            }}
            onClose={() => setOpenPill(null)}
          />
        )}
      </div>

      <span>on</span>

      {/* Event pill (or minutes-before for calendar) */}
      {isCalendar ? (
        <>
          <div className="relative">
            <Pill
              value={String(minutesBefore ?? 15)}
              onClick={() => setOpenPill(openPill === "minutes" ? null : "minutes")}
              active={openPill === "minutes"}
              minWidth="40px"
            />
            {openPill === "minutes" && (
              <PillInput
                value={String(minutesBefore ?? 15)}
                onChange={(v) => onChange({ trigger_minutes_before: Math.max(0, parseInt(v) || 0) })}
                onClose={() => setOpenPill(null)}
                type="number"
                width="80px"
              />
            )}
          </div>
          <span>minutes before every event</span>
        </>
      ) : (
        <div className="relative">
          <Pill
            value={events.find(e => e.value === event)?.label || events[0]?.label || "event"}
            onClick={() => setOpenPill(openPill === "event" ? null : "event")}
            active={openPill === "event"}
          />
          {openPill === "event" && (
            <PillDropdown
              options={events}
              value={event || events[0]?.value || ""}
              onChange={(v) => onChange({ trigger_event: v })}
              onClose={() => setOpenPill(null)}
            />
          )}
        </div>
      )}

      {/* Filter pill */}
      <span>for</span>
      <div className="relative">
        <Pill
          value={filter?.label || "all"}
          onClick={() => setOpenPill(openPill === "filter" ? null : "filter")}
          active={openPill === "filter"}
        />
        {openPill === "filter" && (
          <PillInput
            value={filter?.label || ""}
            onChange={(v) => onChange({ trigger_filter: v ? { label: v } : null })}
            onClose={() => setOpenPill(null)}
            width="200px"
          />
        )}
      </div>
    </div>
  )
}
