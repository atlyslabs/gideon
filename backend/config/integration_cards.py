"""
Integration card registry — server-owned metadata for the integrations page.

Why this exists separately from `integration_registry.py`:
  `INTEGRATION_REGISTRY` is the SSOT for workflow validation
  (`VALID_INTEGRATIONS`) and uses one entry per *connection provider* (Google
  is one connection, even though it exposes Gmail / Calendar / Drive /
  Sheets / Docs as separate tools). The integrations page, however, shows
  one card per *user-visible product* — Gmail and Google Calendar each get
  their own card. Same provider, different cards. Keeping two registries
  avoids inflating VALID_INTEGRATIONS with card-only keys (which would
  break workflow validation by accepting strings that aren't real
  integrations).

This registry is the SSOT for FE card copy + flags. Previously this lived
in `frontend/src/app/components/integrations/providerConfig.ts`; moving it server-side means flipping
`coming_soon`, tweaking a capability bullet, or rewriting overview text
no longer requires a frontend redeploy — just a gideon BE deploy.

Icons remain FE-only because they're React components (SVG). The FE merges
this server data with its local icon map keyed by card id.
"""

from typing import Optional, TypedDict


class IntegrationCard(TypedDict, total=False):
    # Required
    id: str
    oauth_provider: str
    name: str
    description: str
    hero_color: str
    data_safe_title: str
    data_safe_description: str
    capabilities: list[str]
    overview_title: str
    overview_text: str
    feature_title: str
    feature_text: str
    # Optional
    coming_soon: bool
    dark_icon: bool
    api_key_connect: bool
    connect_hint: Optional[str]
    # True iff the integration is connected via a shared/internal credential
    # (no per-user OAuth or API key). Drives whether the FE shows a
    # connect/disconnect CTA. Defaults to False — must be set explicitly on
    # internal cards.
    internal: bool


