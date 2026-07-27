"use client"

import { useCallback, useEffect, useMemo, useState } from "react"
import { ArrowLeft, ClipboardList, Plus, Eye, Users, MessageSquare, BarChart3, Loader2, X } from "lucide-react"
import useAppRouter from "../hooks/useAppRouter"
import useIsMobile from "../hooks/useIsMobile"
import { useTokenStore } from "../hooks/useTokenStore"
import { pingman } from "../utils/pingman"
import useMessages from "../hooks/useMessages"
import useChatStream from "../hooks/useChatStream"
import {
  useSurveysList,
  useSurveyAdmin,
  useSurveyResponses,
  useSurveyAnalysis,
  useOrgBreakdown,
} from "../hooks/useSurveys"
import ChatMessages from "./chat/ChatMessages"
import ChatInput from "./chat/ChatInput"
import SurveyModal from "./SurveyModal"
import type { SurveyAdmin, SurveyEligibility } from "../types"


type RightTab = "preview" | "eligibility" | "responses" | "chat"


function statusColor(status: string): { bg: string; fg: string } {
  if (status === "active") return { bg: "rgba(34,197,94,0.15)", fg: "#4ade80" }
  if (status === "closed") return { bg: "rgba(255,255,255,0.08)", fg: "var(--text-tertiary)" }
  return { bg: "rgba(80,87,234,0.15)", fg: "#a5a9f5" }
}


export default function SurveyBuilderPage({ onBack }: { onBack: () => void }) {
  const isMobile = useIsMobile()
  const router = useAppRouter()
  const selectedSlug = router.subId  // /surveys/{slug}
  const [createOpen, setCreateOpen] = useState(false)
  const [previewOpen, setPreviewOpen] = useState(false)
  const [activeTab, setActiveTab] = useState<RightTab>("chat")

  const { surveys, mutate: mutateList, create } = useSurveysList()

  // Default-select most recent draft on first load
  useEffect(() => {
    if (!selectedSlug && surveys.length > 0 && !createOpen) {
      router.navigate(`/surveys/${surveys[0].slug}`)
    }
  }, [selectedSlug, surveys, createOpen, router])

  const handleCreate = useCallback(
    async (slug: string, title: string, intro?: string) => {
      const result = await create({ slug, title, intro_text: intro })
      router.navigate(`/surveys/${result.slug}`)
      setCreateOpen(false)
      setActiveTab("chat")
    },
    [create, router]
  )

  return (
    <div
      className={`flex-1 flex flex-col min-w-0 overflow-hidden ${isMobile ? "" : "rounded-2xl"}`}
      style={{ background: "var(--surface)", border: isMobile ? "none" : "1px solid rgba(255,255,255,0.06)" }}
    >
      {/* Header */}
      <div className="flex items-center justify-between gap-3 px-4 py-3 shrink-0" style={{ borderBottom: "1px solid var(--border)" }}>
        <div className="flex items-center gap-2 min-w-0">
          <button onClick={onBack} className="p-1.5 rounded-lg hover:bg-white/5" style={{ color: "var(--text-tertiary)" }}>
            <ArrowLeft size={16} />
          </button>
          <ClipboardList size={16} style={{ color: "var(--text-secondary)" }} />
          <h1 className="text-[14px] font-semibold" style={{ color: "var(--text-primary)" }}>Surveys</h1>
        </div>
        <button
          onClick={() => setCreateOpen(true)}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[12px] font-medium transition-colors"
          style={{ background: "#5057EA", color: "#ffffff" }}
        >
          <Plus size={14} /> New survey
        </button>
      </div>

      {/* Body — split: list left, detail right */}
      <div className="flex-1 flex min-h-0">
        {/* List */}
        <div
          className={`shrink-0 overflow-y-auto ${isMobile ? "hidden" : ""}`}
          style={{ width: 280, borderRight: "1px solid var(--border)" }}
        >
          {surveys.length === 0 && (
            <div className="px-4 py-8 text-center">
              <p className="text-[13px]" style={{ color: "var(--text-tertiary)" }}>
                No surveys yet. Click &quot;New survey&quot; to start.
              </p>
            </div>
          )}
          {surveys.map((s) => {
            const c = statusColor(s.status)
            const isSel = s.slug === selectedSlug
            return (
              <button
                key={s.id}
                onClick={() => router.navigate(`/surveys/${s.slug}`)}
                className="w-full text-left px-4 py-3 transition-colors"
                style={{
                  background: isSel ? "var(--surface-hover)" : "transparent",
                  borderBottom: "1px solid var(--border)",
                  borderLeft: isSel ? "2px solid #5057EA" : "2px solid transparent",
                }}
              >
                <div className="flex items-center gap-2 mb-1">
                  <span
                    className="px-1.5 py-0.5 rounded text-[10px] font-medium uppercase tracking-wide"
                    style={{ background: c.bg, color: c.fg }}
                  >
                    {s.status}
                  </span>
                  <span className="text-[11px]" style={{ color: "var(--text-tertiary)" }}>
                    {s.submitted_count} responses
                  </span>
                </div>
                <p className="text-[13px] font-medium truncate" style={{ color: "var(--text-primary)" }}>
                  {s.title}
                </p>
                <p className="text-[11px] mt-0.5 truncate" style={{ color: "var(--text-tertiary)" }}>
                  {s.slug} · {s.question_count} questions
                </p>
              </button>
            )
          })}
        </div>

        {/* Detail */}
        <div className="flex-1 flex flex-col min-w-0">
          {!selectedSlug ? (
            <EmptyDetail />
          ) : (
            <SurveyDetail
              key={selectedSlug}
              slug={selectedSlug}
              activeTab={activeTab}
              onTabChange={setActiveTab}
              onPreview={() => setPreviewOpen(true)}
              onListMutate={mutateList}
            />
          )}
        </div>
      </div>

      {createOpen && (
        <CreateSurveyModal
          existingSlugs={new Set(surveys.map((s) => s.slug))}
          onCreate={handleCreate}
          onClose={() => setCreateOpen(false)}
        />
      )}

      {previewOpen && selectedSlug && (
        <PreviewSurveyDialog slug={selectedSlug} onClose={() => setPreviewOpen(false)} />
      )}
    </div>
  )
}


