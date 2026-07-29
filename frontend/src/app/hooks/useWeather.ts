"use client"

import useSWR from "swr"
import { pingman } from "../utils/pingman"

interface WeatherData {
  city: string
  temp: number
  condition: string
  icon: string
}

// WeatherAPI condition codes -> video condition
const CONDITION_MAP: Record<number, string> = {
  1000: "clear",
  1003: "clouds",
  1006: "clouds",
  1009: "clouds",
  1030: "mist",
  1063: "rain",
  1066: "snow",
  1069: "rain",
  1072: "rain",
  1087: "thunderstorm",
  1114: "snow",
  1117: "snow",
  1135: "mist",
  1147: "mist",
  1150: "rain",
  1153: "rain",
  1168: "rain",
  1171: "rain",
  1180: "rain",
  1183: "rain",
  1186: "rain",
  1189: "rain",
  1192: "rain",
  1195: "rain",
  1198: "rain",
  1201: "rain",
  1204: "rain",
  1207: "rain",
  1210: "snow",
  1213: "snow",
  1216: "snow",
  1219: "snow",
  1222: "snow",
  1225: "snow",
  1237: "snow",
  1240: "rain",
  1243: "rain",
  1246: "rain",
  1249: "rain",
  1252: "rain",
  1255: "snow",
  1258: "snow",
  1261: "snow",
  1264: "snow",
  1273: "thunderstorm",
  1276: "thunderstorm",
  1279: "thunderstorm",
  1282: "thunderstorm",
}

async function fetchWeather(): Promise<WeatherData | null> {
  try {
    const res = await pingman.get<{ weather: {
      city: string
      temp_c: number
      condition_code: number
      icon: string
    } | null }>("/api/gideon/v1/hub/weather")

    if (!res.ok || !res.data.weather) return null

    const w = res.data.weather
    return {
      city: w.city,
      temp: Math.round(w.temp_c),
      condition: CONDITION_MAP[w.condition_code] || "clear",
      icon: w.icon,
    }
  } catch {
    return null
  }
}

export default function useWeather() {
  const { data, error, isLoading } = useSWR(
    "weather",
    fetchWeather,
    {
      revalidateOnFocus: false,
      refreshInterval: 10 * 60 * 1000, // 10 min (matches backend poll)
      dedupingInterval: 5 * 60 * 1000,
    }
  )

  return {
    weather: data ?? null,
    loading: isLoading,
    error,
  }
}
