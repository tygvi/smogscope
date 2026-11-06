import { useEffect, useMemo, useState } from 'react'
import { motion } from 'framer-motion'
import {
  Area, AreaChart, Bar, BarChart, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { api, type CityHistory, type CityNow, type DailyRow } from '../api'
import { CATEGORIES, POLLUTANT_LABELS, categoryInfo, colorOf } from '../aqi'
import { formatTime } from '../hooks'
import AnimatedNumber from './AnimatedNumber'

interface Props {
  city: CityNow
  onClose: () => void
}

const hourFmt = new Intl.DateTimeFormat('en-IN', { hour: '2-digit', hour12: false, timeZone: 'Asia/Kolkata' })
const dayFmt = new Intl.DateTimeFormat('en-IN', { day: 'numeric', month: 'short', timeZone: 'Asia/Kolkata' })

/**
 * Vertical gradient for the AQI chart: each category's color sits at the height
 * of its AQI band, so the line literally turns red where the air turns Very Poor.
 */
function categoryStops(maxY: number) {
  const stops: { offset: string; color: string }[] = []
  let lower = 0
  for (const c of CATEGORIES) {
    if (lower > maxY) break
    const top = Math.min(c.max, maxY)
    stops.push({ offset: `${(1 - top / maxY) * 100}%`, color: c.color })
    stops.push({ offset: `${(1 - lower / maxY) * 100}%`, color: c.color })
    lower = c.max
  }
  return stops.reverse()
}

export default function CityPanel({ city, onClose }: Props) {
  const [history, setHistory] = useState<CityHistory | null>(null)
  const [daily, setDaily] = useState<DailyRow[] | null>(null)
  const info = categoryInfo(city.aqi)

  useEffect(() => {
    const ctrl = new AbortController()
    setHistory(null); setDaily(null)
    api.history(city.location, ctrl.signal).then(setHistory).catch(() => {})
    api.daily(city.location, ctrl.signal).then(setDaily).catch(() => {})
    return () => ctrl.abort()
  }, [city.location])

  const chart = useMemo(() => (history?.aqi ?? []).map((p) => ({ t: p.t, aqi: p.aqi })), [history])
  const maxY = Math.max(100, ...chart.map((p) => p.aqi)) * 1.1
  const subIndices = Object.entries(city.sub_indices).sort((a, b) => b[1] - a[1])
  const w = city.weather

  return (
    <motion.aside
      className="panel"
      style={{ '--c': info.color } as React.CSSProperties}
      role="dialog"
      aria-label={`${city.location} air quality details`}
      initial={{ x: 40, opacity: 0 }}
      animate={{ x: 0, opacity: 1 }}
      exit={{ x: 40, opacity: 0 }}
      transition={{ type: 'spring', stiffness: 260, damping: 30 }}
    >
      <header className="panel-head">
        <div>
          <h2>{city.location}</h2>
          <p className="muted">{city.state} · {formatTime(city.observed_at)}</p>
        </div>
        <button className="icon-btn" onClick={onClose} aria-label="Close panel">✕</button>
      </header>

      <section className="aqi-hero">
        <div className="aqi-big"><AnimatedNumber value={city.aqi} /></div>
        <div>
          <span className="chip">{info.name}</span>
          <p className="muted small">Dominant: <strong>{POLLUTANT_LABELS[city.dominant_pollutant] ?? city.dominant_pollutant}</strong></p>
        </div>
      </section>
      <p className="advice">{info.advice}</p>

      <section>
        <h3>Pollutant sub-indices</h3>
        <ul className="bars">
          {subIndices.map(([p, si], i) => (
            <li key={p}>
              <span className="bar-label">{POLLUTANT_LABELS[p] ?? p}</span>
              <span className="bar-track">
                <motion.span
                  className="bar-fill"
                  style={{ background: colorOf(si) }}
                  initial={{ width: 0 }}
                  animate={{ width: `${Math.min(100, (si / 500) * 100)}%` }}
                  transition={{ delay: 0.05 * i, duration: 0.7, ease: [0.16, 1, 0.3, 1] }}
                />
              </span>
              <span className="bar-value">{si}</span>
            </li>
          ))}
        </ul>
      </section>

      {w && (
        <section className="weather">
          <div><span className="muted small">Temp</span><strong>{w.temperature_c?.toFixed(0)}°C</strong></div>
          <div><span className="muted small">Humidity</span><strong>{w.humidity_pct?.toFixed(0)}%</strong></div>
          <div>
            <span className="muted small">Wind</span>
            <strong>
              {/* Meteorological direction = where wind comes FROM; +180° points where it goes. */}
              <span className="wind-arrow" style={{ transform: `rotate(${(w.wind_dir_deg ?? 0) + 180}deg)` }} aria-hidden>↑</span>
              {w.wind_speed_kmh?.toFixed(0)} km/h
            </strong>
          </div>
        </section>
      )}

      <section>
        <h3>Last 72 hours</h3>
        <div className="chart">
          {chart.length === 0 ? <div className="skeleton" /> : (
            <ResponsiveContainer width="100%" height={150}>
              <AreaChart data={chart} margin={{ top: 6, right: 4, bottom: 0, left: -18 }}>
                <defs>
                  <linearGradient id="aqiStroke" x1="0" y1="0" x2="0" y2="1">
                    {categoryStops(maxY).map((s, i) => <stop key={i} offset={s.offset} stopColor={s.color} />)}
                  </linearGradient>
                  <linearGradient id="aqiFill" x1="0" y1="0" x2="0" y2="1">
                    {categoryStops(maxY).map((s, i) => <stop key={i} offset={s.offset} stopColor={s.color} stopOpacity={0.22} />)}
                  </linearGradient>
                </defs>
                <XAxis dataKey="t" tickFormatter={(t: string) => hourFmt.format(new Date(t))}
                  minTickGap={28} stroke="#5b6475" fontSize={11} tickLine={false} axisLine={false} />
                <YAxis domain={[0, Math.round(maxY)]} stroke="#5b6475" fontSize={11} tickLine={false} axisLine={false} />
                <Tooltip
                  contentStyle={{ background: '#121826', border: '1px solid #253047', borderRadius: 8 }}
                  labelFormatter={(t) => formatTime(String(t))}
                  formatter={(v) => [String(v), 'AQI']}
                />
                <Area type="monotone" dataKey="aqi" stroke="url(#aqiStroke)" strokeWidth={2.2}
                  fill="url(#aqiFill)" isAnimationActive animationDuration={900} />
              </AreaChart>
            </ResponsiveContainer>
          )}
        </div>
      </section>

      <section>
        <h3>Daily (mean AQI)</h3>
        <div className="chart">
          {!daily ? <div className="skeleton short" /> : (
            <ResponsiveContainer width="100%" height={110}>
              <BarChart data={daily} margin={{ top: 6, right: 4, bottom: 0, left: -18 }}>
                <XAxis dataKey="date" tickFormatter={(d: string) => dayFmt.format(new Date(d))}
                  stroke="#5b6475" fontSize={11} tickLine={false} axisLine={false} />
                <YAxis stroke="#5b6475" fontSize={11} tickLine={false} axisLine={false} />
                <Tooltip
                  cursor={{ fill: 'rgba(255,255,255,0.04)' }}
                  contentStyle={{ background: '#121826', border: '1px solid #253047', borderRadius: 8 }}
                  formatter={(v, _n, item) => {
                    const row = (item as { payload?: DailyRow }).payload
                    return [`${v}  (${row?.aqi_hours ?? '?'} h of data)`, 'Mean AQI']
                  }}
                />
                <Bar dataKey="aqi_mean" radius={[4, 4, 0, 0]} animationDuration={700}>
                  {daily.map((d) => <Cell key={d.date} fill={colorOf(d.aqi_mean)} fillOpacity={(d.aqi_hours ?? 0) < 18 ? 0.45 : 1} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          )}
        </div>
        <p className="muted small">Faded bars: under 18 hours of data that day.</p>
      </section>
    </motion.aside>
  )
}
