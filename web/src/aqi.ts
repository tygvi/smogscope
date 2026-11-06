// AQI categories: the same bands as aqi_india/aqi.py (CPCB), with colors tuned
// for a dark map. CPCB's official palette runs green -> yellow -> orange -> red ->
// maroon; the hues are kept so the meaning stays familiar to Indian users.

export type Category = 'Good' | 'Satisfactory' | 'Moderate' | 'Poor' | 'Very Poor' | 'Severe'

export interface CategoryInfo {
  name: Category
  max: number
  color: string
  /** Marker pulse period in seconds; null = no pulse. Worse air pulses faster. */
  pulse: number | null
  advice: string
}

export const CATEGORIES: CategoryInfo[] = [
  { name: 'Good', max: 50, color: '#3fb950', pulse: null,
    advice: 'Minimal impact on health.' },
  { name: 'Satisfactory', max: 100, color: '#a3d65c', pulse: null,
    advice: 'Minor breathing discomfort for sensitive people.' },
  { name: 'Moderate', max: 200, color: '#f2d33d', pulse: 2.6,
    advice: 'Breathing discomfort for people with lung or heart disease, children and older adults.' },
  { name: 'Poor', max: 300, color: '#f59e2c', pulse: 1.9,
    advice: 'Breathing discomfort for most people on prolonged exposure.' },
  { name: 'Very Poor', max: 400, color: '#ef4444', pulse: 1.3,
    advice: 'Respiratory illness on prolonged exposure. Limit time outdoors.' },
  { name: 'Severe', max: 500, color: '#d6246e', pulse: 0.8,
    advice: 'Affects healthy people; serious risk for those with existing conditions. Stay indoors.' },
]

export function categoryInfo(aqi: number): CategoryInfo {
  return CATEGORIES.find((c) => aqi <= c.max) ?? CATEGORIES[CATEGORIES.length - 1]
}

export const colorOf = (aqi: number | null): string => (aqi == null ? '#5b6475' : categoryInfo(aqi).color)

export const POLLUTANT_LABELS: Record<string, string> = {
  'PM2.5': 'PM₂.₅', PM10: 'PM₁₀', NO2: 'NO₂', SO2: 'SO₂', CO: 'CO', O3: 'O₃', NH3: 'NH₃',
}
