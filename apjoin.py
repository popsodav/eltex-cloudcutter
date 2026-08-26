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


def _run(cmd, timeout=40):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError:
        return 127, f"нет команды: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "таймаут"


# ---------------- Linux (nmcli) ----------------

def _nmcli_connect(ssid, password):
    cmd = ["nmcli", "dev", "wifi", "connect", ssid]
    if password:
        cmd += ["password", password]
    rc, out = _run(cmd, timeout=45)
    ok = rc == 0 and "successfully activated" in out.lower()
    return ok, out.strip()


def _nmcli_current_ssid():
    rc, out = _run(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"], timeout=15)
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


def _netsh_current_ssid():
    rc, out = _run(["netsh", "wlan", "show", "interfaces"], timeout=15)
    state = ssid = None
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("State") and ":" in s:
            state = s.split(":", 1)[1].strip()
        elif s.startswith("SSID") and not s.startswith("BSSID") and ":" in s:
            ssid = s.split(":", 1)[1].strip()
    return ssid if state == "connected" else None


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
