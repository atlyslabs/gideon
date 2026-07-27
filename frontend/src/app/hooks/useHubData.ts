"use client"

import useSWR from "swr"
import { pingman } from "../utils/pingman"
import type { Todo, TodoSuggestion, HubEmail, HubMeeting, Delegatee, TodoUpdateEntry } from "../types"

const API_BASE = "/api/gideon/v1/hub"

export default function useHubData() {
  // Todos
  const {
    data: todosData,
    error: todosError,
    isLoading: todosLoading,
    mutate: mutateTodos,
  } = useSWR<{ todos: Todo[]; delegated: Todo[]; delegatedToMe: Todo[]; snoozed: Todo[] }>(
    `${API_BASE}/todos`,
    async (url: string) => {
      const res = await pingman.get<{ todos: Todo[]; delegated: Todo[]; delegated_to_me: Todo[]; snoozed: Todo[]; count: number }>(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return {
        todos: res.data.todos,
        delegated: res.data.delegated ?? [],
        delegatedToMe: res.data.delegated_to_me ?? [],
        snoozed: res.data.snoozed ?? [],
      }
    },
    { revalidateOnFocus: true, refreshInterval: 30_000 }
  )

  const todos = todosData?.todos ?? []
  const delegatedTodos = todosData?.delegated ?? []
  const delegatedToMeTodos = todosData?.delegatedToMe ?? []
  const snoozedTodos = todosData?.snoozed ?? []


  // Suggestions
  const {
    data: suggestions,
    error: suggestionsError,
    isLoading: suggestionsLoading,
    mutate: mutateSuggestions,
  } = useSWR<TodoSuggestion[]>(
    `${API_BASE}/suggestions`,
    async (url: string) => {
      const res = await pingman.get<{ suggestions: TodoSuggestion[] }>(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.data.suggestions
    },
    { revalidateOnFocus: true, refreshInterval: 30_000 }
  )

  // Emails
  const {
    data: emails,
    error: emailsError,
    isLoading: emailsLoading,
    mutate: mutateEmails,
  } = useSWR<HubEmail[]>(
    `${API_BASE}/emails`,
    async (url: string) => {
      const res = await pingman.get<{ items: HubEmail[] }>(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.data.items
    },
    { revalidateOnFocus: true, refreshInterval: 300_000 }
  )

  // Meetings
  const {
    data: meetingsData,
    error: meetingsError,
    isLoading: meetingsLoading,
    mutate: mutateMeetings,
  } = useSWR<{ items: HubMeeting[] }>(
    `${API_BASE}/meetings`,
    async (url: string) => {
      const res = await pingman.get<{ connected?: boolean; items?: HubMeeting[] }>(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return { items: res.data?.items || [] }
    },
    { revalidateOnFocus: true, refreshInterval: 60_000 }
  )
  const meetings = meetingsData?.items || []

  // --- Todo mutations ---

  const createTodo = async (text: string, priority: Todo["priority"] = "none", parentId?: string) => {
    // Optimistic: add a placeholder to cache immediately
    const tempId = `temp-${Date.now()}`
    const optimistic: Todo = {
      id: tempId,
      text,
      completed: false,
      priority,
      source: "manual",
      source_detail: null,
      assigned_to_gideon: false,
      assigned_conversation_id: null,
      agent_status: null,
      parent_id: parentId || null,
      snoozed_until: null,
      delegated_to: null,
      delegated_from_user_name: null,
      slack_channel_id: null,
      slack_thread_ts: null,
      reminder_cadence: null,
      subtasks: [],
      created_at: new Date().toISOString(),
    }

    if (parentId) {
      // Optimistic: add subtask to parent's subtasks array
      mutateTodos((prev) => {
        if (!prev) return prev
        return {
          ...prev,
          todos: prev.todos.map((t) =>
            t.id === parentId ? { ...t, subtasks: [...(t.subtasks || []), optimistic] } : t
          ),
        }
      }, false)
    } else {
      mutateTodos((prev) => ({
        todos: [optimistic, ...(prev?.todos || [])],
        delegated: prev?.delegated || [],
        delegatedToMe: prev?.delegatedToMe || [],
        snoozed: prev?.snoozed || [],
      }), false)
    }

    const body: Record<string, unknown> = { text, priority }
    if (parentId) body.parent_id = parentId

    const res = await pingman.post<Todo>(`${API_BASE}/todos`, body)
    if (!res.ok) {
      await mutateTodos()
      throw new Error("Failed to create todo")
    }

    if (parentId) {
      mutateTodos((prev) => {
        if (!prev) return prev
        return {
          ...prev,
          todos: prev.todos.map((t) =>
            t.id === parentId
              ? { ...t, subtasks: (t.subtasks || []).map((s) => (s.id === tempId ? res.data : s)) }
              : t
          ),
        }
      }, false)
    } else {
      mutateTodos((prev) => ({
        todos: (prev?.todos || []).map((t) => (t.id === tempId ? res.data : t)),
        delegated: prev?.delegated || [],
        delegatedToMe: prev?.delegatedToMe || [],
        snoozed: prev?.snoozed || [],
      }), false)
    }
    return res.data
  }

  const updateTodo = async (
    id: string,
    updates: { text?: string; completed?: boolean; priority?: Todo["priority"]; source_detail?: string | null }
  ) => {
    // Optimistic update — check both top-level todos and subtasks
    const applyUpdate = (list: Todo[]): Todo[] =>
      list.map((t) => {
        if (t.id === id) return { ...t, ...updates }
        if (t.subtasks?.length) return { ...t, subtasks: applyUpdate(t.subtasks) }
        return t
      })
    mutateTodos((prev) => {
      if (!prev) return prev
      return {
        ...prev,
        todos: applyUpdate(prev.todos),
        delegated: applyUpdate(prev.delegated),
        delegatedToMe: applyUpdate(prev.delegatedToMe),
      }
    }, false)
    const res = await pingman.patch<Todo>(`${API_BASE}/todos/${id}`, updates)
    if (!res.ok) {
      await mutateTodos()
      throw new Error("Failed to update todo")
    }
    // Revalidate when toggling completion so cross-section moves (delegated /
    // delegated-to-me → completed) reflect immediately, not on next poll.
    if (updates.completed !== undefined) await mutateTodos()
    return res.data
  }

  const deleteTodo = async (id: string) => {
    // Optimistic: remove from cache — check both top-level todos and subtasks
    const removeFromList = (list: Todo[]): Todo[] =>
      list
        .filter((t) => t.id !== id)
        .map((t) =>
          t.subtasks?.length ? { ...t, subtasks: removeFromList(t.subtasks) } : t
        )
    mutateTodos((prev) => {
      if (!prev) return prev
      return {
        ...prev,
        todos: removeFromList(prev.todos),
        delegated: removeFromList(prev.delegated),
        delegatedToMe: removeFromList(prev.delegatedToMe),
      }
    }, false)
    const res = await pingman.delete(`${API_BASE}/todos/${id}`)
    if (!res.ok) {
      await mutateTodos()
      throw new Error("Failed to delete todo")
    }
  }

  // --- Suggestion mutations ---

  const addSuggestion = async (id: string) => {
    // Optimistic: move suggestion to todos instantly
    const suggestion = (suggestions || []).find((s) => s.id === id)
    if (suggestion) {
      const optimisticTodo: Todo = {
        id: `temp-${Date.now()}`,
        text: suggestion.text,
        completed: false,
        priority: "none",
        source: "suggestion",
        source_detail: suggestion.detail || null,
        assigned_to_gideon: false,
        assigned_conversation_id: null,
        agent_status: null,
        parent_id: null,
        snoozed_until: null,
        delegated_to: null,
        delegated_from_user_name: null,
        slack_channel_id: null,
        slack_thread_ts: null,
        reminder_cadence: null,
        subtasks: [],
        created_at: new Date().toISOString(),
      }
      mutateTodos((prev) => ({
        todos: [optimisticTodo, ...(prev?.todos || [])],
        delegated: prev?.delegated || [],
        delegatedToMe: prev?.delegatedToMe || [],
        snoozed: prev?.snoozed || [],
      }), { revalidate: false })
      mutateSuggestions((prev) => (prev || []).filter((s) => s.id !== id), { revalidate: false })
    }

    const res = await pingman.post(`${API_BASE}/suggestions/${id}/add`)
    if (!res.ok) {
      await Promise.all([mutateTodos(), mutateSuggestions()])
      throw new Error("Failed to add suggestion")
    }
    await Promise.all([mutateTodos(), mutateSuggestions()])
  }

  const dismissSuggestion = async (id: string) => {
    // Optimistic: remove immediately
    mutateSuggestions((prev) => prev?.filter((s) => s.id !== id), false)
    const res = await pingman.post(`${API_BASE}/suggestions/${id}/dismiss`)
    if (!res.ok) {
      await mutateSuggestions() // revert on failure
      throw new Error("Failed to dismiss suggestion")
    }
  }

  const assignToGideon = async (todoId: string): Promise<{ conversation_id: string }> => {
    const res = await pingman.post<{ conversation_id: string; status: string }>(
      `${API_BASE}/todos/${todoId}/assign`
    )
    if (!res.ok) throw new Error("Failed to assign to Gideon")
    await mutateTodos()
    return { conversation_id: res.data.conversation_id }
  }

  const createSubtask = async (parentId: string, text: string) => {
    return createTodo(text, "none", parentId)
  }

  const snoozeTodo = async (id: string, until: string) => {
    // Optimistic: move todo from any section to snoozed
    mutateTodos((prev) => {
      if (!prev) return prev
      const todo = prev.todos.find((t) => t.id === id) || prev.delegated.find((t) => t.id === id) || prev.delegatedToMe.find((t) => t.id === id)
      if (!todo) return prev
      const snoozed = { ...todo, snoozed_until: until }
      return {
        todos: prev.todos.filter((t) => t.id !== id),
        delegated: prev.delegated.filter((t) => t.id !== id),
        delegatedToMe: prev.delegatedToMe.filter((t) => t.id !== id),
        snoozed: [snoozed, ...prev.snoozed],
      }
    }, false)

    const res = await pingman.post(`${API_BASE}/todos/${id}/snooze`, { until })
    if (!res.ok) {
      await mutateTodos()
      throw new Error("Failed to snooze todo")
    }
  }

  const unsnoozeTodo = async (id: string) => {
    // Optimistic: move todo from snoozed back to todos
    mutateTodos((prev) => {
      if (!prev) return prev
      const todo = prev.snoozed.find((t) => t.id === id)
      if (!todo) return prev
      const unsnoozed = { ...todo, snoozed_until: null }
      return {
        todos: [unsnoozed, ...prev.todos],
        delegated: prev.delegated,
        delegatedToMe: prev.delegatedToMe,
        snoozed: prev.snoozed.filter((t) => t.id !== id),
      }
    }, false)

    const res = await pingman.post(`${API_BASE}/todos/${id}/unsnooze`)
    if (!res.ok) {
      await mutateTodos()
      throw new Error("Failed to unsnooze todo")
    }
  }

  const generateSuggestions = async () => {
    const res = await pingman.post(`${API_BASE}/suggestions/generate`)
    if (!res.ok) throw new Error("Failed to generate suggestions")
    await mutateSuggestions()
  }

  // --- Delegation mutations ---

  const delegateTodo = async (todoId: string, delegatees: Delegatee[], message?: string, reminderCadence?: string, followUpAt?: string) => {
    // Optimistic: move todo from active to delegated section
    mutateTodos((prev) => {
      if (!prev) return prev
      const todo = prev.todos.find((t) => t.id === todoId)
      if (!todo) return prev
      const updated = { ...todo, delegated_to: delegatees }
      return {
        todos: prev.todos.filter((t) => t.id !== todoId),
        delegated: [updated, ...prev.delegated],
        delegatedToMe: prev.delegatedToMe,
        snoozed: prev.snoozed,
      }
    }, false)

    const res = await pingman.post<{ status: string; delegated_to: Delegatee[]; not_on_gideon?: Delegatee[] }>(
      `${API_BASE}/todos/${todoId}/delegate`,
      { delegatees, message, reminder_cadence: reminderCadence, follow_up_at: followUpAt }
    )
    if (!res.ok) {
      await mutateTodos()
      const detail = (res.data as unknown as { detail?: string } | null)?.detail
      throw new Error(detail || `Failed to delegate todo (HTTP ${res.status})`)
    }
    await mutateTodos()
    return res.data
  }

  const nudgeTodo = async (todoId: string) => {
    const res = await pingman.post(`${API_BASE}/todos/${todoId}/nudge`)
    if (!res.ok) throw new Error("Failed to send nudge")
  }

  const getTodoUpdates = async (todoId: string): Promise<TodoUpdateEntry[]> => {
    const res = await pingman.get<{ updates: TodoUpdateEntry[]; count: number }>(
      `${API_BASE}/todos/${todoId}/updates`
    )
    if (!res.ok) throw new Error("Failed to fetch updates")
    return res.data.updates
  }

  return {
    // Data
    todos,
    delegatedTodos,
    delegatedToMeTodos,
    snoozedTodos,
    suggestions: suggestions || [],
    emails: emails || [],
    meetings: meetings || [],

    // Loading states
    todosLoading,
    suggestionsLoading,
    emailsLoading,
    meetingsLoading,

    // Errors
    todosError,
    suggestionsError,
    emailsError,
    meetingsError,

    // Mutations
    createTodo,
    createSubtask,
    updateTodo,
    deleteTodo,
    snoozeTodo,
    unsnoozeTodo,
    assignToGideon,
    addSuggestion,
    dismissSuggestion,
    generateSuggestions,
    delegateTodo,
    nudgeTodo,
    getTodoUpdates,

    // Manual revalidation
    mutateTodos,
    mutateSuggestions,
    mutateEmails,
    mutateMeetings,
  }
}
