#!/usr/bin/env python3
"""
Сборщик базы радаров для GPS-антирадара: весь мир, нарезанный на квадраты 2°x2°.

Источники (можно комбинировать; дубликаты ближе 30 м склеиваются, приоритет csv > osm > mapatlas):
  --csv файл.csv          свои камеры: lat,lon,limit,dir,type  (limit/dir/type необязательны)
  --osm-world             OpenStreetMap (Overpass API), весь мир кусками 30°x30°
  --osm-cache ПАПКА       куда сохранять куски OSM; если кусок не скачался — берётся прошлая копия
  --osm-country BY        OpenStreetMap по стране (ISO-3166-1)
  --osm-bbox S,W,N,E      OpenStreetMap по прямоугольнику
  --osm-json файл.json    готовый ответ Overpass (out:json), без скачивания
  --mapatlas-all          MapAtlas Camera Index (CC BY 4.0), все страны
  --mapatlas BY           MapAtlas по стране
  --geojson файл.geojson  любой GeoJSON с Point

Результат:
  --tiles ПАПКА           index.bin + t/<ty>_<tx>.bin — то, что лежит на сервере
  --pack LAT,LON,KM       плюс --out ПАПКА: набор квадратов вокруг точки для заливки в устройство
                          (pio run -t uploadfs). Можно брать квадраты из готовой папки: --from-tiles ПАПКА
  --prev-index файл       не записывать, если камер стало меньше на >10% (сбой источника)

Форматы (little-endian):
  квадрат:  "GRDB", version u32, count u32, crc32(записей) u32, затем записи по 12 байт,
            отсортированы по широте: lat i32 (°*1e6), lon i32, dir u16 (0xFFFF = любое), limit u8, type u8
  index:    "GRDI", version u32, tiles u32, cameras u32, затем по 8 байт: ty u8, tx u8, count u16, crc u32
  cov.bin:  "GCOV", lat f32, lon f32, radius_km f32, index_version u32 — какую зону покрывают квадраты

Только стандартная библиотека Python 3.8+.
Примеры:
  python tools/build_db.py --osm-world --mapatlas-all --tiles site
  python tools/build_db.py --from-tiles site --pack 53.7,28.0,450 --out data
"""
import argparse, csv, glob, json, math, os, shutil, struct, sys, time, urllib.error, urllib.parse, urllib.request, zlib

OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
UA = {"User-Agent": "gpsradar-db-builder/2.0 (+https://github.com/daniilprofe-dev/gpsradar-db)"}
COMPASS = {"N": 0, "NNE": 22, "NE": 45, "ENE": 67, "E": 90, "ESE": 112, "SE": 135, "SSE": 157,
           "S": 180, "SSW": 202, "SW": 225, "WSW": 247, "W": 270, "WNW": 292, "NW": 315, "NNW": 337}
DIR_ANY = 0xFFFF
TILE_DEG = 2

ISO2 = ("AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV "
        "BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES "
        "ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE "
        "IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY "
        "MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU "
        "NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM "
        "SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE "
        "VG VI VN VU WF WS XK YE YT ZA ZM ZW").split()


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ OSM
def overpass(query, timeout=400, rounds=6):
    """Запрос к Overpass: по кругу по всем зеркалам, пауза между кругами растёт (30 с ... 5 мин)."""
    data = urllib.parse.urlencode({"data": query}).encode()
    last = None
    for rnd in range(rounds):
        for url in OVERPASS:
            try:
                log(f"  Overpass: {url}")
                req = urllib.request.Request(url, data=data, headers=UA)
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    js = json.load(r)
                # при перегрузке Overpass отвечает 200 с пустым списком и "remark" — это ошибка, а не 0 камер
                if not js.get("elements") and js.get("remark"):
                    raise RuntimeError(js["remark"][:200])
                return js
            except Exception as e:
                last = e
                log(f"    ошибка: {e}")
        if rnd < rounds - 1:
            pause = min(30 * 2 ** rnd, 300)
            log(f"  все зеркала отказали, жду {pause} с (круг {rnd + 1}/{rounds})")
            time.sleep(pause)
    raise RuntimeError(f"Overpass недоступен: {last}")


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


def osm_query(area_filter, scope, timeout=360):
    return f"""[out:json][timeout:{timeout}];
{area_filter}
(
  node["highway"="speed_camera"]{scope};
  node["enforcement"~"^(maxspeed|average_speed|traffic_signals)$"]{scope};
);
out body;"""


def load_osm_country(code):
    return osm_to_records(overpass(osm_query(f'area["ISO3166-1"="{code}"][admin_level=2]->.a;', "(area.a)")))


def load_osm_bbox(bbox):
    return osm_to_records(overpass(osm_query("", f"({bbox})")))


def _cache_path(cache_dir, s, w):
    return os.path.join(cache_dir, f"{s}_{w}.csv")


