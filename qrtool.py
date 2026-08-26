"""Достать пароль точки доступа из QR-кода на корпусе устройства.

Зачем: на ELTEX-SW-PLG01 пароль AP — это серийный номер В СМЕШАННОМ РЕГИСТРЕ
(`Fc00000000`), поэтому вручную его не угадать: перебор `FC…`/`fc…` проваливается.
Оригинальное приложение просит отсканировать именно этот QR.

Формат (подтверждён на железе):
    WIFI:T:WPA;ELTXMAC:0CEE20000000;S:ELTEX-SW-PLG01-000000;P:Fc00000000;;

Декодеры пробуются по очереди — какой найдётся: pyzbar, zxing-cpp, OpenCV.
Если ни одного нет, можно просто передать строку из любого сканера телефона:
    python3 qrtool.py --text "WIFI:T:WPA;...;P:...;;"
"""

import sys


# ---------- разбор строки ----------

def parse_wifi(text):
    """WIFI:...;; -> dict. Возвращает {} если строка не того формата."""
    if not text or not text.upper().startswith("WIFI:"):
        return {}
    body = text[5:]
    out, key, val, esc = {}, None, "", False
    field = ""
    # поля разделены ';', внутри поля 'K:value'
    for part in body.split(";"):
        if not part:
            continue
        k, _, v = part.partition(":")
        if not _:
            continue
        out[k.strip().upper()] = v
    res = {}
    if "S" in out:
        res["ssid"] = out["S"]
    if "P" in out:
        res["password"] = out["P"]
    if "T" in out:
        res["security"] = out["T"]
    for k, v in out.items():
        if k not in ("S", "P", "T", "H"):
            res.setdefault("extra", {})[k] = v
    return res


# ---------- декодеры ----------

def _decode_pyzbar(path):
    from pyzbar.pyzbar import decode          # noqa
    from PIL import Image                     # noqa
    return [d.data.decode("utf-8", "replace") for d in decode(Image.open(path))]


def _decode_zxing(path):
    import zxingcpp                           # noqa
    import cv2                                # noqa
    img = cv2.imread(path)
    return [r.text for r in zxingcpp.read_barcodes(img)]


def _decode_opencv(path):
    """OpenCV-декодер слабее прочих: смазанные коды не берёт, поэтому
    прогоняем набор предобработок и поворотов."""
    import cv2
    import numpy as np
    try:
        from PIL import Image, ImageOps
        pil = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
        img = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    except Exception:
        img = cv2.imread(path)
    if img is None:
        return []
    det = cv2.QRCodeDetector()
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    H, W = g.shape[:2]
    found = []

    def push(m):
        try:
            d, _, _ = det.detectAndDecode(m)
        except Exception:
            return
        if d and d not in found:
            found.append(d)

    # целиком, затем сеткой кропов — код на фото обычно мелкий
    regions = [g]
    for fy in (0.0, 0.25, 0.5):
        for fx in (0.0, 0.25, 0.5):
            regions.append(g[int(H * fy):int(H * (fy + 0.5)),
                             int(W * fx):int(W * (fx + 0.5))])
    for reg in regions:
        if reg.size == 0:
            continue
        for scale in (1, 2, 3):
            m = reg if scale == 1 else cv2.resize(reg, None, fx=scale, fy=scale,
                                                  interpolation=cv2.INTER_CUBIC)
            push(m)
            blur = cv2.GaussianBlur(m, (0, 0), 3)          # unsharp — против расфокуса
            push(cv2.addWeighted(m, 2.0, blur, -1.0, 0))
            _, o = cv2.threshold(m, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            push(o)
            if found:
                return found
    return found


DECODERS = [("pyzbar", _decode_pyzbar), ("zxing-cpp", _decode_zxing), ("opencv", _decode_opencv)]


def decode_file(path, log=print):
    for name, fn in DECODERS:
        try:
            res = [r for r in (fn(path) or []) if r]
        except ImportError:
            continue
        except Exception as e:
            log(f"[?] декодер {name} не смог: {type(e).__name__}")
            continue
        if res:
            log(f"[+] декодер: {name}")
            return res
    return []


def creds_from_file(path, log=print):
    """-> (ssid, password, raw) или (None, None, None)."""
    for raw in decode_file(path, log):
        info = parse_wifi(raw)
        if info.get("password"):
            return info.get("ssid"), info["password"], raw
    return None, None, None


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="Достать SSID/пароль точки доступа из QR-кода на корпусе")
    ap.add_argument("image", nargs="?", help="фотография наклейки с QR")
    ap.add_argument("--text", help="уже отсканированная строка (из сканера телефона)")
    a = ap.parse_args()

    raw = a.text
    if not raw:
        if not a.image:
            ap.error("укажите файл с фотографией или --text")
        _, _, raw = creds_from_file(a.image)
        if not raw:
            print("[!] QR не распознан.")
            print("[!] Переснимите резче (10-15 см, тап по коду для фокуса, без бликов,")
            print("[!] камера параллельно наклейке) либо отсканируйте телефоном и передайте --text")
            return 2

    info = parse_wifi(raw)
    if not info:
        print(f"[!] строка не похожа на WIFI:-код: {raw!r}")
        return 2
    print(f"[+] строка: {raw}")
    print(f"    SSID:     {info.get('ssid','?')}")
    print(f"    пароль:   {info.get('password','?')}")
    print(f"    защита:   {info.get('security','?')}")
    for k, v in (info.get("extra") or {}).items():
        print(f"    {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
