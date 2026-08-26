#!/usr/bin/env python3
"""eltex-cutter — заливка своей прошивки в ELTEX-SW-PLG01 «по воздуху», без программатора.

Идея — как в tuya-cloudcutter:

  1. Розетка в режиме настройки поднимает свою точку доступа и слушает TCP:56684.
  2. При подключении она шлёт HELLO, ждёт CONFIG с Wi-Fi и адресом MQTT-брокера.
  3. Адрес брокера задаётся как "host:port": если порт == 1883,
     прошивка соединяется по tcp:// открытым текстом — TLS и вшитый CA
     не задействуются. Значит её можно увести на свой брокер.
  4. По MQTT прилетает команда device_upgrade с http-ссылкой; прошивка качает
     образ по обычному HTTP и пишет во второй OTA-слот.

Скрипт делает все шаги: провижининг -> локальный MQTT-брокер -> HTTP-раздача
образа -> команда OTA -> наблюдение.
"""

import argparse
import ipaddress
import os
import socket
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import device
import imgcheck
import proto
from mqtt import Broker


def log(msg, prefix="*"):
    print(f"[{prefix}] {msg}", flush=True)


def die(msg):
    log(msg, "!")
    sys.exit(1)


# --------------------------------------------------------------------------
# Шаг 1. Точка доступа розетки (опционально, через nmcli)
# --------------------------------------------------------------------------

def wifi_scan_plugs(profiles=None):
    """Ищет точки доступа, подходящие под какой-нибудь профиль.

    Возвращает [(ssid, имя профиля|None)].
    """
    try:
        out = subprocess.run(["nmcli", "-t", "-f", "SSID,SIGNAL", "dev", "wifi", "list"],
                             capture_output=True, text=True, timeout=30).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    profiles = profiles if profiles is not None else device.load_all()
    found = {}
    for line in out.splitlines():
        ssid = line.split(":")[0]
        if not ssid:
            continue
        for pr in profiles:
            if pr.matches_ssid(ssid):
                found[ssid] = pr.name
                break
    return sorted(found.items())