def _cache_write(path, recs):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for lat, lon, lim, d, typ in sorted(recs):
            f.write(f"{lat:.7f},{lon:.7f},{lim},{d},{typ}\n")


def _cache_read(path):
    with open(path, encoding="utf-8") as f:
        return [(float(a), float(b), int(c), int(d), int(e)) for a, b, c, d, e in (l.split(",") for l in f if l.strip())]


def load_osm_world(cache_dir=None):
    """Весь мир кусками 30°x30° (каждый запрос лёгкий). Удачные куски сохраняются в cache_dir;
    если кусок не скачался даже после всех повторов — берётся его прошлая копия из кэша."""
    out, stale = [], []
    for s in range(-60, 90, 30):
        for w in range(-180, 180, 30):
            cp = _cache_path(cache_dir, s, w) if cache_dir else None
            try:
                r = osm_to_records(overpass(osm_query("", f"({s},{w},{s + 30},{w + 30})", timeout=300), timeout=330))
                if cp:
                    _cache_write(cp, r)
                log(f"  OSM [{s},{w}]: {len(r)}")
            except RuntimeError as e:
                if not (cp and os.path.exists(cp)):
                    raise
                r = _cache_read(cp)
                stale.append(f"{s},{w}")
                log(f"  OSM [{s},{w}]: не скачался ({e}), беру прошлую копию: {len(r)}")
            out += r
            time.sleep(1)
    if stale:
        log(f"OSM: из кэша взяты куски: {'; '.join(stale)}")
    return out


# ------------------------------------------------------------------ MapAtlas / GeoJSON / CSV
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


def load_mapatlas(code, missing_ok=False):
    req = urllib.request.Request(MAPATLAS.format(code.upper()), headers=UA)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return geojson_to_records(json.load(r))
        except urllib.error.HTTPError as e:
            if missing_ok and e.code == 404:
                return []
            if attempt == 3 or e.code < 500:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == 3:
                raise
            log(f"  MapAtlas {code}: {e}, повтор")
        time.sleep(5 * (attempt + 1))


def load_mapatlas_all():
    out, found = [], 0
    for code in ISO2:
        r = load_mapatlas(code, missing_ok=True)
        if r:
            found += 1
            out += r
        time.sleep(0.3)
    log(f"MapAtlas: {found} стран")
    return out


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


# ------------------------------------------------------------------ обработка
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


