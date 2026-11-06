"""Build web/public/india-boundary.geojson: India's official boundary, simplified for the web.

Source: DataMeet "india-soi.geojson", dissolved from Survey of India / Census 2011 maps
        https://github.com/datameet/maps/tree/master/Country  (license CC-BY-SA 2.5 / ODbL)
        It follows the official boundary of India, including Jammu & Kashmir and Ladakh
        in full (PoK, Gilgit-Baltistan, Aksai Chin), unlike OSM-based basemaps.

Usage:  python scripts/simplify_boundary.py india-soi.geojson web/public/india-boundary.geojson 0.01

Steps: Ramer-Douglas-Peucker simplification (tolerance in degrees; 0.01 is ~1 km),
coordinates rounded to 3 decimals (~100 m), slivers under ~2 x 2 km dropped, then
point-in-polygon checks on known locations. See design-qa.md, Q37.
"""
import json, sys

src, dst, tol = sys.argv[1], sys.argv[2], float(sys.argv[3])

def rdp(pts, eps):
    """Iterative Ramer-Douglas-Peucker on an open polyline."""
    keep = [False] * len(pts); keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (x1, y1), (x2, y2) = pts[a], pts[b]
        dx, dy = x2 - x1, y2 - y1
        norm = (dx * dx + dy * dy) ** 0.5 or 1e-12
        best, idx = -1.0, -1
        for i in range(a + 1, b):
            x, y = pts[i]
            d = abs(dy * x - dx * y + x2 * y1 - y2 * x1) / norm
            if d > best: best, idx = d, i
        if best > eps:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [p for p, k in zip(pts, keep) if k]

def simplify_ring(ring):
    # A closed ring starts and ends at the same point, so RDP's baseline would have
    # zero length. Split at the point farthest from the start; simplify each half.
    if len(ring) > 4:
        x0, y0 = ring[0]
        k = max(range(len(ring)), key=lambda i: (ring[i][0] - x0) ** 2 + (ring[i][1] - y0) ** 2)
        out = rdp(ring[: k + 1], tol)[:-1] + rdp(ring[k:], tol)
    else:
        out = ring
    out = [[round(x, 3), round(y, 3)] for x, y in out]
    return out if len(out) >= 4 else None

def ring_area(r):
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(r, r[1:]))) / 2

geom = json.load(open(src))["features"][0]["geometry"]
polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
out_polys = []
for poly in polys:
    rings = [simplify_ring(r) for r in poly]
    if rings[0] is None or ring_area(rings[0]) < 0.0004:   # drop specks smaller than ~2 km x 2 km
        continue
    out_polys.append([rings[0]] + [r for r in rings[1:] if r])

def inside(pt, polys):
    x, y = pt; hit = False
    for poly in polys:
        for ring in poly:
            for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
                if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                    hit = not hit
    return hit

checks = {  # (lon, lat): expected inside India's official boundary?
    "New Delhi": ((77.21, 28.61), True),
    "Srinagar": ((74.80, 34.08), True),
    "Muzaffarabad (PoK)": ((73.47, 34.37), True),
    "Gilgit (Gilgit-Baltistan)": ((74.31, 35.92), True),
    "Aksai Chin": ((79.50, 35.20), True),
    "Port Blair (Andaman)": ((92.73, 11.62), True),
    "Lahore (Pakistan)": ((74.34, 31.55), False),
    "Islamabad (Pakistan)": ((73.05, 33.68), False),
    "Kathmandu (Nepal)": ((85.32, 27.71), False),
    "Dhaka (Bangladesh)": ((90.41, 23.81), False),
}
ok = True
for name, (pt, expected) in checks.items():
    got = inside(pt, out_polys)
    ok &= got == expected
    print(f"{'✓' if got == expected else '✗'} {name}: inside={got}")

n_in = sum(len(r) for p in polys for r in p); n_out = sum(len(r) for p in out_polys for r in p)
fc = {"type": "FeatureCollection", "features": [{
    "type": "Feature",
    "properties": {"name": "India", "source": "Survey of India State Map via DataMeet (CC-BY-SA 2.5 / ODbL), simplified"},
    "geometry": {"type": "MultiPolygon", "coordinates": out_polys},
}]}
json.dump(fc, open(dst, "w"), separators=(",", ":"))
print(f"points {n_in} -> {n_out}, polygons {len(polys)} -> {len(out_polys)}, all checks passed: {ok}")