// ---------------------------------------------------------------------------
// Empty state
// ---------------------------------------------------------------------------

function EmptyDetail() {
  return (
    <div className="flex-1 flex items-center justify-center px-4">
      <div className="text-center max-w-[400px]">
        <ClipboardList size={32} style={{ color: "var(--text-tertiary)" }} className="mx-auto mb-3" />
        <p className="text-[15px] font-medium mb-1" style={{ color: "var(--text-primary)" }}>
          Select or create a survey
        </p>
        <p className="text-[13px]" style={{ color: "var(--text-secondary)" }}>
          Click &quot;New survey&quot; to start. Then chat with Gideon to draft questions, target the right people, and analyze responses.
        </p>
      </div>
    </div>
  )
}


// ---------------------------------------------------------------------------
// Detail (the right panel)
// ---------------------------------------------------------------------------

function SurveyDetail({
  slug,
  activeTab,
  onTabChange,
  onPreview,
  onListMutate,
}: {
  slug: string
  activeTab: RightTab
  onTabChange: (t: RightTab) => void
  onPreview: () => void
  onListMutate: () => void
}) {
  // Refresh the survey while a chat reply is streaming so tool-driven edits show up live.
  const { survey, loading, mutate, setStatus } = useSurveyAdmin(slug, { refreshInterval: 4000 })
  const [statusBusy, setStatusBusy] = useState<string | null>(null)

  const handleStatus = useCallback(
    async (next: "draft" | "active" | "closed") => {
      if (!survey) return
      const ok = next === "active"
        ? confirm(`Activate "${survey.title}"? Eligible users will see this on their next page load.`)
        : next === "closed"
        ? confirm(`Close "${survey.title}"? It cannot be reopened.`)
        : true
      if (!ok) return
      setStatusBusy(next)
      try {
        await setStatus(next)
        onListMutate()
      } finally {
        setStatusBusy(null)
      }
    },
    [survey, setStatus, onListMutate]
  )

  if (loading || !survey) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <Loader2 size={20} className="animate-spin" style={{ color: "var(--text-tertiary)" }} />
      </div>
    )
  }

  const c = statusColor(survey.status)

  return (
    <div className="flex-1 flex flex-col min-h-0">
      {/* Title bar */}
      <div className="px-5 py-4 shrink-0" style={{ borderBottom: "1px solid var(--border)" }}>
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2 mb-1">
              <span
                className="px-2 py-0.5 rounded text-[10px] font-medium uppercase tracking-wide"
                style={{ background: c.bg, color: c.fg }}
              >
                {survey.status}
              </span>
              <span className="text-[11px]" style={{ color: "var(--text-tertiary)" }}>
                slug: {survey.slug}
              </span>
            </div>
            <h2 className="text-[18px] font-semibold truncate" style={{ color: "var(--text-primary)" }}>
              {survey.title}
            </h2>
            {survey.intro_text && (
              <p className="text-[13px] mt-1 line-clamp-2" style={{ color: "var(--text-secondary)" }}>
                {survey.intro_text}
              </p>
            )}
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <button
              onClick={onPreview}
              className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-[12px] hover:bg-white/5"
              style={{ color: "var(--text-secondary)" }}
            >
              <Eye size={13} /> Preview
            </button>
            {survey.status === "draft" && (
              <button
                onClick={() => handleStatus("active")}
                disabled={statusBusy === "active" || survey.questions.length === 0}
                className="px-3 py-1.5 rounded-lg text-[12px] font-medium disabled:opacity-40"
                style={{ background: "#22c55e", color: "#ffffff" }}
              >
                {statusBusy === "active" ? "..." : "Activate"}
              </button>
            )}
            {survey.status === "active" && (
              <button
                onClick={() => handleStatus("closed")}
                disabled={statusBusy === "closed"}
                className="px-3 py-1.5 rounded-lg text-[12px] font-medium disabled:opacity-40"
                style={{ background: "rgba(255,255,255,0.08)", color: "var(--text-secondary)" }}
              >
                {statusBusy === "closed" ? "..." : "Close"}
              </button>
            )}
          </div>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex items-center gap-1 px-3 pt-2 shrink-0" style={{ borderBottom: "1px solid var(--border)" }}>
        <TabButton label="Chat with Gideon" icon={MessageSquare} active={activeTab === "chat"} onClick={() => onTabChange("chat")} />
        <TabButton label="Form" icon={Eye} active={activeTab === "preview"} onClick={() => onTabChange("preview")} />
        <TabButton label="Targeting" icon={Users} active={activeTab === "eligibility"} onClick={() => onTabChange("eligibility")} />
        <TabButton label="Responses" icon={BarChart3} active={activeTab === "responses"} onClick={() => onTabChange("responses")} />
      </div>

      {/* Tab content */}
      <div className="flex-1 overflow-hidden min-h-0">
        {activeTab === "chat" && <BuilderChatTab survey={survey} onSurveyMutate={mutate} onListMutate={onListMutate} />}
        {activeTab === "preview" && <FormPreviewTab survey={survey} />}
        {activeTab === "eligibility" && <EligibilityTab survey={survey} />}
        {activeTab === "responses" && <ResponsesTab slug={survey.slug} status={survey.status} />}
      </div>
    </div>
  )
}