def tile_of(lat, lon):
    ty = min(int((lat + 90) // TILE_DEG), 180 // TILE_DEG - 1)
    tx = min(int((lon + 180) // TILE_DEG), 360 // TILE_DEG - 1)
    return max(ty, 0), max(tx, 0)


def tile_bytes(recs, version):
    recs = sorted(recs, key=lambda r: (round(r[0] * 1e6), round(r[1] * 1e6)))
    body = b"".join(struct.pack("<iiHBB", round(lat * 1e6), round(lon * 1e6), d, min(lim, 255), typ)
                    for lat, lon, lim, d, typ in recs)
    crc = zlib.crc32(body) & 0xFFFFFFFF
    return struct.pack("<4sIII", b"GRDB", version, len(recs), crc) + body, crc


def read_index(path):
    b = open(path, "rb").read()
    magic, ver, n, total = struct.unpack("<4sIII", b[:16])
    assert magic == b"GRDI", path
    return ver, total, [struct.unpack("<BBHI", b[16 + 8 * i:24 + 8 * i]) for i in range(n)]


def write_tiles(recs, outdir, prev_index=None):
    version = int(time.time())
    tiles = {}
    for r in dedupe(recs):
        tiles.setdefault(tile_of(r[0], r[1]), []).append(r)
    total = sum(len(v) for v in tiles.values())
    if prev_index and os.path.exists(prev_index):
        _, prev_total, _ = read_index(prev_index)
        log(f"Камер было {prev_total}, стало {total}")
        if total * 100 < prev_total * 90:
            raise SystemExit(f"Камер стало меньше на >10% ({prev_total} -> {total}) — похоже на сбой источника, не записываю")

    tdir = os.path.join(outdir, "t")
    os.makedirs(tdir, exist_ok=True)
    old = set(glob.glob(os.path.join(tdir, "*.bin")))
    entries = []
    for (ty, tx), rs in sorted(tiles.items()):
        assert len(rs) < 65536
        path = os.path.join(tdir, f"{ty}_{tx}.bin")
        data, crc = tile_bytes(rs, version)
        # не трогаем файл, если камеры в квадрате не изменились (меньше коммитов и скачиваний)
        if not (os.path.exists(path) and open(path, "rb").read()[16:] == data[16:]):
            open(path, "wb").write(data)
        old.discard(path)
        entries.append((ty, tx, len(rs), crc))
    for p in old:
        os.remove(p)
    with open(os.path.join(outdir, "index.bin"), "wb") as f:
        f.write(struct.pack("<4sIII", b"GRDI", version, len(entries), total))
        for e in entries:
            f.write(struct.pack("<BBHI", *e))
    log(f"Записано {total} камер в {len(entries)} квадратах, версия {version} -> {outdir}")


def tile_dist_km(lat, lon, ty, tx):
    """Расстояние от точки до ближайшего края квадрата, км (0, если точка внутри)."""
    s, w = ty * TILE_DEG - 90, tx * TILE_DEG - 180
    clat = min(max(lat, s), s + TILE_DEG)
    dlon = 0.0 if w <= lon <= w + TILE_DEG else min(abs(lon - w), abs(lon - w - TILE_DEG), abs(lon - w + 360), abs(lon - w - TILE_DEG - 360))
    dy = (lat - clat) * 111.32
    dx = dlon * 111.32 * math.cos(math.radians(clat))
    return math.hypot(dx, dy)


def write_pack(tiles_dir, center, outdir):
    lat, lon, km = (float(x) for x in center.split(","))
    ver, _, entries = read_index(os.path.join(tiles_dir, "index.bin"))
    tdir = os.path.join(outdir, "t")
    shutil.rmtree(tdir, ignore_errors=True)
    os.makedirs(tdir)
    n = cams = 0
    for ty, tx, cnt, crc in entries:
        if tile_dist_km(lat, lon, ty, tx) <= km:
            shutil.copy(os.path.join(tiles_dir, "t", f"{ty}_{tx}.bin"), tdir)
            n += 1
            cams += cnt
    with open(os.path.join(outdir, "cov.bin"), "wb") as f:
        f.write(struct.pack("<4sfffI", b"GCOV", lat, lon, km, ver))
    for legacy in ("radars.bin", "radars.ver"):
        if os.path.exists(os.path.join(outdir, legacy)):
            os.remove(os.path.join(outdir, legacy))
    log(f"Пакет для устройства: {n} квадратов, {cams} камер, радиус {km:.0f} км вокруг {lat},{lon} -> {outdir}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", action="append", default=[])
    ap.add_argument("--osm-world", action="store_true")
    ap.add_argument("--osm-cache", metavar="DIR", help="кэш кусков OSM: при сбое Overpass берётся прошлая копия")
    ap.add_argument("--osm-country", action="append", default=[], metavar="ISO")
    ap.add_argument("--osm-bbox", action="append", default=[], metavar="S,W,N,E")
    ap.add_argument("--osm-json", action="append", default=[], metavar="FILE")
    ap.add_argument("--mapatlas-all", action="store_true")
    ap.add_argument("--mapatlas", action="append", default=[], metavar="ISO")
    ap.add_argument("--geojson", action="append", default=[])
    ap.add_argument("--tiles", metavar="DIR", help="куда записать index.bin и t/*.bin")
    ap.add_argument("--prev-index", metavar="FILE")
    ap.add_argument("--from-tiles", metavar="DIR", help="готовые квадраты для --pack (без скачивания)")
    ap.add_argument("--pack", metavar="LAT,LON,KM")
    ap.add_argument("--out", default="data")
    a = ap.parse_args()

    sources = a.csv or a.osm_world or a.osm_country or a.osm_bbox or a.osm_json or a.mapatlas_all or a.mapatlas or a.geojson
    if sources:
        if not a.tiles:
            ap.error("укажи --tiles ПАПКА")
        recs, empty = [], []

        def add(name, r, must=True):
            log(f"{name}: {len(r)}")
            recs.extend(r)
            if must and not r:
                empty.append(name)

        for p in a.csv:  # приоритет: свои данные первыми
            add(f"CSV {p}", load_csv(p), must=False)
        if a.osm_world:
            add("OSM мир", load_osm_world(a.osm_cache))
        for c in a.osm_country:
            add(f"OSM {c}", load_osm_country(c))
        for b in a.osm_bbox:
            add(f"OSM bbox {b}", load_osm_bbox(b))
        for p in a.osm_json:
            with open(p, encoding="utf-8") as f:
                add(f"OSM {p}", osm_to_records(json.load(f)))
        for p in a.geojson:
            add(f"GeoJSON {p}", load_geojson(p))
        if a.mapatlas_all:
            add("MapAtlas мир", load_mapatlas_all())
        for c in a.mapatlas:
            add(f"MapAtlas {c}", load_mapatlas(c))
        log(f"Всего до склейки: {len(recs)}")
        if empty:
            raise SystemExit(f"Источник вернул 0 камер: {', '.join(empty)} — база не записана")
        write_tiles(recs, a.tiles, a.prev_index)

    if a.pack:
        src = a.from_tiles or a.tiles
        if not src:
            ap.error("для --pack нужен --from-tiles или --tiles")
        write_pack(src, a.pack, a.out)
    elif not sources:
        ap.error("укажи источник или --pack")


if __name__ == "__main__":
    main()
