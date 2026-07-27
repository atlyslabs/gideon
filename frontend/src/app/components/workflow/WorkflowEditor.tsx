"use client"

import { useState } from "react"
import { Loader2, Command, CornerDownLeft, Clock, Zap, Eye, Sparkles, Globe, Code, ArrowDown, AlertCircle } from "lucide-react"
import { getIntegrationIcon, getIntegrationLabel } from "../../utils/integrationIcons"
import useIntegrationManifest from "../../hooks/useIntegrationManifest"
import { ScheduleBuilder, UntilPill } from "./SchedulePills"
import { TriggerBuilder } from "./SchedulePills"
import useAppRouter from "../../hooks/useAppRouter"

interface StepPreview {
  tool: string
  label: string
}

interface WorkflowEditorProps {
  whenType: "schedule" | "trigger"
  setWhenType: (v: "schedule" | "trigger") => void
  scheduleStartDate: string
  scheduleInterval: number
  scheduleUnit: string
  scheduleDayOfWeek: string | null
  scheduleDaysOfWeek: string[] | null
  scheduleTime: string
  onScheduleChange: (fields: Partial<{
    schedule_start_date: string
    schedule_interval: number
    schedule_unit: string
    schedule_day_of_week: string | null
    schedule_days_of_week: string[] | null
    schedule_time: string
  }>) => void
  triggerIntegration: string | null
  triggerEvent: string | null
  triggerMinutesBefore: number | null
  triggerFilter: Record<string, any> | null
  onTriggerChange: (fields: Partial<{
    trigger_integration: string | null
    trigger_event: string | null
    trigger_minutes_before: number | null
    trigger_filter: Record<string, any> | null
  }>) => void
  activeUntil: string | null
  onActiveUntilChange: (iso: string | null) => void
  alertOn: "none" | "failure" | "success" | "both"
  onAlertOnChange: (v: "none" | "failure" | "success" | "both") => void
  preferredProvider: "auto" | "anthropic" | "kimi"
  onPreferredProviderChange: (v: "auto" | "anthropic" | "kimi") => void
  prompt: string
  setPrompt: (v: string) => void
  integrationsUsed: string[]
  connectedProviders?: string[]
  isSaving: boolean
  onSubmit: () => void
  onPreviewSteps: (prompt: string) => Promise<StepPreview[]>
}

function getStepIcon(tool: string) {
  if (tool === "ai") return <Sparkles size={16} style={{ color: "#a78bfa" }} />
  if (tool === "web_search") return <Globe size={16} style={{ color: "#60a5fa" }} />
  if (tool === "code_execution") return <Code size={16} style={{ color: "#34d399" }} />
  return getIntegrationIcon(tool, 16)
}

function getStepLabel(tool: string) {
  if (tool === "ai") return "AI"
  if (tool === "web_search") return "Web Search"
  if (tool === "code_execution") return "Code"
  return getIntegrationLabel(tool)
}

