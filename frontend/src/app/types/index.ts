// @-mention system
export type MentionCategory = "collaborator" | "tool" | "linear" | "notion" | "slack" | "gmail" | "google_calendar" | "google_drive" | "google_docs" | "google_sheets" | "google_slides"

export interface MentionItem {
  id: string
  category: MentionCategory
  label: string
  subtitle?: string
  icon?: string
  url?: string
  avatar_url?: string
}

export interface Conversation {
  id: string
  title: string | null
  created_at: string
  updated_at: string
  is_pinned: boolean
  active_turn_id?: string | null
  // Set when this conversation was seeded from a workflow run via the
  // "Follow up in chat" button on the run detail page. Used to render a
  // "Continuing from workflow: …" banner with a deep link back to the run.
  workflow_run_id?: string | null
  workflow_id?: string | null
}

// Sharing
export interface ShareSettings {
  link_shared: boolean
  shared_with_emails: string[]
  share_url: string
}

export interface SharedConversation {
  conversation: {
    id: string
    title: string | null
    owner_name: string
    created_at: string
    updated_at: string
  }
  messages: Message[]
  access: "owner" | "shared"
}

export type MessageStatus = "streaming" | "done" | "error" | "interrupted" | "aborted"

export interface Message {
  id: string
  conversation_id: string
  role: "user" | "assistant" | "system" | "tool"
  content: string | null
  thinking: string | null
  tool_calls: ToolCall[] | null
  tool_results: any | null
  model: string | null
  tokens_used: number | null
  files?: { filename: string; ext: string }[] | null
  status?: MessageStatus
  created_at: string
}

export interface ToolCall {
  id: string
  name: string
  input: Record<string, any>
}

// SSE event types from streaming chat
export interface SSETextDelta {
  type: "text_delta"
  content: string
}

export interface SSEToolStart {
  type: "tool_start"
  tool: string
  tool_call_id: string
}

export interface DraftData {
  draft_id: string
  to: string
  subject: string
  body: string
  cc?: string
  bcc?: string
}

export interface DataTableData {
  columns: string[]
  rows: Record<string, any>[]
  row_count: number
  display_limit: number
  sql: string
}

export interface ChartData {
  title: string
  fields: string[]
  rows: Record<string, any>[]
  time_range?: { from: string; to: string }
  active_filters?: Record<string, string>
}

export interface SSEToolResult {
  type: "tool_result"
  tool: string
  tool_call_id: string
  summary: string
  reconnect?: string  // provider name if token expired
  draft?: DraftData   // present for draft_email/reply_to_email
  data_table?: DataTableData  // present for query_analytics
  chart_data?: ChartData  // present for query_grafana_panel
  preview?: {
    framework: "react" | "html"
    code: string
    description?: string
  }  // present for generate_preview
}

export interface SSEDone {
  type: "done"
  message_id: string
  conversation_id: string
  title?: string
}

export interface SSEError {
  type: "error"
  message: string
}

export interface SSEThinkingStart {
  type: "thinking_start"
}

export interface SSEThinkingDelta {
  type: "thinking_delta"
  content: string
}

export interface SSECodeImage {
  type: "code_image"
  data: string
  mime: string
  filename: string
}

export interface SSECodeFile {
  type: "code_file"
  filename: string
  mime: string
  drive_url: string
  drive_file_id: string
  size_bytes: number
}

export interface SSEConversationCreated {
  type: "conversation_created"
  conversation_id: string
  title?: string
}

export interface SSETurnStarted {
  type: "turn_started"
  turn_id: string
  conversation_id: string
}

export interface SSEAborted {
  type: "aborted"
  conversation_id?: string
  turn_id?: string
  message?: string
}

export interface SSEBusy {
  type: "busy"
  turn_id: string
  conversation_id?: string
  message?: string
}

export type SSEEvent =
  | SSETextDelta
  | SSEToolStart
  | SSEToolResult
  | SSEDone
  | SSEError
  | SSEThinkingStart
  | SSEThinkingDelta
  | SSECodeImage
  | SSECodeFile
  | SSEConversationCreated
  | SSETurnStarted
  | SSEAborted
  | SSEBusy

// Structured content blocks for agent messages
export interface ThinkingBlock {
  type: "thinking"
  content: string
  duration_seconds: number
}

