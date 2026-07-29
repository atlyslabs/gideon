import { NextRequest, NextResponse } from "next/server"

const WEATHER_API_KEY = process.env.WEATHER_API_KEY

// When a search term doesn't return the expected city, also try these.
// e.g. "Delhi" -> WeatherAPI only has "New Delhi", not "Delhi".
const SEARCH_ALIASES: Record<string, string> = {
  "delhi": "New Delhi",
  "bangalore": "Bengaluru",
  "bombay": "Mumbai",
  "calcutta": "Kolkata",
  "madras": "Chennai",
  "trivandrum": "Thiruvananthapuram",
  "poona": "Pune",
  "benaras": "Varanasi",
  "cochin": "Kochi",
}

async function searchCities(query: string): Promise<any[]> {
  const res = await fetch(
    `https://api.weatherapi.com/v1/search.json?key=${WEATHER_API_KEY}&q=${encodeURIComponent(query)}`
  )
  if (!res.ok) return []
  return (await res.json()) || []
}

export async function GET(request: NextRequest) {
  const q = request.nextUrl.searchParams.get("q")
  if (!q || q.length < 2) {
    return NextResponse.json([])
  }

  if (!WEATHER_API_KEY) {
    return NextResponse.json({ error: "Missing WEATHER_API_KEY environment variable" }, { status: 500 })
  }

  try {
    const data = await searchCities(q)

    // If the query has an alias, search that too and merge results
    const alias = SEARCH_ALIASES[q.toLowerCase()]
    if (alias) {
      const aliasData = await searchCities(alias)
      const seen = new Set(data.map((d: any) => `${d.name}|${d.country}`))
      // Prepend alias results (they're likely more relevant)
      for (const item of aliasData) {
        const key = `${item.name}|${item.country}`
        if (!seen.has(key)) {
          data.unshift(item)
          seen.add(key)
        }
      }
    }

    const results = data.slice(0, 8).map((item: any) => ({
      name: item.name,
      region: item.region,
      country: item.country,
    }))

    return NextResponse.json(results)
  } catch {
    return NextResponse.json([], { status: 500 })
  }
}