def local_ip_towards(host):
    """IP этой машины в сети, через которую виден host."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 9))
        return s.getsockname()[0]
    finally:
        s.close()


# --------------------------------------------------------------------------
# Шаг 2. Провижининг
# --------------------------------------------------------------------------

def provision(device_ip, ssid, wifi_pass, mqtt_url, node_id,
              mqtt_login="", mqtt_pass="", timeout=30.0):
    log(f"соединяюсь с розеткой {device_ip}:{proto.PROV_PORT} …")
    try:
        sock = socket.create_connection((device_ip, proto.PROV_PORT), timeout=timeout)
    except OSError as e:
        die(f"не удалось подключиться к розетке: {e}\n"
            f"    Проверьте, что вы в её Wi-Fi и что адрес верный (--device-ip).")

    with sock:
        raw = proto.read_frame(sock, timeout)
        msg = proto.parse_message(raw)
        if msg.get("type") != proto.HELLO:
            die(f"ожидался HELLO, пришло: {msg}")
        h = msg["hello"]
        log(f"HELLO: устройство «{h['device_name']}», MAC {h['mac']}, magic «{h['magic']}»")
        if h["magic"] != proto.MAGIC:
            log(f"неожиданная magic-строка (ожидалась «{proto.MAGIC}») — продолжаю", "?")

        cfg = proto.build_config(ssid, wifi_pass, mqtt_url, mqtt_login, mqtt_pass, node_id)
        log(f"шлю CONFIG: ssid=«{ssid}», брокер={mqtt_url}, node_id={node_id}")
        sock.sendall(proto.frame(cfg))

        try:
            raw = proto.read_frame(sock, timeout)
        except (OSError, ConnectionError, ValueError) as e:
            log(f"ответ GOODBY не получен ({e}) — устройство могло сразу уйти в сеть", "?")
            return h
        ans = proto.parse_message(raw)
        if ans.get("type") == proto.GOODBY:
            err = ans.get("goodby", {}).get("error", "")
            if err and err not in ("empty", ""):
                die(f"розетка отвергла конфиг: «{err}»")
            log("GOODBY без ошибок — конфиг принят")
        else:
            log(f"неожиданный ответ: {ans}", "?")
        return h


# --------------------------------------------------------------------------
# Шаг 3. HTTP-раздача образа
# --------------------------------------------------------------------------

class _QuietHTTP(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log(f"HTTP {self.address_string()}: {fmt % args}", "http")


def start_http(directory, port):
    handler = partial(_QuietHTTP, directory=directory)
    srv = ThreadingHTTPServer(("0.0.0.0", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"раздаю {directory} по HTTP на порту {port}")
    return srv


# --------------------------------------------------------------------------
# Шаг 4. OTA
# --------------------------------------------------------------------------

# Форматы команды OTA берутся из профиля (ota.payloads).
# Для ELTEX-SW-PLG01 первый вариант подтверждён на живом устройстве.


def _find_cmd_topic(broker, node_id, wait, profile=None):
    """Топик КОМАНД устройства.

    ПРОВЕРЕНО НА ЖИВОЙ РОЗЕТКЕ (SW-PLG01, прошивка 2.3.0): она подписывается на
        sys/cmd/<node_id>, sys/cmd/redirect, sys/cmd/<node_id>/redirect, dev/cmd/<node_id>/#
    а в sys/event/<node_id> она САМА ПУБЛИКУЕТ события. Слать команды в sys/event
    бесполезно — раньше инструмент делал именно это.
    """
    wanted = profile.cmd_topics(node_id) if profile else [f"sys/cmd/{node_id}"]
    end = time.time() + wait
    while time.time() < end:
        for want in wanted:
            if want in broker.subscriptions:
                return want
        for t in broker.subscriptions:            # запасной вариант: любой sys/cmd/*
            if t.startswith("sys/cmd/") and not t.endswith("/redirect") and t != "sys/cmd/redirect":
                return t
        time.sleep(0.3)
    return None


def run_ota(broker, url, node_id, variants, wait, profile=None):
    topic = _find_cmd_topic(broker, node_id, wait, profile)
    if not topic:
        log(f"устройство не подписалось ни на один командный топик: "
            f"{profile.cmd_topics(node_id) if profile else ['sys/cmd/'+node_id]}", "!")
        log(f"подписки, которые видел брокер: {broker.subscriptions or 'нет'}", "!")
        return False
    log(f"командный топик устройства: {topic}")

    for i, payload in enumerate(variants, 1):   # варианты приходят уже с подставленным URL
        log(f"вариант {i}/{len(variants)}: публикую команду OTA")
        try:
            broker.publish(topic, payload)
        except RuntimeError as e:
            log(f"публикация не удалась: {e}", "!")
            return False
        # ждём, не пойдёт ли устройство за образом
        for _ in range(int(wait / 0.5)):
            if _http_hit.is_set():
                log("устройство начало скачивать образ — команда сработала", "+")
                return True
            time.sleep(0.5)
        if len(variants) > 1:
            log("реакции нет, пробую следующий формат команды", "?")
    return False


_http_hit = threading.Event()


class _HitHTTP(_QuietHTTP):
    def do_GET(self):
        _http_hit.set()
        return super().do_GET()


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Заливка прошивки в ELTEX-SW-PLG01 по воздуху (без программатора)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
ПОРЯДОК РАБОТЫ
  1. Переведите розетку в режим настройки (долгое удержание кнопки — обычно
     ~5 с, до частого мигания индикатора) и найдите её точку доступа.
  2. Запустите:
       sudo ./eltex_cutter.py --fw firmware.bin --ssid ДОМ --password СЕКРЕТ \\
                              --ap ELTEX-XXXX --host-ip 192.168.1.50
  3. Скрипт подключится к точке розетки, отправит настройки, затем попросит
     вернуть компьютер в домашнюю сеть и поднимет брокер + HTTP.

ОБРАЗ ПРОШИВКИ
  Нужен OTA-образ формата AmebaZ2 (ambz2), а не сырой bin от ESPHome.
  Собирается через ltchiptool. Загрузчик проверяет заголовок/подпись образа —
  secure boot на этих розетках выключен, поэтому свой образ принимается.

ЕСЛИ НЕ СРАБОТАЛО
  --try-variants перебирает форматы команды device_upgrade.
  Точные топики розетка печатает в свой лог при подключении к брокеру;
  скрипт показывает все подписки, на которые она подписалась.
""")
    ap.add_argument("--profile", default="eltex-sw-plg01",
                    help="профиль устройства из profiles/ (см. --list-profiles)")
    ap.add_argument("--list-profiles", action="store_true",
                    help="показать доступные профили устройств и выйти")
    ap.add_argument("--fw", help="файл образа прошивки для раздачи по HTTP")
    ap.add_argument("--ssid", help="SSID домашней сети для розетки")
    ap.add_argument("--password", default="", help="пароль домашней сети")
    ap.add_argument("--host-ip", help="IP этого компьютера в домашней сети "
                                      "(куда розетка будет ходить за MQTT и образом)")
    ap.add_argument("--device-ip", default=None,
                    help="адрес устройства в его точке доступа (по умолчанию — из профиля)")
    ap.add_argument("--ap", help="SSID точки доступа розетки — подключиться через nmcli")
    ap.add_argument("--ap-password", help="пароль точки устройства (если знаете явно)")
    ap.add_argument("--serial", help="серийный номер (для подбора пароля точки перебором регистра)")
    ap.add_argument("--qr", help="фото наклейки с QR или отсканированная строка WIFI: (пароль точки)")
    ap.add_argument("--ap-order", default="serial,qr",
                    help="порядок источников пароля точки: serial (перебор регистра S/N) и qr")
    ap.add_argument("--node-id", default="plug", help="node_id для розетки (по умолчанию plug)")
    ap.add_argument("--mqtt-port", type=int, default=1883,
                    help="порт брокера; ДОЛЖЕН быть 1883, иначе прошивка пойдёт по TLS")
    ap.add_argument("--http-port", type=int, default=8000, help="порт HTTP-раздачи")
    ap.add_argument("--bind", default="0.0.0.0",
                    help="интерфейс для брокера/HTTP. Для СУХИХ ТЕСТОВ ставьте 127.0.0.1, "
                         "иначе к брокеру может подключиться настоящая розетка из сети "
                         "и получить тестовую команду")
    ap.add_argument("--wait", type=float, default=90.0, help="таймаут ожидания, с")
    ap.add_argument("--skip-provision", action="store_true",
                    help="розетка уже настроена на ваш брокер — только OTA")
    ap.add_argument("--scan", action="store_true", help="показать точки доступа ELTEX-* и выйти")
    ap.add_argument("--force", action="store_true",
                    help="заливать образ, даже если проверка формата ругается")
    ap.add_argument("--try-variants", action="store_true",
                    help="перебрать форматы команды device_upgrade")
    args = ap.parse_args()

    if args.list_profiles:
        for pr in device.load_all():
            print(f"  {pr.name:24} {pr.display}  [{pr.data.get('status','?')}]")
        return

    try:
        prof = device.load(args.profile)
    except device.ProfileError as e:
        die(str(e))
    log(f"профиль: {prof.name} — {prof.display}")

    if args.scan:
        found = wifi_scan_plugs()
        if found:
            for ssid, pname in found:
                print(f"  {ssid}   -> профиль {pname}")
        else:
            print("подходящих точек доступа не найдено")
        return

    device_ip = args.device_ip or prof.device_ip
    if not device_ip:
        die("в профиле нет ap.device_ip — задайте --device-ip")
    # порт провижининга: env (для сухих тестов) важнее профиля
    proto.PROV_PORT = int(os.environ.get("ELTEX_PROV_PORT", prof.prov_port))
    proto.MAGIC = prof.magic

    if args.mqtt_port != prof.plain_port:
        log(f"порт брокера не {prof.plain_port} — устройство пойдёт по ssl:// "
            f"и будет проверять сертификат. {prof.get('mqtt','tls_note') or ''}", "!")

    if not args.skip_provision and not (args.ssid and args.host_ip):
        die("нужны --ssid и --host-ip (или --skip-provision). См. --help")

    fw_path = None
    if args.fw:
        fw_path = os.path.abspath(args.fw)
        if not os.path.isfile(fw_path):
            die(f"файл образа не найден: {fw_path}")
        if not imgcheck.report(fw_path, lambda m: print(m, flush=True), profile=prof) and not args.force:
            die("образ не похож на пригодный для OTA. Проверьте файл "
                "или запустите с --force, если уверены.")

    # --- провижининг ---
    if not args.skip_provision:
        try:
            ipaddress.ip_address(args.host_ip)
        except ValueError:
            die(f"--host-ip должен быть IP-адресом, получено: {args.host_ip}")

        if args.ap:
            import apjoin, apcreds
            cands = apcreds.resolve(args.serial, args.qr, args.ap_password,
                                    order=tuple(args.ap_order.split(",")),
                                    log=lambda m: print(m, flush=True))
            if not apjoin.join_with_candidates(args.ap, cands,
                                               log=lambda m: print(m, flush=True)):
                die("не удалось подключиться к точке устройства. "
                    "Задайте --serial, --qr или --ap-password.")

        mqtt_url = f"{args.host_ip}:{args.mqtt_port}"
        provision(device_ip, args.ssid, args.password, mqtt_url, args.node_id)
        log("настройки приняты — розетка уходит в вашу сеть", "+")

        if args.ap:
            log("верните компьютер в домашнюю сеть.")
            input("    Нажмите Enter, когда вернётесь в сеть с розеткой… ")

    if not fw_path:
        log("образ не задан (--fw) — OTA пропускаю, работа закончена")
        return

    # --- брокер + HTTP ---
    broker = Broker(host=args.bind, port=args.mqtt_port, log=lambda m: log(m, "mqtt")
                    if not m.startswith("[mqtt]") else print(m, flush=True))
    broker.start()

    directory, filename = os.path.dirname(fw_path), os.path.basename(fw_path)
    handler = partial(_HitHTTP, directory=directory)
    srv = ThreadingHTTPServer((args.bind, args.http_port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"раздаю {filename} по HTTP на порту {args.http_port}")

    host_ip = args.host_ip or local_ip_towards("8.8.8.8")
    url = f"http://{host_ip}:{args.http_port}/{filename}"
    log(f"ссылка на образ: {url}")

    log(f"жду подключения розетки к брокеру (до {args.wait:.0f} с)…")
    if not broker.connected.wait(timeout=args.wait):
        log("розетка не подключилась к брокеру.", "!")
        log("Проверьте: она в вашей сети? --host-ip верный? порт 1883 открыт "
            "в фаерволе? Розетка ходит именно на этот адрес?", "!")
        return

    all_variants = prof.payloads(url)
    variants = all_variants if args.try_variants else all_variants[:1]
    ok = run_ota(broker, url, args.node_id, variants, args.wait, prof)

    if ok:
        log("образ отдан. Розетка пишет его во второй слот и перезагрузится.", "+")
        log("Не выключайте питание, пока она не поднимется заново.", "+")
        time.sleep(20)
    else:
        log("розетка не пошла за образом.", "!")
        log(f"подписки: {broker.subscriptions}", "!")
        log(f"сообщения от неё: {broker.messages[-5:] if broker.messages else 'нет'}", "!")
        log("Попробуйте --try-variants; если и это не помогло — пришлите лог "
            "розетки (UART) с текстом её топиков.", "!")

    srv.shutdown()
    broker.stop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        log("прервано пользователем")
