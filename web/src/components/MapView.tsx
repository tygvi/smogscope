import { useEffect, useRef, useState } from 'react'
import maplibregl, { type GeoJSONSource, type Map as MlMap } from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import type { Basis, CityNow, Station, Timeline, WindGrid } from '../api'
import { CATEGORIES, POLLUTANT_LABELS, categoryInfo, colorOf } from '../aqi'
import { formatTime } from '../hooks'
import WindLayer from './WindLayer'

const STALE_MS = 3 * 60 * 60 * 1000   // a station silent for 3 h is drawn faded

// Free vector basemap (CARTO Dark Matter, OpenStreetMap data), no API key needed.
const STYLE_URL = 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json'

// The basemap draws borders the OSM way (de facto lines in Jammu & Kashmir and
// Ladakh). These layers are hidden and India's OFFICIAL boundary (Survey of India)
// is drawn instead, from web/public/india-boundary.geojson (design-qa.md, Q37).
const BASEMAP_BORDER_LAYERS = ['boundary_country_outline', 'boundary_country_inner', 'boundary_state', 'boundary_county']
const BOUNDARY_URL = '/india-boundary.geojson'

type IndiaBoundary = GeoJSON.FeatureCollection<GeoJSON.MultiPolygon>

/** A world-sized polygon with India cut out as holes: used to dim everything outside India. */
function outsideMask(india: IndiaBoundary): GeoJSON.Feature<GeoJSON.Polygon> {
  const world = [[-180, -85], [180, -85], [180, 85], [-180, 85], [-180, -85]]
  const holes = india.features.flatMap((f) => f.geometry.coordinates.map((polygon) => polygon[0]))
  return { type: 'Feature', properties: {}, geometry: { type: 'Polygon', coordinates: [world, ...holes] } }
}

async function addOfficialBoundary(m: MlMap) {
  for (const id of BASEMAP_BORDER_LAYERS) {
    if (m.getLayer(id)) m.setLayoutProperty(id, 'visibility', 'none')
  }
  const india = (await (await fetch(BOUNDARY_URL)).json()) as IndiaBoundary
  // Insert below the basemap's labels, so place names stay crisp on top.
  const firstLabel = m.getStyle().layers.find((l) => l.type === 'symbol')?.id
  m.addSource('india', { type: 'geojson', data: india })
  m.addSource('outside-india', { type: 'geojson', data: outsideMask(india) })
  m.addLayer({
    id: 'outside-india', type: 'fill', source: 'outside-india',
    paint: { 'fill-color': '#04060b', 'fill-opacity': 0.5 },
  }, firstLabel)
  m.addLayer({
    id: 'india-border', type: 'line', source: 'india',
    paint: {
      'line-color': '#9fb3d1',
      'line-opacity': 0.7,
      'line-width': ['interpolate', ['linear'], ['zoom'], 3, 0.8, 6, 1.4, 9, 2.2],
    },
  }, firstLabel)
}

interface Props {
  cities: CityNow[]
  timeline: Timeline | null
  timeIndex: number | null           // null = live
  selected: string | null
  onSelect: (location: string) => void
  wind: WindGrid | null
  showWind: boolean
  showGlow: boolean
  stations: Station[]
  showStations: boolean
}

/** AQI to show for each location: live values, or the timeline's value at timeIndex.
 *  basis is only known live; during playback an hour may be either, so it's null. */
function displayedAqi(cities: CityNow[], timeline: Timeline | null, timeIndex: number | null) {
  const out = new Map<string, { lat: number; lon: number; aqi: number | null; basis: Basis | null }>()
  if (timeIndex != null && timeline) {
    for (const c of timeline.cities) {
      out.set(c.location, { lat: c.lat, lon: c.lon, aqi: c.aqi[timeIndex] ?? null, basis: null })
    }
  } else {
    for (const c of cities) out.set(c.location, { lat: c.lat, lon: c.lon, aqi: c.aqi, basis: c.basis })
  }
  return out
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, text = '', className = ''): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag)
  node.textContent = text        // textContent, never innerHTML: station names come from outside data
  if (className) node.className = className
  return node
}

/** The popup shown when a station dot is clicked. */
function stationCard(s: Station): HTMLElement {
  const root = el('div', '', 'station-card')
  root.append(el('strong', s.name, 'station-name'), el('div', `${s.city}, ${s.state}`, 'muted small'))

  const head = el('div', '', 'station-aqi')
  if (s.aqi != null) {
    const info = categoryInfo(s.aqi)
    const num = el('span', String(s.aqi), 'station-aqi-num')
    num.style.color = info.color
    head.append(num, el('span', info.name, 'muted small'))
  } else {
    head.append(el('span', 'No valid AQI: fewer than 3 pollutants reported', 'muted small'))
  }
  root.append(head)

  const list = el('ul', '', 'station-subs')
  for (const [p, v] of Object.entries(s.sub_indices).sort((a, b) => b[1] - a[1])) {
    const li = el('li')
    const value = el('span', String(v))
    value.style.color = colorOf(v)
    li.append(el('span', POLLUTANT_LABELS[p] ?? p, 'muted'), value)
    list.append(li)
  }
  root.append(list)
  if (s.observed_at) {
    const stale = Date.now() - new Date(s.observed_at).getTime() > STALE_MS
    root.append(el('div', `${stale ? 'Last report' : 'Updated'} ${formatTime(s.observed_at)} · CPCB sub-indices`, 'muted tiny'))
  }
  return root
}

