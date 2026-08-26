"""Сквозной тест: эмулятор розетки, говорящий по протоколу устройства."""
import socket, struct, threading, time, sys, os, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import proto
from mqtt import _enc_len, _enc_str

RESULT = {}

def fake_plug(prov_port, mqtt_port, mqtt_ver=5):
    # --- 1. режим настройки: слушаем TCP, шлём HELLO, ждём CONFIG ---
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", prov_port)); srv.listen(1)
    conn, _ = srv.accept()
    hello = (proto.enc_varint_field(1, proto.HELLO) +
             proto.enc_bytes_field(2,
                 proto.enc_bytes_field(1, proto.MAGIC) +
                 proto.enc_bytes_field(2, "0C:EE:20:00:00:00") +
                 proto.enc_bytes_field(3, "SW-PLG01")))
    conn.sendall(proto.frame(hello))
    raw = proto.read_frame(conn, 10)
    m = proto.dec_message(raw)
    assert m[1][0] == proto.CONFIG, "ожидался CONFIG"
    cfg = proto.dec_message(m[3][0])
    RESULT["ssid"] = cfg[1][0].decode()
    RESULT["wifi_pass"] = cfg[3][0].decode()
    RESULT["broker"] = cfg[4][0].decode()
    RESULT["node_id"] = cfg[7][0].decode() if 7 in cfg else ""
    conn.sendall(proto.frame(proto.enc_varint_field(1, proto.GOODBY)))
    conn.close(); srv.close()

    # --- 2. как настоящая прошивка: разбираем host:port, port==1883 -> tcp:// ---
    host, _, port = RESULT["broker"].partition(":")
    RESULT["scheme"] = "tcp" if int(port) == 1883 else "ssl"
    assert RESULT["scheme"] == "tcp", "прошивка ушла бы в TLS!"
    time.sleep(0.8)

    # --- 3. подключаемся к брокеру, подписываемся ---
    s = socket.create_connection((host, int(port)), timeout=10)
    body = _enc_str("MQTT") + bytes([mqtt_ver, 0x02]) + struct.pack(">H", 60)
    if mqtt_ver >= 5: body += b"\x00"
    body += _enc_str(f"eltex-{RESULT['node_id']}")
    s.sendall(bytes([1 << 4]) + _enc_len(len(body)) + body)
    s.recv(32)
    # ПРОВЕРЕНО НА ЖИВОЙ РОЗЕТКЕ: команды приходят в sys/cmd/<node_id>;
    # в sys/event/<node_id> устройство само публикует события.
    topic = f"sys/cmd/{RESULT['node_id']}"
    sb = struct.pack(">H", 1) + (b"\x00" if mqtt_ver >= 5 else b"") + _enc_str(topic) + b"\x00"
    s.sendall(bytes([8 << 4 | 2]) + _enc_len(len(sb)) + sb)
    s.recv(32)

    # --- 4. ждём device_upgrade и качаем образ, как ota_http ---
    s.settimeout(60)
    data = s.recv(4096)
    i = 2
    tlen = struct.unpack_from(">H", data, i)[0]; i += 2 + tlen
    if mqtt_ver >= 5: i += 1
    payload = data[i:].decode()
    RESULT["cmd"] = payload
    cmd, _, rest = payload.partition(" ")
    assert cmd == "device_upgrade", f"неожиданная команда: {cmd}"
    # прошивка: strtok(NULL,"|") отбрасывается, strtok(NULL,"") -> URL
    url = rest.split("|", 1)[1] if "|" in rest else rest
    RESULT["url"] = url
    RESULT["fw"] = urllib.request.urlopen(url, timeout=10).read()
    s.close()

if __name__ == "__main__":
    prov_port = int(sys.argv[1]); mqtt_port = int(sys.argv[2])
    ver = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    t = threading.Thread(target=fake_plug, args=(prov_port, mqtt_port, ver))
    t.start(); t.join(120)
    import json; print("RESULT " + json.dumps({k: (v.decode('latin1')[:20] if isinstance(v, bytes) else v)
                                               for k, v in RESULT.items()}, ensure_ascii=False))
