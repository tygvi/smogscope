import { describe, expect, it } from 'vitest'
import type { WindGrid } from '../api'
import { sampleWind } from './WindLayer'

// 2 x 2 grid: lon 70..72, lat 10..12. u differs west->east, v differs south->north.
const grid: WindGrid = {
  lat0: 10, lon0: 70, dlat: 2, dlon: 2, nx: 2, ny: 2,
  //   (row 0 = south)  SW   SE   (row 1 = north)  NW   NE
  u: [0, 4, 0, 4],
  v: [0, 0, 6, 6],
  unit: 'm/s', observed_at: null,
}

describe('sampleWind (bilinear interpolation)', () => {
  it('returns exact values at grid points', () => {
    expect(sampleWind(grid, 70, 10)).toEqual([0, 0])
    expect(sampleWind(grid, 72, 12)).toEqual([4, 6])
  })

  it('interpolates linearly between points', () => {
    const [u, v] = sampleWind(grid, 71, 11)!   // centre of the cell
    expect(u).toBeCloseTo(2)
    expect(v).toBeCloseTo(3)
    expect(sampleWind(grid, 71.5, 10)![0]).toBeCloseTo(3)  // 3/4 of the way east
  })

  it('returns null outside the grid', () => {
    expect(sampleWind(grid, 69.9, 11)).toBeNull()
    expect(sampleWind(grid, 71, 12.1)).toBeNull()
  })
})