export default function WorkflowEditor({
  whenType,
  setWhenType,
  scheduleStartDate,
  scheduleInterval,
  scheduleUnit,
  scheduleDayOfWeek,
  scheduleDaysOfWeek,
  scheduleTime,
  onScheduleChange,
  triggerIntegration,
  triggerEvent,
  triggerMinutesBefore,
  triggerFilter,
  onTriggerChange,
  activeUntil,
  onActiveUntilChange,
  alertOn,
  onAlertOnChange,
  preferredProvider,
  onPreferredProviderChange,
  prompt,
  setPrompt,
  integrationsUsed,
  connectedProviders = [],
  isSaving,
  onSubmit,
  onPreviewSteps,
}: WorkflowEditorProps) {
  const [steps, setSteps] = useState<StepPreview[] | null>(null)
  const [isPreviewing, setIsPreviewing] = useState(false)
  const router = useAppRouter()
  const { isConnected } = useIntegrationManifest()
  const missingIntegrations = integrationsUsed.filter(
    (i) => !isConnected(i, connectedProviders)
  )

  const handlePreview = async () => {
    if (!prompt?.trim() || isPreviewing) return
    setIsPreviewing(true)
    try {
      const result = await onPreviewSteps(prompt)
      setSteps(result)
    } catch {
      setSteps(null)
    } finally {
      setIsPreviewing(false)
    }
  }

  return (
    <div className="space-y-8 mt-2">
      {/* When section */}
      <div>
        <h4 className="text-[12px] font-medium uppercase tracking-wider mb-4" style={{ color: "var(--text-tertiary)" }}>
          When
        </h4>

        {/* Schedule builder */}
        <div
          className="rounded-xl p-5"
          style={{ background: "var(--surface-card)", border: "1px solid var(--border)" }}
        >
          <ScheduleBuilder
            startDate={scheduleStartDate}
            interval={scheduleInterval}
            unit={scheduleUnit}
            dayOfWeek={scheduleDayOfWeek}
            daysOfWeek={scheduleDaysOfWeek}
            time={scheduleTime}
            onChange={onScheduleChange}
          />
          <div className="mt-3">
            <UntilPill value={activeUntil} onChange={onActiveUntilChange} />
          </div>
        </div>
      </div>

      {/* Slack alert preference */}
      <div>
        <h4 className="text-[12px] font-medium uppercase tracking-wider mb-3" style={{ color: "var(--text-tertiary)" }}>
          Slack alert
        </h4>
        <div className="flex flex-wrap gap-2">
          {([
            { value: "failure", label: "On failure" },
            { value: "success", label: "On success" },
            { value: "both", label: "On both" },
            { value: "none", label: "Off" },
          ] as const).map((opt) => {
            const active = alertOn === opt.value
            return (
              <button
                key={opt.value}
                type="button"
                onClick={() => onAlertOnChange(opt.value)}
                className="px-3 py-1.5 rounded-full text-[13px] font-medium transition-colors"
                style={{
                  background: active ? "#7c3aed" : "var(--surface-elevated)",
                  color: active ? "#fff" : "var(--text-secondary)",
                  border: `1px solid ${active ? "#7c3aed" : "var(--border)"}`,
                }}
              >
                {opt.label}
              </button>
            )
          })}
        </div>
        <p className="text-[12px] mt-2" style={{ color: "var(--text-tertiary)" }}>
          Gideon DMs you on Slack when a run finishes. Default is on failure.
        </p>
      </div>

      {/* Run provider preference */}
      <div>
        <h4 className="text-[12px] font-medium uppercase tracking-wider mb-3" style={{ color: "var(--text-tertiary)" }}>
          Run on
        </h4>
        <div className="flex flex-wrap gap-2">
          {([
            { value: "auto", label: "Auto" },
            { value: "anthropic", label: "Claude 1M" },
            { value: "kimi", label: "Kimi" },
          ] as const).map((opt) => {
            const active = preferredProvider === opt.value
            return (
              <button
                key={opt.value}
                type="button"
                onClick={() => onPreferredProviderChange(opt.value)}
                className="px-3 py-1.5 rounded-full text-[13px] font-medium transition-colors"
                style={{
                  background: active ? "#7c3aed" : "var(--surface-elevated)",
                  color: active ? "#fff" : "var(--text-secondary)",
                  border: `1px solid ${active ? "#7c3aed" : "var(--border)"}`,
                }}
              >
                {opt.label}
              </button>
            )
          })}
        </div>
        <p className="text-[12px] mt-2" style={{ color: "var(--text-tertiary)" }}>
          Auto runs on Kimi and switches to Claude 1M automatically if it keeps overflowing. Pin Claude 1M for big daily reports to skip the wasted first attempt, or force Kimi to stay cheap.
        </p>
      </div>

      {/* Using Integrations */}
      {integrationsUsed.length > 0 && (
        <div>
          <h4 className="text-[12px] font-medium uppercase tracking-wider mb-3" style={{ color: "var(--text-tertiary)" }}>
            Using Integrations
          </h4>
          <div className="flex flex-wrap gap-2">
            {integrationsUsed.map((integ) => {
              const missing = !isConnected(integ, connectedProviders)
              return (
                <div
                  key={integ}
                  className="flex items-center gap-2 px-3 py-1.5 rounded-full text-[13px] transition-colors"
                  style={{
                    background: missing ? "rgba(239,68,68,0.08)" : "var(--surface-elevated)",
                    border: missing ? "1px solid rgba(239,68,68,0.4)" : "1px solid var(--border)",
                    color: "var(--text-primary)",
                  }}
                >
                  {getIntegrationIcon(integ, 14)}
                  {getIntegrationLabel(integ)}
                </div>
              )
            })}
          </div>
          {missingIntegrations.length > 0 && (
            <div className="mt-3 flex items-start gap-2 text-[13px]">
              <AlertCircle size={14} className="shrink-0 mt-[2px]" style={{ color: "var(--text-tertiary)" }} />
              <p style={{ color: "var(--text-tertiary)" }}>
                Some integrations above aren&apos;t connected yet. The workflow will still save and run, but those steps may be skipped or fail until you connect them on the{" "}
                <a
                  href="/integrations"
                  onClick={(e) => {
                    e.preventDefault()
                    router.navigateToPage("integrations")
                  }}
                  style={{ color: "#7c3aed", textDecoration: "underline" }}
                >
                  integrations page
                </a>
                .
              </p>
            </div>
          )}
        </div>
      )}

      {/* Prompt section */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <h4 className="text-[12px] font-medium uppercase tracking-wider" style={{ color: "var(--text-tertiary)" }}>
            Prompt
          </h4>
          <button
            onClick={handlePreview}
            disabled={!prompt?.trim() || isPreviewing}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[12px] font-medium transition-colors disabled:opacity-40"
            style={{
              background: "rgba(124,58,237,0.1)",
              color: "#a78bfa",
              border: "1px solid rgba(124,58,237,0.2)",
            }}
            onMouseEnter={(e) => { if (!e.currentTarget.disabled) e.currentTarget.style.background = "rgba(124,58,237,0.2)" }}
            onMouseLeave={(e) => { e.currentTarget.style.background = "rgba(124,58,237,0.1)" }}
          >
            {isPreviewing ? (
              <Loader2 size={12} className="animate-spin" />
            ) : (
              <Eye size={12} />
            )}
            {isPreviewing ? "Analyzing..." : "Preview Steps"}
          </button>
        </div>
        <textarea
          value={prompt}
          onChange={(e) => { setPrompt(e.target.value); setSteps(null) }}
          rows={8}
          className="w-full px-4 py-3 rounded-xl text-[14px] leading-relaxed resize-none outline-none transition-colors"
          style={{
            background: "var(--surface-card)",
            border: "1px solid var(--border)",
            color: "var(--text-primary)",
          }}
          onFocus={(e) => { e.currentTarget.style.borderColor = "#7c3aed" }}
          onBlur={(e) => { e.currentTarget.style.borderColor = "var(--border)" }}
          placeholder="Describe what this workflow should do..."
        />
      </div>

      {/* Step visualization */}
      {steps && steps.length > 0 && (
        <div>
          <h4 className="text-[12px] font-medium uppercase tracking-wider mb-4" style={{ color: "var(--text-tertiary)" }}>
            Execution Flow
          </h4>
          <div className="flex flex-col items-center gap-0">
            {steps.map((step, i) => (
              <div key={i} className="flex flex-col items-center w-full">
                {/* Connector arrow */}
                {i > 0 && (
                  <div className="flex flex-col items-center py-1">
                    <div style={{ width: 1, height: 16, background: "var(--border)" }} />
                    <ArrowDown size={12} style={{ color: "var(--text-tertiary)", marginTop: -2 }} />
                  </div>
                )}
                {/* Step card */}
                <div
                  className="flex items-center gap-3 w-full px-4 py-3 rounded-xl"
                  style={{
                    background: "var(--surface-card)",
                    border: "1px solid var(--border)",
                  }}
                >
                  <div
                    className="flex items-center justify-center shrink-0 rounded-lg"
                    style={{
                      width: 32,
                      height: 32,
                      background: step.tool === "ai" ? "rgba(167,139,250,0.1)" : "var(--surface-elevated)",
                      border: "1px solid var(--border-subtle)",
                    }}
                  >
                    {getStepIcon(step.tool)}
                  </div>
                  <div className="min-w-0 flex-1">
                    <span className="text-[11px] font-medium uppercase tracking-wider" style={{ color: "var(--text-tertiary)" }}>
                      {getStepLabel(step.tool)}
                    </span>
                    <p className="text-[13px] mt-0.5" style={{ color: "var(--text-primary)" }}>
                      {step.label}
                    </p>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Submit */}
      <div className="flex justify-end">
        <button
          onClick={onSubmit}
          disabled={isSaving || !prompt?.trim()}
          className="flex items-center gap-2 px-5 py-2.5 rounded-lg text-[13px] font-medium transition-colors disabled:opacity-40"
          style={{ background: "#7c3aed", color: "white" }}
          onMouseEnter={(e) => { if (!e.currentTarget.disabled) e.currentTarget.style.background = "#6d28d9" }}
          onMouseLeave={(e) => { e.currentTarget.style.background = "#7c3aed" }}
        >
          {isSaving ? (
            <>
              <Loader2 size={14} className="animate-spin" />
              Saving...
            </>
          ) : (
            <>
              Submit changes
              <span
                className="flex items-center gap-0.5 text-[11px] opacity-60 ml-1 px-1.5 py-0.5 rounded"
                style={{ background: "rgba(255,255,255,0.1)" }}
              >
                <Command size={10} />
                <CornerDownLeft size={10} />
              </span>
            </>
          )}
        </button>
      </div>
    </div>
  )
}
