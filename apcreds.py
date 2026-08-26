"""Пароль точки доступа устройства: подобрать или прочитать.

Порядок по умолчанию (настраивается): сначала перебор РЕГИСТРА букв серийного
номера, потом QR-код. Обоснование и оговорки:

- На ELTEX-SW-PLG01 пароль = серийник в смешанном регистре (`Fc00000000`).
  Если серийник известен и букв в нём немного — перебор их регистра дёшев.
- НО: число вариантов = 2^(число букв). Для `FC00000000` это 2 буквы -> 4 варианта,
  а для серийника с 5+ буквами это десятки попыток; каждая проверка идёт через
  Wi-Fi-хендшейк (медленно), и точка может временно блокировать после неудач.
- QR однозначен и быстр, поэтому он — надёжный фолбэк (и может стоять первым,
  см. параметр order).

Модуль только СОБИРАЕТ кандидатов; реальное подключение — в join-слое
(nmcli/netsh), который пробует их по очереди.
"""

import itertools
import os


def _letters(s):
    return [c for c in s if c.isalpha()]


def serial_case_candidates(serial, max_variants=64, wpa_min=8):
    """Все варианты регистра букв серийника (цифры не трогаем).

    Возвращает список, отсортированный по правдоподобию:
      1) как передано,
      2) как на наклейке часто печатают (Первая-Заглавная-остальные-строчные),
      3) остальные комбинации.
    Слишком короткие для WPA (<8) и слишком «широкие» переборы отсекаются.
    """
    serial = serial.strip()
    if len(serial) < wpa_min:
        return []
    letters = _letters(serial)
    if not letters:
        return [serial]
    if 2 ** len(letters) > max_variants:
        # слишком много букв — не устраиваем брутфорс, отдаём только 3 разумных формы
        forms = [serial, _title_serial(serial), serial.upper(), serial.lower()]
        seen, out = set(), []
        for f in forms:
            if f not in seen:
                seen.add(f); out.append(f)
        return out

    idxs = [i for i, c in enumerate(serial) if c.isalpha()]
    variants = []
    for combo in itertools.product(*[("lower", "upper")] * len(idxs)):
        chars = list(serial)
        for pos, mode in zip(idxs, combo):
            chars[pos] = chars[pos].lower() if mode == "lower" else chars[pos].upper()
        variants.append("".join(chars))

    # приоритет: исходное, «титульное», всё-заглавное, всё-строчное, затем прочее
    priority = [serial, _title_serial(serial), serial.upper(), serial.lower()]
    ordered, seen = [], set()
    for f in priority + variants:
        if f not in seen and len(f) >= wpa_min:
            seen.add(f); ordered.append(f)
    return ordered


def _title_serial(serial):
    out, first = [], True
    for c in serial:
        if c.isalpha():
            out.append(c.upper() if first else c.lower())
            first = False
        else:
            out.append(c)
    return "".join(out)


def qr_candidates(image_or_text, log=print):
    """Пароль из QR (фото или уже отсканированная строка)."""
    import qrtool
    if image_or_text and os.path.isfile(image_or_text):
        _, pw, raw = qrtool.creds_from_file(image_or_text, log)
        if pw:
            log(f"[+] QR распознан: {raw}")
            return [pw]
        return []
    info = qrtool.parse_wifi(image_or_text or "")
    return [info["password"]] if info.get("password") else []


def resolve(serial=None, qr=None, explicit=None, order=("serial", "qr"), log=print):
    """Собрать упорядоченный список паролей-кандидатов без дублей.

    explicit (--ap-password) всегда идёт первым.
    order — последовательность источников: "serial" (перебор регистра) и "qr".
    """
    out, seen = [], set()

    def add(pw):
        if pw and pw not in seen:
            seen.add(pw); out.append(pw)

    add(explicit)
    for src in order:
        if src == "serial" and serial:
            cands = serial_case_candidates(serial)
            if cands:
                log(f"[*] кандидатов из S/N «{serial}»: {len(cands)} "
                    f"(первые: {', '.join(cands[:4])}{'…' if len(cands) > 4 else ''})")
            for c in cands:
                add(c)
        elif src == "qr" and qr:
            for c in qr_candidates(qr, log):
                add(c)
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Показать кандидатов пароля точки доступа")
    ap.add_argument("--serial")
    ap.add_argument("--qr", help="фото наклейки или отсканированная строка WIFI:")
    ap.add_argument("--ap-password")
    ap.add_argument("--order", default="serial,qr", help="источники через запятую")
    a = ap.parse_args()
    cands = resolve(a.serial, a.qr, a.ap_password, tuple(a.order.split(",")))
    if not cands:
        print("кандидатов нет — задайте --serial и/или --qr")
    for i, c in enumerate(cands, 1):
        print(f"  {i:2}. {c}")
