"use client"

import { useState } from "react"
import { Download, FileSpreadsheet, ChevronDown, ChevronUp, Table2 } from "lucide-react"
import type { DataTableData } from "../../types"
import { pingman } from "../../utils/pingman"

interface DataTableCardProps {
  data: DataTableData
}

function downloadCsv(columns: string[], rows: Record<string, any>[]) {
  const escape = (v: any) => {
    const s = v == null ? "" : String(v)
    return s.includes(",") || s.includes('"') || s.includes("\n") ? `"${s.replace(/"/g, '""')}"` : s
  }
  const lines = [columns.map(escape).join(",")]
  for (const row of rows) {
    lines.push(columns.map((c) => escape(row[c])).join(","))
  }
  const blob = new Blob([lines.join("\n")], { type: "text/csv" })
  const url = URL.createObjectURL(blob)
  const a = document.createElement("a")
  a.href = url
  a.download = "analytics_data.csv"
  a.click()
  URL.revokeObjectURL(url)
}

export default function DataTableCard({ data }: DataTableCardProps) {
  const { columns, rows, row_count, display_limit = 100, sql } = data
  const visibleRows = rows.slice(0, display_limit)
  const hasMore = rows.length > display_limit
  const [expanded, setExpanded] = useState(visibleRows.length <= 20)
  const [showSql, setShowSql] = useState(false)
  const [creatingSheet, setCreatingSheet] = useState(false)
  const [sheetUrl, setSheetUrl] = useState<string | null>(null)

  const displayRows = expanded ? visibleRows : visibleRows.slice(0, 10)

  const handleCreateSheet = async () => {
    setCreatingSheet(true)
    try {
      const title = `Gideon Analytics ${new Date().toLocaleDateString()}`
      const res = await pingman.post<{ spreadsheet_id: string; url: string }>("/api/gideon/v1/hub/analytics/create-sheet", {
        title,
        columns,
        rows,
      })
      if (res.ok && res.data?.url) {
        setSheetUrl(res.data.url)
        window.open(res.data.url, "_blank")
      }
    } catch {
      // Silently fail - user can retry
    } finally {
      setCreatingSheet(false)
    }
  }

  return (
    <div
      className="my-3 rounded-2xl overflow-hidden relative"
      style={{
        background: "rgba(27,27,27,0.5)",
        backdropFilter: "blur(12px)",
        boxShadow: "0 0 0 0.5px rgba(0,0,0,0.4)",
        borderWidth: "0.5px",
        borderStyle: "solid",
        borderImage: "linear-gradient(to bottom right, rgba(255,255,255,0.25) 0%, rgba(255,255,255,0.08) 42%, rgba(255,255,255,0.08) 62%, rgba(255,255,255,0.25) 100%) 1",
      }}
    >
      {/* Radial glow */}
      <div
        className="pointer-events-none absolute inset-0 rounded-[inherit] overflow-hidden"
        style={{ background: "radial-gradient(circle at 15% 0%, rgba(255,255,255,0.04) 0%, transparent 50%)" }}
      />
      {/* Header */}
      <div
        className="flex items-center justify-between px-4 py-2.5 relative"
        style={{ borderBottom: "0.5px solid rgba(255,255,255,0.08)" }}
      >
        <div className="flex items-center gap-2">
          <Table2 size={14} style={{ color: "var(--accent)" }} />
          <span className="text-[12px] font-medium" style={{ color: "rgba(255,255,255,0.7)" }}>
            {row_count} row{row_count !== 1 ? "s" : ""}{hasMore ? ` (showing ${display_limit}, download for all)` : ""}
          </span>
        </div>
        <div className="flex items-center gap-1">
          {sql && (
            <button
              onClick={() => setShowSql(!showSql)}
              className="px-2 py-1 rounded text-[11px] hover:bg-white/10 transition-colors"
              style={{ color: "rgba(255,255,255,0.4)" }}
            >
              SQL
            </button>
          )}
          <button
            onClick={() => downloadCsv(columns, rows)}
            className="p-1.5 rounded hover:bg-white/10 transition-colors"
            style={{ color: "rgba(255,255,255,0.5)" }}
            title="Download CSV"
          >
            <Download size={13} />
          </button>
          <button
            onClick={handleCreateSheet}
            disabled={creatingSheet}
            className="p-1.5 rounded hover:bg-white/10 transition-colors"
            style={{ color: sheetUrl ? "#34a853" : "rgba(255,255,255,0.5)", opacity: creatingSheet ? 0.5 : 1 }}
            title={sheetUrl ? "Open Google Sheet" : "Create Google Sheet"}
          >
            <FileSpreadsheet size={13} />
          </button>
        </div>
      </div>

      {/* SQL preview */}
      {showSql && (
        <div
          className="px-4 py-2 text-[11px] font-mono overflow-x-auto relative"
          style={{
            background: "rgba(0,0,0,0.3)",
            color: "rgba(255,255,255,0.5)",
            borderBottom: "0.5px solid rgba(255,255,255,0.06)",
            whiteSpace: "pre-wrap",
            maxHeight: 120,
            overflowY: "auto",
          }}
        >
          {sql}
        </div>
      )}

      {/* Google Sheet link */}
      {sheetUrl && (
        <div
          className="px-4 py-1.5 flex items-center gap-2 relative"
          style={{ background: "rgba(52,168,83,0.06)", borderBottom: "0.5px solid rgba(255,255,255,0.06)" }}
        >
          <FileSpreadsheet size={12} style={{ color: "#34a853" }} />
          <a
            href={sheetUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="text-[11px] hover:underline"
            style={{ color: "#34a853" }}
          >
            Open in Google Sheets
          </a>
        </div>
      )}

      {/* Table */}
      <div className="overflow-x-auto relative" style={{ maxHeight: expanded ? 400 : 280 }}>
        <table className="w-full text-[12px] border-collapse">
          <thead>
            <tr>
              {columns.map((col) => (
                <th
                  key={col}
                  className="text-left px-4 py-2.5 font-medium whitespace-nowrap"
                  style={{ color: "rgba(255,255,255,0.5)", borderBottom: "0.5px solid rgba(255,255,255,0.1)" }}
                >
                  {col}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {displayRows.map((row, i) => (
              <tr
                key={i}
                style={{ borderBottom: "0.5px solid rgba(255,255,255,0.06)" }}
                className="hover:bg-white/[0.03]"
              >
                {columns.map((col) => (
                  <td
                    key={col}
                    className="px-4 py-2 whitespace-nowrap"
                    style={{ color: "rgba(255,255,255,0.85)" }}
                  >
                    {row[col] == null ? "" : String(row[col])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Expand/collapse */}
      {visibleRows.length > 10 && (
        <button
          onClick={() => setExpanded(!expanded)}
          className="w-full flex items-center justify-center gap-1 py-2 text-[11px] hover:bg-white/[0.03] transition-colors relative"
          style={{ color: "rgba(255,255,255,0.4)", borderTop: "0.5px solid rgba(255,255,255,0.06)" }}
        >
          {expanded ? (
            <>Show less <ChevronUp size={12} /></>
          ) : (
            <>Show {visibleRows.length} rows <ChevronDown size={12} /></>
          )}
        </button>
      )}
    </div>
  )
}
