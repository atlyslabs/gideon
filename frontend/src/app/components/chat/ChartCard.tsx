"use client"

import { useState, useMemo } from "react"
import { BarChart3, TrendingUp, ChevronDown, ChevronUp } from "lucide-react"
import {
  ResponsiveContainer,
  LineChart,
  Line,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
  Legend,
} from "recharts"
import type { ChartData } from "../../types"

// Consistent color palette for chart series
const SERIES_COLORS = [
  "#a78bfa", // purple
  "#fb923c", // orange
  "#34d399", // green
  "#60a5fa", // blue
  "#f472b6", // pink
  "#facc15", // yellow
  "#2dd4bf", // teal
  "#c084fc", // light purple
]

const TIME_FIELD_NAMES = new Set(["time", "date", "ts", "timestamp", "created_at", "day", "week", "month", "hour"])

function isTimeField(name: string): boolean {
  return TIME_FIELD_NAMES.has(name.toLowerCase())
}

function isEpochMs(val: unknown): boolean {
  return typeof val === "number" && val > 1_000_000_000_000
}

function formatNumber(val: number): string {
  if (Math.abs(val) >= 1_000_000) return `${(val / 1_000_000).toFixed(1)}M`
  if (Math.abs(val) >= 1_000) return `${(val / 1_000).toFixed(1)}K`
  if (Number.isInteger(val)) return val.toString()
  return val.toFixed(2)
}

function formatTimeLabel(val: unknown): string {
  if (typeof val === "number" && isEpochMs(val)) {
    const d = new Date(val)
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric" })
  }
  if (typeof val === "string") {
    // Try parse date string
    const d = new Date(val)
    if (!isNaN(d.getTime())) {
      return d.toLocaleDateString("en-US", { month: "short", day: "numeric" })
    }
    // Truncate long labels
    return val.length > 12 ? val.slice(0, 12) + "..." : val
  }
  return String(val ?? "")
}

interface ChartCardProps {
  data: ChartData
}