function TabButton({
  label,
  icon: Icon,
  active,
  onClick,
}: {
  label: string
  icon: typeof MessageSquare
  active: boolean
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      className="flex items-center gap-1.5 px-3 py-2 text-[12px] transition-colors relative"
      style={{
        color: active ? "var(--text-primary)" : "var(--text-tertiary)",
        fontWeight: active ? 600 : 400,
      }}
    >
      <Icon size={13} />
      {label}
      {active && (
        <span
          className="absolute left-2 right-2 -bottom-px h-[2px]"
          style={{ background: "#5057EA" }}
        />
      )}
    </button>
  )
}


// ---------------------------------------------------------------------------
// Tab: Form preview
// ---------------------------------------------------------------------------

function FormPreviewTab({ survey }: { survey: SurveyAdmin }) {
  if (survey.questions.length === 0) {
    return (
      <div className="h-full flex items-center justify-center px-4">
        <div className="text-center max-w-[400px]">
          <p className="text-[14px] font-medium mb-1" style={{ color: "var(--text-primary)" }}>
            No questions yet
          </p>
          <p className="text-[13px]" style={{ color: "var(--text-secondary)" }}>
            Switch to &quot;Chat with Gideon&quot; and ask it to draft questions for you.
          </p>
        </div>
      </div>
    )
  }
  return (
    <div className="h-full overflow-y-auto px-5 py-5 space-y-4">
      {survey.questions.map((q, i) => (
        <div
          key={q.id}
          className="p-4 rounded-xl"
          style={{ background: "var(--surface-elevated)", border: "1px solid var(--border)" }}
        >
          <div className="flex items-center gap-2 mb-1">
            <span className="text-[10px] uppercase tracking-wide" style={{ color: "var(--text-tertiary)" }}>
              Q{i + 1} · {q.type}
              {q.required && " · required"}
            </span>
          </div>
          <p className="text-[14px]" style={{ color: "var(--text-primary)" }}>
            {q.prompt}
          </p>
          {q.options && q.options.length > 0 && (
            <ul className="mt-2 space-y-1">
              {q.options.map((opt) => (
                <li key={opt} className="text-[12px]" style={{ color: "var(--text-secondary)" }}>
                  • {opt}
                </li>
              ))}
            </ul>
          )}
        </div>
      ))}
    </div>
  )
}


