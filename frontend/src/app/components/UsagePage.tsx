"use client"

import { useState } from "react"
import { ArrowLeft, BarChart3, ChevronLeft, ChevronRight, Loader2 } from "lucide-react"
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  Legend,
} from "recharts"
import useUsage, { type GroupBy, type DailyEntry } from "../hooks/useUsage"

interface UsagePageProps {
  onBack: () => void
}

/* -- Cost estimation ----------------------------------- */

// USD per million tokens. Keep in sync with
// services/gideon/services/anthropic_pricing.py:_PRICING_PER_M.
// Verified against the same May 2026 list prices used on the backend so
// /usage shows the same cost the backend computes for every row.
const MODEL_RATES: Record<string, { input: number; output: number }> = {
  // Anthropic
  "claude-opus-4-6": { input: 15, output: 75 },
  "claude-opus-4-7": { input: 15, output: 75 },
  "claude-sonnet-4-6": { input: 3, output: 15 },
  "claude-haiku-4-5-20251001": { input: 1, output: 5 },
  // OpenAI (verified May 2026 list)
  "gpt-5.5": { input: 5, output: 30 },
  "gpt-5.5-pro": { input: 30, output: 180 },
  "gpt-5.4": { input: 2.5, output: 15 },
  "gpt-5.4-pro": { input: 30, output: 180 },
  "gpt-5.4-mini": { input: 0.4, output: 1.6 },
  "gpt-5.4-nano": { input: 0.2, output: 1.25 },
  "gpt-5": { input: 15, output: 60 },
  "gpt-4.1": { input: 2, output: 8 },
  "gpt-4.1-mini": { input: 0.4, output: 1.6 },
  "gpt-4o": { input: 2.5, output: 10 },
  "gpt-4o-mini": { input: 0.15, output: 0.6 },
  "o3": { input: 2, output: 8 },
  "o3-pro": { input: 60, output: 240 },
  "o3-mini": { input: 1.1, output: 4.4 },
  "o4-mini": { input: 1.1, output: 4.4 },
  // Kimi / Moonshot (May 2026 list per kimi.com)
  "kimi-k2.6": { input: 0.95, output: 4.0 },
  "kimi-k2.5": { input: 0.6, output: 3.0 },
  "kimi-k2-0905-preview": { input: 0.6, output: 2.5 },
  "kimi-k2-thinking": { input: 0.6, output: 2.5 },
  "moonshot-v1-8k": { input: 0.2, output: 2.0 },
  "moonshot-v1-32k": { input: 1.0, output: 3.0 },
  "moonshot-v1-128k": { input: 2.0, output: 5.0 },
}

// Alias map — usage rows may carry the literal "advisor" when an advisor
// iteration block didn't surface its own model name. Route those rows to
// Opus 4.7 pricing rather than the Sonnet fallback (advisor always runs
// on Opus per services/gideon/CLAUDE.md). Mirrors _PRICING_ALIASES in the
// backend pricing module.
const MODEL_ALIASES: Record<string, string> = {
  "advisor": "claude-opus-4-7",
}

const MODEL_LABELS: Record<string, string> = {
  "claude-opus-4-6": "Opus 4.6",
  "claude-opus-4-7": "Opus 4.7",
  "claude-sonnet-4-6": "Sonnet 4.6",
  "claude-haiku-4-5-20251001": "Haiku 4.5",
  "gpt-5.5": "GPT-5.5",
  "gpt-5.5-pro": "GPT-5.5 Pro",
  "gpt-5.4": "GPT-5.4",
  "gpt-5.4-mini": "GPT-5.4 mini",
  "gpt-5": "GPT-5",
  "gpt-4.1": "GPT-4.1",
  "gpt-4.1-mini": "GPT-4.1 mini",
  "gpt-4o": "GPT-4o",
  "gpt-4o-mini": "GPT-4o mini",
  "o3": "o3",
  "o3-pro": "o3 pro",
  "o3-mini": "o3 mini",
  "o4-mini": "o4 mini",
  "kimi-k2.6": "Kimi K2.6",
  "kimi-k2.5": "Kimi K2.5",
  "kimi-k2-thinking": "Kimi K2 Thinking",
  "moonshot-v1-32k": "Moonshot v1 32k",
}

