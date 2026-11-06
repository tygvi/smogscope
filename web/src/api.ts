// Types mirror the Pydantic response models in aqi_india/api.py.
// If the API contract changes, this file must change with it.

export interface Weather {
  temperature_c: number | null
  humidity_pct: number | null
  wind_speed_kmh: number | null
  wind_dir_deg: number | null
}

export interface CityNow {
  location: string
  state: string | null
  lat: number
  lon: number
  observed_at: string
  aqi: number
  category: string
  dominant_pollutant: string
  sub_indices: Record<string, number>
  weather: Weather | null
}

export interface AqiPoint { t: string; aqi: number; category: string; dominant_pollutant: string }
export interface ValuePoint { t: string; value: number }

export interface CityHistory {
  location: string
  hours: number
  aqi: AqiPoint[]
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

async function getJSON<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, { signal })
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} for ${path}`)
  return res.json() as Promise<T>
}

const enc = encodeURIComponent

export const api = {
  cities: (s?: AbortSignal) => getJSON<CityNow[]>('/api/cities', s),
  overview: (s?: AbortSignal) => getJSON<Overview>('/api/overview', s),
  timeline: (s?: AbortSignal) => getJSON<Timeline>('/api/timeline?hours=72', s),
  wind: (s?: AbortSignal) => getJSON<WindGrid>('/api/wind', s),
  history: (city: string, s?: AbortSignal) => getJSON<CityHistory>(`/api/cities/${enc(city)}/history?hours=72`, s),
  daily: (city: string, s?: AbortSignal) => getJSON<DailyRow[]>(`/api/cities/${enc(city)}/daily?days=14`, s),
}