// ---------------------------------------------------------------------------
// Tab: Eligibility / targeting
// ---------------------------------------------------------------------------

function EligibilityTab({ survey }: { survey: SurveyAdmin }) {
  const { breakdown, loading } = useOrgBreakdown()
  const elig: SurveyEligibility = survey.eligibility || {}
  const departments = elig.departments || []
  const roleKeywords = elig.role_keywords || []
  const emails = elig.emails || []

  const eligibleCount = useMemo(() => {
    if (!breakdown) return null
    if (!departments.length && !roleKeywords.length && !emails.length) {
      return breakdown.total_users
    }
    // Approximate: count of users whose department is in the list (we do not
    // have per-user role data on the client, just role distribution). For an
    // exact count, this would need a dedicated /admin/eligibility-count
    // endpoint — fine to skip in v1.
    if (departments.length && !roleKeywords.length) {
      const deptSum = breakdown.departments
        .filter((d) => departments.includes(d.name))
        .reduce((acc, d) => acc + d.count, 0)
      return deptSum
    }
    return null
  }, [breakdown, departments, roleKeywords, emails])

  return (
    <div className="h-full overflow-y-auto px-5 py-5 space-y-5">
      <Section title="Targeting" hint="A user must pass departments AND role keywords (if set). Specific emails always pass.">
        <FilterGroup label="Departments" items={departments} />
        <FilterGroup label="Role keywords (substring match, case-insensitive)" items={roleKeywords} />
        <FilterGroup label="Allowlisted emails" items={emails} />
        {!departments.length && !roleKeywords.length && !emails.length && (
          <p className="text-[12px] italic" style={{ color: "var(--text-tertiary)" }}>
            Empty filter — every Gideon user will see this survey.
          </p>
        )}
      </Section>

      <Section
        title="Estimated reach"
        hint={
          eligibleCount === null
            ? "Approximation requires a dedicated count endpoint. Listed below for reference."
            : `≈ ${eligibleCount} eligible users (out of ${breakdown?.total_users ?? "—"})`
        }
      >
        <p className="text-[12px]" style={{ color: "var(--text-secondary)" }}>
          {eligibleCount !== null && (
            <>{eligibleCount} of {breakdown?.total_users} users.</>
          )}
        </p>
      </Section>

      <Section title="Org chart reference" hint="Live counts from users.department / users.role.">
        {loading || !breakdown ? (
          <Loader2 size={16} className="animate-spin" style={{ color: "var(--text-tertiary)" }} />
        ) : (
          <div className="grid grid-cols-2 gap-4">
            <div>
              <p className="text-[11px] uppercase mb-2" style={{ color: "var(--text-tertiary)" }}>Departments</p>
              <ul className="space-y-1">
                {breakdown.departments.map((d) => (
                  <li key={d.name} className="flex justify-between text-[12px]">
                    <span style={{ color: "var(--text-secondary)" }}>{d.name}</span>
                    <span style={{ color: "var(--text-tertiary)" }}>{d.count}</span>
                  </li>
                ))}
              </ul>
            </div>
            <div>
              <p className="text-[11px] uppercase mb-2" style={{ color: "var(--text-tertiary)" }}>Roles (top)</p>
              <ul className="space-y-1">
                {breakdown.roles.slice(0, 20).map((r) => (
                  <li key={r.name} className="flex justify-between text-[12px]">
                    <span className="truncate pr-2" style={{ color: "var(--text-secondary)" }}>{r.name}</span>
                    <span style={{ color: "var(--text-tertiary)" }}>{r.count}</span>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        )}
      </Section>
    </div>
  )
}


function Section({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="text-[13px] font-medium mb-1" style={{ color: "var(--text-primary)" }}>
        {title}
      </p>
      {hint && (
        <p className="text-[12px] mb-2" style={{ color: "var(--text-tertiary)" }}>
          {hint}
        </p>
      )}
      <div>{children}</div>
    </div>
  )
}

function FilterGroup({ label, items }: { label: string; items: string[] }) {
  return (
    <div className="mb-3">
      <p className="text-[11px] uppercase mb-1" style={{ color: "var(--text-tertiary)" }}>{label}</p>
      {items.length === 0 ? (
        <p className="text-[12px] italic" style={{ color: "var(--text-tertiary)" }}>(none)</p>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {items.map((it) => (
            <span
              key={it}
              className="px-2 py-0.5 rounded text-[12px]"
              style={{ background: "var(--surface-elevated)", color: "var(--text-secondary)", border: "1px solid var(--border)" }}
            >
              {it}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}


// ---------------------------------------------------------------------------
// Tab: Responses (raw table + analysis)
// ---------------------------------------------------------------------------

function ResponsesTab({ slug, status }: { slug: string; status: string }) {
  const { responses, loading: loadingResp } = useSurveyResponses(slug)
  const { analysis, generatedAt, loading: loadingAnalysis, refreshing, refresh } = useSurveyAnalysis(slug)
  const submitted = responses.filter((r) => r.status === "submitted")
  const busy = loadingAnalysis || refreshing

  return (
    <div className="h-full overflow-y-auto px-5 py-5 space-y-5">
      <div className="flex items-center justify-between">
        <p className="text-[13px]" style={{ color: "var(--text-secondary)" }}>
          {submitted.length} submitted · {responses.length - submitted.length} dismissed
          {generatedAt && (
            <span className="ml-3 text-[11px]" style={{ color: "var(--text-tertiary)" }}>
              Analysis: {new Date(generatedAt).toLocaleString()}
            </span>
          )}
        </p>
        <button
          onClick={refresh}
          disabled={busy}
          aria-busy={busy}
          className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-[12px] hover:bg-white/5 disabled:opacity-60 disabled:cursor-wait"
          style={{ color: "var(--text-secondary)" }}
          title={refreshing ? "Running Claude analysis — usually 5-15s" : undefined}
        >
          {busy && <Loader2 size={12} className="animate-spin" />}
          {refreshing
            ? "Analyzing responses…"
            : loadingAnalysis
              ? "Loading…"
              : "Refresh analysis"}
        </button>
      </div>

      {refreshing && (
        <div
          className="px-3 py-2 rounded-lg text-[12px] flex items-center gap-2"
          style={{
            background: "var(--surface-elevated)",
            border: "1px solid var(--border)",
            color: "var(--text-secondary)",
          }}
          role="status"
          aria-live="polite"
        >
          <Loader2 size={14} className="animate-spin" style={{ color: "var(--text-tertiary)" }} />
          <span>
            Running analysis on {submitted.length} submitted response{submitted.length === 1 ? "" : "s"}.
            Usually 5-15s — feel free to wait or switch tabs.
          </span>
        </div>
      )}

      {analysis && (
        <div className="p-4 rounded-xl space-y-3" style={{ background: "var(--surface-elevated)", border: "1px solid var(--border)" }}>
          <p className="text-[14px] font-medium" style={{ color: "var(--text-primary)" }}>{analysis.headline}</p>
          {analysis.themes && analysis.themes.length > 0 && (
            <div>
              <p className="text-[11px] uppercase mb-2" style={{ color: "var(--text-tertiary)" }}>Themes</p>
              <ul className="space-y-2">
                {analysis.themes.map((t) => (
                  <li key={t.name} className="text-[12px]">
                    <span className="font-medium" style={{ color: "var(--text-primary)" }}>{t.name}</span>{" "}
                    <span style={{ color: "var(--text-tertiary)" }}>(weight {t.weight})</span>
                    <p style={{ color: "var(--text-secondary)" }}>{t.description}</p>
                    {t.supporting_quotes.length > 0 && (
                      <ul className="mt-1 ml-3 space-y-0.5">
                        {t.supporting_quotes.map((q, i) => (
                          <li key={i} className="italic" style={{ color: "var(--text-tertiary)" }}>&ldquo;{q}&rdquo;</li>
                        ))}
                      </ul>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {analysis.gaps && analysis.gaps.length > 0 && (
            <div>
              <p className="text-[11px] uppercase mb-1" style={{ color: "var(--text-tertiary)" }}>Top gaps</p>
              <ul className="space-y-0.5 text-[12px]" style={{ color: "var(--text-secondary)" }}>
                {analysis.gaps.map((g, i) => (
                  <li key={i}>• {g}</li>
                ))}
              </ul>
            </div>
          )}
          {analysis.recommendations && analysis.recommendations.length > 0 && (
            <div>
              <p className="text-[11px] uppercase mb-1" style={{ color: "var(--text-tertiary)" }}>Recommendations</p>
              <ul className="space-y-0.5 text-[12px]" style={{ color: "var(--text-secondary)" }}>
                {analysis.recommendations.map((r, i) => (
                  <li key={i}>• {r}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      <div>
        <p className="text-[11px] uppercase mb-2" style={{ color: "var(--text-tertiary)" }}>Submitted ({submitted.length})</p>
        {loadingResp ? (
          <Loader2 size={16} className="animate-spin" style={{ color: "var(--text-tertiary)" }} />
        ) : submitted.length === 0 ? (
          <p className="text-[12px]" style={{ color: "var(--text-tertiary)" }}>
            No submissions yet.{status === "active" && " Survey is live — eligible users will see it on their next page load."}
          </p>
        ) : (
          <div className="rounded-xl overflow-hidden" style={{ border: "1px solid var(--border)" }}>
            {submitted.map((r) => (
              <div key={r.id} className="px-4 py-3" style={{ borderBottom: "1px solid var(--border)" }}>
                <p className="text-[12px] mb-1" style={{ color: "var(--text-primary)" }}>
                  <span className="font-medium">{r.user_name || r.user_email}</span>
                  <span style={{ color: "var(--text-tertiary)" }}> · {r.user_role || "—"} · {r.user_department || "—"}</span>
                </p>
                <pre className="text-[11px] whitespace-pre-wrap" style={{ color: "var(--text-secondary)" }}>
                  {JSON.stringify(r.answers, null, 2)}
                </pre>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}


// ---------------------------------------------------------------------------
// Tab: Builder chat (Gideon does the work)
// ---------------------------------------------------------------------------

function BuilderChatTab({
  survey,
  onSurveyMutate,
  onListMutate,
}: {
  survey: SurveyAdmin
  onSurveyMutate: () => void
  onListMutate: () => void
}) {
  const authToken = useTokenStore((s) => s.authToken)
  const [convId, setConvId] = useState<string | null>(survey.builder_conversation_id)
  const [convError, setConvError] = useState<string | null>(null)

  // Lazily create the conversation the first time the tab opens. Use pingman
  // so the request hits NEXT_PUBLIC_API_URL (port 8000) directly — a raw
  // relative fetch would go through the Next.js dev server (3002) which has
  // no rewrite for /api/gideon/* and would 404.
  useEffect(() => {
    if (convId) return
    let cancelled = false
    ;(async () => {
      try {
        const res = await pingman.post<{ conversation_id: string }>(
          `/api/gideon/v1/surveys/admin/${survey.slug}/conversation`,
          {}
        )
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        if (!cancelled) {
          setConvId(res.data.conversation_id)
          onSurveyMutate()
        }
      } catch (e) {
        if (!cancelled) setConvError(e instanceof Error ? e.message : "Failed to start chat")
      }
    })()
    return () => {
      cancelled = true
    }
  }, [convId, survey.slug, onSurveyMutate])

  const { messages, mutate: mutateMessages } = useMessages(convId)

  const {
    rows,
    isStreaming,
    syncDbMessages,
    handleSendMessage,
    handleEditMessage,
    handleStop,
  } = useChatStream({
    authToken,
    activeConversationId: convId,
    onConversationCreated: () => {
      // Builder conversation already exists — should not fire here
      onSurveyMutate()
    },
    onTitleUpdate: () => {},
    mutateMessages,
    mutateConversations: () => {},
  })

  useEffect(() => {
    if (messages.length > 0) syncDbMessages(messages)
  }, [messages, syncDbMessages])

  // When a Gideon turn finishes, refresh the survey + list so any
  // tool-driven edits to questions/eligibility/status appear immediately.
  useEffect(() => {
    if (!isStreaming) {
      onSurveyMutate()
      onListMutate()
    }
  }, [isStreaming, onSurveyMutate, onListMutate])

  if (convError) {
    return (
      <div className="h-full flex items-center justify-center px-4">
        <p className="text-[13px]" style={{ color: "#f87171" }}>Couldn&apos;t open builder chat: {convError}</p>
      </div>
    )
  }

  if (!convId) {
    return (
      <div className="h-full flex items-center justify-center">
        <Loader2 size={20} className="animate-spin" style={{ color: "var(--text-tertiary)" }} />
      </div>
    )
  }

  return (
    <div className="h-full flex flex-col min-h-0">
      <div className="px-5 py-2 shrink-0" style={{ background: "rgba(80,87,234,0.08)", borderBottom: "1px solid var(--border)" }}>
        <p className="text-[11px]" style={{ color: "#a5a9f5" }}>
          You&apos;re editing <strong>{survey.slug}</strong>. Tell Gideon what you want — it has tools to draft, update,
          activate, and analyze this survey. Changes appear live in the other tabs.
        </p>
      </div>
      {/* ChatMessages must be a direct flex child — its outer is `flex-1 flex
          flex-col overflow-hidden`, so wrapping it nests two flex-1s and breaks
          the height cascade (scroll inside fails). Mirrors Layout.tsx.
          We deliberately omit `onSend` to suppress its inline empty-state input;
          the bottom ChatInput below is the only composer. */}
      <ChatMessages
        rows={rows}
        isStreaming={isStreaming}
        userName=""
        userAvatarUrl={undefined}
        onEditMessage={handleEditMessage}
        onStop={handleStop}
        sendDisabled={isStreaming}
        connectedProviders={[]}
        autoMode={false}
        onAutoModeChange={() => {}}
      />
      <div className="shrink-0">
        <ChatInput
          onSend={handleSendMessage}
          onStop={handleStop}
          disabled={isStreaming}
          connectedProviders={[]}
          conversationId={convId}
          autoMode={false}
          onAutoModeChange={() => {}}
        />
      </div>
    </div>
  )
}


// ---------------------------------------------------------------------------
// Create-survey modal (slug + title only — questions are added via Gideon chat)
// ---------------------------------------------------------------------------

function CreateSurveyModal({
  existingSlugs,
  onCreate,
  onClose,
}: {
  existingSlugs: Set<string>
  onCreate: (slug: string, title: string, intro?: string) => Promise<void>
  onClose: () => void
}) {
  const [title, setTitle] = useState("")
  const [slug, setSlug] = useState("")
  const [intro, setIntro] = useState("")
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const slugFromTitle = useMemo(() => {
    return title
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 60)
  }, [title])

  const finalSlug = slug.trim() || slugFromTitle
  const slugValid = /^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$/.test(finalSlug)
  const slugUnique = !existingSlugs.has(finalSlug)
  const canSubmit = title.trim().length > 2 && slugValid && slugUnique

  const handleSubmit = async () => {
    if (!canSubmit) return
    setBusy(true)
    setErr(null)
    try {
      await onCreate(finalSlug, title.trim(), intro.trim() || undefined)
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed")
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center px-4" style={{ background: "rgba(0,0,0,0.7)", backdropFilter: "blur(4px)" }}>
      <div
        className="w-full max-w-[480px] rounded-2xl"
        style={{ background: "var(--surface)", border: "1px solid rgba(255,255,255,0.1)" }}
      >
        <div className="px-5 py-4 flex items-center justify-between" style={{ borderBottom: "1px solid var(--border)" }}>
          <p className="text-[15px] font-semibold" style={{ color: "var(--text-primary)" }}>New survey</p>
          <button onClick={onClose} className="p-1 rounded hover:bg-white/5" style={{ color: "var(--text-tertiary)" }}>
            <X size={16} />
          </button>
        </div>
        <div className="px-5 py-4 space-y-4">
          <div>
            <label className="text-[11px] uppercase mb-1 block" style={{ color: "var(--text-tertiary)" }}>Title</label>
            <input
              autoFocus
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="e.g. Gideon Impact – Q2"
              className="w-full px-3 py-2 rounded-lg text-[13px]"
              style={{ background: "var(--surface-elevated)", border: "1px solid var(--border)", color: "var(--text-primary)" }}
            />
          </div>
          <div>
            <label className="text-[11px] uppercase mb-1 block" style={{ color: "var(--text-tertiary)" }}>Slug</label>
            <input
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              placeholder={slugFromTitle || "auto-generated from title"}
              className="w-full px-3 py-2 rounded-lg text-[13px] font-mono"
              style={{ background: "var(--surface-elevated)", border: "1px solid var(--border)", color: "var(--text-primary)" }}
            />
            {finalSlug && !slugValid && (
              <p className="text-[11px] mt-1" style={{ color: "#f87171" }}>
                Slug must be lowercase a–z, 0–9, hyphens only (3–64 chars).
              </p>
            )}
            {finalSlug && slugValid && !slugUnique && (
              <p className="text-[11px] mt-1" style={{ color: "#f87171" }}>
                Slug already in use.
              </p>
            )}
          </div>
          <div>
            <label className="text-[11px] uppercase mb-1 block" style={{ color: "var(--text-tertiary)" }}>Intro (optional)</label>
            <textarea
              value={intro}
              onChange={(e) => setIntro(e.target.value)}
              rows={2}
              placeholder="Two minutes to help shape what we build next..."
              className="w-full px-3 py-2 rounded-lg text-[13px] resize-none"
              style={{ background: "var(--surface-elevated)", border: "1px solid var(--border)", color: "var(--text-primary)" }}
            />
          </div>
          {err && <p className="text-[12px]" style={{ color: "#f87171" }}>{err}</p>}
        </div>
        <div className="px-5 py-3 flex justify-end gap-2" style={{ borderTop: "1px solid var(--border)" }}>
          <button onClick={onClose} disabled={busy} className="px-3 py-1.5 rounded-lg text-[13px] hover:bg-white/5" style={{ color: "var(--text-secondary)" }}>
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={!canSubmit || busy}
            className="px-4 py-1.5 rounded-lg text-[13px] font-medium disabled:opacity-40"
            style={{ background: "#5057EA", color: "#ffffff" }}
          >
            {busy ? "Creating..." : "Create draft"}
          </button>
        </div>
      </div>
    </div>
  )
}


// ---------------------------------------------------------------------------
// Preview dialog — shows the survey exactly as a respondent will see it.
// Reuses the same SurveyModal we ship to end users.
// ---------------------------------------------------------------------------

function PreviewSurveyDialog({ slug, onClose }: { slug: string; onClose: () => void }) {
  const { survey } = useSurveyAdmin(slug)
  if (!survey) return null
  return (
    <SurveyModal
      previewMode
      onClose={onClose}
      survey={{
        id: survey.id,
        slug: survey.slug,
        title: survey.title,
        intro_text: survey.intro_text,
        questions: survey.questions,
      }}
      onSubmit={async () => {
        onClose()
      }}
    />
  )
}