// Resolve a raw usage-row model to its human label, applying the alias map
// first (e.g. "advisor" -> "claude-opus-4-7" -> "Opus 4.7"). Without the
// alias step, advisor rows render + group under a separate "advisor" bucket
// instead of merging into Opus 4.7.
function modelLabel(model: string): string {
  const canonical = MODEL_ALIASES[model] || model
  return MODEL_LABELS[canonical] || MODEL_LABELS[model] || model || "unknown"
}

// Provider-aware fallback when an exact id isn't in MODEL_RATES — keeps
// estimates roughly correct for unknown models in known families instead of
// silently treating everything as Sonnet (the old behavior, off by 2-10x
// for OpenAI/Kimi).
function _fallbackRate(model: string): { input: number; output: number } {
  const m = (model || "").toLowerCase()
  // moonshot-v1-* is a cheaper family than the Kimi K2 line — keep them split.
  if (m.startsWith("moonshot")) return MODEL_RATES["moonshot-v1-32k"]
  if (m.startsWith("kimi")) return MODEL_RATES["kimi-k2.6"]
  if (m.startsWith("gpt-") || /^o\d/.test(m)) return MODEL_RATES["gpt-5"]
  return MODEL_RATES["claude-sonnet-4-6"]
}

function estimateCost(model: string, inputTokens: number, outputTokens: number): number {
  const canonical = MODEL_ALIASES[model] || model
  const rate = MODEL_RATES[canonical] || _fallbackRate(canonical)
  return (inputTokens * rate.input + outputTokens * rate.output) / 1_000_000
}

/** Cache-aware total cost using actual cache read/creation token counts */
function estimateTotalCost(data: {
  by_model?: any[]
  total_cache_read_tokens?: number
  total_cache_creation_tokens?: number
  total_web_searches?: number
}): number {
  // Base cost from per-model input/output
  const baseCost = (data.by_model ?? []).reduce(
    (sum: number, m: any) => sum + estimateCost(m.model || "", m.input_tokens, m.output_tokens),
    0
  )

  // Cache savings: reads are 90% cheaper, creation is 25% more expensive
  // We approximate using a weighted average model rate (Sonnet as default)
  const avgInputRate = 3 / 1_000_000 // Sonnet rate as conservative estimate
  const cacheReadSavings = (data.total_cache_read_tokens ?? 0) * avgInputRate * 0.9
  const cacheCreationExtra = (data.total_cache_creation_tokens ?? 0) * avgInputRate * 0.25

  // Web search cost
  const webSearchCost = (data.total_web_searches ?? 0) * 0.01

  return baseCost - cacheReadSavings + cacheCreationExtra + webSearchCost
}

/* -- Color palette for chart series -------------------- */

const CHART_COLORS = [
  "#8b9dc3", // muted blue
  "#c49a6c", // warm tan
  "#7eb8a0", // sage green
  "#b07aa1", // muted purple
  "#e8927c", // coral
  "#6fa8dc", // sky blue
  "#d4a574", // gold
  "#82b3a8", // teal
]

/* -- Formatting helpers -------------------------------- */

function fmtNumber(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return n.toLocaleString("en-US")
}

function fmtCost(n: number): string {
  return `$${n.toFixed(2)}`
}

function fmtMonthYear(year: number, month: number): string {
  const date = new Date(year, month - 1)
  return date.toLocaleDateString("en-US", { month: "long", year: "numeric" })
}

function fmtDay(dateStr: string): string {
  const d = new Date(dateStr + "T00:00:00")
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" })
}

/* -- View modes ---------------------------------------- */

type ViewMode = "tokens" | "cost"

const GROUP_BY_OPTIONS: { value: GroupBy; label: string }[] = [
  { value: "model", label: "Model" },
  { value: "use_case", label: "Use Case" },
  { value: "user", label: "User" },
]

/* -- Dropdown component -------------------------------- */

function Dropdown({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: string
  options: { value: string; label: string }[]
  onChange: (v: string) => void
}) {
  return (
    <div className="relative">
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="appearance-none px-4 py-1.5 pr-8 rounded-full text-[13px] font-medium cursor-pointer outline-none"
        style={{
          background: "var(--surface-elevated)",
          color: "var(--text-primary)",
          border: "1px solid var(--border)",
        }}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {label} {o.label}
          </option>
        ))}
      </select>
      <ChevronRight
        size={12}
        className="absolute right-2.5 top-1/2 -translate-y-1/2 rotate-90 pointer-events-none"
        style={{ color: "var(--text-tertiary)" }}
      />
    </div>
  )
}

