"use client"

import useSWR from "swr"
import { pingman } from "../utils/pingman"
import {
  CARD_ICONS,
  FALLBACK_PROVIDERS,
  FALLBACK_OAUTH_PROVIDERS,
  type ProviderConfig,
} from "../components/integrations/providerConfig"

// Shape returned by GET /v1/integrations/cards. Matches the Pydantic
// `IntegrationCardsResponse` on the BE — camelCase via alias_generator.
interface IntegrationCardServer {
  id: string
  oauthProvider: string
  name: string
  description: string
  heroColor: string
  dataSafeTitle: string
  dataSafeDescription: string
  capabilities: string[]
  overviewTitle: string
  overviewText: string
  featureTitle: string
  featureText: string
  comingSoon?: boolean
  darkIcon?: boolean
  apiKeyConnect?: boolean
  connectHint?: string | null
}

interface IntegrationCardsResponse {
  cards: IntegrationCardServer[]
  oauthProviders: string[]
}

const ENDPOINT = "/api/gideon/v1/integrations/cards"

/**
 * Fetches server-owned integration card metadata and merges in FE-only
 * icon components. While the request is in flight, returns the static
 * fallback so first-paint isn't blank. The BE wins once it loads.
 */
export default function useIntegrationCards() {
  const { data, error, isLoading } = useSWR<IntegrationCardsResponse>(
    ENDPOINT,
    async (url: string) => {
      const res = await pingman.get<IntegrationCardsResponse>(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.data
    },
    {
      // Card metadata is static config — a long dedup window is fine and
      // saves re-fetching whenever IntegrationsPage re-mounts.
      dedupingInterval: 60_000,
      revalidateOnFocus: false,
    }
  )

  const providers: ProviderConfig[] = data
    ? data.cards.map((c) => ({
        id: c.id,
        oauthProvider: c.oauthProvider,
        name: c.name,
        description: c.description,
        icon: CARD_ICONS[c.id],
        darkIcon: c.darkIcon,
        comingSoon: c.comingSoon,
        apiKeyConnect: c.apiKeyConnect,
        connectHint: c.connectHint ?? undefined,
        heroColor: c.heroColor,
        dataSafeTitle: c.dataSafeTitle,
        dataSafeDescription: c.dataSafeDescription,
        capabilities: c.capabilities,
        overviewTitle: c.overviewTitle,
        overviewText: c.overviewText,
        featureTitle: c.featureTitle,
        featureText: c.featureText,
      }))
    : FALLBACK_PROVIDERS

  const oauthProviders = data
    ? new Set(data.oauthProviders)
    : FALLBACK_OAUTH_PROVIDERS

  return { providers, oauthProviders, isLoading, error }
}