export interface ToolActionBlock {
  type: "tool_action"
  tool: string
  summary: string
}

export interface TextBlock {
  type: "text"
  content: string
}

export type ContentBlock = ThinkingBlock | ToolActionBlock | TextBlock

// Integrations
export interface Integration {
  id: string
  provider: string
  is_active: boolean
  error_count: number
  last_error_at: string | null
  last_error: string | null
  scopes: string[] | null
  extra_data: Record<string, any> | null
  connected_at: string
}

// Skills
export interface Skill {
  id: string
  name: string
  description: string
  prompt: string
  integrations_used: string[]
  memory_content: string | null
  is_default: boolean
  last_run_at: string | null
  status: "idle" | "learning" | "learned" | "error"
  created_at: string
  updated_at: string
}

export interface SkillHistoryEntry {
  id: string
  skill_id: string
  run_at: string
  status: "approved" | "rejected" | "pending"
  memory_snapshot: string | null
}

// Workflows
export interface Workflow {
  id: string
  name: string
  description: string | null
  prompt: string
  when_type: "schedule" | "trigger"
  schedule_start_date: string | null
  schedule_interval: number
  schedule_unit: string
  schedule_day_of_week: string | null
  schedule_days_of_week: string[] | null
  schedule_time: string
  trigger_integration: string | null
  trigger_event: string | null
  trigger_minutes_before: number | null
  trigger_filter: Record<string, any> | null
  integrations_used: string[]
  status: "inactive" | "active"
  approval_mode: "auto" | "manual"
  // Slack DM alert preference per run outcome. Default "failure".
  alert_on: "none" | "failure" | "success" | "both"
  // LLM provider for runs. "auto" = Kimi, auto-pins to Anthropic on chronic
  // escalation; "anthropic" = pinned to Claude 1M; "kimi" = forced Kimi.
  preferred_provider: "auto" | "anthropic" | "kimi"
  active_until: string | null  // ISO datetime; null = forever
  link_shared: boolean
  // Most-recent run summary for the card status line. Null when never run.
  last_run_status?: string | null  // "completed" | "failed" | "running" | …
  last_run_at?: string | null       // ISO datetime
  last_run_id?: string | null
  created_at: string
  updated_at: string
}

// Read-only template shape returned for shared workflow links (no owner,
// no runs). Mirrors the WorkflowTemplateResponse on the backend.
export interface WorkflowTemplate {
  id: string
  name: string
  description: string | null
  prompt: string
  when_type: "schedule" | "trigger"
  schedule_start_date: string | null
  schedule_interval: number
  schedule_unit: string
  schedule_day_of_week: string | null
  schedule_days_of_week: string[] | null
  schedule_time: string
  trigger_integration: string | null
  trigger_event: string | null
  trigger_minutes_before: number | null
  trigger_filter: Record<string, any> | null
  integrations_used: string[]
  approval_mode: "auto" | "manual"
}

export interface WorkflowRun {
  id: string
  workflow_id: string
  user_id: string
  triggered_at: string
  completed_at: string | null
  status: "running" | "pending_approval" | "approved" | "rejected" | "completed" | "failed"
  output: string | null
  error: string | null
  created_at: string
}

// Hub types
export interface Delegatee {
  name: string
  email: string
  avatar_url?: string
}

export interface TodoUpdateEntry {
  id: string
  author_name: string
  author_email: string | null
  content: string
  source: "slack" | "nudge" | "manual" | "delegation"
  created_at: string
}

export interface Todo {
  id: string
  text: string
  completed: boolean
  priority: "none" | "low" | "medium" | "high" | "urgent"
  source: "manual" | "suggestion" | "delegation"
  source_detail: string | null
  assigned_to_gideon: boolean
  assigned_conversation_id: string | null
  agent_status: "working" | "completed" | "failed" | null
  parent_id: string | null
  snoozed_until: string | null
  delegated_to: Delegatee[] | null
  delegated_from_user_name: string | null
  slack_channel_id: string | null
  slack_thread_ts: string | null
  reminder_cadence: "never" | "daily" | "every_2_days" | "weekly" | null
  subtasks: Todo[]
  created_at: string
}

