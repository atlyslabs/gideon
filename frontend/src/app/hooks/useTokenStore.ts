"use client"

import { create } from "zustand"
import { pingman } from "../utils/pingman"

interface TokenStore {
  authToken?: string
  boot: () => void
  logOut: () => void
  setAuthToken: (token: string) => void
}

const TOKEN_KEY = "@gideon/auth-token"

const initialToken = typeof window !== "undefined" ? window.localStorage.getItem(TOKEN_KEY) || "" : ""

if (initialToken) {
  pingman.setAuth(initialToken)
}

export const useTokenStore = create<TokenStore>((set) => ({
  authToken: initialToken,

  boot: () => {
    const authToken = window.localStorage.getItem(TOKEN_KEY)
    set((state) => ({ ...state, authToken: authToken || "" }))
  },

  setAuthToken: (token: string) => {
    localStorage.setItem(TOKEN_KEY, token)
    pingman.setAuth(token)
    set((state) => ({ ...state, authToken: token }))
  },

  logOut: () => {
    localStorage.removeItem(TOKEN_KEY)
    pingman.clearAuth()
    set((state) => ({ ...state, authToken: undefined }))
  },
}))
