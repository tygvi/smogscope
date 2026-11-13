// Types mirror the Pydantic response models in aqi_india/api.py.
// If the API contract changes, this file must change with it.

export interface Weather {
  temperature_c: number | null
  humidity_pct: number | null
  wind_speed_kmh: number | null
  wind_dir_deg: number | null
}

/** "measured" = CPCB ground stations within 25 km; "estimated" = Open-Meteo model. */
export type Basis = 'measured' | 'estimated'

export interface CityNow {
  location: string
  state: string | null
  lat: number
  lon: number
  observed_at: string
  aqi: number
  category: string
  basis: Basis
  dominant_pollutant: string | null
  sub_indices: Record<string, number>
  stations: number | null
  min_aqi: number | null
  max_aqi: number | null
  measured_aqi: number | null
  model_aqi: number | null
  model_gap: number | null      // model - measured; positive = the model reads too high
  weather: Weather | null
}

export interface Station {
  id: number
  name: string
  city: string
  state: string
  lat: number
  lon: number
  observed_at: string | null
  aqi: number | null            // null = too few pollutants for a valid AQI
  category: string | null
  dominant_pollutant: string | null
  sub_indices: Record<string, number>
}

export interface AqiPoint { t: string; aqi: number; category: string; dominant_pollutant: string | null }
export interface MeasuredPoint { t: string; aqi: number; min_aqi: number; max_aqi: number; stations: number }
export interface ValuePoint { t: string; value: number }

export interface CityHistory {
  location: string
  hours: number
  aqi: AqiPoint[]
  measured: MeasuredPoint[]
  pollutants: Record<string, ValuePoint[]>
  units: Record<string, string>
}

export interface DailyRow {
  date: string
  aqi_mean: number | null
  aqi_min: number | null
  aqi_max: number | null
  aqi_hours: number | null
  worst_category: string | null
  dominant_pollutant: string | null
  pct_ok: number | null
}

export interface Timeline {
  times: string[]
  cities: { location: string; lat: number; lon: number; aqi: (number | null)[] }[]
}

export interface Overview {
  observed_at: string | null
  cities: number
  measured: number
  average_aqi: number | null
  counts_by_category: Record<string, number>
}

export interface WindGrid {
  lat0: number; lon0: number; dlat: number; dlon: number
  nx: number; ny: number
  u: number[]; v: number[]
  unit: string
  observed_at: string | null
}

const FETCH_TIMEOUT_MS = 15_000

async function getJSON<T>(path: string, signal?: AbortSignal): Promise<T> {
  // Don't let one hung request stall the polling loop forever.
  const controller = new AbortController()
  const abort = () => controller.abort()
  if (signal?.aborted) controller.abort()
  else signal?.addEventListener('abort', abort, { once: true })
  const timer = window.setTimeout(abort, FETCH_TIMEOUT_MS)
  try {
    const res = await fetch(path, { signal: controller.signal })
    if (!res.ok) throw new Error(`${res.status} ${res.statusText} for ${path}`)
    return res.json() as Promise<T>
  } finally {
    window.clearTimeout(timer)
    signal?.removeEventListener('abort', abort)
  }
}

const enc = encodeURIComponent

export const api = {
  cities: (s?: AbortSignal) => getJSON<CityNow[]>('/api/cities', s),
  stations: (s?: AbortSignal) => getJSON<Station[]>('/api/stations', s),
  overview: (s?: AbortSignal) => getJSON<Overview>('/api/overview', s),
  timeline: (s?: AbortSignal) => getJSON<Timeline>('/api/timeline?hours=72', s),
  wind: (s?: AbortSignal) => getJSON<WindGrid>('/api/wind', s),
  history: (city: string, s?: AbortSignal) => getJSON<CityHistory>(`/api/cities/${enc(city)}/history?hours=72`, s),
  daily: (city: string, s?: AbortSignal) => getJSON<DailyRow[]>(`/api/cities/${enc(city)}/daily?days=14`, s),
}
