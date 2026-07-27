"use client"

import useSWR from "swr"
import { useCallback } from "react"
import { pingman } from "../utils/pingman"
import type { Skill } from "../types"

const API_BASE = "/api/gideon/v1/skills"

export default function useSkills() {
  const { data, error, isLoading, mutate } = useSWR<Skill[]>(
    API_BASE,
    async (url: string) => {
      const res = await pingman.get<{ skills: Skill[]; count: number }>(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.data.skills
    }
  )

  const getSkill = useCallback(async (id: string): Promise<Skill> => {
    const res = await pingman.get<Skill>(`${API_BASE}/${id}`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    return res.data
  }, [])

  const createSkill = useCallback(
    async (payload: { name: string; description: string; prompt: string; integrations_used: string[] }): Promise<Skill> => {
      const res = await pingman.post<Skill>(API_BASE, payload)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await mutate()
      return res.data
    },
    [mutate]
  )

  const updateSkill = useCallback(
    async (id: string, payload: Partial<Pick<Skill, "name" | "description" | "prompt" | "integrations_used">>): Promise<Skill> => {
      const res = await pingman.patch<Skill>(`${API_BASE}/${id}`, payload)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await mutate()
      return res.data
    },
    [mutate]
  )

  const deleteSkill = useCallback(
    async (id: string) => {
      const res = await pingman.delete(`${API_BASE}/${id}`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await mutate()
    },
    [mutate]
  )

  const learnSkill = useCallback(
    async (id: string): Promise<Skill> => {
      const res = await pingman.post<Skill>(`${API_BASE}/${id}/learn`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await mutate()
      return res.data
    },
    [mutate]
  )

  const initializeDefaults = useCallback(async () => {
    const res = await pingman.post(`${API_BASE}/initialize-defaults`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    await mutate()
  }, [mutate])

  return {
    skills: data || [],
    loading: isLoading,
    error,
    mutate,
    getSkill,
    createSkill,
    updateSkill,
    deleteSkill,
    learnSkill,
    initializeDefaults,
  }
}
