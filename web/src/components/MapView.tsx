import { useEffect, useRef, useState } from 'react'
import maplibregl, { type GeoJSONSource, type Map as MlMap } from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import type { CityNow, Timeline, WindGrid } from '../api'
import { CATEGORIES, categoryInfo, colorOf } from '../aqi'
import WindLayer from './WindLayer'

// Free vector basemap (CARTO Dark Matter, OpenStreetMap data), no API key needed.
// Note: OSM-based tiles draw international boundaries per OSM conventions, which
// differ from India's official map in disputed areas (see design-qa.md, Q30).
const STYLE_URL = 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json'

interface Props {
  cities: CityNow[]
  timeline: Timeline | null
  timeIndex: number | null           // null = live
  selected: string | null
  onSelect: (location: string) => void
  wind: WindGrid | null
  showWind: boolean
  showGlow: boolean
}

/** AQI to show for each location: live values, or the timeline's value at timeIndex. */
function displayedAqi(cities: CityNow[], timeline: Timeline | null, timeIndex: number | null) {
  const out = new Map<string, { lat: number; lon: number; aqi: number | null }>()
  if (timeIndex != null && timeline) {
    for (const c of timeline.cities) out.set(c.location, { lat: c.lat, lon: c.lon, aqi: c.aqi[timeIndex] ?? null })
  } else {
    for (const c of cities) out.set(c.location, { lat: c.lat, lon: c.lon, aqi: c.aqi })
  }
  return out
}

export default function MapView(props: Props) {
  const { cities, timeline, timeIndex, selected, onSelect, wind, showWind, showGlow } = props
  const containerRef = useRef<HTMLDivElement>(null)
  const [map, setMap] = useState<MlMap | null>(null)
  const markersRef = useRef(new Map<string, { marker: maplibregl.Marker; el: HTMLButtonElement }>())
  const onSelectRef = useRef(onSelect)
  onSelectRef.current = onSelect  // markers are created once; always call the latest handler

  // 1. Create the map once.
  useEffect(() => {
    const m = new maplibregl.Map({
      container: containerRef.current!,
      style: STYLE_URL,
      center: [80.5, 22.5],
      zoom: window.innerWidth < 700 ? 3.3 : 4.1,
      minZoom: 3,
      maxZoom: 9,
      maxBounds: [[50, -5], [112, 42]],
      attributionControl: { compact: true },
      dragRotate: false,
      pitchWithRotate: false,
    })
    m.touchZoomRotate.disableRotation()
    m.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'bottom-right')
    m.on('load', () => {
      // "Smog glow": a large, fully blurred circle per city, COLORED BY ITS AQI and
      // sized by it (worse air = bigger haze). Not a heatmap: a heatmap colors by
      // point DENSITY, so 15 isolated cities would all glow "low" (Q31).
      m.addSource('aqi', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } })
      m.addLayer({
        id: 'aqi-glow',
        type: 'circle',
        source: 'aqi',
        paint: {
          'circle-color': [
            'step', ['get', 'aqi'],
            CATEGORIES[0].color, 51, CATEGORIES[1].color, 101, CATEGORIES[2].color,
            201, CATEGORIES[3].color, 301, CATEGORIES[4].color, 401, CATEGORIES[5].color,
          ],
          // radius grows with AQI, and doubles per zoom level so the haze covers the same ground
          'circle-radius': [
            'interpolate', ['exponential', 2], ['zoom'],
            3, ['interpolate', ['linear'], ['get', 'aqi'], 0, 14, 500, 46],
            9, ['interpolate', ['linear'], ['get', 'aqi'], 0, 900, 500, 2900],
          ],
          'circle-blur': 1,
          'circle-opacity': 0.42,
        },
      })
      setMap(m)
    })
    return () => { m.remove(); setMap(null) }
  }, [])

  // 2. Markers + glow data follow the displayed AQI (live or timeline).
  useEffect(() => {
    if (!map) return
    const shown = displayedAqi(cities, timeline, timeIndex)
    const markers = markersRef.current

    for (const [location, { lat, lon, aqi }] of shown) {
      let entry = markers.get(location)
      if (!entry) {
        const el = document.createElement('button')
        el.className = 'marker'
        el.innerHTML = '<span class="marker-ring"></span><span class="marker-dot"></span><span class="marker-name"></span>'
        el.addEventListener('click', (e) => { e.stopPropagation(); onSelectRef.current(location) })
        const marker = new maplibregl.Marker({ element: el }).setLngLat([lon, lat]).addTo(map)
        entry = { marker, el }
        markers.set(location, entry)
      }
      const { el } = entry
      const info = aqi == null ? null : categoryInfo(aqi)
      el.style.setProperty('--c', colorOf(aqi))
      el.style.setProperty('--pulse', info?.pulse ? `${info.pulse}s` : '0s')
      el.classList.toggle('pulsing', !!info?.pulse)
      el.classList.toggle('selected', location === selected)
      el.querySelector('.marker-dot')!.textContent = aqi == null ? '–' : String(aqi)
      el.querySelector('.marker-name')!.textContent = location
      el.setAttribute('aria-label', `${location}: AQI ${aqi ?? 'no data'}${info ? `, ${info.name}` : ''}`)
    }
    for (const [location, { marker }] of markers) {
      if (!shown.has(location)) { marker.remove(); markers.delete(location) }
    }

    const source = map.getSource('aqi') as GeoJSONSource | undefined
    source?.setData({
      type: 'FeatureCollection',
      features: [...shown.entries()]
        .filter(([, d]) => d.aqi != null)
        .map(([location, d]) => ({
          type: 'Feature' as const,
          properties: { location, aqi: d.aqi },
          geometry: { type: 'Point' as const, coordinates: [d.lon, d.lat] },
        })),
    })
  }, [map, cities, timeline, timeIndex, selected])

  // 3. Glow on/off.
  useEffect(() => {
    if (map?.getLayer('aqi-glow')) map.setLayoutProperty('aqi-glow', 'visibility', showGlow ? 'visible' : 'none')
  }, [map, showGlow])

  // 4. Fly to the selected city.
  useEffect(() => {
    if (!map || !selected) return
    const c = cities.find((x) => x.location === selected)
    if (c) map.flyTo({ center: [c.lon, c.lat], zoom: Math.max(map.getZoom(), 5.6), speed: 0.9, essential: false })
  }, [map, selected])  // only on selection change, not on every data refresh

  return (
    <div className="map" ref={containerRef}>
      {map && wind && <WindLayer map={map} grid={wind} enabled={showWind} />}
    </div>
  )
}