export default function ChartCard({ data }: ChartCardProps) {
  const [chartType, setChartType] = useState<"line" | "bar">("line")
  const [expanded, setExpanded] = useState(true)

  const { chartRows, xField, seriesFields } = useMemo(() => {
    const { fields, rows } = data
    if (!fields.length || !rows.length) return { chartRows: [], xField: "", seriesFields: [] }

    // First field is typically x-axis (time or category)
    const xField = fields[0]
    // Remaining fields with numeric values are series
    const seriesFields = fields.slice(1).filter((f) => {
      const sample = rows.find((r) => r[f] != null)
      return sample && typeof sample[f] === "number"
    })

    // Process rows: convert epoch ms to dates for display, sort by x
    const processed = rows
      .map((row) => {
        const newRow = { ...row }
        // Keep original value for sorting, add display label
        if (isTimeField(xField) && isEpochMs(row[xField])) {
          newRow._xLabel = formatTimeLabel(row[xField])
          newRow._xSort = row[xField]
        } else {
          newRow._xLabel = formatTimeLabel(row[xField])
          newRow._xSort = typeof row[xField] === "number" ? row[xField] : 0
        }
        return newRow
      })
      .sort((a, b) => (a._xSort as number) - (b._xSort as number))

    return { chartRows: processed, xField, seriesFields }
  }, [data])

  if (!seriesFields.length || !chartRows.length) return null

  const ChartComponent = chartType === "bar" ? BarChart : LineChart

  return (
    <div
      className="my-3 rounded-xl overflow-hidden"
      style={{
        background: "rgba(255,255,255,0.03)",
        border: "1px solid var(--border)",
      }}
    >
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2.5" style={{ borderBottom: "1px solid var(--border)" }}>
        <button
          onClick={() => setExpanded(!expanded)}
          className="flex items-center gap-2 min-w-0"
        >
          <BarChart3 size={14} style={{ color: "#a78bfa" }} className="shrink-0" />
          <span className="text-[13px] font-medium truncate" style={{ color: "var(--text-primary)" }}>
            {data.title || "Chart"}
          </span>
          {expanded ? <ChevronUp size={13} style={{ color: "var(--text-tertiary)" }} /> : <ChevronDown size={13} style={{ color: "var(--text-tertiary)" }} />}
        </button>
        {expanded && (
          <div className="flex items-center gap-1 shrink-0">
            <button
              onClick={() => setChartType("line")}
              className="p-1.5 rounded-md transition-colors"
              style={{
                background: chartType === "line" ? "rgba(167,139,250,0.15)" : "transparent",
                color: chartType === "line" ? "#a78bfa" : "var(--text-tertiary)",
              }}
              title="Line chart"
            >
              <TrendingUp size={14} />
            </button>
            <button
              onClick={() => setChartType("bar")}
              className="p-1.5 rounded-md transition-colors"
              style={{
                background: chartType === "bar" ? "rgba(167,139,250,0.15)" : "transparent",
                color: chartType === "bar" ? "#a78bfa" : "var(--text-tertiary)",
              }}
              title="Bar chart"
            >
              <BarChart3 size={14} />
            </button>
          </div>
        )}
      </div>

      {/* Chart */}
      {expanded && (
        <div className="px-2 py-3" style={{ height: 300 }}>
          <ResponsiveContainer width="100%" height="100%">
            <ChartComponent data={chartRows} margin={{ top: 5, right: 20, bottom: 5, left: 10 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.06)" />
              <XAxis
                dataKey="_xLabel"
                tick={{ fontSize: 11, fill: "rgba(255,255,255,0.45)" }}
                tickLine={false}
                axisLine={{ stroke: "rgba(255,255,255,0.1)" }}
                interval="preserveStartEnd"
              />
              <YAxis
                tick={{ fontSize: 11, fill: "rgba(255,255,255,0.45)" }}
                tickLine={false}
                axisLine={{ stroke: "rgba(255,255,255,0.1)" }}
                tickFormatter={formatNumber}
                width={50}
              />
              <Tooltip
                contentStyle={{
                  background: "rgba(28,28,30,0.95)",
                  border: "1px solid rgba(255,255,255,0.1)",
                  borderRadius: 8,
                  fontSize: 12,
                  color: "rgba(255,255,255,0.9)",
                  backdropFilter: "blur(10px)",
                }}
                labelStyle={{ color: "rgba(255,255,255,0.6)", marginBottom: 4 }}
                formatter={(value: number) => [formatNumber(value)]}
              />
              {seriesFields.length > 1 && (
                <Legend
                  wrapperStyle={{ fontSize: 11, color: "rgba(255,255,255,0.6)" }}
                />
              )}
              {seriesFields.map((field, i) =>
                chartType === "bar" ? (
                  <Bar
                    key={field}
                    dataKey={field}
                    fill={SERIES_COLORS[i % SERIES_COLORS.length]}
                    radius={[3, 3, 0, 0]}
                    opacity={0.85}
                  />
                ) : (
                  <Line
                    key={field}
                    type="monotone"
                    dataKey={field}
                    stroke={SERIES_COLORS[i % SERIES_COLORS.length]}
                    strokeWidth={2}
                    dot={chartRows.length <= 30}
                    activeDot={{ r: 4 }}
                  />
                )
              )}
            </ChartComponent>
          </ResponsiveContainer>
        </div>
      )}

      {/* Filters + Footer */}
      {expanded && (
        <div style={{ borderTop: "1px solid var(--border)" }}>
          {data.active_filters && Object.keys(data.active_filters).length > 0 && (
            <div className="px-4 pt-2 pb-1 flex flex-wrap gap-1.5">
              {Object.entries(data.active_filters).map(([k, v]) => (
                <span
                  key={k}
                  className="text-[10px] px-2 py-0.5 rounded-full"
                  style={{ background: "rgba(167,139,250,0.1)", color: "rgba(167,139,250,0.7)" }}
                >
                  {k}: {v}
                </span>
              ))}
            </div>
          )}
          <div className="px-4 py-1.5 flex items-center justify-between">
            <span className="text-[10px]" style={{ color: "var(--text-tertiary)" }}>
              {chartRows.length} data points · {seriesFields.join(", ")}
            </span>
            {data.time_range && (
              <span className="text-[10px]" style={{ color: "var(--text-tertiary)" }}>
                {data.time_range.from} to {data.time_range.to}
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
