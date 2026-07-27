"use client"

import { useCallback, useEffect, useMemo, useState } from "react"
import { ArrowLeft, ExternalLink, FileDown, Loader2, MessageSquarePlus } from "lucide-react"
import ReactMarkdown from "react-markdown"
import { mdPlugins, mdComponents } from "./chat/markdownConfig"
import { getIntegrationIcon } from "../utils/integrationIcons"
import { pingman } from "../utils/pingman"
import useAppRouter from "../hooks/useAppRouter"
import WorkflowRunProgress from "./WorkflowRunProgress"

interface Artifact {
  url: string
  kind: string
  title: string
  created_at?: string
  tool_call_id?: string
}

interface RunDetail {
  id: string
  workflow_id: string
  workflow_name: string | null
  status: string
  triggered_at: string
  completed_at: string | null
  output: string | null
  error: string | null
  artifacts: Artifact[]
  report_markdown: string | null
  conversation_id: string | null
  has_replay_messages: boolean
}

const KIND_LABEL: Record<string, string> = {
  gdoc: "Google Doc",
  gsheet: "Google Sheet",
  gslide: "Google Slides",
  gdrive_file: "Drive File",
  notion_page: "Notion Page",
  notion_database: "Notion Database",
  linear_issue: "Linear Issue",
  linear_project: "Linear Project",
  github_issue: "GitHub Issue",
  github_pr: "GitHub PR",
  slack_message: "Slack Message",
  email: "Email",
  figma_file: "Figma File",
  report: "Report",
  link: "Link",
}

// Map artifact kind → integration icon name (the icon set is keyed on
// integration providers, not artifact kinds — collapse the kind to its
// nearest provider so we don't ship a parallel icon set).
function iconForKind(kind: string): string {
  if (kind === "gdoc") return "google_docs"
  if (kind === "gsheet") return "google_sheets"
  if (kind === "gslide") return "google_slides"
  if (kind === "gdrive_file") return "google_drive"
  if (kind === "notion_page" || kind === "notion_database") return "notion"
  if (kind === "linear_issue" || kind === "linear_project") return "linear"
  if (kind === "github_issue" || kind === "github_pr") return "github"
  if (kind === "slack_message") return "slack"
  if (kind === "email") return "gmail"
  if (kind === "figma_file") return "figma"
  return "" // generic
}

interface Props {
  workflowId: string
  runId: string
  onBack: () => void
}

