# gpsradar-db

База камер контроля скорости **всего мира** для GPS-антирадара на ESP32.
Мир нарезан на квадраты 2°×2°; устройство при подключении к Wi-Fi `gpsradar` скачивает
только квадраты в радиусе 300 км от себя.

Адрес для прошивки (`DB_BASE_URLS` в `config.h`):

```
https://raw.githubusercontent.com/daniilprofe-dev/gpsradar-db/main
```

## Файлы

- `index.bin` — список всех квадратов с камерами (номер квадрата, число камер, CRC32)
- `t/<ty>_<tx>.bin` — камеры квадрата; `ty = (lat + 90) / 2`, `tx = (lon + 180) / 2` (целая часть)

Устройство качает `index.bin`, сравнивает CRC с тем, что у него есть, и докачивает только изменившиеся квадраты.

## Обновление

- **Автоматически:** GitHub Actions по понедельникам и четвергам пересобирает базу и публикует только изменившиеся квадраты.
  Если серверы OpenStreetMap не отвечают, кусок мира берётся из прошлой копии (`cache/osm`).
  Сборка не публикуется, если источник ответил пусто или камер стало меньше больше чем на 10%.
- **Вручную:** вкладка **Actions** → *Update radar database* → **Run workflow**.
- **Свои камеры:** положи в корень `my_radars.csv` (`lat,lon,limit,dir,type`) — они попадут в базу с высшим приоритетом.

Локальная сборка: `python tools/build_db.py --osm-world --mapatlas-all --tiles .`

## Форматы (little-endian)

Квадрат: заголовок 16 байт — `"GRDB"`, version u32, count u32, crc32 записей u32; далее записи по 12 байт,
отсортированы по широте: lat i32 (°×1e6), lon i32 (°×1e6), dir u16 (0–359, 0xFFFF = любое),
limit u8 (км/ч, 0 = неизвестно), type u8 (0 стационарная, 1 мобильная, 2 светофор, 3 средняя скорость).

Индекс: `"GRDI"`, version u32, tiles u32, cameras u32; далее по 8 байт: ty u8, tx u8, count u16, crc32 u32.

## Источники и лицензии

- © участники **OpenStreetMap**, данные доступны по лицензии [ODbL](https://opendatacommons.org/licenses/odbl/) — https://www.openstreetmap.org/copyright
- **MapAtlas Camera Index** — [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), MapAtlas, https://mapatlas.eu

Производная база распространяется на условиях ODbL. Данные могут быть неполными или устаревшими; ответственность за скорость — на водителе.
