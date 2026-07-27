"use client"

import { Clock, Mail, FileText, Search, Zap } from "lucide-react"
import { MeetingVideoIcon } from "../hub/MeetingCard"
import { getIntegrationIcon } from "../../utils/integrationIcons"

const GmailIcon = getIntegrationIcon("gmail", 28)
const SlackIcon = getIntegrationIcon("slack", 28)
const NotionIcon = getIntegrationIcon("notion", 28)
const LinearIcon = getIntegrationIcon("linear", 28)
const GrafanaIcon = getIntegrationIcon("grafana", 28)

export interface FeatureTab {
  id: string
  label: string
  icon: React.ReactNode
  description: string
}

export const FEATURE_TABS: FeatureTab[] = [
  { id: "ticket-lookup", label: "Ticket Lookup", icon: <FileText size={14} />, description: "Instantly look up any support ticket by ID, customer, or keyword." },
  { id: "daily-briefing", label: "Daily Briefing", icon: <Clock size={14} />, description: "Start every morning with a personalized summary of your day." },
  { id: "smart-drafts", label: "Smart Drafts", icon: <Mail size={14} />, description: "AI-drafted replies that match your tone and context." },
  { id: "search-files", label: "Search Files", icon: <Search size={14} />, description: "Search across Google Drive, Sheets, Docs, and more in one place." },
  { id: "workflows", label: "Workflows", icon: <Zap size={14} />, description: "Automate repetitive tasks with natural language instructions." },
  { id: "meetings", label: "Meetings", icon: <MeetingVideoIcon size={14} />, description: "Meeting prep, summaries, and action items — automatically." },
]

export interface IntegrationCardConfig {
  id: string
  oauthProvider: string
  name: string
  description: string
  icon: React.ReactNode
  darkIcon?: boolean
  connectHint?: string
  alwaysConnected?: boolean
}

export const DEFAULT_REQUIRED = ["google", "slack"]

export const ONBOARDING_INTEGRATIONS: IntegrationCardConfig[] = [
  { id: "google", oauthProvider: "google", name: "Google", description: "Gmail, Calendar, and Drive", icon: GmailIcon },
  { id: "slack", oauthProvider: "slack", name: "Slack", description: "Messages and channels", icon: SlackIcon, connectHint: "Sign in to your Slack workspace when prompted" },
  { id: "notion", oauthProvider: "notion", name: "Notion", description: "Pages and databases", icon: NotionIcon, darkIcon: true },
  { id: "linear", oauthProvider: "linear", name: "Linear", description: "Issues and projects", icon: LinearIcon, darkIcon: true },
  { id: "grafana", oauthProvider: "grafana", name: "Grafana", description: "Dashboards and metrics", icon: GrafanaIcon, alwaysConnected: true },
  { id: "freshdesk", oauthProvider: "freshdesk", name: "Freshdesk", description: "Support tickets (read-only)", icon: getIntegrationIcon("freshdesk", 28), alwaysConnected: true },
  { id: "freshchat", oauthProvider: "freshchat", name: "Freshchat", description: "Customer chats (read-only)", icon: getIntegrationIcon("freshchat", 28), alwaysConnected: true },
]

export const STAGE_MAP: Record<string, { progress: number; label: string }> = {
  searching_web: { progress: 10, label: "Researching your background" },
  profile_ready: { progress: 20, label: "Profile research complete" },
  fetching_data: { progress: 30, label: "Fetching your emails, calendar, and Slack" },
  data_gathered: { progress: 45, label: "Data gathered, preparing analysis" },
  analyzing: { progress: 55, label: "Analyzing your workspace patterns" },
  building_profile: { progress: 70, label: "Building your profile" },
  generating_workflows: { progress: 85, label: "Generating personalized workflows" },
  saving: { progress: 95, label: "Saving everything" },
  done: { progress: 100, label: "All done!" },
}

export const IMPORT_PROMPT = `I'm moving to another service and need to export my data. List every memory you have stored about me, as well as any context you've learned about me from past conversations. Output everything in a single code block so I can easily copy it. Format each entry as: [date saved, if available] - memory content. Make sure to cover all of the following — preserve my words verbatim where possible: Instructions I've given you about how to respond (tone, format, style, 'always do X', 'never do Y'). Personal details: name, location, job, family, interests. Projects, goals, and recurring topics. Tools, languages, and frameworks I use. Preferences and corrections I've made to your behavior. Any other stored context not covered above. Do not summarize, group, or omit any entries. After the code block, confirm whether that is the complete set or if any remain.`
