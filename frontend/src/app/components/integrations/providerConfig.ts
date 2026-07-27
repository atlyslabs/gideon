/**
 * Integration card config.
 *
 * Most fields (copy, capabilities, comingSoon, etc.) now come from the BE
 * via `useIntegrationCards()` — see `config/integration_cards.py`. This file
 * keeps only what HAS to live on the FE:
 *   - The icon component for each card id (React SVGs can't be serialized
 *     through JSON, so the BE doesn't know about them).
 *   - A `FALLBACK_PROVIDERS` snapshot of the rich data, used during
 *     first-paint while the BE response is still in flight. Keep this
 *     roughly in sync with the BE if you tweak hero gradients / copy you
 *     want visible before the manifest loads. Treat the BE as the source
 *     of truth otherwise.
 *
 * Adding a new card: add it to `config/integration_cards.py` on the BE and
 * register an icon below by the same id. The page picks it up automatically.
 */

import { getIntegrationIcon } from "../../utils/integrationIcons"

export interface ProviderConfig {
  id: string
  oauthProvider: string
  name: string
  description: string
  icon: React.ReactNode
  darkIcon?: boolean
  comingSoon?: boolean
  apiKeyConnect?: boolean
  connectHint?: string
  heroColor: string
  dataSafeTitle: string
  dataSafeDescription: string
  capabilities: string[]
  overviewTitle: string
  overviewText: string
  featureTitle: string
  featureText: string
}

// Icon map keyed by card id. The integrations page merges these with the
// BE manifest data — without an entry here a card renders with no icon.
export const CARD_ICONS: Record<string, React.ReactNode> = {
  "gmail": getIntegrationIcon("gmail", 32),
  "google-calendar": getIntegrationIcon("google_calendar", 32),
  "google-drive": getIntegrationIcon("google_drive", 32),
  "google-sheets": getIntegrationIcon("google_sheets", 32),
  "google-docs": getIntegrationIcon("google_docs", 32),
  "google-slides": getIntegrationIcon("google_slides", 32),
  "google-search-console": getIntegrationIcon("google_search_console", 32),
  "slack": getIntegrationIcon("slack", 32),
  "notion": getIntegrationIcon("notion", 32),
  "linear": getIntegrationIcon("linear", 32),
  "github": getIntegrationIcon("github", 32),
  "grafana": getIntegrationIcon("grafana", 32),
  "freshdesk": getIntegrationIcon("freshdesk", 32),
  "freshchat": getIntegrationIcon("freshchat", 32),
  "granola": getIntegrationIcon("granola", 32),
  "ramp": getIntegrationIcon("ramp", 32),
  "figma": getIntegrationIcon("figma", 32),
  "recruitee": getIntegrationIcon("recruitee", 32),
  "posthog": getIntegrationIcon("posthog", 32),
  "meta-ads": getIntegrationIcon("meta_ads", 32),
  "semrush": getIntegrationIcon("semrush", 32),
}

