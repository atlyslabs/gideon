"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import { Check, Code2, Copy, Download, ExternalLink, Eye, Loader2, Maximize2, RefreshCw } from "lucide-react"
import { useTokenStore } from "../../hooks/useTokenStore"

/**
 * In-chat preview component. Compiles user-provided React/HTML via the
 * server-side route at /api/preview/compile and renders the result in a
 * sandboxed iframe. Three tabs: Preview, Code, Download.
 */

export interface PreviewPayload {
  framework: "react" | "html"
  code: string
  description?: string
}

interface WireframePreviewProps {
  payload: PreviewPayload
}

type ApiResponse =
  | { ok: true; cached: boolean; framework: string; srcdoc: string; size: number }
  | { ok: false; framework: string; error: { message: string; line?: number; column?: number } }

type Tab = "preview" | "code" | "download"

export default function WireframePreview({ payload }: WireframePreviewProps) {
  const [tab, setTab] = useState<Tab>("preview")
  const [state, setState] = useState<
    | { status: "idle" }
    | { status: "compiling" }
    | { status: "ready"; srcdoc: string; cached: boolean; size: number }
    | { status: "error"; message: string; line?: number; column?: number }
  >({ status: "idle" })

  // De-dup: same payload mounted twice shouldn't compile twice. Track
  // the last code hash we attempted to compile.
  const lastAttemptRef = useRef<string | null>(null)

  // Iframe ref so the fullscreen / open-in-new-tab buttons in the
  // header can act on the rendered preview without lifting the iframe
  // element out of PreviewBody.
  const iframeRef = useRef<HTMLIFrameElement | null>(null)

  // The compile route lives on the Next.js server (same origin), not on
  // the Gideon BE. We can't use `pingman` because it's configured with
  // `baseURL = NEXT_PUBLIC_API_URL` (the BE on port 8000) so any pingman
  // call routes to the BE — which 404s for /api/preview/compile.
  // Instead: same-origin `fetch()` with the auth token grabbed straight
  // from the zustand store. The Next.js API route validates that token
  // against the backend's auth layer.
  const authToken = useTokenStore((s) => s.authToken)

  const doCompile = useCallback(async () => {
    setState({ status: "compiling" })
    try {
      const res = await fetch("/api/preview/compile", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(authToken ? { Authorization: `Bearer ${authToken}` } : {}),
        },
        body: JSON.stringify({
          framework: payload.framework,
          code: payload.code,
        }),
      })

      let data: ApiResponse | { error?: { message?: string } } | null = null
      try {
        data = (await res.json()) as ApiResponse
      } catch {
        // Non-JSON response (network error page, etc.) — fall through to error.
      }

      if (!res.ok) {
        const errMsg =
          (data as { error?: { message?: string } } | null)?.error?.message ||
          `HTTP ${res.status}`
        setState({ status: "error", message: errMsg })
        return
      }
      if (!data) {
        setState({ status: "error", message: "Empty compile response" })
        return
      }
      const d = data as ApiResponse
      if (d.ok) {
        setState({
          status: "ready",
          srcdoc: d.srcdoc,
          cached: d.cached,
          size: d.size,
        })
      } else {
        setState({
          status: "error",
          message: d.error.message,
          line: d.error.line,
          column: d.error.column,
        })
      }
    } catch (e: unknown) {
      setState({
        status: "error",
        message: e instanceof Error ? e.message : "Compile request failed",
      })
    }
  }, [payload.framework, payload.code, authToken])

  // Auto-compile on first mount + whenever the code identity changes
  // (e.g. Claude edits the preview in a follow-up turn).
  useEffect(() => {
    const sig = `${payload.framework}::${payload.code}`
    if (lastAttemptRef.current === sig) return
    lastAttemptRef.current = sig
    void doCompile()
  }, [payload.framework, payload.code, doCompile])

  // Listen for runtime errors from inside the iframe. The compile may
  // succeed (Vite produces a valid bundle) but the React app can still
  // fail at render time — TypeError accessing undefined, missing DS
  // export, hook order bug, etc. The error-capture script injected into
  // the iframe srcDoc postMessages those up to us; without this we'd
  // show a blank iframe with no clue why.
  useEffect(() => {
    function onMessage(ev: MessageEvent) {
      const data = ev.data
      if (!data || typeof data !== "object" || !data.__gideonPreview) return
      if (data.kind === "error" || data.kind === "unhandledrejection") {
        setState({
          status: "error",
          message: `Preview runtime error: ${data.message}`,
          line: typeof data.line === "number" ? data.line : undefined,
          column: typeof data.column === "number" ? data.column : undefined,
        })
      } else if (data.kind === "lifecycle" && data.message === "loaded-but-empty") {
        // Bundle executed, no errors thrown, but #root is still empty.
        // Usually means the user component returned null/undefined, or
        // the import chain swallowed the mount call.
        setState({
          status: "error",
          message:
            "Preview loaded but rendered nothing. The component may be returning null, or an import is failing silently. Check that the default export is a React component.",
        })
      }
      // kind === "lifecycle" with "mounted" → all good, no action.
    }
    window.addEventListener("message", onMessage)
    return () => window.removeEventListener("message", onMessage)
  }, [])

  // ---- Download helpers ----

  const downloadSingleHtml = useCallback(() => {
    if (state.status !== "ready") return
    const blob = new Blob([state.srcdoc], { type: "text/html;charset=utf-8" })
    triggerDownload(blob, suggestedFilename(payload, "html"))
  }, [state, payload])

  const openFullscreen = useCallback(async () => {
    if (state.status !== "ready") return
    const el = iframeRef.current
    if (!el) return
    try {
      // requestFullscreen on the iframe element itself. The user exits
      // via Esc; no exit-button needed inside the iframe (which would
      // be tricky anyway given the sandbox=allow-scripts isolation).
      await el.requestFullscreen()
    } catch (e) {
      // Some browsers (older Safari) reject without a user gesture or
      // don't support iframes in fullscreen. Fall back to opening the
      // preview in a new tab via a blob URL — same content, full
      // viewport real estate, native browser controls.
      // eslint-disable-next-line no-console
      console.warn("[WireframePreview] requestFullscreen rejected, falling back to new tab:", e)
      openInNewTab()
    }
  }, [state]) // eslint-disable-line react-hooks/exhaustive-deps

  const openInNewTab = useCallback(() => {
    if (state.status !== "ready") return
    const blob = new Blob([state.srcdoc], { type: "text/html;charset=utf-8" })
    const url = URL.createObjectURL(blob)
    // `noopener` so the new tab can't reach back into our window via
    // window.opener — mirrors the iframe's sandbox isolation.
    const win = window.open(url, "_blank", "noopener,noreferrer")
    if (!win) {
      // Popup blocker — surface a friendly hint.
      // eslint-disable-next-line no-console
      console.warn("[WireframePreview] window.open blocked by browser")
    }
    // Don't revoke immediately — the new tab still needs the URL. Let
    // the browser GC it when the tab closes / after a long delay.
    setTimeout(() => URL.revokeObjectURL(url), 60_000)
  }, [state])

  const downloadZip = useCallback(async () => {
    if (state.status !== "ready") return
    try {
      // Lazy import JSZip so the chat page bundle doesn't carry it.
      // JSZip is CommonJS-only and its default-export shape varies
      // depending on whether the bundler wraps CJS modules with an
      // interop default. Some setups give us `{ default: JSZipClass }`,
      // others give us `JSZipClass` directly at the top of the
      // namespace. Probe both before falling back to the namespace.
      const mod = (await import("jszip")) as unknown as {
        default?: new () => { file: (n: string, c: string) => void; generateAsync: (o: { type: string }) => Promise<Blob> }
      } & (new () => { file: (n: string, c: string) => void; generateAsync: (o: { type: string }) => Promise<Blob> })
      const JSZip = (mod.default ?? mod) as new () => {
        file: (n: string, c: string) => void
        generateAsync: (o: { type: string }) => Promise<Blob>
      }
      if (typeof JSZip !== "function") {
        throw new Error("JSZip module did not export a constructor")
      }
      const zip = new JSZip()
      zip.file("README.md", buildReadme(payload))
      zip.file(
        payload.framework === "react" ? "App.tsx" : "index.html",
        payload.code,
      )
      zip.file("preview.html", state.srcdoc)
      const blob = await zip.generateAsync({ type: "blob" })
      triggerDownload(blob, suggestedFilename(payload, "zip"))
    } catch (e) {
      // Surface the failure in the chat — easier than digging through
      // the browser console.
      const msg = e instanceof Error ? e.message : String(e)
      // eslint-disable-next-line no-console
      console.error("[WireframePreview] zip download failed:", e)
      setState({
        status: "error",
        message: `Zip download failed: ${msg}`,
      })
    }
  }, [state, payload])

  // ---- Render ----

  return (
    <div
      className="my-3 rounded-xl overflow-hidden"
      style={{ border: "1px solid var(--border)", background: "var(--surface-elevated)" }}
    >
      {/* Header */}
      <div
        className="flex items-center justify-between px-4 py-2.5"
        style={{ borderBottom: "1px solid var(--border-subtle)" }}
      >
        <div className="flex items-center gap-2 min-w-0">
          <span
            className="text-[11px] uppercase tracking-wider px-1.5 py-0.5 rounded"
            style={{ background: "var(--surface)", color: "var(--text-tertiary)" }}
          >
            {payload.framework}
          </span>
          {payload.description && (
            <span
              className="text-[12.5px] truncate"
              style={{ color: "var(--text-secondary)" }}
              title={payload.description}
            >
              {payload.description}
            </span>
          )}
        </div>
        <div className="flex items-center gap-0.5">
          <TabButton
            active={tab === "preview"}
            onClick={() => setTab("preview")}
            icon={<Eye size={12} />}
            label="Preview"
          />
          <TabButton
            active={tab === "code"}
            onClick={() => setTab("code")}
            icon={<Code2 size={12} />}
            label="Code"
          />
          <TabButton
            active={tab === "download"}
            onClick={() => setTab("download")}
            icon={<Download size={12} />}
            label="Download"
          />
          {/* Fullscreen + new-tab buttons — only meaningful once we have
              a compiled preview to show. Disabled (grayed) otherwise so
              the UI stays consistent across states instead of buttons
              popping in. */}
          <span className="mx-1 w-px h-3.5 self-center" style={{ background: "var(--border-subtle)" }} aria-hidden="true" />
          <IconButton
            onClick={openFullscreen}
            disabled={state.status !== "ready"}
            icon={<Maximize2 size={12} />}
            title="Fullscreen (Esc to exit)"
          />
          <IconButton
            onClick={openInNewTab}
            disabled={state.status !== "ready"}
            icon={<ExternalLink size={12} />}
            title="Open in new tab"
          />
        </div>
      </div>

      {/* Body */}
      <div style={{ background: "var(--surface)" }}>
        {tab === "preview" && (
          <PreviewBody
            state={state}
            onRetry={doCompile}
            iframeRef={iframeRef}
          />
        )}
        {tab === "code" && (
          <CodeBody code={payload.code} framework={payload.framework} />
        )}
        {tab === "download" && (
          <DownloadBody
            ready={state.status === "ready"}
            onZip={downloadZip}
            onHtml={downloadSingleHtml}
            framework={payload.framework}
          />
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function TabButton({
  active,
  onClick,
  icon,
  label,
}: {
  active: boolean
  onClick: () => void
  icon: React.ReactNode
  label: string
}) {
  return (
    <button
      onClick={onClick}
      className="flex items-center gap-1.5 px-2.5 py-1 rounded text-[11.5px] transition-colors"
      style={{
        background: active ? "var(--surface-hover)" : "transparent",
        color: active ? "var(--text-primary)" : "var(--text-tertiary)",
      }}
    >
      {icon}
      {label}
    </button>
  )
}

function IconButton({
  onClick,
  icon,
  title,
  disabled,
}: {
  onClick: () => void
  icon: React.ReactNode
  title: string
  disabled?: boolean
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-label={title}
      className="flex items-center justify-center w-7 h-7 rounded transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
      style={{
        background: "transparent",
        color: "var(--text-tertiary)",
      }}
    >
      {icon}
    </button>
  )
}

function PreviewBody({
  state,
  onRetry,
  iframeRef,
}: {
  state:
    | { status: "idle" }
    | { status: "compiling" }
    | { status: "ready"; srcdoc: string; cached: boolean; size: number }
    | { status: "error"; message: string; line?: number; column?: number }
  onRetry: () => void
  iframeRef: React.RefObject<HTMLIFrameElement | null>
}) {
  if (state.status === "compiling" || state.status === "idle") {
    return (
      <div
        className="flex items-center justify-center gap-2 px-4 py-12 text-[12.5px]"
        style={{ color: "var(--text-tertiary)" }}
        role="status"
        aria-live="polite"
      >
        <Loader2 size={14} className="animate-spin" />
        <span>Compiling preview… usually 1-3s</span>
      </div>
    )
  }

  if (state.status === "error") {
    return (
      <div className="px-4 py-4" role="alert">
        <p className="text-[12.5px] font-medium" style={{ color: "#ef4444" }}>
          Compile failed
        </p>
        <pre
          className="mt-2 text-[11.5px] whitespace-pre-wrap break-words"
          style={{ color: "var(--text-secondary)", fontFamily: "ui-monospace, monospace" }}
        >
          {state.message}
          {state.line ? `\n  at line ${state.line}${state.column ? `:${state.column}` : ""}` : ""}
        </pre>
        <button
          onClick={onRetry}
          className="mt-3 flex items-center gap-1.5 px-2.5 py-1 rounded text-[11.5px]"
          style={{
            background: "var(--surface-hover)",
            color: "var(--text-secondary)",
          }}
        >
          <RefreshCw size={11} />
          Retry compile
        </button>
      </div>
    )
  }

  // state.status === "ready"
  return (
    <div>
      <iframe
        ref={iframeRef as React.Ref<HTMLIFrameElement>}
        // sandbox="allow-scripts" only: no same-origin → scripts in the
        // preview can't read parent cookies or fetch from Gideon's API.
        // `allowFullScreen` lets the Maximize button in the header
        // promote the iframe via requestFullscreen().
        sandbox="allow-scripts"
        allowFullScreen
        srcDoc={state.srcdoc}
        className="w-full"
        style={{ height: 480, border: "none", background: "#ffffff" }}
        title="Preview"
      />
      <div
        className="flex items-center justify-between px-3 py-1.5 text-[10.5px]"
        style={{ borderTop: "1px solid var(--border-subtle)", color: "var(--text-tertiary)" }}
      >
        <span>
          {state.cached ? "cached" : "fresh"} · {formatSize(state.size)}
        </span>
        <button
          onClick={onRetry}
          className="flex items-center gap-1 hover:bg-white/5 px-1.5 py-0.5 rounded"
          style={{ color: "var(--text-tertiary)" }}
          title="Recompile"
        >
          <RefreshCw size={10} />
        </button>
      </div>
    </div>
  )
}

function CodeBody({ code, framework }: { code: string; framework: "react" | "html" }) {
  const [copied, setCopied] = useState(false)

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(code)
      setCopied(true)
      // Revert the icon back to the clipboard glyph after 1.5s. Short
      // enough that the user knows the action completed but doesn't
      // linger so long that re-clicks look broken.
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // navigator.clipboard can fail in some sandboxed contexts. Best
      // effort — if it throws we leave the icon as-is rather than
      // surfacing a noisy error toast for an action the user will just
      // retry with Cmd+C.
    }
  }, [code])

  return (
    <div className="relative">
      <button
        type="button"
        onClick={handleCopy}
        className="absolute top-2 right-2 inline-flex items-center gap-1 rounded-md px-2 py-1 text-[10.5px] transition-colors"
        style={{
          background: "var(--surface-elevated, var(--surface))",
          border: "1px solid var(--border-subtle)",
          color: copied ? "var(--accent-success, #16a34a)" : "var(--text-secondary)",
        }}
        aria-label={copied ? "Copied" : "Copy code"}
        title={copied ? "Copied!" : "Copy"}
      >
        {copied ? <Check size={12} /> : <Copy size={12} />}
        <span>{copied ? "Copied" : "Copy"}</span>
      </button>
      <pre
        className="m-0 px-4 py-3 text-[11.5px] leading-relaxed overflow-auto"
        style={{
          color: "var(--text-secondary)",
          background: "var(--surface)",
          fontFamily: "ui-monospace, SFMono-Regular, monospace",
          maxHeight: 480,
        }}
      >
        <code>{code}</code>
        <div
          className="mt-2 pt-2 text-[10.5px]"
          style={{ borderTop: "1px solid var(--border-subtle)", color: "var(--text-tertiary)" }}
        >
          {framework === "react" ? "React (.tsx)" : "HTML"} · {Buffer.byteLength?.(code) ?? code.length} bytes
        </div>
      </pre>
    </div>
  )
}