/* -- Build stacked bar chart data ---------------------- */

function buildChartData(
  byDay: DailyEntry[],
  viewMode: ViewMode
): { data: Record<string, unknown>[]; keys: string[] } {
  const dayMap = new Map<string, Record<string, number>>()
  const groupSet = new Set<string>()

  for (const entry of byDay) {
    const label = modelLabel(entry.group)
    groupSet.add(label)
    if (!dayMap.has(entry.date)) dayMap.set(entry.date, {})
    const day = dayMap.get(entry.date)!
    if (viewMode === "cost") {
      const cost = estimateCost(entry.group, entry.input_tokens, entry.output_tokens)
      day[label] = (day[label] || 0) + cost
    } else {
      day[label] = (day[label] || 0) + entry.input_tokens + entry.output_tokens
    }
  }

  const keys = Array.from(groupSet)
  const data = Array.from(dayMap.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([date, values]) => ({
      date: fmtDay(date),
      ...values,
    }))

  return { data, keys }
}

/* -- Reusable table component -------------------------- */

function UsageTable({
  title,
  rows,
  columns,
}: {
  title: string
  rows: { key: string; values: (string | number)[] }[]
  columns: string[]
}) {
  return (
    <div className="mb-6">
      <h2
        className="text-[14px] font-semibold mb-3"
        style={{ color: "var(--text-primary)" }}
      >
        {title}
      </h2>
      <div
        className="rounded-xl overflow-hidden"
        style={{ border: "1px solid var(--border)" }}
      >
        <table className="w-full text-[13px]">
          <thead>
            <tr style={{ borderBottom: "1px solid var(--border)" }}>
              {columns.map((col, i) => (
                <th
                  key={col}
                  className={`px-4 py-3 font-medium ${i === 0 ? "text-left" : "text-right"}`}
                  style={{ color: "var(--text-secondary)", background: "var(--surface-elevated)" }}
                >
                  {col}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td
                  colSpan={columns.length}
                  className="px-4 py-6 text-center text-[13px]"
                  style={{ color: "var(--text-tertiary)" }}
                >
                  No data
                </td>
              </tr>
            ) : (
              rows.map((row, idx) => (
                <tr
                  key={row.key}
                  style={{
                    borderBottom: idx < rows.length - 1 ? "1px solid var(--border)" : "none",
                  }}
                >
                  {row.values.map((val, i) => (
                    <td
                      key={i}
                      className={`px-4 py-3 ${i === 0 ? "text-left" : "text-right tabular-nums"}`}
                      style={{
                        color:
                          i === row.values.length - 1
                            ? "#22c55e"
                            : i === 0
                            ? "var(--text-primary)"
                            : "var(--text-secondary)",
                        fontFamily: i > 0 ? "var(--font-mono, monospace)" : undefined,
                      }}
                    >
                      {val}
                    </td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

/* -- Custom tooltip for chart -------------------------- */

function ChartTooltip({ active, payload, label, viewMode }: any) {
  if (!active || !payload?.length) return null
  return (
    <div
      className="rounded-lg px-3 py-2 text-[12px] shadow-lg"
      style={{
        background: "var(--surface-elevated)",
        border: "1px solid var(--border)",
        color: "var(--text-primary)",
      }}
    >
      <p className="font-medium mb-1" style={{ color: "var(--text-secondary)" }}>
        {label}
      </p>
      {payload.map((entry: any) => (
        <div key={entry.name} className="flex items-center gap-2">
          <div
            className="w-2 h-2 rounded-full"
            style={{ background: entry.color }}
          />
          <span>{entry.name}:</span>
          <span className="font-medium tabular-nums" style={{ fontFamily: "var(--font-mono, monospace)" }}>
            {viewMode === "cost" ? fmtCost(entry.value) : fmtNumber(entry.value)}
          </span>
        </div>
      ))}
    </div>
  )
}

/* -- Main component ------------------------------------ */

export default function UsagePage({ onBack }: UsagePageProps) {
  const now = new Date()
  const [year, setYear] = useState(now.getFullYear())
  const [month, setMonth] = useState(now.getMonth() + 1)
  const [groupBy, setGroupBy] = useState<GroupBy>("model")
  const [viewMode, setViewMode] = useState<ViewMode>("tokens")

  const { data, loading } = useUsage(year, month, groupBy)

  const goToPrevMonth = () => {
    if (month === 1) { setYear(year - 1); setMonth(12) }
    else setMonth(month - 1)
  }
  const goToNextMonth = () => {
    const isCurrentMonth = year === now.getFullYear() && month === now.getMonth() + 1
    if (isCurrentMonth) return
    if (month === 12) { setYear(year + 1); setMonth(1) }
    else setMonth(month + 1)
  }
  const isCurrentMonth = year === now.getFullYear() && month === now.getMonth() + 1

  // Summary values
  const totalInput = data?.total_input_tokens ?? 0
  const totalOutput = data?.total_output_tokens ?? 0
  const totalWebSearches = data?.total_web_searches ?? 0
  const totalCacheRead = data?.total_cache_read_tokens ?? 0
  const totalCacheCreation = data?.total_cache_creation_tokens ?? 0
  const totalCost = data ? estimateTotalCost(data) : 0

  // Chart data
  const { data: chartData, keys: chartKeys } = buildChartData(data?.by_day ?? [], viewMode)

  // Table rows
  const modelRows = (data?.by_model ?? []).map((r: any) => {
    const name = modelLabel(r.model)
    const cost = estimateCost(r.model || "", r.input_tokens, r.output_tokens)
    return {
      key: r.model || name,
      values: [name, fmtNumber(r.requests), fmtNumber(r.input_tokens), fmtNumber(r.output_tokens), fmtCost(cost)],
    }
  })

  const useCaseRows = (data?.by_use_case ?? []).map((r: any) => {
    const name = r.use_case || "unknown"
    const cost = estimateCost("claude-sonnet-4-6", r.input_tokens, r.output_tokens)
    return {
      key: name,
      values: [name, fmtNumber(r.requests), fmtNumber(r.input_tokens), fmtNumber(r.output_tokens), fmtCost(cost)],
    }
  })

  const userRows = (data?.by_user ?? []).map((r: any) => {
    const name = r.name || r.email || "unknown"
    const cost = estimateCost("claude-sonnet-4-6", r.input_tokens, r.output_tokens)
    return {
      key: r.user_id || name,
      values: [name, fmtNumber(r.requests), fmtNumber(r.input_tokens + r.output_tokens), fmtCost(cost)],
    }
  })

  return (
    <div
      className="flex-1 flex flex-col min-w-0 rounded-xl overflow-hidden"
      style={{ background: "var(--surface)" }}
    >
      {/* Header */}
      <div className="shrink-0 px-6 py-5" style={{ borderBottom: "1px solid var(--border)" }}>
        <div className="flex items-center gap-3">
          <button
            onClick={onBack}
            className="p-1.5 rounded-lg transition-colors"
            style={{ color: "var(--text-secondary)" }}
            onMouseEnter={(e) => { e.currentTarget.style.background = "var(--surface-elevated)" }}
            onMouseLeave={(e) => { e.currentTarget.style.background = "transparent" }}
          >
            <ArrowLeft size={18} />
          </button>
          <div
            className="w-9 h-9 rounded-xl flex items-center justify-center"
            style={{ background: "var(--surface-elevated)" }}
          >
            <BarChart3 size={18} style={{ color: "var(--text-secondary)" }} />
          </div>
          <h1 className="text-[16px] font-semibold" style={{ color: "var(--text-primary)" }}>
            Usage
          </h1>
        </div>
      </div>

      {/* Controls bar */}
      <div
        className="shrink-0 px-6 py-3 flex items-center gap-3 flex-wrap"
        style={{ borderBottom: "1px solid var(--border)" }}
      >
        {/* Month picker */}
        <div className="flex items-center gap-1">
          <button
            onClick={goToPrevMonth}
            className="p-1.5 rounded-lg transition-colors"
            style={{ color: "var(--text-secondary)" }}
            onMouseEnter={(e) => { e.currentTarget.style.background = "var(--surface-elevated)" }}
            onMouseLeave={(e) => { e.currentTarget.style.background = "transparent" }}
          >
            <ChevronLeft size={16} />
          </button>
          <span
            className="text-[13px] font-medium min-w-[130px] text-center"
            style={{ color: "var(--text-primary)" }}
          >
            {fmtMonthYear(year, month)}
          </span>
          <button
            onClick={goToNextMonth}
            className="p-1.5 rounded-lg transition-colors"
            style={{
              color: isCurrentMonth ? "var(--text-tertiary)" : "var(--text-secondary)",
              cursor: isCurrentMonth ? "not-allowed" : "pointer",
              opacity: isCurrentMonth ? 0.4 : 1,
            }}
            onMouseEnter={(e) => {
              if (!isCurrentMonth) e.currentTarget.style.background = "var(--surface-elevated)"
            }}
            onMouseLeave={(e) => { e.currentTarget.style.background = "transparent" }}
            disabled={isCurrentMonth}
          >
            <ChevronRight size={16} />
          </button>
        </div>

        <div className="w-px h-5" style={{ background: "var(--border)" }} />

        {/* Group by */}
        <Dropdown
          label="Group by:"
          value={groupBy}
          options={GROUP_BY_OPTIONS}
          onChange={(v) => setGroupBy(v as GroupBy)}
        />

        {/* View mode */}
        <div
          className="flex rounded-full overflow-hidden"
          style={{ border: "1px solid var(--border)" }}
        >
          {(["tokens", "cost"] as ViewMode[]).map((mode) => (
            <button
              key={mode}
              onClick={() => setViewMode(mode)}
              className="px-4 py-1.5 text-[13px] font-medium transition-all"
              style={{
                background: viewMode === mode ? "var(--accent)" : "transparent",
                color: viewMode === mode ? "#fff" : "var(--text-secondary)",
              }}
            >
              {mode === "tokens" ? "Tokens" : "Cost"}
            </button>
          ))}
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto">
        {loading ? (
          <div className="flex items-center justify-center py-20">
            <Loader2 size={20} className="animate-spin" style={{ color: "var(--text-tertiary)" }} />
          </div>
        ) : (
          <div className="max-w-[900px] mx-auto px-6 py-6">
            {/* Summary cards */}
            {viewMode === "tokens" ? (
              <div className="grid grid-cols-4 gap-4 mb-6">
                <div className="rounded-xl px-5 py-4" style={{ border: "1px solid var(--border)", background: "var(--surface-elevated)" }}>
                  <p className="text-[12px] font-medium mb-1" style={{ color: "var(--text-secondary)" }}>Total tokens in</p>
                  <p className="text-[24px] font-semibold tabular-nums" style={{ color: "var(--text-primary)", fontFamily: "var(--font-mono, monospace)" }}>
                    {fmtNumber(totalInput)}
                  </p>
                </div>
                <div className="rounded-xl px-5 py-4" style={{ border: "1px solid var(--border)", background: "var(--surface-elevated)" }}>
                  <p className="text-[12px] font-medium mb-1" style={{ color: "var(--text-secondary)" }}>Total tokens out</p>
                  <p className="text-[24px] font-semibold tabular-nums" style={{ color: "var(--text-primary)", fontFamily: "var(--font-mono, monospace)" }}>
                    {fmtNumber(totalOutput)}
                  </p>
                </div>
                <div className="rounded-xl px-5 py-4" style={{ border: "1px solid var(--border)", background: "var(--surface-elevated)" }}>
                  <p className="text-[12px] font-medium mb-1" style={{ color: "var(--text-secondary)" }}>Cache read tokens</p>
                  <p className="text-[24px] font-semibold tabular-nums" style={{ color: "#22c55e", fontFamily: "var(--font-mono, monospace)" }}>
                    {fmtNumber(totalCacheRead)}
                  </p>
                  <p className="text-[11px] mt-0.5" style={{ color: "var(--text-tertiary)" }}>90% cheaper</p>
                </div>
                <div className="rounded-xl px-5 py-4" style={{ border: "1px solid var(--border)", background: "var(--surface-elevated)" }}>
                  <p className="text-[12px] font-medium mb-1" style={{ color: "var(--text-secondary)" }}>Web searches</p>
                  <p className="text-[24px] font-semibold tabular-nums" style={{ color: "var(--text-primary)", fontFamily: "var(--font-mono, monospace)" }}>
                    {fmtNumber(totalWebSearches)}
                  </p>
                </div>
              </div>
            ) : (
              <div className="grid grid-cols-3 gap-4 mb-6">
                <div className="rounded-xl px-5 py-4" style={{ border: "1px solid var(--border)", background: "var(--surface-elevated)" }}>
                  <p className="text-[12px] font-medium mb-1" style={{ color: "var(--text-secondary)" }}>Total token cost</p>
                  <p className="text-[24px] font-semibold tabular-nums" style={{ color: "#22c55e", fontFamily: "var(--font-mono, monospace)" }}>
                    {fmtCost(totalCost)}
                  </p>
                </div>
                <div className="rounded-xl px-5 py-4" style={{ border: "1px solid var(--border)", background: "var(--surface-elevated)" }}>
                  <p className="text-[12px] font-medium mb-1" style={{ color: "var(--text-secondary)" }}>Total web search cost</p>
                  <p className="text-[12px] mb-1" style={{ color: "var(--text-tertiary)" }}>$0.01 per search</p>
                  <p className="text-[24px] font-semibold tabular-nums" style={{ color: "#22c55e", fontFamily: "var(--font-mono, monospace)" }}>
                    {fmtCost(totalWebSearches * 0.01)}
                  </p>
                </div>
                <div className="rounded-xl px-5 py-4" style={{ border: "1px solid var(--border)", background: "var(--surface-elevated)" }}>
                  <p className="text-[12px] font-medium mb-1" style={{ color: "var(--text-secondary)" }}>Total requests</p>
                  <p className="text-[24px] font-semibold tabular-nums" style={{ color: "var(--text-primary)", fontFamily: "var(--font-mono, monospace)" }}>
                    {fmtNumber(data?.total_requests ?? 0)}
                  </p>
                </div>
              </div>
            )}

            {/* Stacked bar chart */}
            {chartData.length > 0 && (
              <div
                className="rounded-xl p-5 mb-6"
                style={{ border: "1px solid var(--border)", background: "var(--surface-elevated)" }}
              >
                <h2 className="text-[14px] font-semibold mb-1" style={{ color: "var(--text-primary)" }}>
                  {viewMode === "tokens" ? "Token usage" : "Daily token cost"}
                </h2>
                <p className="text-[12px] mb-4" style={{ color: "var(--text-tertiary)" }}>
                  Grouped by {groupBy === "use_case" ? "use case" : groupBy}
                </p>
                <ResponsiveContainer width="100%" height={280}>
                  <BarChart data={chartData} barCategoryGap="20%">
                    <XAxis
                      dataKey="date"
                      tick={{ fill: "var(--text-tertiary)", fontSize: 11 }}
                      axisLine={false}
                      tickLine={false}
                    />
                    <YAxis
                      tick={{ fill: "var(--text-tertiary)", fontSize: 11 }}
                      axisLine={false}
                      tickLine={false}
                      tickFormatter={(v: number) =>
                        viewMode === "cost" ? `$${v.toFixed(0)}` : fmtNumber(v)
                      }
                      width={65}
                    />
                    <Tooltip
                      content={(props: any) => <ChartTooltip {...props} viewMode={viewMode} />}
                      cursor={{ fill: "rgba(255,255,255,0.03)" }}
                    />
                    <Legend
                      wrapperStyle={{ fontSize: 11, color: "var(--text-secondary)", paddingTop: 8 }}
                    />
                    {chartKeys.map((key, i) => (
                      <Bar
                        key={key}
                        dataKey={key}
                        stackId="a"
                        fill={CHART_COLORS[i % CHART_COLORS.length]}
                        radius={i === chartKeys.length - 1 ? [4, 4, 0, 0] : [0, 0, 0, 0]}
                      />
                    ))}
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )}

            {/* Tables */}
            <UsageTable
              title="By Model"
              columns={["Model", "Requests", "Input", "Output", "Est. Cost"]}
              rows={modelRows}
            />

            <UsageTable
              title="By Use Case"
              columns={["Use Case", "Requests", "Input", "Output", "Est. Cost"]}
              rows={useCaseRows}
            />

            {userRows.length > 0 && (
              <UsageTable
                title="By User"
                columns={["User", "Requests", "Total Tokens", "Est. Cost"]}
                rows={userRows}
              />
            )}
          </div>
        )}
      </div>
    </div>
  )
}
