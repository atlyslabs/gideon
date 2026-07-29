"use client"

import { ArrowLeft, BarChart3, MessageSquare, Globe, FileSpreadsheet, Calendar, Ticket, Grid3x3, Clock, Workflow, CheckSquare, Sparkles, UserPlus, Lightbulb, CalendarClock, Mail, Link as LinkIcon, AtSign, Inbox, Wand2, Brain, Slash, LayoutTemplate } from "lucide-react"

interface ExplorePageProps {
  onBack: () => void
  onStartPrompt: (prompt: string) => void
  onOpenWorkflows: () => void
}

interface Category {
  id: string
  title: string
  description: string
  icon: typeof BarChart3
  color: string
  prompts: string[]
}

interface Feature {
  id: string
  title: string
  icon: typeof BarChart3
  color: string
  description: string
  howTo: string
}

const FEATURES: Feature[] = [
  {
    id: "todos",
    title: "Todos",
    icon: CheckSquare,
    color: "#22C55E",
    description: "Your task list, tracked in the right panel.",
    howTo: "Type a todo into the Hub panel on the right, or ask Gideon in chat: \"Add a todo: ship the pricing page by Friday.\" Click a todo to expand subtasks, snooze it, or mark it done.",
  },
  {
    id: "assign-gideon",
    title: "Assign a todo to Gideon",
    icon: Sparkles,
    color: "#8B5CF6",
    description: "Let Gideon run the todo as an agent.",
    howTo: "Hover any todo and click the purple sparkle icon. Gideon picks it up in a new chat, uses all your tools to actually do the work, and marks the todo done when finished.",
  },
  {
    id: "delegate",
    title: "Delegate a todo",
    icon: UserPlus,
    color: "#3B82F6",
    description: "Send a todo to a teammate via Slack with reminders.",
    howTo: "Open a todo → \"Delegate\" → pick people, set a reminder cadence (never / daily / every 2 days / weekly) and an optional follow-up time (1h, 4h, tomorrow 10am, custom). Gideon creates a Slack group DM, posts recurring nudges in-thread, and DMs you the moment they mark it done with a permalink. Replies flow back into the todo's timeline.",
  },
  {
    id: "suggestions",
    title: "Suggestions",
    icon: Lightbulb,
    color: "#F59E0B",
    description: "Action items Gideon proposes from your activity.",
    howTo: "Every hour (8am–9pm), Gideon scans your Slack, email, Linear, GitHub and suggests follow-ups in the Hub. Accept turns one into a todo; dismiss to ignore.",
  },
  {
    id: "meetings",
    title: "Meeting prep",
    icon: CalendarClock,
    color: "#EC4899",
    description: "Auto-generated prep 30 min before every meeting.",
    howTo: "Any calendar event in the next 7 days gets a prep card in the Meetings tab — attendee bios (via Exa), recent threads with them, and talking points. A banner appears in the sidebar for the next meeting.",
  },
  {
    id: "emails",
    title: "Email auto-drafts",
    icon: Mail,
    color: "#EF4444",
    description: "Drafts replies to unread emails in your voice.",
    howTo: "Gideon scans unread Gmail every 10 min and drafts replies using your Email Tone skill. Review in the Emails tab; edit and send with Cmd+Enter.",
  },
  {
    id: "skills",
    title: "Create a skill",
    icon: Wand2,
    color: "#A855F7",
    description: "Teach Gideon a reusable behavior or tone.",
    howTo: "Open Skills in the sidebar → New Skill. Give it a name (e.g. \"Email tone\", \"PRD format\") and a prompt describing how Gideon should behave when this skill applies. Skills load automatically into relevant chats — e.g. the Email tone skill shapes auto-drafts.",
  },
  {
    id: "integrations",
    title: "Integrations",
    icon: LinkIcon,
    color: "#14B8A6",
    description: "Connect your work tools — Gideon gets access.",
    howTo: "Open Integrations in the sidebar and connect Google, Slack, Notion, Linear, GitHub, Figma, Ramp, etc. Once connected, Gideon can read and write on your behalf in any chat.",
  },
  {
    id: "trigger-slack",
    title: "@Gideon on Slack",
    icon: AtSign,
    color: "#F97316",
    description: "Tag Gideon in any Slack channel or DM.",
    howTo: "Type @Gideon <your instruction> in any channel Gideon is in (it auto-joins public ones). Gideon replies in the same thread with the result, using all your tools.",
  },
  {
    id: "trigger-email",
    title: "Email Gideon",
    icon: Inbox,
    color: "#06B6D4",
    description: "CC Gideon's address to run tasks from your inbox.",
    howTo: "Compose or reply to any email, CC Gideon's address, and write \"Gideon, …\" with your instruction. Gideon replies in the same thread, which is great for forwarding a customer email and asking for a draft response.",
  },
  {
    id: "memory-graph",
    title: "Memory graph",
    icon: Brain,
    color: "#A855F7",
    description: "See — and curate — what Gideon knows about you.",
    howTo: "Open Memory in the sidebar for an interactive 3D map of people, projects, companies, and preferences Gideon has learned. Nodes are typed, color-coded, and filterable via the legend top-left. Click any node to focus; hit the trash icon on a fact to delete it, or \"Forget this entirely\" to remove the whole entity. Add a memory with the top-right button; a smart pruning pass filters out transactional noise before ingestion.",
  },
  {
    id: "slash-commands",
    title: "Slash commands",
    icon: Slash,
    color: "#EAB308",
    description: "Type / in chat to run a recipe in one shot.",
    howTo: "Start a chat message with / and a dropdown shows what's available. Built-ins include /eod (end-of-day digest) and /research (Exa-driven brief). Add your own from Commands in the sidebar — same prompt-template pattern, scoped to your account, can override built-ins.",
  },
  {
    id: "preview",
    title: "Live React previews",
    icon: LayoutTemplate,
    color: "#F472B6",
    description: "Describe a screen, see it rendered live with real DS components.",
    howTo: "Ask Gideon for any UI in plain English: \"design me a settings page with notification toggles\", \"sketch a checkout flow for cafe orders\", \"show a stacked card layout with three pricing tiers\". The result renders as an interactive iframe inline in the chat, built from real React components and Tailwind v4. Tweak with follow-up prompts (\"make the CTA full-width\", \"swap to a danger color\") for fast iteration. Aimed at PMs and designers who want to prototype without opening a code editor.",
  },
]

