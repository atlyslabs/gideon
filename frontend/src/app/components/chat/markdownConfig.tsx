"use client"

import { useRef, useState, isValidElement, type ReactNode } from "react"
import { Download, Copy, Check } from "lucide-react"
import ReactMarkdown, { Components } from "react-markdown"
import remarkGfm from "remark-gfm"
import { PrismAsyncLight as SyntaxHighlighter } from "react-syntax-highlighter"
import oneDark from "react-syntax-highlighter/dist/esm/styles/prism/one-dark"
import { ScoreCards, RichTable, InsightCards } from "./InsightBlocks"

const INSIGHT_LANGS = new Set(["scorecard", "rich-table", "insight-cards"])

// ---------------------------------------------------------------------------
// Markdown config -- links open in new tab
// ---------------------------------------------------------------------------

export const mdPlugins = [remarkGfm]

/** Extract plain text from React children recursively */
export function extractText(node: ReactNode): string {
  if (node == null || typeof node === "boolean") return ""
  if (typeof node === "string" || typeof node === "number") return String(node)
  if (Array.isArray(node)) return node.map(extractText).join("")
  // React 19 types `element.props` as `unknown`, so narrow with isValidElement
  // and assert only the `children` shape we actually read.
  if (isValidElement(node)) {
    return extractText((node.props as { children?: ReactNode }).children)
  }
  return ""
}

/** Download table data as CSV */
function downloadTableAsCsv(tableEl: HTMLTableElement) {
  const rows: string[][] = []
  tableEl.querySelectorAll("tr").forEach((tr) => {
    const cells: string[] = []
    tr.querySelectorAll("th, td").forEach((cell) => {
      const text = (cell.textContent || "").replace(/"/g, '""')
      cells.push(`"${text}"`)
    })
    if (cells.length > 0) rows.push(cells)
  })
  const csv = rows.map((r) => r.join(",")).join("\n")
  const blob = new Blob([csv], { type: "text/csv" })
  const url = URL.createObjectURL(blob)
  const a = document.createElement("a")
  a.href = url
  a.download = "table.csv"
  a.click()
  URL.revokeObjectURL(url)
}

function MarkdownTable({ children }: { children: ReactNode }) {
  const tableRef = useRef<HTMLTableElement>(null)
  return (
    <div className="overflow-x-auto my-3 relative group/table">
      <div
        className="rounded-2xl overflow-hidden relative"
        style={{
          // padding-box layer = inner fill, border-box layer = gradient border.
          // Avoids `border-image` (which ignores border-radius at corners and
          // leaks straight edges past the rounded inner clip).
          background:
            "linear-gradient(rgba(27,27,27,0.5), rgba(27,27,27,0.5)) padding-box, " +
            "linear-gradient(to bottom right, rgba(255,255,255,0.25) 0%, rgba(255,255,255,0.08) 42%, rgba(255,255,255,0.08) 62%, rgba(255,255,255,0.25) 100%) border-box",
          backdropFilter: "blur(12px)",
          boxShadow: "0 0 0 0.5px rgba(0,0,0,0.4)",
          border: "0.5px solid transparent",
        }}
      >
        <div
          className="pointer-events-none absolute inset-0 rounded-[inherit] overflow-hidden"
          style={{
            background: "radial-gradient(circle at 15% 0%, rgba(255,255,255,0.04) 0%, transparent 50%)",
          }}
        />
        <table
          ref={tableRef}
          className="w-full text-[13px] border-collapse relative"
        >
          {children}
        </table>
      </div>
      <button
        onClick={() => tableRef.current && downloadTableAsCsv(tableRef.current)}
        className="absolute top-2.5 right-2.5 p-1.5 rounded-md opacity-0 group-hover/table:opacity-100 transition-opacity z-10"
        style={{ background: "var(--surface-elevated)", border: "1px solid var(--border)", color: "var(--text-tertiary)" }}
        onMouseEnter={(e) => { e.currentTarget.style.color = "var(--text-primary)" }}
        onMouseLeave={(e) => { e.currentTarget.style.color = "var(--text-tertiary)" }}
        title="Download as CSV"
      >
        <Download size={13} />
      </button>
    </div>
  )
}

function LinkChip({ href, children }: { href?: string; children: ReactNode }) {
  if (!href) return <a>{children}</a>

  // Only style external http links as chips
  const isExternal = href.startsWith("http://") || href.startsWith("https://")
  if (!isExternal) return <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>

  let domain = ""
  try { domain = new URL(href).hostname } catch {}

  const label = extractText(children)
  // Use short label: the link text, or domain name if text is just the URL
  const displayLabel = label && !label.startsWith("http") ? label : domain.replace("www.", "")

  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="not-prose no-underline hover:no-underline inline-flex items-center gap-1.5 align-middle rounded-lg px-1.5 py-0.5 text-[13px] max-w-full min-w-0 transition-all duration-150"
      style={{
        background: "rgba(255,255,255,0.04)",
        border: "0.5px solid rgba(255,255,255,0.12)",
        boxShadow: "0 0 7px 1px rgba(255,255,255,0.05) inset",
        color: "rgba(255,255,255,0.9)",
        textDecoration: "none",
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.background = "rgba(255,255,255,0.08)"
        e.currentTarget.style.borderColor = "rgba(255,255,255,0.2)"
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.background = "rgba(255,255,255,0.04)"
        e.currentTarget.style.borderColor = "rgba(255,255,255,0.12)"
      }}
    >
      {domain && (
        <img
          src={`https://www.google.com/s2/favicons?domain=${domain}&sz=128`}
          alt=""
          className="w-4 h-4 rounded shrink-0"
          style={{ imageRendering: "auto" }}
        />
      )}
      <span className="min-w-0 max-w-[30ch] overflow-hidden text-ellipsis whitespace-nowrap">
        {displayLabel}
      </span>
    </a>
  )
}

