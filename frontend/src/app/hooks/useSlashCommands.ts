"use client"

import useSWR from "swr"
import { pingman } from "../utils/pingman"

export interface SlashCommand {
  slug: string
  name: string
  description: string
  param_hint: string
  is_builtin: boolean
}

const MANIFEST_URL = "/api/gideon/v1/commands/manifest"

export default function useSlashCommands() {
  const { data, error, isLoading } = useSWR<SlashCommand[]>(
    MANIFEST_URL,
    async (url: string) => {
      const res = await pingman.get<SlashCommand[]>(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.data
    },
    {
      // Static config — no need to revalidate aggressively.
      revalidateOnFocus: false,
      revalidateIfStale: false,
      dedupingInterval: 60_000,
    }
  )

  // `loaded` is "we have a definitive answer" — either data arrived or the
  // request settled with an error. Without the error branch, a failed
  // manifest fetch would leave consumers spinning on a permanent loading
  // state.
  return {
    commands: data || [],
    loaded: !isLoading && (data !== undefined || error !== undefined),
    error,
  }
}
