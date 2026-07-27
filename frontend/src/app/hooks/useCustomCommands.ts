"use client"

import useSWR, { mutate as globalMutate } from "swr"
import { useCallback } from "react"
import { pingman } from "../utils/pingman"

export interface CustomCommand {
  id: string
  slug: string
  name: string
  description: string | null
  param_hint: string | null
  prompt_template: string
  overrides_builtin: boolean
  created_at: string
  updated_at: string
}

export interface CustomCommandInput {
  slug: string
  name: string
  description?: string
  param_hint?: string
  prompt_template: string
}

export interface CustomCommandUpdate {
  name?: string
  description?: string
  param_hint?: string
  prompt_template?: string
}

const API_BASE = "/api/gideon/v1/commands"
// SWR key for the manifest the chat dropdown reads. Mutating this after a
// CRUD op makes the dropdown reflect new/edited commands without a reload.
const MANIFEST_URL = `${API_BASE}/manifest`

export default function useCustomCommands() {
  const { data, error, isLoading, mutate } = useSWR<CustomCommand[]>(
    API_BASE,
    async (url: string) => {
      const res = await pingman.get<CustomCommand[]>(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.data
    },
  )

  const refresh = useCallback(async () => {
    // Invalidate both:
    // - this hook's data (the management page's row list)
    // - the SWR cache for the manifest URL, which `useSlashCommands` reads to
    //   render the chat dropdown. Using SWR's global `mutate(key)` ensures
    //   every subscriber re-renders with fresh data; a plain pingman.get
    //   would refresh the network response but leave SWR's cached value
    //   untouched, leaving the dropdown stale.
    await Promise.all([mutate(), globalMutate(MANIFEST_URL)])
  }, [mutate])

  const create = useCallback(async (body: CustomCommandInput): Promise<CustomCommand> => {
    const res = await pingman.post<CustomCommand>(API_BASE, body)
    if (!res.ok) {
      const detail = (res.data as any)?.detail || `HTTP ${res.status}`
      throw new Error(detail)
    }
    await refresh()
    return res.data
  }, [refresh])

  const update = useCallback(async (id: string, body: CustomCommandUpdate): Promise<CustomCommand> => {
    const res = await pingman.patch<CustomCommand>(`${API_BASE}/${id}`, body)
    if (!res.ok) {
      const detail = (res.data as any)?.detail || `HTTP ${res.status}`
      throw new Error(detail)
    }
    await refresh()
    return res.data
  }, [refresh])

  const remove = useCallback(async (id: string): Promise<void> => {
    const res = await pingman.delete(`${API_BASE}/${id}`)
    if (!res.ok && res.status !== 204) {
      const detail = (res.data as any)?.detail || `HTTP ${res.status}`
      throw new Error(detail)
    }
    await refresh()
  }, [refresh])

  return {
    commands: data || [],
    loading: isLoading,
    error,
    create,
    update,
    remove,
    mutate: refresh,
  }
}
