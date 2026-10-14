# gpsradar-db

База камер контроля скорости (Беларусь) для GPS-антирадара на ESP32.
Устройство при подключении к Wi-Fi `gpsradar` скачивает отсюда:

- `radars.ver` — номер версии (unix-время сборки)
- `radars.bin` — база, если версия новее той, что на устройстве

Адрес для прошивки (`DB_BASE_URLS` в `config.h`):

```
https://raw.githubusercontent.com/daniilprofe-dev/gpsradar-db/main
```

## Обновление

- **Автоматически:** GitHub Actions каждый понедельник пересобирает базу и публикует её, только если камеры изменились.
- **Вручную:** вкладка **Actions** → *Update radar database* → **Run workflow**.
- **Свои камеры:** положи в корень `my_radars.csv` (`lat,lon,limit,dir,type`) — они попадут в базу с высшим приоритетом.

Локальная сборка: `python tools/build_db.py --osm-country BY --mapatlas BY --out .`

## Формат radars.bin

Little-endian. Заголовок 16 байт: `"GRDB"`, version u32, count u32, crc32 записей u32.
Далее записи по 12 байт, отсортированы по широте: lat i32 (°×1e6), lon i32 (°×1e6),
dir u16 (0–359, 0xFFFF = любое), limit u8 (км/ч, 0 = неизвестно), type u8
(0 стационарная, 1 мобильная, 2 светофор, 3 средняя скорость).

## Источники и лицензии

- © участники **OpenStreetMap**, данные доступны по лицензии [ODbL](https://opendatacommons.org/licenses/odbl/) — https://www.openstreetmap.org/copyright
- **MapAtlas Camera Index** — [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), MapAtlas, https://mapatlas.eu

Производная база распространяется на условиях ODbL. Данные могут быть неполными или устаревшими; ответственность за скорость — на водителе.
