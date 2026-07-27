"use client"

import useSWR from "swr"
import { useCallback } from "react"
import { pingman } from "../utils/pingman"
import type { Workflow, WorkflowRun, WorkflowTemplate } from "../types"

const API_BASE = "/api/gideon/v1/workflows"

export default function useWorkflows() {
  const { data, error, isLoading, mutate } = useSWR<Workflow[]>(
    API_BASE,
    async (url: string) => {
      const res = await pingman.get<{ workflows: Workflow[]; count: number }>(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.data.workflows
    }
  )

  const getWorkflow = useCallback(async (id: string): Promise<Workflow> => {
    const res = await pingman.get<Workflow>(`${API_BASE}/${id}`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    return res.data
  }, [])

  // Read-only template view — any logged-in user can fetch. Used when
  // getWorkflow returns 404 (workflow belongs to someone else).
  const getWorkflowTemplate = useCallback(async (id: string): Promise<WorkflowTemplate> => {
    const res = await pingman.get<WorkflowTemplate>(`${API_BASE}/template/${id}`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    return res.data
  }, [])

  // Clone a shared workflow into the current user's account as a new
  // inactive workflow with manual approval.
  const cloneWorkflow = useCallback(async (id: string): Promise<Workflow> => {
    const res = await pingman.post<Workflow>(`${API_BASE}/clone/${id}`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    await mutate()
    return res.data
  }, [mutate])

  // Flip the link_shared opt-in on a workflow the caller owns.
  const updateWorkflowShare = useCallback(async (id: string, linkShared: boolean): Promise<Workflow> => {
    const res = await pingman.patch<Workflow>(`${API_BASE}/${id}/share`, { link_shared: linkShared })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    await mutate()
    return res.data
  }, [mutate])

  const createWorkflow = useCallback(
    async (payload: Partial<Workflow>): Promise<Workflow> => {
      const res = await pingman.post<Workflow>(API_BASE, payload)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await mutate()
      return res.data
    },
    [mutate]
  )

  const updateWorkflow = useCallback(
    async (id: string, payload: Partial<Workflow>): Promise<Workflow> => {
      const res = await pingman.patch<Workflow>(`${API_BASE}/${id}`, payload)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await mutate()
      return res.data
    },
    [mutate]
  )

  const deleteWorkflow = useCallback(
    async (id: string) => {
      const res = await pingman.delete(`${API_BASE}/${id}`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await mutate()
    },
    [mutate]
  )

  const activateWorkflow = useCallback(
    async (id: string): Promise<Workflow> => {
      const res = await pingman.post<Workflow>(`${API_BASE}/${id}/activate`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await mutate()
      return res.data
    },
    [mutate]
  )

  const deactivateWorkflow = useCallback(
    async (id: string): Promise<Workflow> => {
      const res = await pingman.post<Workflow>(`${API_BASE}/${id}/deactivate`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await mutate()
      return res.data
    },
    [mutate]
  )

  const runWorkflow = useCallback(
    async (id: string): Promise<WorkflowRun> => {
      const res = await pingman.post<WorkflowRun>(`${API_BASE}/${id}/run`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.data
    },
    []
  )

  const getRuns = useCallback(async (id: string): Promise<WorkflowRun[]> => {
    const res = await pingman.get<{ runs: WorkflowRun[]; count: number }>(`${API_BASE}/${id}/runs`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    return res.data.runs
  }, [])

  const previewSteps = useCallback(
    async (prompt: string): Promise<{ tool: string; label: string }[]> => {
      const res = await pingman.post<{ steps: { tool: string; label: string }[] }>(`${API_BASE}/preview-steps`, { prompt })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.data.steps
    },
    []
  )

  return {
    workflows: data || [],
    loading: isLoading,
    error,
    mutate,
    getWorkflow,
    getWorkflowTemplate,
    cloneWorkflow,
    updateWorkflowShare,
    createWorkflow,
    updateWorkflow,
    deleteWorkflow,
    activateWorkflow,
    deactivateWorkflow,
    runWorkflow,
    getRuns,
    previewSteps,
  }
}