const CATEGORIES: Category[] = [
  {
    id: "analytics",
    title: "Dashboards & analytics",
    description: "ClickHouse and Grafana. Results render as a table with CSV and Sheet export.",
    icon: BarChart3,
    color: "#22C55E",
    prompts: [
      "What is the total revenue for yesterday?",
      "What was the average order value by region for the last 3 months?",
      "What were the top 10 products last week by transactions?",
      "What was the weekly signup-to-paid conversion rate for the last 3 weeks?",
      "What was the device-level breakdown of transactions yesterday?",
      "How many new users transacted daily in the last 10 days, and what did they spend?",
      "Compare this month's revenue against the same period last month, by week.",
      "What's the conversion rate for mobile users yesterday vs the day before?",
      "Pull signups and conversions for the last 8 weeks, split by paid vs organic, as a table.",
      "Deep dive into customer uid=<UID>. What's blocking their order?",
      "RCA for order <order-id>: what went wrong, when, and which step failed?",
    ],
  },
  {
    id: "data-tables",
    title: "Data tables (exportable)",
    description: "ClickHouse queries that return a table, one click to CSV or Google Sheet.",
    icon: FileSpreadsheet,
    color: "#10B981",
    prompts: [
      "Show me a table of the top 50 stalled orders (created more than 14 days ago, not completed) with customer email and days stuck.",
      "Pull yesterday's purchases as a table: user email, amount, product, payment method. Export to a Google Sheet.",
      "List customers who paid in the last 30 days but never activated, with email and purchase date.",
      "Give me the product funnel for last week, broken down by day as a table.",
      "Show all orders above $500 from the past 7 days with status and customer email.",
    ],
  },
  {
    id: "communication",
    title: "Daily digests & comms",
    description: "Slack, Gmail, Linear, GitHub — build your morning briefing.",
    icon: MessageSquare,
    color: "#3B82F6",
    prompts: [
      "Check my Linear tickets and GitHub comments for today and send me a summary on Slack DM.",
      "Summarize unread Slack threads from #engineering over the last 24 hours.",
      "Find emails I haven't replied to in the last 3 days and draft quick responses.",
      "Pull today's @-mentions across Slack, Linear, and GitHub into a single digest.",
      "Read the attachment on the latest email from <Name> and summarize what it says.",
      "Open a Linear issue \"<Title>\" in <Team>, set due 2026-05-14, estimate 3, and link the design doc <URL>.",
      "Mark <ENG-123> as blocked by <ENG-100>, set its parent to <ENG-90>, and move it to the current cycle.",
      "Open a GitHub issue in <org>/<repo> titled \"<Title>\", body \"<details>\", labels enhancement, and request review from <github-username>.",
      "On Slack DM with <Name>, edit my last message to fix the typo and react with :white_check_mark:.",
    ],
  },
  {
    id: "research",
    title: "Web research",
    description: "Exa-powered search — companies, people, news, competitors.",
    icon: Globe,
    color: "#8B5CF6",
    prompts: [
      "Research <Company Name> — customers, funding, recent news, and key leadership.",
      "Find 5 articles this week about <topic> and summarize the common thread.",
      "Look up <Person Name>'s background and recent posts — prep me for a meeting.",
      "What are competitors shipping this month? Summarize their public changelogs.",
    ],
  },
  {
    id: "docs",
    title: "Docs & sheets",
    description: "Google Sheets, Docs, Drive — read, fill, generate.",
    icon: FileSpreadsheet,
    color: "#F59E0B",
    prompts: [
      "Go through the 'Q1 Hiring Plan' Google Sheet and summarize open roles per team.",
      "Read the doc I just uploaded and extract action items into my todo list.",
      "Create a Google Sheet comparing these three vendors on pricing and features.",
      "Fill in the status column of my project tracker using my latest Linear updates.",
      "Take this report I'm about to paste, render it as a styled PDF, and save it to Drive.",
      "/deck Build a 6-slide deck on \"Why we cut read latency 50x\" — opener, problem, before/after table, architecture flow, takeaways, closing.",
      "Append a row to the \"Q2 Pipeline\" sheet with these values: <Name>, <Stage>, <ARR>, <Owner>. Then sort by ARR descending.",
      "In the doc \"Roadmap\", find every \"Q1\" and replace with \"Q2\", then insert a page break before the Risks section.",
      "Add a column chart to my \"Weekly Metrics\" sheet on the range A1:B10 titled \"Weekly txns\".",
      "Move the file <File Name> in my Drive to the \"Archive 2026\" folder, then share it with <email> as commenter.",
    ],
  },
  {
    id: "meetings",
    title: "Meetings",
    description: "Calendar + Granola + attendee research — walk in prepared.",
    icon: Calendar,
    color: "#EC4899",
    prompts: [
      "Prep me for my next meeting — notes, attendee background, recent threads.",
      "After my 3pm with <Name>, write follow-up notes with action items and send to them.",
      "Find 3 open slots next week where <Name> and I are both free.",
      "Summarize my Granola notes from yesterday's meetings into one bullet list.",
      "Find a 30-min slot tomorrow when <name1@example.com>, <name2@example.com>, and I are all free.",
      "Add <Name> as an attendee to my 4pm meeting today and send the invite.",
    ],
  },
  {
    id: "ops",
    title: "Ops & tickets",
    description: "Freshdesk and Freshchat — customer lookups and ticket triage.",
    icon: Ticket,
    color: "#EF4444",
    prompts: [
      "Look up Freshdesk ticket #<ticket-id> and summarize the full conversation history.",
      "Find all Freshdesk tickets from today mentioning 'refund' and group them by theme.",
      "Which tickets have been open longer than 14 days? Who owns each?",
      "Summarize this customer's recent Freshchat conversations and what's still unresolved.",
    ],
  },
  {
    id: "tools",
    title: "Other tools",
    description: "Notion, Figma, Ramp, PostHog, Recruitee — the rest of your stack.",
    icon: Grid3x3,
    color: "#14B8A6",
    prompts: [
      "Archive done tasks in my Notion 'Tasks' database and move them to an Archive page.",
      "Pull my Ramp card transactions from last week and categorize them by vendor.",
      "What's the latest PostHog funnel conversion for the checkout flow?",
      "Find Figma frames linked to the new pricing page and summarize the spec.",
    ],
  },
  {
    id: "schedule",
    title: "Schedule & automate",
    description: "Turn any of the above into a recurring workflow.",
    icon: Clock,
    color: "#F97316",
    prompts: [
      "Every Monday at 9am, send me a DM with my open Linear tickets and pending PR reviews.",
      "At the start of each day, summarize my calendar and flag overlapping meetings.",
      "Every Friday at 5pm, draft a weekly update based on my closed tickets and commits.",
      "Every hour during work hours, check for new @-mentions and ping me if urgent.",
      "Every day at 12 PM, pull the last 50 rows from the Grafana dashboard at https://grafana.example.com/d/<uid>/<slug>, look up each user_id in Freshdesk to fetch their tickets, join the two, write the combined rows to a Google Sheet, and DM me a summary with success/failure counts and the sheet link.",
    ],
  },
]