export default function WorkflowRunDetailPage({ workflowId, runId, onBack }: Props) {
  const [run, setRun] = useState<RunDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [exportingPdf, setExportingPdf] = useState(false)
  const [pdfError, setPdfError] = useState<string | null>(null)
  const [followingUp, setFollowingUp] = useState(false)
  const [followUpError, setFollowUpError] = useState<string | null>(null)
  const router = useAppRouter()

  const loadRun = useCallback(async () => {
    try {
      const res = await pingman.get<RunDetail>(
        `/api/gideon/v1/workflows/${workflowId}/runs/${runId}`
      )
      if (res.ok) setRun(res.data)
      else setError(`Run not found (HTTP ${res.status})`)
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }, [workflowId, runId])

  useEffect(() => {
    setLoading(true)
    setError(null)
    loadRun()
  }, [loadRun])

  // While a run is active, the live progress component polls and renders the
  // timeline; on terminal transition it calls this to reload report/artifacts.
  const isActiveRun = run?.status === "running" || run?.status === "interrupted"

  const artifacts = run?.artifacts || []
  const reportMarkdown = useMemo(
    () => run?.report_markdown || run?.output || "",
    [run]
  )

  const handleFollowUp = async () => {
    if (followingUp || !run) return
    setFollowingUp(true)
    setFollowUpError(null)
    try {
      const res = await pingman.post<{ conversation_id: string; title: string; fallback: boolean }>(
        `/api/gideon/v1/workflows/${workflowId}/runs/${runId}/follow-up`,
      )
      if (!res.ok) {
        const detail = (res.data as { detail?: string } | undefined)?.detail
        throw new Error(detail || `HTTP ${res.status}`)
      }
      router.navigateToConversation(res.data.conversation_id)
    } catch (e: unknown) {
      setFollowUpError(e instanceof Error ? e.message : "Could not open chat.")
      setFollowingUp(false)
    }
  }

  const handleExportPdf = async () => {
    if (exportingPdf || !reportMarkdown) return
    setExportingPdf(true)
    setPdfError(null)
    try {
      const res = await pingman.post<{ url: string; filename: string }>(
        `/api/gideon/v1/workflows/${workflowId}/runs/${runId}/export-pdf`,
      )
      if (!res.ok) {
        const detail = (res.data as { detail?: string } | undefined)?.detail
        throw new Error(detail || `HTTP ${res.status}`)
      }
      // Open the new Drive PDF in a new tab.
      window.open(res.data.url, "_blank", "noopener")
    } catch (e: unknown) {
      setPdfError(e instanceof Error ? e.message : "PDF export failed.")
    } finally {
      setExportingPdf(false)
    }
  }

  return (
    <div className="flex-1 flex flex-col min-w-0 rounded-xl overflow-hidden bg-[#0c0c0c]">
      <div className="flex-1 overflow-y-auto">
        <div className="max-w-3xl mx-auto px-4 py-6 sm:p-12">
          {/* Back */}
          <button
            onClick={onBack}
            className="flex items-center gap-2 text-[13px] mb-6 text-gray-400 hover:text-white transition-colors"
          >
            <ArrowLeft size={14} />
            Back to workflow
          </button>

          {loading ? (
            <div className="flex items-center justify-center py-20">
              <Loader2 size={20} className="animate-spin text-gray-500" />
            </div>
          ) : error ? (
            <div className="text-[14px] text-red-400">{error}</div>
          ) : !run ? null : (
            <>
              {/* Status header — workflow name as title, run status as subtitle */}
              <div className="mb-6">
                <p className="text-[12px] uppercase tracking-wider text-gray-500">
                  {new Date(run.triggered_at).toLocaleString()}
                </p>
                <h1 className="mt-1 text-[28px] font-semibold tracking-tight text-white">
                  {run.workflow_name || "Workflow run"}
                </h1>
                <p className="mt-1 text-[14px] text-gray-400">
                  {run.status === "failed"
                    ? "Run failed"
                    : run.status === "completed"
                    ? "Run completed"
                    : run.status === "pending_approval"
                    ? "Awaiting approval"
                    : run.status === "interrupted"
                    ? "Interrupted — resuming"
                    : "Run in progress"}
                </p>
                {run.error && (
                  <pre className="mt-3 whitespace-pre-wrap text-[13px] text-red-300 bg-red-500/10 rounded-lg p-3">
                    {run.error}
                  </pre>
                )}
              </div>

              {/* Live progress — only while the run is active. Polls the
                  per-round checkpoint and reloads the run on completion. */}
              {isActiveRun && (
                <WorkflowRunProgress
                  workflowId={workflowId}
                  runId={runId}
                  onComplete={loadRun}
                />
              )}

              {/* Artifacts */}
              {artifacts.length > 0 && (
                <div className="mb-8">
                  <h2 className="text-[12px] font-medium uppercase tracking-wider mb-3 text-gray-500">
                    Artifacts
                  </h2>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    {artifacts.map((a, i) => {
                      const iconName = iconForKind(a.kind)
                      return (
                        <a
                          key={`${a.url}-${i}`}
                          href={a.url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="group flex items-start gap-3 p-4 rounded-2xl bg-[#161616] border border-[#222] hover:border-[#333] hover:bg-[#1a1a1a] transition-colors"
                        >
                          <span className="flex items-center justify-center size-9 shrink-0 rounded-lg bg-[#1f1f1f] border border-[#2a2a2a]">
                            {iconName ? getIntegrationIcon(iconName, 18) : <ExternalLink size={16} className="text-gray-400" />}
                          </span>
                          <div className="min-w-0 flex-1">
                            <p className="text-[11px] uppercase tracking-wider text-gray-500">
                              {KIND_LABEL[a.kind] || a.kind}
                            </p>
                            <p className="text-[14px] text-white mt-0.5 truncate group-hover:underline">
                              {a.title}
                            </p>
                          </div>
                          <ExternalLink size={14} className="shrink-0 mt-1 text-gray-500 group-hover:text-white" />
                        </a>
                      )
                    })}
                  </div>
                </div>
              )}

              {/* Report — hidden while the live progress view is active */}
              {!isActiveRun && (
              <div>
                <div className="flex items-center justify-between mb-3">
                  <h2 className="text-[12px] font-medium uppercase tracking-wider text-gray-500">
                    Report
                  </h2>
                  <div className="flex items-center gap-2">
                    {reportMarkdown && (
                      <button
                        onClick={handleFollowUp}
                        disabled={followingUp}
                        className="flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[11px] transition-colors disabled:opacity-50"
                        style={{
                          background: "rgba(80, 87, 234, 0.12)",
                          color: "#a5a9f5",
                          border: "1px solid rgba(80, 87, 234, 0.3)",
                        }}
                        title={
                          run.has_replay_messages
                            ? "Open this run in chat to follow up — full context (thinking, tool calls, results) is preserved."
                            : "Open this run in chat. Limited context — this run pre-dates full replay capture, so only the report seeds the conversation."
                        }
                      >
                        {followingUp ? (
                          <Loader2 size={11} className="animate-spin" />
                        ) : (
                          <MessageSquarePlus size={11} />
                        )}
                        {run.conversation_id ? "Continue chat" : "Follow up in chat"}
                      </button>
                    )}
                    {reportMarkdown && (
                      <button
                        onClick={handleExportPdf}
                        disabled={exportingPdf}
                        className="flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[11px] transition-colors disabled:opacity-50"
                        style={{
                          background: "rgba(255,255,255,0.04)",
                          color: "var(--text-secondary)",
                          border: "1px solid rgba(255,255,255,0.08)",
                        }}
                        title="Render this report as a PDF and save it to Gideon/PDFs/ in your Drive."
                      >
                        {exportingPdf ? (
                          <Loader2 size={11} className="animate-spin" />
                        ) : (
                          <FileDown size={11} />
                        )}
                        Export as PDF
                      </button>
                    )}
                  </div>
                </div>
                {followUpError && (
                  <p className="text-[12px] mb-3" style={{ color: "#fca5a5" }}>
                    {followUpError}
                  </p>
                )}
                {pdfError && (
                  <p className="text-[12px] mb-3" style={{ color: "#fca5a5" }}>
                    {pdfError}
                  </p>
                )}
                {reportMarkdown ? (
                  <div className="prose prose-invert prose-sm max-w-none">
                    <ReactMarkdown remarkPlugins={mdPlugins} components={mdComponents}>
                      {reportMarkdown}
                    </ReactMarkdown>
                  </div>
                ) : (
                  <p className="text-[13px] text-gray-500">No report content.</p>
                )}
              </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
