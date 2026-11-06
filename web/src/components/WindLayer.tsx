import { useEffect } from 'react'
import type { Map as MlMap } from 'maplibre-gl'
import type { WindGrid } from '../api'

/**
 * Animated wind particles drawn on a <canvas> laid over the map.
 *
 * How it works (design-qa.md, Phase 4):
 *  1. Particles live in GEOGRAPHIC coordinates (lon/lat), not pixels, so they stay
 *     glued to the map while it pans and zooms.
 *  2. Each frame, every particle asks the wind grid for (u, v) at its position,
 *     using BILINEAR INTERPOLATION between the 4 surrounding grid points.
 *  3. It moves: position += velocity x step  (Euler integration of the vector field).
 *  4. Trails: instead of clearing the canvas, each frame fades it slightly, so old
 *     segments linger and fade out.
 *  5. Particles die after a random lifetime (or when they leave the grid) and
 *     respawn at a random visible spot, keeping the field evenly covered.
 */

const SPEED_BUCKETS = [
  { max: 2, color: 'rgba(120, 160, 255, 0.45)' },
  { max: 4, color: 'rgba(130, 205, 255, 0.6)' },
  { max: 6, color: 'rgba(190, 240, 255, 0.75)' },
  { max: Infinity, color: 'rgba(255, 255, 255, 0.9)' },
]
const FADE = 0.92          // fraction of the previous frame kept each frame (trail length)
const BASE_STEP = 0.0075   // degrees moved per frame per m/s, at zoom 4
const MIN_AGE = 50, MAX_AGE = 140

/** Bilinear interpolation of (u, v) at lon/lat; null outside the grid. */
export function sampleWind(g: WindGrid, lon: number, lat: number): [number, number] | null {
  const x = (lon - g.lon0) / g.dlon
  const y = (lat - g.lat0) / g.dlat
  if (x < 0 || y < 0 || x > g.nx - 1 || y > g.ny - 1) return null
  const x0 = Math.floor(x), y0 = Math.floor(y)
  const x1 = Math.min(x0 + 1, g.nx - 1), y1 = Math.min(y0 + 1, g.ny - 1)
  const fx = x - x0, fy = y - y0
  const i00 = y0 * g.nx + x0, i10 = y0 * g.nx + x1, i01 = y1 * g.nx + x0, i11 = y1 * g.nx + x1
  const lerp2 = (a: number[]) =>
    (a[i00] * (1 - fx) + a[i10] * fx) * (1 - fy) + (a[i01] * (1 - fx) + a[i11] * fx) * fy
  return [lerp2(g.u), lerp2(g.v)]
}

interface Props {
  map: MlMap
  grid: WindGrid
  enabled: boolean
}

export default function WindLayer({ map, grid, enabled }: Props) {
  useEffect(() => {
    if (!enabled) return
    // The map's ROOT element: it has a real size. (The inner canvas container that
    // markers live in has no size of its own, so measuring it gives 0 x 0.)
    const container = map.getContainer()
    const canvas = document.createElement('canvas')
    canvas.className = 'wind-canvas'
    container.appendChild(canvas)
    const ctx = canvas.getContext('2d')!
    const dpr = Math.min(window.devicePixelRatio || 1, 2)

    const gridLonMax = grid.lon0 + grid.dlon * (grid.nx - 1)
    const gridLatMax = grid.lat0 + grid.dlat * (grid.ny - 1)

    let count = 0
    let lon = new Float32Array(0), lat = new Float32Array(0)
    let age = new Uint16Array(0), maxAge = new Uint16Array(0)

    const spawn = (i: number) => {
      // Respawn inside what's visible AND covered by the grid.
      const b = map.getBounds()
      const w = Math.max(b.getWest(), grid.lon0), e = Math.min(b.getEast(), gridLonMax)
      const s = Math.max(b.getSouth(), grid.lat0), n = Math.min(b.getNorth(), gridLatMax)
      lon[i] = w + Math.random() * Math.max(0, e - w)
      lat[i] = s + Math.random() * Math.max(0, n - s)
      age[i] = 0
      maxAge[i] = MIN_AGE + Math.random() * (MAX_AGE - MIN_AGE)
    }

    const resize = () => {
      const { clientWidth: w, clientHeight: h } = container
      canvas.width = w * dpr
      canvas.height = h * dpr
      canvas.style.width = `${w}px`
      canvas.style.height = `${h}px`
      // Particle count scales with screen area: ~3000 on a laptop, fewer on phones.
      count = Math.round(Math.min(3000, Math.max(600, (w * h) / 420)))
      lon = new Float32Array(count); lat = new Float32Array(count)
      age = new Uint16Array(count); maxAge = new Uint16Array(count)
      for (let i = 0; i < count; i++) { spawn(i); age[i] = Math.random() * maxAge[i] }
    }
    resize()
    const observer = new ResizeObserver(resize)
    observer.observe(container)

    // While the map moves, trails would smear across the screen: clear fully instead.
    let moving = false
    const onStart = () => { moving = true }
    const onEnd = () => { moving = false }
    map.on('movestart', onStart)
    map.on('moveend', onEnd)

    const paths = SPEED_BUCKETS.map(() => new Path2D())
    let raf = 0

    const frame = () => {
      // 1. Fade what's already drawn ('destination-in' keeps existing pixels, scaled by alpha).
      ctx.globalCompositeOperation = 'destination-in'
      ctx.fillStyle = `rgba(0, 0, 0, ${moving ? 0 : FADE})`
      ctx.fillRect(0, 0, canvas.width, canvas.height)
      ctx.globalCompositeOperation = 'source-over'

      // Same on-screen speed at every zoom: halve the step for every zoom level in.
      const step = BASE_STEP / Math.pow(2, map.getZoom() - 4)
      for (let b = 0; b < paths.length; b++) paths[b] = new Path2D()

      for (let i = 0; i < count; i++) {
        const wind = sampleWind(grid, lon[i], lat[i])
        if (!wind || age[i] > maxAge[i]) { spawn(i); continue }
        const [u, v] = wind
        const p0 = map.project([lon[i], lat[i]])
        // Longitude degrees shrink toward the poles: divide by cos(latitude).
        lon[i] += (u * step) / Math.cos((lat[i] * Math.PI) / 180)
        lat[i] += v * step
        const p1 = map.project([lon[i], lat[i]])
        age[i]++

        const speed = Math.hypot(u, v)
        const bucket = SPEED_BUCKETS.findIndex((s) => speed < s.max)
        paths[bucket].moveTo(p0.x * dpr, p0.y * dpr)
        paths[bucket].lineTo(p1.x * dpr, p1.y * dpr)
      }

      // 2. Draw all segments of a color in ONE stroke call (far cheaper than 3000 strokes).
      ctx.lineWidth = 1.15 * dpr
      ctx.lineCap = 'round'
      SPEED_BUCKETS.forEach((s, b) => { ctx.strokeStyle = s.color; ctx.stroke(paths[b]) })

      raf = requestAnimationFrame(frame)  // browsers pause this in background tabs
    }
    raf = requestAnimationFrame(frame)

    return () => {
      cancelAnimationFrame(raf)
      observer.disconnect()
      map.off('movestart', onStart)
      map.off('moveend', onEnd)
      canvas.remove()
    }
  }, [map, grid, enabled])

  return null
}
