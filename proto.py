"""Протокол провижининга ELTEX-SW-PLG01.

Схема сообщений:

    message Hello   { bytes/string magic_string=1; string mac=2; string device_name=3; }
    message Config  { bytes ssid=1; bytes bssid=2; string password=3;
                      string mqtt_broker_url=4; string mqtt_login=5;
                      string mqtt_password=6; string node_id=7; }
    message Goodby  { string error_description=1; }
    message ProtocolMessage { Type type=1; Hello hello=2; Config config=3; Goodby goodby=4; }
    enum Type { HELLO=0; CONFIG=1; GOODBY=2; }

Транспорт: TCP, порт 56684 (0xDD6C), кадр = 4 байта длины BIG-ENDIAN + protobuf.
Ограничения: приём 1..1024 байт, отправка <= 1020 байт.
"""

import os
import struct

PROV_PORT = int(os.environ.get("ELTEX_PROV_PORT", 56684))  # 0xDD6C; env — только для тестов
MAGIC = "ELTEX_CONFIG 1.0"
HELLO, CONFIG, GOODBY = 0, 1, 2


# ---------- минимальный кодек protobuf (без внешних зависимостей) ----------

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def enc_varint_field(field: int, value: int) -> bytes:
    return _tag(field, 0) + _varint(value)


def enc_bytes_field(field: int, value) -> bytes:
    if isinstance(value, str):
        value = value.encode()
    return _tag(field, 2) + _varint(len(value)) + value


def dec_message(buf: bytes) -> dict:
    """Разбирает protobuf в {field: [значения]}; строки/сообщения — как bytes."""
    out, i = {}, 0
    while i < len(buf):
        key, i = _dec_varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            val, i = _dec_varint(buf, i)
        elif wire == 2:
            ln, i = _dec_varint(buf, i)
            val, i = buf[i:i + ln], i + ln
        elif wire == 5:
            val, i = buf[i:i + 4], i + 4
        elif wire == 1:
            val, i = buf[i:i + 8], i + 8
        else:
            raise ValueError(f"неподдерживаемый wire type {wire}")
        out.setdefault(field, []).append(val)
    return out


def _dec_varint(buf: bytes, i: int):
    shift = res = 0
    while True:
        b = buf[i]
        i += 1
        res |= (b & 0x7F) << shift
        if not b & 0x80:
            return res, i
        shift += 7


# ---------- сообщения ----------

def build_config(ssid: str, wifi_password: str, mqtt_url: str,
                 mqtt_login: str = "", mqtt_password: str = "",
                 node_id: str = "", bssid: bytes = b"") -> bytes:
    """ProtocolMessage{type=CONFIG, config=Config{...}}"""
    cfg = enc_bytes_field(1, ssid)
    if bssid:
        cfg += enc_bytes_field(2, bssid)
    cfg += enc_bytes_field(3, wifi_password)
    cfg += enc_bytes_field(4, mqtt_url)
    cfg += enc_bytes_field(5, mqtt_login)
    cfg += enc_bytes_field(6, mqtt_password)
    if node_id:
        cfg += enc_bytes_field(7, node_id)
    return enc_varint_field(1, CONFIG) + enc_bytes_field(3, cfg)


def parse_message(raw: bytes) -> dict:
    """Разбирает ProtocolMessage в удобный словарь."""
    m = dec_message(raw)
    res = {"type": m.get(1, [None])[0]}
    if 2 in m:
        h = dec_message(m[2][0])
        res["hello"] = {
            "magic": _s(h.get(1)), "mac": _s(h.get(2)), "device_name": _s(h.get(3)),
        }
    if 4 in m:
        g = dec_message(m[4][0])
        res["goodby"] = {"error": _s(g.get(1))}
    return res


def _s(lst):
    if not lst:
        return ""
    return lst[0].decode("utf-8", "replace") if isinstance(lst[0], bytes) else lst[0]


# ---------- кадрирование ----------

def frame(payload: bytes) -> bytes:
    if len(payload) > 1020:
        raise ValueError(f"сообщение {len(payload)} б > лимита прошивки 1020 б")
    return struct.pack(">I", len(payload)) + payload


def read_frame(sock, timeout: float = 30.0) -> bytes:
    sock.settimeout(timeout)
    hdr = _recv_exact(sock, 4)
    (ln,) = struct.unpack(">I", hdr)
    if not 1 <= ln <= 1024:
        raise ValueError(f"некорректная длина кадра: {ln}")
    return _recv_exact(sock, ln)


def _recv_exact(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("соединение закрыто устройством")
        buf += chunk
    return buf
