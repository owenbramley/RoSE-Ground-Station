#!/usr/bin/env python3
"""Download a small offline USGS topo tile cache for field maps."""

from __future__ import annotations

import argparse
import json
import math
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


TILE_URL = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}"
OUT_DIR = Path("static/map_tiles/usgs_topo")

AREAS = {
    # User requested "Hansville Utah"; Hanksville is the URC-relevant Utah field area.
    "hanksville_ut": {
        "label": "Hanksville, Utah",
        "bounds": {
            "south": 38.18,
            "west": -110.98,
            "north": 38.58,
            "east": -110.45,
        },
    },
    "downtown_oahu": {
        "label": "Downtown Oahu / Honolulu",
        "bounds": {
            "south": 21.25,
            "west": -157.93,
            "north": 21.36,
            "east": -157.78,
        },
    },
}


def lon_to_x(lon: float, zoom: int) -> int:
    return int((lon + 180.0) / 360.0 * (1 << zoom))


def lat_to_y(lat: float, zoom: int) -> int:
    lat_rad = math.radians(lat)
    return int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * (1 << zoom))


def tiles_for_bounds(bounds: dict[str, float], min_zoom: int, max_zoom: int) -> list[tuple[int, int, int]]:
    tiles: list[tuple[int, int, int]] = []
    for z in range(min_zoom, max_zoom + 1):
        x_min = lon_to_x(bounds["west"], z)
        x_max = lon_to_x(bounds["east"], z)
        y_min = lat_to_y(bounds["north"], z)
        y_max = lat_to_y(bounds["south"], z)
        for x in range(min(x_min, x_max), max(x_min, x_max) + 1):
            for y in range(min(y_min, y_max), max(y_min, y_max) + 1):
                tiles.append((z, x, y))
    return tiles


def download_tile(tile: tuple[int, int, int], retries: int = 2) -> tuple[tuple[int, int, int], str]:
    z, x, y = tile
    path = OUT_DIR / str(z) / str(x) / f"{y}.jpg"
    if path.exists() and path.stat().st_size > 0:
        return tile, "cached"

    url = TILE_URL.format(z=z, x=x, y=y)
    path.parent.mkdir(parents=True, exist_ok=True)
    last_error = ""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "RoSE-Ground-Station/1.0"})
            with urllib.request.urlopen(req, timeout=20) as res:
                data = res.read()
            if not data:
                raise RuntimeError("empty tile response")
            path.write_bytes(data)
            return tile, "downloaded"
        except (OSError, urllib.error.URLError, RuntimeError) as exc:
            last_error = str(exc)
            if attempt < retries:
                time.sleep(0.3 * (attempt + 1))
    return tile, f"failed: {last_error}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-zoom", type=int, default=7)
    parser.add_argument("--max-zoom", type=int, default=15)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    all_tiles: dict[tuple[int, int, int], set[str]] = {}
    for area_key, area in AREAS.items():
        for tile in tiles_for_bounds(area["bounds"], args.min_zoom, args.max_zoom):
            all_tiles.setdefault(tile, set()).add(area_key)

    tiles = sorted(all_tiles)
    print(f"{len(tiles)} unique tiles for {', '.join(a['label'] for a in AREAS.values())}")
    if args.dry_run:
        return 0

    counts = {"cached": 0, "downloaded": 0, "failed": 0}
    failures: list[tuple[tuple[int, int, int], str]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(download_tile, tile) for tile in tiles]
        for i, future in enumerate(as_completed(futures), 1):
            tile, status = future.result()
            if status.startswith("failed"):
                counts["failed"] += 1
                failures.append((tile, status))
            else:
                counts[status] += 1
            if i % 100 == 0 or i == len(tiles):
                print(f"{i}/{len(tiles)} tiles processed ({counts})")

    manifest = {
        "source": TILE_URL,
        "min_zoom": args.min_zoom,
        "max_zoom": args.max_zoom,
        "areas": AREAS,
        "tile_count": len(tiles),
        "counts": counts,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    if failures:
        print("Failures:")
        for tile, status in failures[:20]:
            print(f"  {tile}: {status}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