function DownloadBody({
  ready,
  onZip,
  onHtml,
  framework,
}: {
  ready: boolean
  onZip: () => void
  onHtml: () => void
  framework: "react" | "html"
}) {
  return (
    <div className="px-4 py-4 space-y-2">
      <p className="text-[12px]" style={{ color: "var(--text-secondary)" }}>
        Download the preview as a self-contained file or a project zip.
      </p>
      <div className="flex flex-col gap-2 pt-1">
        <DownloadButton
          disabled={!ready}
          onClick={onHtml}
          label="Single .html file"
          sub="Inline JS/CSS · opens in any browser"
        />
        <DownloadButton
          disabled={!ready}
          onClick={onZip}
          label=".zip project"
          sub={
            framework === "react"
              ? "App.tsx + preview.html + README"
              : "index.html + preview.html + README"
          }
        />
      </div>
      {!ready && (
        <p className="text-[10.5px] pt-1" style={{ color: "var(--text-tertiary)" }}>
          Download enabled once the compile completes.
        </p>
      )}
    </div>
  )
}

function DownloadButton({
  disabled,
  onClick,
  label,
  sub,
}: {
  disabled: boolean
  onClick: () => void
  label: string
  sub: string
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className="flex items-center justify-between gap-3 px-3 py-2 rounded-lg text-left disabled:opacity-50 disabled:cursor-not-allowed"
      style={{
        background: "var(--surface-hover)",
        border: "1px solid var(--border-subtle)",
      }}
    >
      <div>
        <p className="text-[12.5px] font-medium" style={{ color: "var(--text-primary)" }}>
          {label}
        </p>
        <p className="text-[10.5px]" style={{ color: "var(--text-tertiary)" }}>
          {sub}
        </p>
      </div>
      <Download size={14} style={{ color: "var(--text-secondary)" }} />
    </button>
  )
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`
}

function suggestedFilename(payload: PreviewPayload, ext: "html" | "zip"): string {
  const slug = (payload.description || "gideon-preview")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40) || "gideon-preview"
  return `${slug}.${ext}`
}

function triggerDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement("a")
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  setTimeout(() => URL.revokeObjectURL(url), 1_000)
}

function buildReadme(payload: PreviewPayload): string {
  return [
    `# Gideon preview${payload.description ? ` — ${payload.description}` : ""}`,
    "",
    `Generated by Gideon's wireframe preview feature.`,
    "",
    `## Files`,
    `- \`preview.html\` — self-contained rendered preview (open in any browser)`,
    payload.framework === "react"
      ? `- \`App.tsx\` — the React source component`
      : `- \`index.html\` — the HTML source`,
    "",
    payload.framework === "react"
      ? "To run locally, drop App.tsx into a Vite + React project with Tailwind configured."
      : "To run locally, open `preview.html` in a browser.",
  ].join("\n")
}