export default function MapView(props: Props) {
  const { cities, timeline, timeIndex, selected, onSelect, wind, showWind, showGlow, stations, showStations } = props
  const containerRef = useRef<HTMLDivElement>(null)
  const [map, setMap] = useState<MlMap | null>(null)
  const markersRef = useRef(new Map<string, { marker: maplibregl.Marker; el: HTMLButtonElement }>())
  const onSelectRef = useRef(onSelect)
  onSelectRef.current = onSelect  // markers are created once; always call the latest handler
  // The click handler is registered once on load, so it looks stations up through a ref.
  const stationsRef = useRef(new Map<number, Station>())
  stationsRef.current = new Map(stations.map((s) => [s.id, s]))
  const popupRef = useRef<maplibregl.Popup | null>(null)

  // 1. Create the map once.
  useEffect(() => {
    const m = new maplibregl.Map({
      container: containerRef.current!,
      style: STYLE_URL,
      center: [80.5, 23.4],
      zoom: window.innerWidth < 700 ? 3.25 : 3.95,  // fits India's full official extent, 6.7°N to 37.1°N
      minZoom: 3,
      maxZoom: 9,
      maxBounds: [[50, -5], [112, 42]],
      attributionControl: {
        compact: true,
        customAttribution: 'India boundary: Survey of India via DataMeet (CC-BY-SA)',
      },
      dragRotate: false,
      pitchWithRotate: false,
    })
    m.touchZoomRotate.disableRotation()
    m.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'bottom-right')
    m.on('load', () => {
      addOfficialBoundary(m).catch((e) => console.error('Could not load India boundary', e))

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

      // Every CPCB station as a small dot in its AQI color: the real, street-level
      // picture behind each city's single number. A GPU circle layer, not HTML
      // markers: ~550 DOM elements moving on every pan would stutter.
      m.addSource('stations', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } })
      m.addLayer({
        id: 'stations',
        type: 'circle',
        source: 'stations',
        paint: {
          'circle-color': [
            'case', ['has', 'aqi'],
            ['step', ['get', 'aqi'],
              CATEGORIES[0].color, 51, CATEGORIES[1].color, 101, CATEGORIES[2].color,
              201, CATEGORIES[3].color, 301, CATEGORIES[4].color, 401, CATEGORIES[5].color],
            '#5b6475',                                     // no valid AQI: grey
          ],
          'circle-radius': ['interpolate', ['linear'], ['zoom'], 3, 2.2, 6, 4, 9, 7],
          'circle-stroke-color': '#0a0e16',
          'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 3, 0.4, 9, 1.2],
          'circle-opacity': ['case', ['get', 'stale'], 0.35, 0.9],
          'circle-stroke-opacity': ['case', ['get', 'stale'], 0.35, 0.9],
        },
      })
      m.on('click', 'stations', (e) => {
        const id = e.features?.[0]?.properties?.id as number | undefined
        const s = id == null ? undefined : stationsRef.current.get(id)
        if (!s) return
        popupRef.current?.remove()
        popupRef.current = new maplibregl.Popup({ maxWidth: '270px', className: 'station-popup', offset: 8 })
          .setLngLat([s.lon, s.lat])
          .setDOMContent(stationCard(s))
          .addTo(m)
      })
      m.on('mouseenter', 'stations', () => { m.getCanvas().style.cursor = 'pointer' })
      m.on('mouseleave', 'stations', () => { m.getCanvas().style.cursor = '' })
      setMap(m)
    })
    return () => { m.remove(); setMap(null) }
  }, [])

  // 2. Markers + glow data follow the displayed AQI (live or timeline).
  useEffect(() => {
    if (!map) return
    const shown = displayedAqi(cities, timeline, timeIndex)
    const markers = markersRef.current

    for (const [location, { lat, lon, aqi, basis }] of shown) {
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
      el.classList.toggle('estimated', basis === 'estimated')   // dashed ring = model estimate
      el.querySelector('.marker-dot')!.textContent = aqi == null ? '–' : String(aqi)
      el.querySelector('.marker-name')!.textContent = location
      el.setAttribute('aria-label',
        `${location}: AQI ${aqi ?? 'no data'}${info ? `, ${info.name}` : ''}${basis ? `, ${basis}` : ''}`)
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

  // 3b. Station dots follow the latest station data; on/off with the toggle.
  useEffect(() => {
    const source = map?.getSource('stations') as GeoJSONSource | undefined
    const now = Date.now()
    source?.setData({
      type: 'FeatureCollection',
      features: stations.map((s) => ({
        type: 'Feature' as const,
        // aqi is omitted (not null) when missing, so the style's ['has', 'aqi'] can test for it
        properties: {
          id: s.id,
          stale: !s.observed_at || now - new Date(s.observed_at).getTime() > STALE_MS,
          ...(s.aqi != null ? { aqi: s.aqi } : {}),
        },
        geometry: { type: 'Point' as const, coordinates: [s.lon, s.lat] },
      })),
    })
  }, [map, stations])

  useEffect(() => {
    if (map?.getLayer('stations')) map.setLayoutProperty('stations', 'visibility', showStations ? 'visible' : 'none')
    if (!showStations) popupRef.current?.remove()
  }, [map, showStations])

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
