"use client"

import { useCallback, useMemo, useSyncExternalStore } from "react"

type AppPage = "chat" | "skills" | "workflows" | "settings" | "integrations" | "memory" | "files" | "usage" | "todo" | "explore" | "commands" | "surveys"

const PAGE_PATHS: Record<string, AppPage> = {
  "/skills": "skills",
  "/workflows": "workflows",
  "/settings": "settings",
  "/integrations": "integrations",
  "/memory": "memory",
  "/files": "files",
  "/usage": "usage",
  "/todo": "todo",
  "/explore": "explore",
  "/commands": "commands",
  "/surveys": "surveys",
}

function parsePath(pathname: string): {
  page: AppPage
  conversationId: string | null
  subId: string | null
  // Tertiary id for nested routes like /workflows/{id}/runs/{run_id}.
  // The intermediate segment ("runs") is not stored — pages know which
  // sub-resource they expose, so a single tertiary slot is enough.
  tertiaryId: string | null
} {
  // /chat/[id]
  if (pathname.startsWith("/chat/")) {
    return { page: "chat", conversationId: pathname.split("/")[2] || null, subId: null, tertiaryId: null }
  }
  // /skills/[id], /workflows/[id], /workflows/[id]/runs/[run_id], etc.
  for (const [prefix, page] of Object.entries(PAGE_PATHS)) {
    if (pathname === prefix) return { page, conversationId: null, subId: null, tertiaryId: null }
    if (pathname.startsWith(prefix + "/")) {
      const parts = pathname.split("/")
      return {
        page,
        conversationId: null,
        subId: parts[2] || null,
        // parts[3] is the intermediate segment name ("runs"); parts[4] is the id we want.
        tertiaryId: parts[4] || null,
      }
    }
  }
  // / or /chat → chat
  return { page: "chat", conversationId: null, subId: null, tertiaryId: null }
}

// External store for pathname that reacts to pushState + popstate
let currentPath = typeof window !== "undefined" ? window.location.pathname : "/"
const listeners = new Set<() => void>()

function notifyListeners() {
  currentPath = window.location.pathname
  for (const cb of listeners) cb()
}

if (typeof window !== "undefined") {
  window.addEventListener("popstate", notifyListeners)
}

function subscribe(cb: () => void) {
  listeners.add(cb)
  return () => listeners.delete(cb)
}

function getSnapshot() { return currentPath }
function getServerSnapshot() { return "/" }

export default function useAppRouter() {
  const pathname = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot)
  const { page, conversationId, subId, tertiaryId } = parsePath(pathname)

  const navigate = useCallback((path: string) => {
    if (path === pathname) return
    window.history.pushState(null, "", path)
    notifyListeners()
  }, [pathname])

  const navigateToPage = useCallback((p: AppPage) => {
    const target = p === "chat" ? "/" : `/${p}`
    if (target === pathname) return
    window.history.pushState(null, "", target)
    notifyListeners()
  }, [pathname])

  const navigateToConversation = useCallback((id: string) => {
    const target = `/chat/${id}`
    window.history.replaceState(null, "", target)
    notifyListeners()
  }, [])

  const replaceConversation = useCallback((id: string | null) => {
    const target = id ? `/chat/${id}` : "/"
    window.history.replaceState(null, "", target)
    notifyListeners()
  }, [])

  // Memoize the returned object so its identity only changes when the path
  // changes. Consumers (e.g. Layout) put `router` in useCallback dep arrays;
  // a fresh object every render would invalidate all of those callbacks on
  // every render tick (notably the 30-60fps chat-streaming ticks), defeating
  // React.memo on components that receive them as props.
  return useMemo(
    () => ({
      page,
      conversationId,
      subId,
      tertiaryId,
      pathname,
      navigate,
      navigateToPage,
      navigateToConversation,
      replaceConversation,
    }),
    [page, conversationId, subId, tertiaryId, pathname, navigate, navigateToPage, navigateToConversation, replaceConversation],
  )
}
