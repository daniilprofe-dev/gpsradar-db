#!/usr/bin/env python3
"""
Сборщик базы радаров для GPS-антирадара.

Источники:
  --osm-country RU        скачать из OpenStreetMap (Overpass API) по коду страны ISO-3166-1
  --osm-bbox S,W,N,E      то же, но по прямоугольнику (для быстрых тестов)
  --mapatlas BY           MapAtlas Camera Index (CC BY 4.0, данные от водителей — независимо от OSM)
  --geojson файл.geojson  любой GeoJSON с Point (limitKph/maxspeed, facingDeg/direction — если есть)
  --csv файл.csv          свои камеры: lat,lon,limit,dir,type  (limit/dir/type необязательны)
Можно комбинировать; дубликаты (камеры ближе 30 м) склеиваются. Приоритет полей: csv > osm > mapatlas. Результат (--out папка):
  radars.bin   база (формат описан в src/radar_db.h)
  radars.ver   номер версии (unix-время) — прошивка качает radars.bin, только если он вырос

Только стандартная библиотека Python 3.8+.
Примеры:
  python tools/build_db.py --osm-country RU --csv tools/my_radars.csv --out dist
  python tools/build_db.py --csv tools/radars_sample.csv --out data       # для uploadfs
"""
import argparse, csv, json, math, os, struct, sys, time, urllib.parse, urllib.request, zlib

OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
COMPASS = {"N": 0, "NNE": 22, "NE": 45, "ENE": 67, "E": 90, "ESE": 112, "SE": 135, "SSE": 157,
           "S": 180, "SSW": 202, "SW": 225, "WSW": 247, "W": 270, "WNW": 292, "NW": 315, "NNW": 337}
DIR_ANY = 0xFFFF


def overpass(query):
    data = urllib.parse.urlencode({"data": query}).encode()
    last = None
    for url in OVERPASS:
        try:
            print(f"Overpass: {url}", file=sys.stderr)
            req = urllib.request.Request(url, data=data, headers={"User-Agent": "gpsradar-db-builder/1.0"})
            with urllib.request.urlopen(req, timeout=400) as r:
                js = json.load(r)
            # Overpass при перегрузке отвечает 200 с пустым списком и "remark" — это ошибка, а не 0 камер
            if not js.get("elements") and js.get("remark"):
                raise RuntimeError(js["remark"][:200])
            return js
        except Exception as e:  # пробуем следующее зеркало
            last = e
            print(f"  ошибка: {e}", file=sys.stderr)
    raise SystemExit(f"Overpass недоступен: {last}")


def parse_speed(v):
    if not v:
        return 0
    v = v.strip().lower()
    try:
        if v.endswith("mph"):
            return min(255, round(float(v[:-3]) * 1.609))
        return min(255, int(float(v.split()[0].replace("km/h", ""))))
    except ValueError:
        return 0


def parse_dir(tags):
    for key in ("direction", "camera:direction"):
        v = tags.get(key)
        if not v:
            continue
        v = v.strip().upper()
        if v in COMPASS:
            return COMPASS[v]
        try:
            return int(float(v)) % 360
        except ValueError:
            pass  # forward/backward и т.п. — без привязки к дороге не понять
    return DIR_ANY


def osm_to_records(js):
    out = []
    for el in js.get("elements", []):
        if el.get("type") != "node":
            continue
        t = el.get("tags", {})
        enf = t.get("enforcement", "")
        typ = 3 if enf == "average_speed" else 2 if enf == "traffic_signals" else 0
        out.append((el["lat"], el["lon"], parse_speed(t.get("maxspeed")), parse_dir(t), typ))
    return out


def osm_query(area_filter):
    return f"""[out:json][timeout:360];
{area_filter}
(
  node["highway"="speed_camera"]{{AREA}};
  node["enforcement"~"^(maxspeed|average_speed|traffic_signals)$"]{{AREA}};
);
out body;"""


def load_osm_country(code):
    q = osm_query(f'area["ISO3166-1"="{code}"][admin_level=2]->.a;').replace("{AREA}", "(area.a)")
    return osm_to_records(overpass(q))


def load_osm_bbox(bbox):
    q = osm_query("").replace("{AREA}", f"({bbox})")
    return osm_to_records(overpass(q))


MAPATLAS = "https://mapatlas.eu/data/camera-index/{}.geojson"


def geojson_to_records(js):
    out = []
    for ft in js.get("features", []):
        g, p = ft.get("geometry") or {}, ft.get("properties") or {}
        if g.get("type") != "Point":
            continue
        lon, lat = g["coordinates"][:2]
        lim = parse_speed(str(p.get("limitKph") or p.get("maxspeed") or ""))
        d = p.get("facingDeg", p.get("direction"))
        try:
            d = int(float(d)) % 360
        except (TypeError, ValueError):
            d = DIR_ANY
        typ = 3 if p.get("class") == "corridor" else 0
        out.append((lat, lon, lim, d, typ))
    return out


