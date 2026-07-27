"use client"

// ---------------------------------------------------------------------------
// Insight Blocks — rich analytical components rendered from Claude's output.
// Claude emits these as fenced code blocks with special language tags
// (scorecard, rich-table, insight-cards) containing JSON. The markdown
// renderer in markdownConfig.tsx detects them and renders these components.
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Color palette
// ---------------------------------------------------------------------------

const COLORS: Record<string, { text: string; bg: string; border: string; badge: string }> = {
  green:  { text: "#4ade80", bg: "rgba(74,222,128,0.06)",  border: "#22c55e", badge: "rgba(74,222,128,0.15)" },
  blue:   { text: "#60a5fa", bg: "rgba(96,165,250,0.06)",  border: "#3b82f6", badge: "rgba(96,165,250,0.15)" },
  orange: { text: "#fb923c", bg: "rgba(251,146,60,0.06)",  border: "#f97316", badge: "rgba(251,146,60,0.15)" },
  red:    { text: "#f87171", bg: "rgba(248,113,113,0.06)", border: "#ef4444", badge: "rgba(248,113,113,0.15)" },
  gray:   { text: "#9ca3af", bg: "rgba(156,163,175,0.06)", border: "#6b7280", badge: "rgba(156,163,175,0.15)" },
  purple: { text: "#c084fc", bg: "rgba(192,132,252,0.06)", border: "#a855f7", badge: "rgba(192,132,252,0.15)" },
}

function getColor(name: string) {
  return COLORS[name] || COLORS.gray
}

// ---------------------------------------------------------------------------
// ScoreCards — row of KPI metric cards
// ---------------------------------------------------------------------------

interface ScoreCardData {
  label: string
  tag?: string
  value: string
  detail?: string
  trend?: string
  color: string
}

export function ScoreCards({ cards }: { cards: ScoreCardData[] }) {
  if (!Array.isArray(cards) || cards.length === 0) return null

  return (
    <div className="flex flex-wrap gap-3 my-4">
      {cards.map((card, i) => {
        const c = getColor(card.color)
        return (
          <div
            key={i}
            className="flex-1 min-w-[150px] max-w-[280px] rounded-xl p-4"
            style={{
              background: c.bg,
              border: "1px solid rgba(255,255,255,0.06)",
              borderLeft: `3px solid ${c.border}`,
            }}
          >
            <div className="flex items-center gap-2 mb-1.5">
              <span className="text-[12px] font-medium" style={{ color: "var(--text-secondary)" }}>
                {card.label}
              </span>
              {card.tag && (
                <span
                  className="text-[9px] font-semibold uppercase px-1.5 py-0.5 rounded"
                  style={{ color: c.text, background: c.badge }}
                >
                  {card.tag}
                </span>
              )}
            </div>
            <div className="text-[26px] font-bold leading-tight mb-1" style={{ color: c.text }}>
              {card.value}
            </div>
            {card.detail && (
              <div className="text-[11px] mb-2 leading-snug" style={{ color: "var(--text-tertiary)" }}>
                {card.detail}
              </div>
            )}
            {card.trend && (
              <span
                className="text-[10px] px-2 py-0.5 rounded-md inline-block"
                style={{ color: c.text, background: c.badge }}
              >
                {card.trend}
              </span>
            )}
          </div>
        )
      })}
    </div>
  )
}

// ---------------------------------------------------------------------------
// RichTable — table with colored cells and optional title
// ---------------------------------------------------------------------------

interface RichTableData {
  title?: string
  headers: string[]
  rows: (string | { v: string; c: string })[][]
}

export function RichTable({ data }: { data: RichTableData }) {
  if (!data.headers || !data.rows) return null

  return (
    <div className="my-4">
      {data.title && (
        <div
          className="text-[11px] font-semibold tracking-wider uppercase mb-2 px-1"
          style={{ color: "var(--text-secondary)" }}
        >
          {data.title}
        </div>
      )}
      <div className="overflow-x-auto rounded-xl" style={{ border: "1px solid var(--border)" }}>
        <table className="w-full text-[13px] border-collapse">
          <thead>
            <tr style={{ background: "rgba(255,255,255,0.03)" }}>
              {data.headers.map((h, i) => (
                <th
                  key={i}
                  className="text-left px-4 py-2.5 font-semibold text-[10px] tracking-wider uppercase whitespace-nowrap"
                  style={{ color: "var(--text-secondary)", borderBottom: "1px solid var(--border)" }}
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.rows.map((row, ri) => (
              <tr key={ri}>
                {row.map((cell, ci) => {
                  const isObj = typeof cell === "object" && cell !== null && "v" in cell
                  const value = isObj ? cell.v : String(cell ?? "")
                  const color = isObj ? getColor(cell.c).text : undefined
                  return (
                    <td
                      key={ci}
                      className="px-4 py-2 whitespace-nowrap"
                      style={{
                        color: color || (ci === 0 ? "var(--text-primary)" : "var(--text-secondary)"),
                        fontWeight: ci === 0 ? 500 : 400,
                        borderBottom: ri < data.rows.length - 1 ? "1px solid rgba(255,255,255,0.04)" : undefined,
                      }}
                    >
                      {value}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// InsightCards — colored summary/analysis cards in a grid
// ---------------------------------------------------------------------------

interface InsightCardData {
  title: string
  body: string
  color: string
}

export function InsightCards({ cards }: { cards: InsightCardData[] }) {
  if (!Array.isArray(cards) || cards.length === 0) return null

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 my-4">
      {cards.map((card, i) => {
        const c = getColor(card.color)
        return (
          <div
            key={i}
            className="rounded-xl p-4"
            style={{
              background: c.bg,
              border: "1px solid rgba(255,255,255,0.06)",
              borderLeft: `3px solid ${c.border}`,
            }}
          >
            <div className="text-[13px] font-semibold mb-1.5" style={{ color: c.text }}>
              {card.title}
            </div>
            <div className="text-[12px] leading-relaxed" style={{ color: "var(--text-secondary)" }}>
              {card.body}
            </div>
          </div>
        )
      })}
    </div>
  )
}