// Static fallback used during first-paint, before the BE manifest resolves.
// Mirrors `config/integration_cards.py` — keep roughly in sync; the BE wins
// once it loads. Stripped-down: only the fields we actually paint without
// the BE response.
export const FALLBACK_PROVIDERS: ProviderConfig[] = [
  fb("gmail", "google", "Gmail", "Manage Gmail emails and drafts", "linear-gradient(135deg, #EA4335 0%, #FF6F61 100%)"),
  fb("google-calendar", "google", "Google Calendar", "Manage calendar and schedule", "linear-gradient(135deg, #1A73E8 0%, #4FC3F7 100%)"),
  fb("google-drive", "google", "Google Drive", "Access and manage Google Drive files", "linear-gradient(135deg, #0F9D58 0%, #4285F4 50%, #F4B400 100%)"),
  fb("google-sheets", "google", "Google Sheets", "Work with Google Sheets", "linear-gradient(135deg, #0F9D58 0%, #33B679 100%)"),
  fb("google-docs", "google", "Google Docs", "Create and edit Google Docs", "linear-gradient(135deg, #4285F4 0%, #6EA6FF 100%)"),
  fb("google-slides", "google", "Google Slides", "Create and edit Google Slides", "linear-gradient(135deg, #F4B400 0%, #F7D76E 100%)"),
  fb("slack", "slack", "Slack", "Manage Slack messages and channels", "linear-gradient(135deg, #4A154B 0%, #E01E5A 100%)"),
  fb("notion", "notion", "Notion", "Manage Notion pages and databases", "linear-gradient(135deg, #191919 0%, #37352F 100%)", { darkIcon: true }),
  fb("linear", "linear", "Linear", "Manage Linear issues and projects", "linear-gradient(135deg, #5E6AD2 0%, #8B5CF6 100%)", { darkIcon: true }),
  fb("github", "github", "GitHub", "Manage GitHub repos and pull requests", "linear-gradient(135deg, #24292E 0%, #586069 100%)", { darkIcon: true }),
  fb("grafana", "grafana", "Grafana", "View dashboards and query metrics", "linear-gradient(135deg, #F46800 0%, #FF9830 100%)"),
  fb("freshdesk", "freshdesk", "Freshdesk", "Support tickets (read-only)", "linear-gradient(135deg, #22A849 0%, #4CC76E 100%)"),
  fb("freshchat", "freshchat", "Freshchat", "Customer chat conversations (read-only)", "linear-gradient(135deg, #22A849 0%, #2ECC71 100%)"),
  fb("granola", "granola", "Granola", "AI meeting notes and transcripts", "linear-gradient(135deg, #1a1a2e 0%, #3d2c5e 100%)", { darkIcon: true }),
  fb("ramp", "ramp", "Ramp", "Expense management and corporate cards", "linear-gradient(135deg, #1EC28B 0%, #0A8F63 100%)"),
  fb("figma", "figma", "Figma", "Design context, variables, and screenshots", "linear-gradient(135deg, #1E1E1E 0%, #333 100%)"),
  fb("recruitee", "recruitee", "Recruitee", "Applicant tracking and hiring", "linear-gradient(135deg, #05BA6F 0%, #048A52 100%)", { apiKeyConnect: true }),
  fb("posthog", "posthog", "PostHog", "Product analytics and feature flags", "linear-gradient(135deg, #F9BD2B 0%, #FF8C00 100%)"),
  fb("google-search-console", "google", "Google Search Console", "Search performance, indexing, and SEO insights", "linear-gradient(135deg, #4285F4 0%, #34A853 50%, #EA4335 100%)"),
  fb("meta-ads", "meta_ads", "Meta Ads", "Manage Facebook and Instagram ad campaigns", "linear-gradient(135deg, #0064E1 0%, #1A8AFA 50%, #6E48E8 100%)", { comingSoon: true }),
  fb("semrush", "semrush", "Semrush", "SEO + marketing intel (read-only)", "linear-gradient(135deg, #FF642D 0%, #FF8F6B 100%)"),
]

// Compact fallback row builder — same shape as the rich BE response, but
// with empty copy/capabilities. Cards still render with hero + icon while
// the manifest is in flight.
function fb(
  id: string,
  oauthProvider: string,
  name: string,
  description: string,
  heroColor: string,
  extra: { darkIcon?: boolean; comingSoon?: boolean; apiKeyConnect?: boolean } = {},
): ProviderConfig {
  return {
    id,
    oauthProvider,
    name,
    description,
    icon: CARD_ICONS[id],
    heroColor,
    dataSafeTitle: "",
    dataSafeDescription: "",
    capabilities: [],
    overviewTitle: "Overview",
    overviewText: "",
    featureTitle: "",
    featureText: "",
    ...extra,
  }
}

// Fallback OAuth providers set — used until the BE manifest tells us the
// real list. Matches the BE `OAUTH_PROVIDERS` in `config/integration_cards.py`.
export const FALLBACK_OAUTH_PROVIDERS = new Set([
  "google",
  "slack",
  "notion",
  "linear",
  "github",
  "granola",
  "ramp",
  "posthog",
  "figma",
  "meta_ads",
])
