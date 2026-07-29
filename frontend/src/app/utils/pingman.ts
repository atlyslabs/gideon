/**
 * PingMan — HTTP client with auth injection.
 * Thin fetch wrapper around the Gideon backend.
 */

export interface PingManConfig {
  baseURL?: string
  headers?: Record<string, string>
  timeout?: number
}

export interface RequestConfig {
  params?: Record<string, any>
  headers?: Record<string, string>
  timeout?: number
}

export interface PingManResponse<T = any> {
  data: T
  status: number
  statusText: string
  headers: Record<string, string>
  ok: boolean
}

export class PingMan {
  private baseURL: string
  private defaultHeaders: Record<string, string>
  private timeout: number
  private authToken?: string

  constructor(config: PingManConfig = {}) {
    this.baseURL = config.baseURL || ""
    this.defaultHeaders = config.headers || { "Content-Type": "application/json" }
    this.timeout = config.timeout || 600000
  }

  setAuth(token: string) {
    this.authToken = token
  }

  clearAuth() {
    this.authToken = undefined
  }

  private buildURL(path: string, params?: Record<string, any>): string {
    const url = new URL(path, this.baseURL)
    if (params) {
      Object.entries(params).forEach(([key, value]) => {
        if (value !== undefined && value !== null) {
          url.searchParams.append(key, String(value))
        }
      })
    }
    return url.toString()
  }

  private async request<T>(
    method: string,
    path: string,
    data?: any,
    config: RequestConfig = {}
  ): Promise<PingManResponse<T>> {
    const url = this.buildURL(path, config.params)
    const headers: Record<string, string> = { ...this.defaultHeaders, ...config.headers }

    if (this.authToken) {
      headers["Authorization"] = `Bearer ${this.authToken}`
    }

    const controller = new AbortController()
    const timeoutId = setTimeout(() => controller.abort(), config.timeout || this.timeout)

    try {
      const options: RequestInit = { method, headers, signal: controller.signal }

      if (data) {
        if (data instanceof FormData) {
          delete headers["Content-Type"]
          options.body = data
        } else {
          options.body = JSON.stringify(data)
        }
      } else {
        delete headers["Content-Type"]
      }

      const response = await fetch(url, options)
      clearTimeout(timeoutId)

      const responseHeaders: Record<string, string> = {}
      response.headers.forEach((value, key) => { responseHeaders[key] = value })

      const contentType = response.headers.get("content-type")
      let responseData: any

      if (contentType?.includes("application/json")) {
        responseData = await response.json()
      } else if (contentType?.includes("text")) {
        responseData = await response.text()
      } else {
        responseData = await response.blob()
      }

      // Maintenance mode detection — backend returns 503 with
      // { maintenance: true, message, eta }. Fire a window event the layout
      // can listen to so every view can swap to the maintenance overlay.
      if (response.status === 503 && responseData && typeof responseData === "object" && responseData.maintenance) {
        if (typeof window !== "undefined") {
          window.dispatchEvent(new CustomEvent("gideon-maintenance", { detail: responseData }))
        }
      }

      return {
        data: responseData,
        status: response.status,
        statusText: response.statusText,
        headers: responseHeaders,
        ok: response.ok,
      }
    } catch (error) {
      clearTimeout(timeoutId)
      if ((error as Error).name === "AbortError") {
        throw new Error("Request timeout")
      }
      throw error
    }
  }

  async get<T>(path: string, config?: RequestConfig) { return this.request<T>("GET", path, undefined, config) }
  async post<T>(path: string, data?: any, config?: RequestConfig) { return this.request<T>("POST", path, data, config) }
  async put<T>(path: string, data?: any, config?: RequestConfig) { return this.request<T>("PUT", path, data, config) }
  async patch<T>(path: string, data?: any, config?: RequestConfig) { return this.request<T>("PATCH", path, data, config) }
  async delete<T>(path: string, config?: RequestConfig) { return this.request<T>("DELETE", path, undefined, config) }
}

export const pingman = new PingMan({
  baseURL: process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080",
  headers: { "Content-Type": "application/json" },
  timeout: 600000,
})