export interface TodoSuggestion {
  id: string
  text: string
  detail: string | null
  source_integration: string | null
  source_url: string | null
  created_at: string
}

export interface HubEmail {
  id: string
  subject: string
  sender: string
  snippet: string
  date: string
  is_read: boolean
}

export interface EmailDraft {
  id: string
  email_id: string
  thread_id: string | null
  sender_name: string
  sender_email: string
  sender_avatar_url: string | null
  subject: string
  email_body: string
  email_date: string
  draft_body: string
  status: string
}

export interface MeetingAttendee {
  email: string
  display_name: string | null
  self: boolean
  response_status: "accepted" | "declined" | "tentative" | "needsAction"
}

// Surveys (in-app blocking)
export type SurveyQuestionType = "likert_1_5" | "text" | "single_select" | "multi_select"

export interface SurveyQuestion {
  id: string
  type: SurveyQuestionType
  prompt: string
  options?: string[] | null
  required: boolean
  placeholder?: string | null
}

export interface Survey {
  id: string
  slug: string
  title: string
  intro_text: string | null
  questions: SurveyQuestion[]
}

export interface SurveyEligibility {
  departments?: string[]
  role_keywords?: string[]
  emails?: string[]
}

export interface SurveyAdmin {
  id: string
  slug: string
  title: string
  intro_text: string | null
  status: "draft" | "active" | "closed"
  eligibility: SurveyEligibility
  questions: SurveyQuestion[]
  builder_conversation_id: string | null
  analysis_generated_at: string | null
  created_at: string
  closed_at: string | null
}

export interface SurveyListItem {
  id: string
  slug: string
  title: string
  status: "draft" | "active" | "closed"
  eligibility: SurveyEligibility
  question_count: number
  submitted_count: number
  dismissed_count: number
  created_at: string
}

export interface SurveyResponseRow {
  id: string
  user_id: string
  user_email: string
  user_name: string | null
  user_role: string | null
  user_department: string | null
  status: string
  answers: Record<string, unknown>
  submitted_at: string | null
  dismissed_at: string | null
}

export interface SurveyAnalysis {
  headline: string
  themes: Array<{
    name: string
    weight: number
    description: string
    supporting_quotes: string[]
  }>
  wins: string[]
  gaps: string[]
  recommendations: string[]
  stats?: Record<string, {
    question_id: string
    question_type: string
    answered: number
    skipped: number
    distribution?: Record<string, number>
    average?: number | null
    counts?: Record<string, number>
    percentages?: Record<string, number>
    average_length?: number
    sample_count?: number
  }>
  usage_correlation?: Record<string, {
    users_with_any_use: number
    submitters_total: number
    users_pct: number
    total_calls: number
    avg_calls_per_user_with_any: number
  }>
  n_submitted?: number
  n_dismissed?: number
}

export interface OrgBreakdown {
  total_users: number
  departments: Array<{ name: string; count: number }>
  roles: Array<{ name: string; count: number }>
}

export interface HubMeeting {
  id: string
  title: string
  start_time: string
  end_time: string
  location: string | null
  attendees_count: number
  meet_link: string | null
  color: string | null
  attendees: MeetingAttendee[]
}

// Chat UI row types (shared between Layout and ChatMessages)
export interface ToolCallState {
  name: string
  status: "running" | "done"
  summary?: string
  reconnect?: string  // provider name if token expired
  draft?: DraftData   // present for draft_email/reply_to_email
  data_table?: DataTableData  // present for query_analytics
}

export interface PreviewPayload {
  framework: "react" | "html"
  code: string
  description?: string
}

export interface Row {
  id: string
  role: "user" | "assistant" | "typing"
  content: string
  thinking?: string
  thinkingStreaming?: boolean
  thinkingDuration?: number  // seconds
  toolCalls?: ToolCall[]
  activeTools?: ToolCallState[]
  drafts?: DraftData[]
  dataTables?: DataTableData[]
  charts?: ChartData[]
  previews?: PreviewPayload[]
  draftSent?: boolean
  codeImages?: { data: string; mime: string; filename: string }[]
  codeFiles?: { filename: string; mime: string; drive_url: string; drive_file_id: string; size_bytes: number }[]
  files?: { filename: string; ext: string }[]
  streaming?: boolean
  agentStatus?: string | null
}