INTEGRATION_CARDS: list[IntegrationCard] = [
    {
        "id": "gmail",
        "oauth_provider": "google",
        "name": "Gmail",
        "description": "Manage Gmail emails and drafts",
        "hero_color": "linear-gradient(135deg, #EA4335 0%, #FF6F61 100%)",
        "data_safe_title": "Your data is safe",
        "data_safe_description": "Your data stays in Google's servers. We only access it on your command.",
        "capabilities": ["List Emails", "Read Emails", "Search Emails", "Draft Emails", "Send Drafts", "Reply to Emails", "List Drafts", "Update Drafts"],
        "overview_title": "Overview",
        "overview_text": "Connect your Gmail to Gideon for intelligent email management. Read, search, draft, and send emails directly from your AI assistant.",
        "feature_title": "Email Intelligence",
        "feature_text": "Gideon can help you manage your inbox, draft replies in your tone, search for specific emails, and keep you on top of important conversations.",
    },
    {
        "id": "google-calendar",
        "oauth_provider": "google",
        "name": "Google Calendar",
        "description": "Manage calendar and schedule",
        "hero_color": "linear-gradient(135deg, #1A73E8 0%, #4FC3F7 100%)",
        "data_safe_title": "Your data is safe",
        "data_safe_description": "Your data stays in Google's servers. We only access it on your command.",
        "capabilities": ["List Today's Events", "List Events by Range", "Get Event Details", "Schedule Management"],
        "overview_title": "Overview",
        "overview_text": "Connect your Google Calendar to Gideon for intelligent schedule management. View events, check availability, and stay on top of your meetings.",
        "feature_title": "Schedule Intelligence",
        "feature_text": "Gideon can help you manage your calendar, check upcoming meetings, find free time slots, and keep you prepared for your day.",
    },
    {
        "id": "google-drive",
        "oauth_provider": "google",
        "name": "Google Drive",
        "description": "Access and manage Google Drive files",
        "hero_color": "linear-gradient(135deg, #0F9D58 0%, #4285F4 50%, #F4B400 100%)",
        "data_safe_title": "Your data is safe",
        "data_safe_description": "Your data stays in Google's servers. We only access it on your command.",
        "capabilities": ["List Files", "Search Files", "Read Documents", "Upload Files", "Manage Folders", "File Sharing"],
        "overview_title": "Overview",
        "overview_text": "Connect your Google Drive to Gideon for intelligent file management. Search, read, and organize your documents and files.",
        "feature_title": "File Intelligence",
        "feature_text": "Gideon can help you find files, read document contents, organize folders, and manage file sharing directly from your assistant.",
    },
    {
        "id": "google-sheets",
        "oauth_provider": "google",
        "name": "Google Sheets",
        "description": "Work with Google Sheets",
        "hero_color": "linear-gradient(135deg, #0F9D58 0%, #33B679 100%)",
        "data_safe_title": "Your data is indexed & encrypted",
        "data_safe_description": "Your data is indexed and encrypted at rest. We never train AI models on your data or share it with third parties.",
        "capabilities": ["Read Spreadsheets", "Create Spreadsheets", "Update Data", "Formula Calculations", "Data Analysis", "Chart Creation"],
        "overview_title": "Overview",
        "overview_text": "Connect your Google Sheets to Gideon for powerful spreadsheet management. Analyze data, create formulas, and build charts with AI assistance.",
        "feature_title": "Data Intelligence",
        "feature_text": "Gideon can help you analyze spreadsheet data, create complex formulas, generate insights from your data, and even create new spreadsheets based on your requirements.",
    },
    {
        "id": "google-docs",
        "oauth_provider": "google",
        "name": "Google Docs",
        "description": "Create and edit Google Docs",
        "hero_color": "linear-gradient(135deg, #4285F4 0%, #6EA6FF 100%)",
        "data_safe_title": "Your data is indexed & encrypted",
        "data_safe_description": "Your data is indexed and encrypted at rest. We never train AI models on your data or share it with third parties.",
        "capabilities": ["Read Documents", "Create Documents", "Update Documents", "Format Text", "Add Comments", "Collaborative Editing"],
        "overview_title": "Overview",
        "overview_text": "Connect your Google Docs to Gideon for intelligent document management. Create, edit, and collaborate on documents directly from your AI assistant.",
        "feature_title": "Document Intelligence",
        "feature_text": "Gideon can analyze document contents, help you write and edit text, format documents, and even create new documents based on your requirements.",
    },
    {
        "id": "google-slides",
        "oauth_provider": "google",
        "name": "Google Slides",
        "description": "Create and edit Google Slides",
        # No Slides tools are registered yet (see tools/tool_registry.py), so the
        # card is surfaced as coming-soon rather than as a working integration.
        "coming_soon": True,
        "hero_color": "linear-gradient(135deg, #F4B400 0%, #F7D76E 100%)",
        "data_safe_title": "Your data is indexed & encrypted",
        "data_safe_description": "Your data is indexed and encrypted at rest. We never train AI models on your data or share it with third parties.",
        "capabilities": ["Read Presentations", "Create Presentations", "Update Slides", "Slide Design", "Content Editing", "Template Usage"],
        "overview_title": "Overview",
        "overview_text": "Connect your Google Slides to Gideon for intelligent presentation management. Create, edit, and design presentations with AI assistance.",
        "feature_title": "Presentation Intelligence",
        "feature_text": "Gideon can help you create compelling presentations, design slides, organize content, and suggest improvements to make your presentations more effective.",
    },
    {
        "id": "slack",
        "oauth_provider": "slack",
        "name": "Slack",
        "description": "Manage Slack messages and channels",
        "hero_color": "linear-gradient(135deg, #4A154B 0%, #E01E5A 100%)",
        "data_safe_title": "Your data is safe",
        "data_safe_description": "Your data stays in Slack's systems. We only access it on your command.",
        "capabilities": ["Send Messages", "Read Messages", "Search Messages", "List Channels", "Thread Management"],
        "overview_title": "Overview",
        "overview_text": "Connect your Slack to Gideon for intelligent team communication management. Send messages, read channels, and stay on top of team conversations.",
        "feature_title": "Communication Intelligence",
        "feature_text": "Gideon can help you manage team communications, search for messages, summarize conversations, and draft messages for your team.",
    },
    {
        "id": "notion",
        "oauth_provider": "notion",
        "name": "Notion",
        "description": "Manage Notion pages and databases",
        "dark_icon": True,
        "hero_color": "linear-gradient(135deg, #191919 0%, #37352F 100%)",
        "data_safe_title": "Your data is indexed & encrypted",
        "data_safe_description": "Your data is indexed and encrypted at rest. We never train AI models on your data or share it with third parties.",
        "capabilities": ["Search Pages", "Read Pages", "Read Databases", "Create Pages"],
        "overview_title": "Overview",
        "overview_text": "Connect your Notion to Gideon for intelligent knowledge management. Search your workspace, read pages, and create new content.",
        "feature_title": "Knowledge Intelligence",
        "feature_text": "Gideon can help you search your Notion workspace, find information across pages and databases, and create new content on your behalf.",
    },
    {
        "id": "linear",
        "oauth_provider": "linear",
        "name": "Linear",
        "description": "Manage Linear issues and projects",
        "dark_icon": True,
        "hero_color": "linear-gradient(135deg, #5E6AD2 0%, #8B5CF6 100%)",
        "data_safe_title": "Your data is indexed & encrypted",
        "data_safe_description": "Your data is indexed and encrypted at rest. We never train AI models on your data or share it with third parties.",
        "capabilities": ["Create Issues", "Update Issues", "Search Issues", "List Issues", "Manage Teams", "Track Projects"],
        "overview_title": "Overview",
        "overview_text": "Connect your Linear to Gideon for intelligent project and task management. Create issues, track progress, and manage your development workflow.",
        "feature_title": "Project Intelligence",
        "feature_text": "Gideon can help you organize your projects, track team progress, create and assign tasks, and suggest improvements based on your workflow patterns.",
    },
    {
        "id": "github",
        "oauth_provider": "github",
        "name": "GitHub",
        "description": "Manage GitHub repos and pull requests",
        "dark_icon": True,
        "hero_color": "linear-gradient(135deg, #24292E 0%, #586069 100%)",
        "data_safe_title": "Your data is indexed & encrypted",
        "data_safe_description": "Your data is indexed and encrypted at rest. We never train AI models on your data or share it with third parties.",
        "capabilities": ["List Repos", "List Issues", "View Pull Requests", "Search Code", "Notifications", "PR Reviews"],
        "overview_title": "Overview",
        "overview_text": "Connect your GitHub to Gideon for intelligent development workflow management. Track repos, review PRs, search code, and manage notifications.",
        "feature_title": "Development Intelligence",
        "feature_text": "Gideon can help you stay on top of your repos, review pull requests, track issues, search codebases, and manage your GitHub notifications.",
    },
    {
        "id": "grafana",
        "oauth_provider": "grafana",
        "name": "Grafana",
        "description": "View dashboards and query metrics",
        "internal": True,
        "hero_color": "linear-gradient(135deg, #F46800 0%, #FF9830 100%)",
        "data_safe_title": "Internal integration",
        "data_safe_description": "Grafana is connected via admin service account. Dashboard access follows your Grafana RBAC permissions.",
        "capabilities": ["List Dashboards", "View Dashboards", "Query Panels", "Render Panels"],
        "overview_title": "Overview",
        "overview_text": "Grafana is connected internally for monitoring and observability. View dashboards, query panel data, and get insights from your metrics.",
        "feature_title": "Monitoring Intelligence",
        "feature_text": "Gideon can help you check dashboard status, query specific metrics, and surface relevant monitoring data when you need it.",
    },
    {
        "id": "freshdesk",
        "oauth_provider": "freshdesk",
        "name": "Freshdesk",
        "description": "Support tickets (read-only)",
        "internal": True,
        "hero_color": "linear-gradient(135deg, #22A849 0%, #4CC76E 100%)",
        "data_safe_title": "Internal integration",
        "data_safe_description": "Freshdesk is connected internally via shared API key. Read-only access to support tickets.",
        "capabilities": ["Search Tickets", "View Ticket Details", "Read Conversations", "List Recent Tickets"],
        "overview_title": "Overview",
        "overview_text": "Freshdesk is connected internally for support ticket lookup. Search tickets, view conversation threads, and get customer support context.",
        "feature_title": "Support Intelligence",
        "feature_text": "Gideon can help you look up Freshdesk tickets, read conversation threads, and surface relevant support context for customer complaints.",
    },
    {
        "id": "freshchat",
        "oauth_provider": "freshchat",
        "name": "Freshchat",
        "description": "Customer chat conversations (read-only)",
        "internal": True,
        "hero_color": "linear-gradient(135deg, #22A849 0%, #2ECC71 100%)",
        "data_safe_title": "Internal integration",
        "data_safe_description": "Freshchat is connected internally via shared API key. Read-only access to customer conversations.",
        "capabilities": ["Search Conversations", "Read Messages", "View Chat History"],
        "overview_title": "Overview",
        "overview_text": "Freshchat is connected internally for customer conversation lookup. Search conversations and read full chat histories.",
        "feature_title": "Chat Intelligence",
        "feature_text": "Gideon can help you find and read Freshchat conversations, providing context on customer interactions and support issues.",
    },
    {
        "id": "granola",
        "oauth_provider": "granola",
        "name": "Granola",
        "description": "AI meeting notes and transcripts",
        "dark_icon": True,
        "hero_color": "linear-gradient(135deg, #1a1a2e 0%, #3d2c5e 100%)",
        "data_safe_title": "Your data is indexed & encrypted",
        "data_safe_description": "Your data is indexed and encrypted at rest. We never train AI models on your data or share it with third parties.",
        "capabilities": ["Search Notes", "Search Transcripts", "List Meetings", "Get Transcript", "Calendar Events", "Meeting Summaries"],
        "overview_title": "Overview",
        "overview_text": "Connect your Granola to Gideon for intelligent meeting intelligence. Search meeting notes, review transcripts, and find what was discussed in past meetings.",
        "feature_title": "Meeting Intelligence",
        "feature_text": "Gideon can help you find what was said in meetings, extract action items, search across all your meeting transcripts, and surface relevant context from past discussions.",
    },
    {
        "id": "ramp",
        "oauth_provider": "ramp",
        "name": "Ramp",
        "description": "Expense management and corporate cards",
        "hero_color": "linear-gradient(135deg, #1EC28B 0%, #0A8F63 100%)",
        "data_safe_title": "Your data is indexed & encrypted",
        "data_safe_description": "Your data is indexed and encrypted at rest. We never train AI models on your data or share it with third parties.",
        "capabilities": ["List Transactions", "List Bills", "Reimbursements", "Team Members", "Company Balance", "Departments"],
        "overview_title": "Overview",
        "overview_text": "Connect your Ramp account to Gideon for intelligent expense management. View transactions, track bills, monitor reimbursements, and get spending insights.",
        "feature_title": "Finance Intelligence",
        "feature_text": "Gideon can help you review recent expenses, track pending bills, check reimbursement status, and surface spending patterns across your team.",
    },
    {
        "id": "figma",
        "oauth_provider": "figma",
        "name": "Figma",
        "description": "Design context, variables, and screenshots",
        "hero_color": "linear-gradient(135deg, #1E1E1E 0%, #333 100%)",
        "data_safe_title": "Your data is indexed & encrypted",
        "data_safe_description": "Your data is indexed and encrypted at rest. We never train AI models on your data or share it with third parties.",
        "capabilities": ["Design Context", "Variables & Styles", "Search Design System", "Screenshots", "File Metadata"],
        "overview_title": "Overview",
        "overview_text": "Connect your Figma to Gideon for design intelligence. Pull design context, review variables, search your design system, and get screenshots of any frame.",
        "feature_title": "Design Intelligence",
        "feature_text": "Gideon can help you understand design decisions, extract tokens and variables, search components across your design system, and capture visual references.",
    },
    {
        "id": "recruitee",
        "oauth_provider": "recruitee",
        "name": "Recruitee",
        "description": "Applicant tracking and hiring",
        "api_key_connect": True,
        "hero_color": "linear-gradient(135deg, #05BA6F 0%, #048A52 100%)",
        "data_safe_title": "Your data is indexed & encrypted",
        "data_safe_description": "Your API key is encrypted at rest. We never train AI models on your data or share it with third parties.",
        "capabilities": ["Search Candidates", "View Candidate Details", "List Jobs", "Job Pipeline", "Talent Pools"],
        "overview_title": "Overview",
        "overview_text": "Connect your Recruitee account to Gideon for intelligent hiring management. Search candidates, review applications, and track your recruitment pipeline.",
        "feature_title": "Hiring Intelligence",
        "feature_text": "Gideon can help you find candidates, check pipeline status, review talent pools, and surface relevant hiring data across your open positions.",
    },
    {
        "id": "posthog",
        "oauth_provider": "posthog",
        "name": "PostHog",
        "description": "Product analytics and feature flags",
        "hero_color": "linear-gradient(135deg, #F9BD2B 0%, #FF8C00 100%)",
        "data_safe_title": "Your data is safe",
        "data_safe_description": "Your data stays in PostHog's systems. We only access it on your command.",
        "capabilities": ["Run Queries", "List Errors", "Create Feature Flags", "Create Experiments", "Search Docs", "View Dashboards", "LLM Analytics"],
        "overview_title": "Overview",
        "overview_text": "Connect PostHog to Gideon for product analytics intelligence. Run queries, track errors, manage feature flags, and monitor experiments.",
        "feature_title": "Analytics & Insights",
        "feature_text": "Gideon can help you run trend, funnel, and HogQL queries, view dashboards and insights, and analyze your product data without leaving your workflow.",
    },
    {
        "id": "google-search-console",
        "oauth_provider": "google",
        "name": "Google Search Console",
        "description": "Search performance, indexing, and SEO insights",
        "hero_color": "linear-gradient(135deg, #4285F4 0%, #34A853 50%, #EA4335 100%)",
        "data_safe_title": "Your data is safe",
        "data_safe_description": "Your data stays in Google's servers. We only access it on your command.",
        "capabilities": ["Search Analytics", "Query Performance", "Page Performance", "URL Inspection", "Sitemap Status", "Index Coverage"],
        "overview_title": "Overview",
        "overview_text": "Connect your Google Search Console to Gideon for SEO intelligence. Analyze search queries, track page performance, inspect URLs, and monitor your site's health in Google Search.",
        "feature_title": "SEO Intelligence",
        "feature_text": "Gideon can help you analyze which queries drive traffic, identify top-performing pages, check indexing status of URLs, monitor sitemaps, and surface actionable SEO insights.",
    },
    {
        "id": "meta-ads",
        "oauth_provider": "meta_ads",
        "name": "Meta Ads",
        "description": "Manage Facebook and Instagram ad campaigns",
        "coming_soon": True,
        "hero_color": "linear-gradient(135deg, #0064E1 0%, #1A8AFA 50%, #6E48E8 100%)",
        "data_safe_title": "Your data is safe",
        "data_safe_description": "Your data stays in Meta's systems. We only access it on your command. Per-user OAuth — Gideon sees only the ad accounts you authorize.",
        "capabilities": ["List Ad Accounts", "Campaigns / Ad Sets / Ads", "Performance Insights", "Product Catalogs", "Catalog Diagnostics", "Pixel / CAPI Datasets", "Pages & Businesses"],
        "overview_title": "Overview",
        "overview_text": "Connect Meta Ads to Gideon for hands-free campaign analysis. Pull spend / impressions / clicks / conversions across Facebook and Instagram, audit pixel quality, and review product catalogs without leaving Gideon.",
        "feature_title": "Marketing Intelligence",
        "feature_text": "Gideon can summarize yesterday's spend by campaign, surface CTR drops, audit your Pixel match quality, and answer ad-hoc questions across all your Meta ad accounts in natural language.",
    },
    {
        "id": "semrush",
        "oauth_provider": "semrush",
        "name": "Semrush",
        "description": "SEO + marketing intel (read-only)",
        "internal": True,
        "hero_color": "linear-gradient(135deg, #FF642D 0%, #FF8F6B 100%)",
        "data_safe_title": "Internal integration",
        "data_safe_description": "Semrush is connected internally via a shared org API key. Read-only access to SEO and traffic data; no per-user OAuth.",
        "capabilities": ["Domain Overview", "Organic Keywords", "Keyword Overview", "Related Keywords", "Competitor Analysis"],
        "overview_title": "Overview",
        "overview_text": "Semrush is connected internally for SEO research. Query domain rankings, organic-keyword footprints, keyword volume + CPC + competition, related-keyword ideas, and top organic competitors across country databases.",
        "feature_title": "SEO Intelligence",
        "feature_text": "Gideon can pull live Semrush data on demand: \"what does example.com rank for in the US\", \"is 'project management software' worth targeting\", \"who are our top organic competitors\". Combines naturally with the rest of your workflow.",
    },
]


# OAuth providers — used by the FE to gate connect/disconnect CTAs.
# Derived from the card data: any card whose provider is not internal AND
# not API-key/form-based contributes its `oauth_provider` here. Adding a new
# OAuth card (e.g. Asana) requires only the card entry — this set updates
# automatically. `coming_soon` cards STILL contribute their provider; the FE
# bucket filter is responsible for hiding them from the "Available" list.
OAUTH_PROVIDERS: set[str] = {
    card["oauth_provider"]
    for card in INTEGRATION_CARDS
    if card["oauth_provider"]
    and not card.get("internal", False)
    and not card.get("api_key_connect", False)
}
