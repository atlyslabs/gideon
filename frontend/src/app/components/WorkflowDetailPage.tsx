"use client"

import { useState, useCallback, useEffect, useMemo, useRef } from "react"
import { ArrowLeft } from "lucide-react"
import useWorkflows from "../hooks/useWorkflows"
import useIntegrations from "../hooks/useIntegrations"
import type { Workflow } from "../types"
import { WorkflowActions } from "./workflow"
import { WorkflowEditor } from "./workflow"
import { HistoryTab } from "./workflow"

/* -- Workflow Detail Page -- */

interface WorkflowDetailPageProps {
  workflowId: string | null
  onBack: () => void
}

export default function WorkflowDetailPage({ workflowId, onBack }: WorkflowDetailPageProps) {
  const {
    workflows, getWorkflow, getWorkflowTemplate, cloneWorkflow, updateWorkflowShare,
    createWorkflow, updateWorkflow, deleteWorkflow,
    activateWorkflow, deactivateWorkflow, runWorkflow,
    getRuns, previewSteps,
  } = useWorkflows()
  const { integrations } = useIntegrations()
  const connectedProviders = useMemo(
    () => integrations.filter((i) => i.is_active).map((i) => i.provider),
    [integrations]
  )

  const isCreateMode = workflowId === null

  const [workflow, setWorkflow] = useState<Workflow | null>(null)
  const [isTemplate, setIsTemplate] = useState(false)
  const [cloning, setCloning] = useState(false)
  const [loading, setLoading] = useState(!isCreateMode)
  const [activeTab, setActiveTab] = useState<"plan" | "history">("plan")

  // Editable fields
  const [name, setName] = useState("")
  const [prompt, setPrompt] = useState("")
  const [whenType, setWhenType] = useState<"schedule" | "trigger">("schedule")
  const [scheduleStartDate, setScheduleStartDate] = useState("")
  const [scheduleInterval, setScheduleInterval] = useState(1)
  const [scheduleUnit, setScheduleUnit] = useState("day")
  const [scheduleDayOfWeek, setScheduleDayOfWeek] = useState<string | null>(null)
  const [scheduleDaysOfWeek, setScheduleDaysOfWeek] = useState<string[] | null>(null)
  const [scheduleTime, setScheduleTime] = useState("09:00")
  const [triggerIntegration, setTriggerIntegration] = useState<string | null>("gmail")
  const [triggerEvent, setTriggerEvent] = useState<string | null>(null)
  const [triggerMinutesBefore, setTriggerMinutesBefore] = useState<number | null>(null)
  const [triggerFilter, setTriggerFilter] = useState<Record<string, any> | null>(null)
  const [integrationsUsed, setIntegrationsUsed] = useState<string[]>([])
  const [approvalMode, setApprovalMode] = useState<"auto" | "manual">("auto")
  const [alertOn, setAlertOn] = useState<"none" | "failure" | "success" | "both">("failure")
  const [preferredProvider, setPreferredProvider] = useState<"auto" | "anthropic" | "kimi">("auto")
  const [activeUntil, setActiveUntil] = useState<string | null>(null)

  const [isSaving, setIsSaving] = useState(false)
  const [isActivating, setIsActivating] = useState(false)
  const [showMenu, setShowMenu] = useState(false)
  const [isDeleting, setIsDeleting] = useState(false)
  const [isRunning, setIsRunning] = useState(false)
  const runInFlightRef = useRef(false)
  const [historyRefreshKey, setHistoryRefreshKey] = useState(0)
  const [editingName, setEditingName] = useState(false)
  const nameInputRef = useRef<HTMLInputElement>(null)

  // Load from fields
  const loadWorkflowFields = useCallback((wf: Workflow) => {
    setWorkflow(wf)
    setName(wf.name)
    setPrompt(wf.prompt)
    setWhenType(wf.when_type)
    setScheduleStartDate(wf.schedule_start_date || "")
    setScheduleInterval(wf.schedule_interval || 1)
    setScheduleUnit(wf.schedule_unit || "day")
    setScheduleDayOfWeek(wf.schedule_day_of_week)
    setScheduleDaysOfWeek(wf.schedule_days_of_week ?? null)
    setScheduleTime(wf.schedule_time || "09:00")
    setTriggerIntegration(wf.trigger_integration)
    setTriggerEvent(wf.trigger_event)
    setTriggerMinutesBefore(wf.trigger_minutes_before)
    setTriggerFilter(wf.trigger_filter)
    setIntegrationsUsed(wf.integrations_used || [])
    setApprovalMode(wf.approval_mode || "auto")
    setAlertOn(wf.alert_on || "failure")
    setPreferredProvider(wf.preferred_provider || "auto")
    setActiveUntil(wf.active_until)
  }, [])

  // Load existing workflow
  useEffect(() => {
    if (isCreateMode) return
    let cancelled = false
    setLoading(true)

    const cached = workflows.find((w) => w.id === workflowId)
    if (cached) {
      loadWorkflowFields(cached)
      setLoading(false)
    }

    getWorkflow(workflowId!)
      .then((fresh) => { if (!cancelled) { setIsTemplate(false); loadWorkflowFields(fresh); setLoading(false) } })
      .catch(() => {
        // Owner filter returned 404 — try fetching as a shared template.
        // That endpoint is accessible to any logged-in user and returns the
        // read-only workflow shape for a "Copy to my workflows" preview.
        getWorkflowTemplate(workflowId!)
          .then((tpl) => {
            if (cancelled) return
            setIsTemplate(true)
            // WorkflowTemplate shape is a subset of Workflow — fill missing
            // owner-only fields with safe defaults so the existing editor
            // renders read-only previews without type mismatches.
            loadWorkflowFields({
              ...tpl,
              status: "inactive",
              active_until: null,
              created_at: "",
              updated_at: "",
            } as unknown as Workflow)
            setLoading(false)
          })
          .catch(() => { if (!cancelled) setLoading(false) })
      })

    return () => { cancelled = true }
  }, [workflowId, isCreateMode, workflows, getWorkflow, getWorkflowTemplate, loadWorkflowFields])

  // Auto-focus name input
  useEffect(() => {
    if (editingName && nameInputRef.current) {
      nameInputRef.current.focus()
      nameInputRef.current.select()
    }
  }, [editingName])

  const buildPayload = useCallback(() => {
    return {
      name: name.trim() || "Untitled workflow",
      prompt: prompt.trim(),
      when_type: whenType,
      schedule_start_date: whenType === "schedule" ? (scheduleStartDate || null) : null,
      schedule_interval: whenType === "schedule" ? scheduleInterval : 1,
      schedule_unit: whenType === "schedule" ? scheduleUnit : "day",
      schedule_day_of_week: whenType === "schedule" && scheduleUnit === "week" ? scheduleDayOfWeek : null,
      // The picker is only shown for unit=day, interval=1. Mirror that here
      // so a stale filter doesn't tag along after the user bumps interval > 1
      // (UI hides the picker but the state value would persist otherwise).
      schedule_days_of_week:
        whenType === "schedule" && scheduleUnit === "day" && scheduleInterval === 1
          ? scheduleDaysOfWeek
          : null,
      schedule_time: whenType === "schedule" ? scheduleTime : "09:00",
      trigger_integration: whenType === "trigger" ? triggerIntegration : null,
      trigger_event: whenType === "trigger" ? triggerEvent : null,
      trigger_minutes_before: whenType === "trigger" ? triggerMinutesBefore : null,
      trigger_filter: whenType === "trigger" ? triggerFilter : null,
      integrations_used: integrationsUsed,
      approval_mode: approvalMode,
      alert_on: alertOn,
      preferred_provider: preferredProvider,
      active_until: activeUntil,
    }
  }, [name, prompt, whenType, scheduleStartDate, scheduleInterval, scheduleUnit, scheduleDayOfWeek, scheduleDaysOfWeek, scheduleTime, triggerIntegration, triggerEvent, triggerMinutesBefore, triggerFilter, integrationsUsed, approvalMode, alertOn, preferredProvider, activeUntil])

  const handleSubmit = useCallback(async () => {
    if (isSaving) return
    setIsSaving(true)
    try {
      const payload = buildPayload()
      if (isCreateMode || !workflow) {
        const created = await createWorkflow(payload)
        loadWorkflowFields(created)
      } else {
        const updated = await updateWorkflow(workflow.id, payload)
        loadWorkflowFields(updated)
      }
    } catch {
    } finally {
      setIsSaving(false)
    }
  }, [isSaving, isCreateMode, workflow, buildPayload, createWorkflow, updateWorkflow, loadWorkflowFields])

  // Cmd+Enter to submit
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && activeTab === "plan" && !isSaving) {
        e.preventDefault()
        handleSubmit()
      }
    }
    window.addEventListener("keydown", handler)
    return () => window.removeEventListener("keydown", handler)
  }, [activeTab, isSaving, handleSubmit])

  const handleActivate = useCallback(async () => {
    if (!workflow || isActivating) return
    setIsActivating(true)
    try {
      if (workflow.status === "active") {
        const updated = await deactivateWorkflow(workflow.id)
        loadWorkflowFields(updated)
      } else {
        const updated = await activateWorkflow(workflow.id)
        loadWorkflowFields(updated)
      }
    } catch {
    } finally {
      setIsActivating(false)
    }
  }, [workflow, isActivating, activateWorkflow, deactivateWorkflow, loadWorkflowFields])

  const handleDelete = useCallback(async () => {
    if (!workflow) { onBack(); return }
    setIsDeleting(true)
    try {
      await deleteWorkflow(workflow.id)
      onBack()
    } catch {
      setIsDeleting(false)
    }
  }, [workflow, deleteWorkflow, onBack])

  const handleRun = useCallback(async () => {
    if (!workflow || runInFlightRef.current) return
    runInFlightRef.current = true
    setIsRunning(true)
    try {
      await runWorkflow(workflow.id)
      setHistoryRefreshKey((k) => k + 1)
      setActiveTab("history")
    } catch (err) {
      console.error("Workflow run failed:", err)
    } finally {
      runInFlightRef.current = false
      setIsRunning(false)
    }
  }, [workflow, runWorkflow])

  const handleNameBlur = useCallback(() => {
    setEditingName(false)
    if (workflow && name.trim() && name.trim() !== workflow.name) {
      updateWorkflow(workflow.id, { name: name.trim() }).then(loadWorkflowFields).catch(() => {})
    }
  }, [name, workflow, updateWorkflow, loadWorkflowFields])

  const handleNameKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === "Enter") { e.preventDefault(); (e.target as HTMLInputElement).blur() }
  }, [])

  const handleScheduleChange = useCallback((fields: Partial<{
    schedule_start_date: string
    schedule_interval: number
    schedule_unit: string
    schedule_day_of_week: string | null
    schedule_days_of_week: string[] | null
    schedule_time: string
  }>) => {
    if (fields.schedule_start_date !== undefined) setScheduleStartDate(fields.schedule_start_date)
    if (fields.schedule_interval !== undefined) setScheduleInterval(fields.schedule_interval)
    if (fields.schedule_unit !== undefined) setScheduleUnit(fields.schedule_unit)
    if (fields.schedule_day_of_week !== undefined) setScheduleDayOfWeek(fields.schedule_day_of_week)
    if (fields.schedule_days_of_week !== undefined) setScheduleDaysOfWeek(fields.schedule_days_of_week)
    if (fields.schedule_time !== undefined) setScheduleTime(fields.schedule_time)
  }, [])

  const handleTriggerChange = useCallback((fields: Partial<{
    trigger_integration: string | null
    trigger_event: string | null
    trigger_minutes_before: number | null
    trigger_filter: Record<string, any> | null
  }>) => {
    if (fields.trigger_integration !== undefined) setTriggerIntegration(fields.trigger_integration)
    if (fields.trigger_event !== undefined) setTriggerEvent(fields.trigger_event)
    if (fields.trigger_minutes_before !== undefined) setTriggerMinutesBefore(fields.trigger_minutes_before)
    if (fields.trigger_filter !== undefined) setTriggerFilter(fields.trigger_filter)
  }, [])

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center rounded-xl" style={{ background: "var(--surface)" }}>
        <div className="spinner" />
      </div>
    )
  }

  if (!isCreateMode && !workflow) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center rounded-xl" style={{ background: "var(--surface)" }}>
        <p className="text-[14px] mb-4" style={{ color: "var(--text-tertiary)" }}>Workflow not found.</p>
        <button onClick={onBack} className="text-[13px] font-medium" style={{ color: "#7c3aed" }}>
          Back to Workflows
        </button>
      </div>
    )
  }

  return (
    <div className="flex-1 flex flex-col min-w-0 rounded-xl overflow-hidden" style={{ background: "var(--surface)" }}>
      <div className="flex-1 overflow-y-auto">
        <div className="max-w-3xl mx-auto px-8 pt-8 pb-12">
          {/* Back link + actions row */}
          <div className="flex items-center justify-between mb-6">
            <button
              onClick={onBack}
              className="flex items-center gap-1.5 text-[13px] transition-colors"
              style={{ color: "var(--text-secondary)" }}
              onMouseEnter={(e) => { e.currentTarget.style.color = "var(--text-primary)" }}
              onMouseLeave={(e) => { e.currentTarget.style.color = "var(--text-secondary)" }}
            >
              <ArrowLeft size={14} />
              All workflows
            </button>

            <WorkflowActions
              workflow={workflow}
              showMenu={showMenu}
              setShowMenu={setShowMenu}
              isDeleting={isDeleting}
              isActivating={isActivating}
              isRunning={isRunning}
              onDelete={handleDelete}
              onActivate={handleActivate}
              onRun={handleRun}
              onEnableShare={async (linkShared) => {
                if (!workflowId) return
                const updated = await updateWorkflowShare(workflowId, linkShared)
                loadWorkflowFields(updated)
              }}
              templateMode={isTemplate}
              cloning={cloning}
              onClone={async () => {
                if (!workflowId || cloning) return
                setCloning(true)
                try {
                  const created = await cloneWorkflow(workflowId)
                  // Navigate to the new workflow; fall back to reload if no router
                  window.history.pushState({}, "", `/workflows/${created.id}`)
                  window.location.assign(`/workflows/${created.id}`)
                } catch {
                  setCloning(false)
                }
              }}
            />
          </div>

          {/* Workflow name -- editable heading */}
          <div className="mb-1">
            {editingName ? (
              <input
                ref={nameInputRef}
                value={name}
                onChange={(e) => setName(e.target.value)}
                onBlur={handleNameBlur}
                onKeyDown={handleNameKeyDown}
                className="w-full text-[28px] font-bold tracking-tight bg-transparent outline-none border-none"
                style={{ color: "var(--text-primary)", caretColor: "#7c3aed" }}
                placeholder="Untitled workflow"
              />
            ) : (
              <h1
                className="text-[28px] font-bold tracking-tight cursor-text"
                style={{ color: name ? "var(--text-primary)" : "var(--text-tertiary)" }}
                onClick={() => setEditingName(true)}
              >
                {name || "Untitled workflow"}
              </h1>
            )}
          </div>

          {/* Shared template banner */}
          {isTemplate && (
            <div
              className="mb-4 mt-2 px-3 py-2 rounded-lg text-[12px] flex items-center gap-2"
              style={{
                background: "rgba(124,58,237,0.12)",
                border: "1px solid rgba(124,58,237,0.3)",
                color: "var(--text-secondary)",
              }}
            >
              <span style={{ color: "#a78bfa" }}>●</span>
              You&apos;re viewing a shared workflow. Click <b style={{ color: "var(--text-primary)" }}>Copy to my workflows</b> to save an editable copy to your account.
            </div>
          )}

          <div className="mb-6" />

          {/* Tabs */}
          <div className="flex gap-6 mb-6" style={{ borderBottom: "1px solid var(--border)" }}>
            {(["plan", "history"] as const).map((tab) => (
              <button
                key={tab}
                onClick={() => setActiveTab(tab)}
                className="pb-3 text-[14px] font-medium transition-colors relative"
                style={{
                  color: activeTab === tab ? "var(--text-primary)" : "var(--text-tertiary)",
                }}
              >
                {tab.charAt(0).toUpperCase() + tab.slice(1)}
                {activeTab === tab && (
                  <div
                    className="absolute bottom-0 left-0 right-0 h-[2px] rounded-full"
                    style={{ background: "#7c3aed" }}
                  />
                )}
              </button>
            ))}
          </div>

          {/* Tab content */}
          {activeTab === "plan" ? (
            <WorkflowEditor
              whenType={whenType}
              setWhenType={setWhenType}
              scheduleStartDate={scheduleStartDate}
              scheduleInterval={scheduleInterval}
              scheduleUnit={scheduleUnit}
              scheduleDayOfWeek={scheduleDayOfWeek}
              scheduleDaysOfWeek={scheduleDaysOfWeek}
              scheduleTime={scheduleTime}
              onScheduleChange={handleScheduleChange}
              activeUntil={activeUntil}
              onActiveUntilChange={setActiveUntil}
              alertOn={alertOn}
              onAlertOnChange={setAlertOn}
              preferredProvider={preferredProvider}
              onPreferredProviderChange={setPreferredProvider}
              triggerIntegration={triggerIntegration}
              triggerEvent={triggerEvent}
              triggerMinutesBefore={triggerMinutesBefore}
              triggerFilter={triggerFilter}
              onTriggerChange={handleTriggerChange}
              prompt={prompt}
              setPrompt={setPrompt}
              integrationsUsed={integrationsUsed}
              connectedProviders={connectedProviders}
              isSaving={isSaving}
              onSubmit={handleSubmit}
              onPreviewSteps={previewSteps}
            />
          ) : (
            <HistoryTab
              workflowId={workflow?.id || null}
              getRuns={getRuns}
              refreshKey={historyRefreshKey}
            />
          )}
        </div>
      </div>
    </div>
  )
}
