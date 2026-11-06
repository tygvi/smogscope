import { motion } from 'framer-motion'
import type { CityNow, Overview } from '../api'
import { CATEGORIES, POLLUTANT_LABELS, colorOf } from '../aqi'
import { formatTime } from '../hooks'
import AnimatedNumber from './AnimatedNumber'

interface Props {
  overview: Overview | null
  cities: CityNow[]
  selected: string | null
  onSelect: (location: string) => void
  showWind: boolean
  showGlow: boolean
  onToggleWind: () => void
  onToggleGlow: () => void
}

export default function Sidebar(p: Props) {
  const total = p.overview?.cities ?? 0
  return (
    <aside className="sidebar glass">
      <header className="brand">
        <h1>Smog<span>Scope</span></h1>
        <p className="muted small">Live air quality across India</p>
      </header>

      {p.overview && p.overview.average_aqi != null && (
        <section className="national">
          <div className="national-avg" style={{ color: colorOf(p.overview.average_aqi) }}>
            <AnimatedNumber value={Math.round(p.overview.average_aqi)} />
          </div>
          <div className="muted small">average AQI · {total} cities<br />
            {p.overview.observed_at && <>updated {formatTime(p.overview.observed_at)}</>}
          </div>
          {/* Distribution of cities across categories, as one stacked bar */}
          <div className="dist" role="img" aria-label="Cities per AQI category">
            {CATEGORIES.map((c) => {
              const n = p.overview!.counts_by_category[c.name] ?? 0
              return n ? (
                <motion.span key={c.name} title={`${c.name}: ${n}`} style={{ background: c.color }}
                  initial={{ flexGrow: 0 }} animate={{ flexGrow: n }} transition={{ duration: 0.8 }} />
              ) : null
            })}
          </div>
        </section>
      )}

      <ol className="city-list">
        {p.cities.map((c, i) => (
          <motion.li key={c.location} layout transition={{ type: 'spring', stiffness: 400, damping: 40 }}>
            <button
              className={`city-row ${p.selected === c.location ? 'active' : ''}`}
              onClick={() => p.onSelect(c.location)}
            >
              <span className="rank">{i + 1}</span>
              <span className="city-name">{c.location}<small>{POLLUTANT_LABELS[c.dominant_pollutant] ?? c.dominant_pollutant}</small></span>
              <span className="city-aqi" style={{ background: colorOf(c.aqi) }}>{c.aqi}</span>
            </button>
          </motion.li>
        ))}
      </ol>

      <div className="toggles">
        <label><input type="checkbox" checked={p.showWind} onChange={p.onToggleWind} /> Wind flow</label>
        <label><input type="checkbox" checked={p.showGlow} onChange={p.onToggleGlow} /> Smog glow</label>
      </div>

      <footer className="muted tiny">
        Modeled data (Open-Meteo / Copernicus CAMS); AQI computed with the CPCB method.
        Ozone estimates over India run high. CPCB station data is planned.
      </footer>
    </aside>
  )
}