def load_mapatlas(code):
    url = MAPATLAS.format(code.upper())
    print(f"MapAtlas: {url}", file=sys.stderr)
    req = urllib.request.Request(url, headers={"User-Agent": "gpsradar-db-builder/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return geojson_to_records(json.load(r))


def load_geojson(path):
    with open(path, encoding="utf-8") as f:
        return geojson_to_records(json.load(f))


def load_csv(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for row in csv.reader(l for l in f if l.strip() and not l.lstrip().startswith("#")):
            if row[0].strip().lower() == "lat":
                continue
            lat, lon = float(row[0]), float(row[1])
            lim = int(row[2]) if len(row) > 2 and row[2].strip() else 0
            d = int(row[3]) % 360 if len(row) > 3 and row[3].strip() not in ("", "-1") else DIR_ANY
            typ = int(row[4]) if len(row) > 4 and row[4].strip() else 0
            out.append((lat, lon, lim, d, typ))
    return out


def dedupe(recs, radius_m=30.0):
    """Склеивает камеры ближе radius_m. Побеждает первая запись, пустые поля
    (лимит, направление) добираются из дублей. Порядок recs = приоритет источников."""
    cell = 0.0004  # ~44 м по широте
    grid, out = {}, []
    for lat, lon, lim, d, typ in recs:
        cx, cy = int(lat / cell), int(lon / cell)
        hit = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in grid.get((cx + dx, cy + dy), ()):
                    a = out[i]
                    ky = (lat - a[0]) * 111320
                    kx = (lon - a[1]) * 111320 * math.cos(math.radians(lat))
                    if kx * kx + ky * ky <= radius_m * radius_m:
                        hit = i
        if hit is None:
            grid.setdefault((cx, cy), []).append(len(out))
            out.append([lat, lon, lim, d, typ])
        else:
            a = out[hit]
            if not a[2] and lim:
                a[2] = lim
            if a[3] == DIR_ANY and d != DIR_ANY:
                a[3] = d
    return [tuple(r) for r in out]


def write_db(recs, outdir):
    recs = sorted(dedupe(recs), key=lambda r: (round(r[0] * 1e6), round(r[1] * 1e6)))
    body = b"".join(
        struct.pack("<iiHBB", round(lat * 1e6), round(lon * 1e6), d, min(lim, 255), typ)
        for lat, lon, lim, d, typ in recs)
    version = int(time.time())
    header = struct.pack("<4sIII", b"GRDB", version, len(recs), zlib.crc32(body) & 0xFFFFFFFF)
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "radars.bin"), "wb") as f:
        f.write(header + body)
    with open(os.path.join(outdir, "radars.ver"), "w") as f:
        f.write(str(version))
    print(f"Записано {len(recs)} камер, {len(header) + len(body)} байт, версия {version} -> {outdir}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--osm-country", action="append", default=[], metavar="ISO", help="RU, BY, KZ, UA ...")
    ap.add_argument("--osm-bbox", action="append", default=[], metavar="S,W,N,E")
    ap.add_argument("--mapatlas", action="append", default=[], metavar="ISO")
    ap.add_argument("--geojson", action="append", default=[])
    ap.add_argument("--csv", action="append", default=[])
    ap.add_argument("--out", default="dist")
    a = ap.parse_args()
    if not (a.osm_country or a.osm_bbox or a.csv or a.mapatlas or a.geojson):
        ap.error("укажи хотя бы один источник")

    recs, empty = [], []
    for p in a.csv:  # приоритет: свои данные первыми
        r = load_csv(p)
        print(f"CSV {p}: {len(r)}", file=sys.stderr)
        recs += r
    for c in a.osm_country:
        r = load_osm_country(c)
        print(f"OSM {c}: {len(r)}", file=sys.stderr)
        recs += r
        if not r:
            empty.append(f"OSM {c}")
    for b in a.osm_bbox:
        r = load_osm_bbox(b)
        print(f"OSM bbox {b}: {len(r)}", file=sys.stderr)
        recs += r
        if not r:
            empty.append(f"OSM bbox {b}")
    for p in a.geojson:
        r = load_geojson(p)
        print(f"GeoJSON {p}: {len(r)}", file=sys.stderr)
        recs += r
        if not r:
            empty.append(f"GeoJSON {p}")
    for c in a.mapatlas:
        r = load_mapatlas(c)
        print(f"MapAtlas {c}: {len(r)}", file=sys.stderr)
        recs += r
        if not r:
            empty.append(f"MapAtlas {c}")
    print(f"Всего до склейки: {len(recs)}", file=sys.stderr)
    if empty:
        raise SystemExit(f"Источник вернул 0 камер: {', '.join(empty)} — база не записана")
    write_db(recs, a.out)


if __name__ == "__main__":
    main()