export default function ExplorePage({ onBack, onStartPrompt, onOpenWorkflows }: ExplorePageProps) {
  return (
    <div className="flex-1 flex flex-col min-w-0 rounded-xl overflow-hidden" style={{ background: "var(--surface)" }}>
      {/* Header */}
      <div className="shrink-0 px-6 pt-6 pb-4" style={{ borderBottom: "1px solid var(--border)" }}>
        <div className="flex items-center gap-3 mb-4">
          <button
            onClick={onBack}
            className="flex items-center gap-1.5 text-[13px] transition-colors"
            style={{ color: "var(--text-secondary)" }}
            onMouseEnter={e => { e.currentTarget.style.color = "var(--text-primary)" }}
            onMouseLeave={e => { e.currentTarget.style.color = "var(--text-secondary)" }}
          >
            <ArrowLeft size={14} />
            Back
          </button>
        </div>
        <h1 className="text-[20px] font-semibold mb-1" style={{ color: "var(--text-primary)" }}>
          Explore
        </h1>
        <p className="text-[13px]" style={{ color: "var(--text-secondary)" }}>
          Starter prompts to show what Gideon can do. Click one to run it.
        </p>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto px-6 py-5">
        {/* Features guide */}
        <div className="mb-2 text-[11px] font-semibold tracking-wider uppercase" style={{ color: "var(--text-tertiary)" }}>
          How to use Gideon
        </div>
        <div className="grid gap-3 mb-6" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))" }}>
          {FEATURES.map(f => {
            const Icon = f.icon
            return (
              <div
                key={f.id}
                className="rounded-xl p-3.5"
                style={{
                  background: "var(--surface-elevated)",
                  border: "1px solid var(--border)",
                }}
              >
                <div className="flex items-center gap-2.5 mb-1.5">
                  <div
                    className="w-7 h-7 rounded-lg flex items-center justify-center shrink-0"
                    style={{ background: `${f.color}22` }}
                  >
                    <Icon size={14} style={{ color: f.color }} />
                  </div>
                  <h3 className="text-[13px] font-semibold" style={{ color: "var(--text-primary)" }}>
                    {f.title}
                  </h3>
                </div>
                <div className="text-[12px] mb-1.5 ml-[38px]" style={{ color: "var(--text-secondary)" }}>
                  {f.description}
                </div>
                <div className="text-[11.5px] leading-relaxed ml-[38px]" style={{ color: "var(--text-tertiary)" }}>
                  {f.howTo}
                </div>
              </div>
            )
          })}
        </div>

        {/* Prompts */}
        <div className="mb-2 text-[11px] font-semibold tracking-wider uppercase" style={{ color: "var(--text-tertiary)" }}>
          Starter prompts
        </div>
        <div className="grid gap-4" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))" }}>
          {CATEGORIES.map(cat => {
            const Icon = cat.icon
            return (
              <div
                key={cat.id}
                className="rounded-xl p-4 flex flex-col"
                style={{
                  background: "var(--surface-elevated)",
                  border: "1px solid var(--border)",
                }}
              >
                <div className="flex items-center gap-2.5 mb-1">
                  <div
                    className="w-7 h-7 rounded-lg flex items-center justify-center shrink-0"
                    style={{ background: `${cat.color}22` }}
                  >
                    <Icon size={15} style={{ color: cat.color }} />
                  </div>
                  <h3 className="text-[14px] font-semibold" style={{ color: "var(--text-primary)" }}>
                    {cat.title}
                  </h3>
                </div>
                <p className="text-[12px] mb-3 ml-[38px]" style={{ color: "var(--text-tertiary)" }}>
                  {cat.description}
                </p>
                <div className="flex flex-col gap-1.5">
                  {cat.prompts.map((p, i) => (
                    <button
                      key={i}
                      onClick={() => onStartPrompt(p)}
                      className="text-left text-[12px] px-3 py-2 rounded-lg transition-colors leading-snug"
                      style={{
                        background: "var(--surface-input)",
                        border: "1px solid var(--border)",
                        color: "var(--text-secondary)",
                      }}
                      onMouseEnter={e => {
                        e.currentTarget.style.background = "rgba(124,58,237,0.08)"
                        e.currentTarget.style.borderColor = "rgba(124,58,237,0.3)"
                        e.currentTarget.style.color = "var(--text-primary)"
                      }}
                      onMouseLeave={e => {
                        e.currentTarget.style.background = "var(--surface-input)"
                        e.currentTarget.style.borderColor = "var(--border)"
                        e.currentTarget.style.color = "var(--text-secondary)"
                      }}
                    >
                      {p}
                    </button>
                  ))}
                </div>
              </div>
            )
          })}
        </div>

        {/* Workflow hint banner */}
        <button
          onClick={onOpenWorkflows}
          className="w-full mt-5 rounded-xl px-4 py-3.5 flex items-center gap-3 transition-colors text-left"
          style={{
            background: "rgba(124,58,237,0.08)",
            border: "1px solid rgba(124,58,237,0.25)",
          }}
          onMouseEnter={e => { e.currentTarget.style.background = "rgba(124,58,237,0.15)" }}
          onMouseLeave={e => { e.currentTarget.style.background = "rgba(124,58,237,0.08)" }}
        >
          <div
            className="w-9 h-9 rounded-lg flex items-center justify-center shrink-0"
            style={{ background: "rgba(124,58,237,0.2)" }}
          >
            <Workflow size={17} style={{ color: "#a78bfa" }} />
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-[13px] font-semibold mb-0.5" style={{ color: "var(--text-primary)" }}>
              Turn any of these into a scheduled workflow
            </div>
            <div className="text-[12px]" style={{ color: "var(--text-secondary)" }}>
              Ask Gideon to "do this every Monday at 9am" — or open Workflows to set it up manually.
            </div>
          </div>
        </button>
      </div>
    </div>
  )
}
