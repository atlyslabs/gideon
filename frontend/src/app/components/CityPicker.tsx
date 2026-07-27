"use client"

import { useState, useRef, useEffect, useCallback } from "react"
import { MapPin, Loader2 } from "lucide-react"

interface CityResult {
  name: string
  region: string
  country: string
}

interface CityPickerProps {
  value: string
  onChange: (city: string) => void
  placeholder?: string
  /** "dark" for onboarding (white/glass), "surface" for settings page */
  variant?: "dark" | "surface"
}

export default function CityPicker({ value, onChange, placeholder = "Search your city...", variant = "surface" }: CityPickerProps) {
  const [query, setQuery] = useState(value)
  const [results, setResults] = useState<CityResult[]>([])
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const containerRef = useRef<HTMLDivElement>(null)

  // Sync external value changes
  useEffect(() => { setQuery(value) }, [value])

  // Clear debounce timer on unmount
  useEffect(() => {
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current)
    }
  }, [])

  // Click outside to close
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener("mousedown", handler)
    return () => document.removeEventListener("mousedown", handler)
  }, [])

  const search = useCallback(async (q: string) => {
    if (q.length < 2) {
      setResults([])
      return
    }
    setLoading(true)
    try {
      const res = await fetch(`/api/weather/search?q=${encodeURIComponent(q)}`)
      if (res.ok) {
        setResults(await res.json())
        setOpen(true)
      }
    } catch {
      // silent
    } finally {
      setLoading(false)
    }
  }, [])

  const handleInput = (val: string) => {
    setQuery(val)
    if (debounceRef.current) clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(() => search(val), 300)
  }

  const handleSelect = (city: CityResult) => {
    const label = city.name
    setQuery(label)
    setOpen(false)
    setResults([])
    onChange(label)
  }

  const isDark = variant === "dark"

  return (
    <div ref={containerRef} className="relative" style={{ width: isDark ? "100%" : 260 }}>
      <div className="relative">
        <MapPin
          size={14}
          className="absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none"
          style={{ color: isDark ? "rgba(255,255,255,0.4)" : "var(--text-tertiary)" }}
        />
        <input
          type="text"
          value={query}
          onChange={(e) => handleInput(e.target.value)}
          onFocus={() => { if (results.length) setOpen(true) }}
          placeholder={placeholder}
          className="w-full pl-8 pr-8 py-2 rounded-lg text-[13px]"
          style={{
            background: isDark ? "rgba(255,255,255,0.1)" : "var(--surface-elevated)",
            border: isDark ? "1px solid rgba(255,255,255,0.1)" : "1px solid var(--border)",
            color: isDark ? "#fff" : "var(--text-primary)",
            outline: "none",
          }}
        />
        {loading && (
          <Loader2
            size={14}
            className="absolute right-3 top-1/2 -translate-y-1/2 animate-spin"
            style={{ color: isDark ? "rgba(255,255,255,0.4)" : "var(--text-tertiary)" }}
          />
        )}
      </div>

      {open && results.length > 0 && (
        <div
          className="absolute z-50 w-full mt-1 rounded-lg overflow-hidden shadow-xl"
          style={{
            background: isDark ? "rgba(30,30,30,0.95)" : "var(--surface-elevated)",
            border: isDark ? "1px solid rgba(255,255,255,0.1)" : "1px solid var(--border)",
            backdropFilter: "blur(12px)",
          }}
        >
          {results.map((city, i) => (
            <button
              key={`${city.name}-${city.region}-${i}`}
              onClick={() => handleSelect(city)}
              className="w-full text-left px-3 py-2 text-[13px] transition-colors"
              style={{ color: isDark ? "#fff" : "var(--text-primary)" }}
              onMouseEnter={(e) => { e.currentTarget.style.background = isDark ? "rgba(255,255,255,0.1)" : "var(--surface-hover)" }}
              onMouseLeave={(e) => { e.currentTarget.style.background = "transparent" }}
            >
              <span className="font-medium">{city.name}</span>
              <span style={{ color: isDark ? "rgba(255,255,255,0.4)" : "var(--text-tertiary)", marginLeft: 6, fontSize: 12 }}>
                {[city.region, city.country].filter(Boolean).join(", ")}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
