"""Минимальный MQTT-брокер под одну задачу: принять розетку и отдать ей команду.

Поддерживает MQTT 3.1.1 (protocol level 4) и 5.0 (level 5) — прошивка собрана с
Paho, в ней есть ветки v5 (onSend5 / MQTTPubPropertedMessage), поэтому нужны обе.
Реализовано ровно столько, сколько требуется: CONNECT/CONNACK, SUBSCRIBE/SUBACK,
PUBLISH в обе стороны (QoS 0/1), PINGREQ/PINGRESP, DISCONNECT.
"""

import socket
import struct
import threading
import time

CONNECT, CONNACK, PUBLISH, PUBACK = 1, 2, 3, 4
SUBSCRIBE, SUBACK, UNSUBSCRIBE, UNSUBACK = 8, 9, 10, 11
PINGREQ, PINGRESP, DISCONNECT = 12, 13, 14


def _enc_len(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _enc_str(s) -> bytes:
    if isinstance(s, str):
        s = s.encode()
    return struct.pack(">H", len(s)) + s


class Broker(threading.Thread):
    def __init__(self, host="0.0.0.0", port=1883, log=print):
        super().__init__(daemon=True)
        self.host, self.port, self.log = host, port, log
        self.sock = None
        self.client = None          # активный сокет розетки
        self.version = 4
        self.client_id = None
        self.subscriptions = []     # топики, на которые подписалась розетка
        self.messages = []          # (topic, payload) от розетки
        self.connected = threading.Event()
        self.subscribed = threading.Event()
        self._stop = threading.Event()

    # ---------- жизненный цикл ----------
    def run(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.listen(4)
        self.sock.settimeout(1.0)
        self.log(f"[mqtt] слушаю {self.host}:{self.port}")
        while not self._stop.is_set():
            try:
                conn, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.log(f"[mqtt] подключился клиент {addr[0]}")
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def stop(self):
        self._stop.set()
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass

    # ---------- обработка клиента ----------
    def _serve(self, conn):
        conn.settimeout(None)
        try:
            while not self._stop.is_set():
                pkt = self._read_packet(conn)
                if pkt is None:
                    break
                ptype, payload = pkt
                if ptype == CONNECT:
                    self._on_connect(conn, payload)
                elif ptype == SUBSCRIBE:
                    self._on_subscribe(conn, payload)
                elif ptype == PUBLISH:
                    self._on_publish(payload)
                elif ptype == PINGREQ:
                    conn.sendall(bytes([PINGRESP << 4, 0]))
                elif ptype == DISCONNECT:
                    self.log("[mqtt] клиент отключился (DISCONNECT)")
                    break
        except (ConnectionError, OSError) as e:
            self.log(f"[mqtt] соединение потеряно: {e}")
        finally:
            if self.client is conn:
                self.client = None
                self.connected.clear()
            try:
                conn.close()
            except OSError:
                pass

    def _read_packet(self, conn):
        hdr = conn.recv(1)
        if not hdr:
            return None
        ptype = hdr[0] >> 4
        mult, length = 1, 0
        while True:
            b = conn.recv(1)
            if not b:
                return None
            length += (b[0] & 127) * mult
            if not b[0] & 128:
                break
            mult *= 128
        body = b""
        while len(body) < length:
            chunk = conn.recv(length - len(body))
            if not chunk:
                return None
            body += chunk
        return ptype, body

    def _on_connect(self, conn, p):
        i = 0
        nlen = struct.unpack_from(">H", p, i)[0]; i += 2
        name = p[i:i + nlen].decode(); i += nlen
        level = p[i]; i += 1
        flags = p[i]; i += 1
        keepalive = struct.unpack_from(">H", p, i)[0]; i += 2
        self.version = level
        if level >= 5:                       # у v5 после keepalive идут properties
            plen, i = self._varint(p, i)
            i += plen
        cidlen = struct.unpack_from(">H", p, i)[0]; i += 2
        self.client_id = p[i:i + cidlen].decode("utf-8", "replace")
        self.log(f"[mqtt] CONNECT: {name} v{level}, clientId='{self.client_id}', "
                 f"keepalive={keepalive}s")
        if level >= 5:
            conn.sendall(bytes([CONNACK << 4, 3, 0x00, 0x00, 0x00]))
        else:
            conn.sendall(bytes([CONNACK << 4, 2, 0x00, 0x00]))
        self.client = conn
        self.connected.set()

    def _on_subscribe(self, conn, p):
        i = 0
        pid = struct.unpack_from(">H", p, i)[0]; i += 2
        if self.version >= 5:
            plen, i = self._varint(p, i)
            i += plen
        codes = []
        while i < len(p):
            tlen = struct.unpack_from(">H", p, i)[0]; i += 2
            topic = p[i:i + tlen].decode("utf-8", "replace"); i += tlen
            i += 1                            # options/QoS
            self.subscriptions.append(topic)
            codes.append(0x00)
            self.log(f"[mqtt] SUBSCRIBE: {topic}")
        body = struct.pack(">H", pid)
        if self.version >= 5:
            body += b"\x00"
        body += bytes(codes)
        conn.sendall(bytes([SUBACK << 4]) + _enc_len(len(body)) + body)
        self.subscribed.set()

    def _on_publish(self, p):
        tlen = struct.unpack_from(">H", p, 0)[0]
        topic = p[2:2 + tlen].decode("utf-8", "replace")
        rest = p[2 + tlen:]
        if self.version >= 5 and rest:
            plen, off = self._varint(rest, 0)
            rest = rest[off + plen:]
        text = rest.decode("utf-8", "replace")
        self.messages.append((topic, text))
        self.log(f"[mqtt] <- [{topic}] {text}")

    @staticmethod
    def _varint(buf, i):
        mult, val = 1, 0
        while True:
            b = buf[i]; i += 1
            val += (b & 127) * mult
            if not b & 128:
                return val, i
            mult *= 128

    # ---------- публикация ----------
    def publish(self, topic: str, payload: str, qos: int = 0):
        if not self.client:
            raise RuntimeError("розетка не подключена к брокеру")
        body = _enc_str(topic)
        if qos:
            body += struct.pack(">H", 1)
        if self.version >= 5:
            body += b"\x00"                   # пустой набор properties
        body += payload.encode()
        hdr = (PUBLISH << 4) | (qos << 1)
        self.client.sendall(bytes([hdr]) + _enc_len(len(body)) + body)
        self.log(f"[mqtt] -> [{topic}] {payload}")

    def wait_topic(self, substr: str, timeout: float = 120.0):
        """Ждёт подписки на топик, содержащий substr; возвращает его или None."""
        end = time.time() + timeout
        while time.time() < end:
            for t in self.subscriptions:
                if substr in t:
                    return t
            time.sleep(0.3)
        return None
