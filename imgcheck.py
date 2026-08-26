"""Проверка образа перед заливкой по OTA.

Самая частая ошибка — отдать не тот файл: LibreTiny/ESPHome кладёт в build-каталог
несколько похожих .bin, и для OTA годится только образ FW-слота.

Проверки берутся ИЗ ПРОФИЛЯ устройства (profiles/*.json): сигнатуры заведомо
негодных файлов, размер слота, нижняя граница. Без профиля работают дефолты
для AmebaZ2 — чтобы модуль оставался пригоден и сам по себе.
"""

import os

# Дефолты (AmebaZ2) на случай вызова без профиля.
_DEFAULT_REJECT = [
    (0, bytes.fromhex("5546320a"), "UF2-контейнер (в т.ч. firmware.bin — он тоже UF2!)"),
    (0, bytes.fromhex("999996963fcc66fc"), "полный образ флеша (таблица разделов AmebaZ2)"),
]
_DEFAULT_SLOT = 1015808
_DEFAULT_MIN = 100_000


def identify(path, profile=None):
    """Возвращает (вид, пояснение, годится_ли_для_OTA)."""
    with open(path, "rb") as f:
        head = f.read(64)
    size = os.path.getsize(path)

    if len(head) < 8:
        return "пусто", "файл слишком мал", False

    rejects = profile.rejects() if profile else _DEFAULT_REJECT
    if not rejects:
        rejects = _DEFAULT_REJECT
    for off, sig, what in rejects:
        if head[off:off + len(sig)] == sig:
            return what, f"сигнатура {sig.hex()} на смещении {off} — по OTA такое не заливают", False

    slot = (profile.slot_size if profile else None) or _DEFAULT_SLOT
    minsz = (profile.min_size if profile else None) or _DEFAULT_MIN

    if size > slot:
        return ("не влезает в слот",
                f"{size} б > размера слота {slot} б", False)
    if size < minsz:
        return ("подозрительно маленький",
                f"{size} б — для прошивки ESPHome это слишком мало", False)

    return ("похоже на образ приложения",
            f"{size} б, запас в слоте {slot - size} б, стоп-сигнатур нет", True)


def report(path, log=print, profile=None):
    kind, why, ok = identify(path, profile)
    log(f"[{'+' if ok else '!'}] образ: {kind} — {why}")
    if not ok:
        glob = profile.get("image", "artifact_glob") if profile else "image_firmware_is.0x*.bin"
        note = profile.get("image", "artifact_note") if profile else None
        log(f"[!] Нужен образ FW-слота из .esphome/build/<имя>/.pioenvs/<имя>/ вида:")
        log(f"[!]   {glob}")
        if note:
            log(f"[!] {note}")
        log("[!] НЕ годятся: firmware.uf2 И firmware.bin (оба UF2-контейнеры),")
        log("[!]   а также image_flash_is.0x000000.bin (полный образ флеша).")
        if profile:
            board = profile.get("image", "esphome_board")
            if board:
                log(f"[!] Плата для сборки: {board}")
    return ok


if __name__ == "__main__":
    import sys
    import device
    prof = None
    args = [a for a in sys.argv[1:]]
    if "--profile" in args:
        i = args.index("--profile")
        prof = device.load(args[i + 1])
        del args[i:i + 2]
    for f in args:
        report(f, profile=prof)