/** A fenced code block: a language label + copy button header bar, with the code
 *  body syntax-highlighted via Prism. PrismAsyncLight lazy-loads each grammar on
 *  demand, so the main bundle stays lean. Inline code (single backticks) is NOT
 *  routed here — it stays a plain chip via the global `.prose code` style. */
function CodeBlock({ code, lang }: { code: string; lang: string }) {
  const [copied, setCopied] = useState(false)
  const handleCopy = async () => {
    if (!code) return
    try {
      await navigator.clipboard.writeText(code)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch { /* clipboard blocked — best-effort */ }
  }
  return (
    <div
      className="my-3 rounded-xl overflow-hidden"
      style={{ border: "1px solid var(--border)", background: "var(--surface-elevated)" }}
    >
      <div
        className="flex items-center justify-between px-3 py-1.5"
        style={{ borderBottom: "1px solid var(--border)" }}
      >
        <span className="text-[11px] font-mono" style={{ color: "var(--text-tertiary)" }}>
          {lang || "text"}
        </span>
        <button
          onClick={handleCopy}
          className="p-1 rounded-md transition-colors"
          style={{ color: copied ? "var(--text-primary)" : "var(--text-tertiary)" }}
          onMouseEnter={(e) => { e.currentTarget.style.color = "var(--text-primary)" }}
          onMouseLeave={(e) => { e.currentTarget.style.color = copied ? "var(--text-primary)" : "var(--text-tertiary)" }}
          title={copied ? "Copied" : "Copy code"}
          aria-label={copied ? "Copied" : "Copy code"}
        >
          {copied ? <Check size={13} /> : <Copy size={13} />}
        </button>
      </div>
      <SyntaxHighlighter
        language={lang || "text"}
        style={oneDark}
        customStyle={{
          margin: 0,
          background: "transparent",
          border: "none",
          borderRadius: 0,
          padding: "12px 16px",
          fontSize: 13,
          lineHeight: 1.5,
        }}
        codeTagProps={{ style: { fontFamily: "var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace)" } }}
      >
        {code}
      </SyntaxHighlighter>
    </div>
  )
}

export const mdComponents: Components = {
  a: ({ href, children }) => <LinkChip href={href}>{children}</LinkChip>,
  img: ({ src, alt }) => (
    <span className="block my-3 rounded-xl overflow-hidden" style={{ border: "1px solid var(--border)" }}>
      <img src={src} alt={alt || "Image"} className="w-full max-w-[600px]" style={{ display: "block" }} />
    </span>
  ),
  // Intercept fenced code blocks: insight language tags render as cards;
  // everything else gets syntax-highlighted via CodeBlock.
  pre: ({ children }: any) => {
    if (isValidElement(children)) {
      const childProps = (children as React.ReactElement<any>).props
      const cls = String(childProps.className || "")
      const match = cls.match(/language-(scorecard|rich-table|insight-cards)/)
      if (match) {
        const raw = extractText(childProps.children)
        try {
          const data = JSON.parse(raw)
          if (match[1] === "scorecard" && Array.isArray(data) && data.length > 0)
            return <ScoreCards cards={data} />
          if (match[1] === "rich-table" && data && Array.isArray(data.headers) && Array.isArray(data.rows))
            return <RichTable data={data} />
          if (match[1] === "insight-cards" && Array.isArray(data) && data.length > 0)
            return <InsightCards cards={data} />
        } catch { /* invalid JSON -- fall through to normal code block */ }
      }
      // Normal fenced block: pull the language tag + raw source, highlight it.
      const langMatch = cls.match(/language-([\w+#-]+)/)
      const lang = langMatch ? langMatch[1] : ""
      const code = extractText(childProps.children).replace(/\n$/, "")
      return <CodeBlock code={code} lang={lang} />
    }
    return <CodeBlock code={extractText(children)} lang="" />
  },
  table: ({ children }) => <MarkdownTable>{children}</MarkdownTable>,
  thead: ({ children }) => (
    <thead>{children}</thead>
  ),
  th: ({ children }) => (
    <th className="text-left px-4 py-2.5 font-medium text-[12px]" style={{ borderBottom: "0.5px solid rgba(255,255,255,0.1)", color: "rgba(255,255,255,0.5)" }}>{children}</th>
  ),
  td: ({ children }) => (
    <td className="px-4 py-2 text-[13px]" style={{ borderBottom: "0.5px solid rgba(255,255,255,0.06)", color: "rgba(255,255,255,0.85)" }}>{children}</td>
  ),
}
