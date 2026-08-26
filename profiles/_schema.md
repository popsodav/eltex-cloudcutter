# Профиль устройства — формат

Один JSON = одно устройство. Добавить новое устройство = положить сюда файл,
исходники править не нужно. Проверка: `python3 device.py <имя>`.

Загрузчик (`device.py`) требует минимум: `provisioning.port`, `provisioning.magic`,
`mqtt.plain_port`, `mqtt.cmd_topics`, `ota.payloads`. Остальное — по возможности.

Плейсхолдеры в строках: `{node_id}`, `{url}`, `{iface}`.

| поле | смысл |
|---|---|
| `name` | короткий id (= имя файла) |
| `display_name` | человекочитаемое имя |
| `status` | `verified` (проверено на железе) / `experimental` / `draft` |
| **ap** | |
| `ap.ssid_glob` | маска SSID точки устройства (`ELTEX-SW-PLG01-*`) — для `--scan` |
| `ap.security` | `wpa2` / `open` |
| `ap.password_source` | откуда пароль: `qr` / `serial` / `fixed` / `open` |
| `ap.device_ip` | адрес устройства в его точке доступа |
| **provisioning** | |
| `provisioning.port` | TCP-порт режима настройки |
| `provisioning.magic` | ожидаемая magic-строка в HELLO |
| `provisioning.framing` | описание кадрирования (справочно) |
| **mqtt** | |
| `mqtt.plain_port` | порт брокера, при котором устройство идёт открытым текстом (без TLS) |
| `mqtt.cmd_topics` | список топиков, куда слать команды (устройство на них подписывается) |
| `mqtt.event_topics` | исходящие топики устройства (справочно) |
| **ota** | |
| `ota.payloads` | список форматов команды OTA; первый — основной, остальные для `--try-variants` |
| **image** | |
| `image.esphome_board` | плата LibreTiny для сборки |
| `image.artifact_glob` | маска нужного файла образа |
| `image.slot_size` | размер OTA-слота (верхняя граница файла) |
| `image.min_size` | нижняя вменяемая граница |
| `image.reject` | `[{offset,hex,what}]` — сигнатуры заведомо негодных файлов (UF2, полный образ флеша) |
| **recovery** | |
| `recovery.wired_fallback` | запасной путь по проводам (UART) |

## Как добавить новое устройство

1. Скопируйте `eltex-sw-plg01.json` в `profiles/<новое>.json`.
2. Замените значения; неизвестные поля оставьте по образцу и поставьте `status: draft`.
3. Проверьте загрузку: `python3 device.py <новое>`.
4. Прогоните на железе; что подтвердилось — впишите и поднимите `status` до `verified`.

Если протокол другого устройства принципиально иной (не protobuf-провижининг,
не MQTT-команда OTA) — профиля мало, понадобится код. Но пока семейство одно
(Eltex/AmebaZ2), всё описывается данными.
