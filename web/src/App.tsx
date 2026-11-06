import { Suspense, lazy, useEffect, useState } from 'react'
import { AnimatePresence } from 'framer-motion'
import { api } from './api'
import { CATEGORIES } from './aqi'
import { usePolling, usePrefersReducedMotion } from './hooks'
import MapView from './components/MapView'
import Sidebar from './components/Sidebar'
import TimelineBar from './components/TimelineBar'

// The city panel pulls in the charting library (~100 KB gzipped). Loading it
// only when a city is first opened keeps the initial page lighter.
const CityPanel = lazy(() => import('./components/CityPanel'))

const FIVE_MIN = 5 * 60 * 1000

export default function App() {
  const reducedMotion = usePrefersReducedMotion()
  const cities = usePolling(api.cities, FIVE_MIN)
  const overview = usePolling(api.overview, FIVE_MIN)
  const timeline = usePolling(api.timeline, FIVE_MIN)
  const wind = usePolling(api.wind, 30 * 60 * 1000)

  const [selected, setSelected] = useState<string | null>(null)
  const [timeIndex, setTimeIndex] = useState<number | null>(null)
  const [playing, setPlaying] = useState(false)
  const [showWind, setShowWind] = useState(!reducedMotion)
  const [showGlow, setShowGlow] = useState(true)

  // Playback: advance one hour every 300 ms; stop (and go live) at the end.
  useEffect(() => {
    if (!playing || !timeline.data) return
    const last = timeline.data.times.length - 1
    const id = window.setInterval(() => {
      setTimeIndex((i) => {
        const next = (i ?? 0) + 1
        if (next >= last) { setPlaying(false); return null }
        return next
      })
    }, 300)
    return () => window.clearInterval(id)
  }, [playing, timeline.data])

  // Esc closes the panel.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setSelected(null) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const cityList = cities.data ?? []
  const selectedCity = cityList.find((c) => c.location === selected) ?? null

  return (
    <div className={`app ${selectedCity ? 'panel-open' : ''}`}>
      <MapView
        cities={cityList}
        timeline={timeline.data}
        timeIndex={timeIndex}
        selected={selected}
        onSelect={setSelected}
        wind={wind.data}
        showWind={showWind}
        showGlow={showGlow}
      />

      <Sidebar
        overview={overview.data}
        cities={cityList}
        selected={selected}
        onSelect={setSelected}
        showWind={showWind}
        showGlow={showGlow}
        onToggleWind={() => setShowWind((v) => !v)}
        onToggleGlow={() => setShowGlow((v) => !v)}
      />

      <Suspense fallback={null}>
        <AnimatePresence>
          {selectedCity && <CityPanel key={selectedCity.location} city={selectedCity} onClose={() => setSelected(null)} />}
        </AnimatePresence>
      </Suspense>

      {timeline.data && (
        <TimelineBar
          times={timeline.data.times}
          index={timeIndex}
          playing={playing}
          onChange={(i) => { setPlaying(false); setTimeIndex(i) }}
          onTogglePlay={() => {
            if (!playing && timeIndex == null) setTimeIndex(0)  // start from the beginning
            setPlaying((p) => !p)
          }}
        />
      )}

      <div className="legend glass" aria-label="AQI legend">
        {CATEGORIES.map((c) => (
          <span key={c.name}><i style={{ background: c.color }} />{c.name} <small>≤{c.max}</small></span>
        ))}
      </div>

      {cities.error && !cities.data && (
        <div className="banner" role="alert">
          Can't reach the SmogScope API. Is it running? <code>uvicorn aqi_india.api:app</code>
        </div>
      )}
    </div>
  )
}
