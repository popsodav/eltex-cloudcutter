"""Подключение к точке доступа устройства с перебором паролей-кандидатов.

Кросс-платформенно: Linux -> nmcli (как в tuya-cloudcutter, для Docker),
Windows -> netsh. Пробует кандидатов по очереди и возвращает сработавший пароль.

Только сетевой слой; список кандидатов готовит apcreds.py.
"""

import platform
import subprocess
import tempfile
import time
import os

IS_WINDOWS = platform.system() == "Windows"


def _encodings():
    """netsh пишет в OEM-кодировке консоли (cp866 у русской Windows), а не в utf-8."""
    encs = ["utf-8"]
    if IS_WINDOWS:
        import ctypes
        k32 = ctypes.windll.kernel32
        encs += [f"cp{k32.GetOEMCP()}", f"cp{k32.GetACP()}"]
    return encs


def _decode(b):
    for enc in _encodings():
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode("utf-8", "replace")


def _run(cmd, timeout=40, env=None):
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env)
        return r.returncode, _decode((r.stdout or b"") + (r.stderr or b""))
    except FileNotFoundError:
        return 127, f"нет команды: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "таймаут"


# nmcli локализован: «successfully activated» ищем в выводе на английском
_C_LOCALE = {**os.environ, "LC_ALL": "C"}


# ---------------- Linux (nmcli) ----------------

def _nmcli_connect(ssid, password):
    cmd = ["nmcli", "dev", "wifi", "connect", ssid]
    if password:
        cmd += ["password", password]
    rc, out = _run(cmd, timeout=45, env=_C_LOCALE)
    ok = rc == 0 and "successfully activated" in out.lower()
    return ok, out.strip()


def _nmcli_current_ssid():
    rc, out = _run(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"], timeout=15,
                   env=_C_LOCALE)
    for line in out.splitlines():
        if line.startswith("yes:"):
            return line.split(":", 1)[1]
    return None


def _nmcli_forget(ssid):
    _run(["nmcli", "connection", "delete", ssid], timeout=15)


# ---------------- Windows (netsh) ----------------

def _netsh_profile_xml(ssid, password):
    import html
    return f'''<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
<name>{html.escape(ssid)}</name>
<SSIDConfig><SSID><name>{html.escape(ssid)}</name></SSID></SSIDConfig>
<connectionType>ESS</connectionType><connectionMode>manual</connectionMode>
<MSM><security><authEncryption><authentication>WPA2PSK</authentication><encryption>AES</encryption><useOneX>false</useOneX></authEncryption>
<sharedKey><keyType>passPhrase</keyType><protected>false</protected><keyMaterial>{html.escape(password)}</keyMaterial></sharedKey></security></MSM>
</WLANProfile>'''


def _netsh_connect(ssid, password, settle=14):
    fd, path = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_netsh_profile_xml(ssid, password))
        _run(["netsh", "wlan", "add", "profile", f"filename={path}", "user=all"])
        _run(["netsh", "wlan", "connect", f"name={ssid}", f"ssid={ssid}"])
        end = time.time() + settle
        while time.time() < end:
            if _netsh_current_ssid() == ssid:
                return True, "connected"
            time.sleep(2)
        return False, "не подключилось за отведённое время"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# Подписи netsh переведены вместе с Windows: «State : connected» / «Состояние : подключено»
_NETSH_STATE = ("state", "состояние")
_NETSH_CONNECTED = ("connected", "подключено")


def _netsh_current_ssid():
    rc, out = _run(["netsh", "wlan", "show", "interfaces"], timeout=15)
    state = ssid = None
    for line in out.splitlines():
        key, sep, val = line.partition(":")
        if not sep:
            continue
        key, val = key.strip().lower(), val.strip()
        if key in _NETSH_STATE:
            state = val.lower()
        elif key == "ssid":                   # не BSSID / AP BSSID
            ssid = val
    return ssid if state in _NETSH_CONNECTED else None


def _netsh_forget(ssid):
    _run(["netsh", "wlan", "delete", "profile", f"name={ssid}"])


# ---------------- общий интерфейс ----------------

def current_ssid():
    return _netsh_current_ssid() if IS_WINDOWS else _nmcli_current_ssid()


def connect(ssid, password):
    return _netsh_connect(ssid, password) if IS_WINDOWS else _nmcli_connect(ssid, password)


def forget(ssid):
    (_netsh_forget if IS_WINDOWS else _nmcli_forget)(ssid)


def join_with_candidates(ssid, candidates, log=print):
    """Пробует пароли по очереди. -> сработавший пароль или None.

    После неудачной попытки удаляем профиль, чтобы ОС не подставляла его снова.
    """
    if not candidates:
        log("[!] нет ни одного пароля-кандидата (укажите --serial, --qr или --ap-password)")
        return None
    for i, pw in enumerate(candidates, 1):
        log(f"[*] подключаюсь к «{ssid}», пароль {i}/{len(candidates)}…")
        ok, msg = connect(ssid, pw)
        if ok:
            log(f"[+] подключился (пароль сработал)")
            return pw
        forget(ssid)
    log(f"[!] ни один из {len(candidates)} паролей не подошёл")
    return None


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Подключиться к точке устройства, перебирая пароли")
    ap.add_argument("ssid")
    ap.add_argument("--serial")
    ap.add_argument("--qr")
    ap.add_argument("--ap-password")
    ap.add_argument("--order", default="serial,qr")
    a = ap.parse_args()
    import apcreds
    cands = apcreds.resolve(a.serial, a.qr, a.ap_password, tuple(a.order.split(",")))
    pw = join_with_candidates(a.ssid, cands)
    print("РЕЗУЛЬТАТ:", "пароль " + pw if pw else "не подключились")
