"use client"

import useSWR from "swr"
import { pingman } from "../utils/pingman"

export interface IntegrationManifestEntry {
  key: string
  label: string
  provider: string
  internal: boolean
}

const MANIFEST_URL = "/api/gideon/v1/integrations/manifest"

// Static fallback used until the manifest fetch resolves. Mirrors the BE
// registry in config/integration_registry.py and is the SSOT for which
// integrations don't need a per-user connection. Keep these in sync if you
// change the BE list — there's a unit assertion in the BE smoke import.
const FALLBACK_INTERNAL = new Set<string>(["grafana", "freshdesk", "freshchat", "semrush"])

export default function useIntegrationManifest() {
  const { data } = useSWR<IntegrationManifestEntry[]>(
    MANIFEST_URL,
    async (url: string) => {
      const res = await pingman.get<IntegrationManifestEntry[]>(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.data
    },
    {
      // Manifest is static config — no need to revalidate aggressively.
      revalidateOnFocus: false,
      revalidateIfStale: false,
      dedupingInterval: 60_000,
    }
  )

  const internal = (() => {
    // Always return a fresh Set so callers can't mutate the shared fallback
    // and have it leak across hook consumers.
    if (!data) return new Set(FALLBACK_INTERNAL)
    return new Set(data.filter((e) => e.internal).map((e) => e.key))
  })()

  const providerOf = (integration: string): string => {
    if (data) {
      const entry = data.find((e) => e.key === integration)
      if (entry) return entry.provider
    }
    if (integration === "gmail" || integration.startsWith("google_")) return "google"
    return integration
  }

  const isConnected = (integration: string, connectedProviders: string[]): boolean => {
    if (internal.has(integration)) return true
    return connectedProviders.includes(providerOf(integration))
  }

  return { manifest: data, internal, providerOf, isConnected }
}
