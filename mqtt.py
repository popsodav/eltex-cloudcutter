"""Минимальный MQTT-брокер под одну задачу: принять розетку и отдать ей команду.

Поддерживает MQTT 3.1.1 (protocol level 4) и 5.0 (level 5) — прошивка собрана с
Paho, в ней есть ветки v5 (onSend5 / MQTTPubPropertedMessage), поэтому нужны обе.
Реализовано ровно столько, сколько требуется: CONNECT/CONNACK, SUBSCRIBE/SUBACK,
UNSUBSCRIBE/UNSUBACK, PUBLISH в обе стороны (QoS 0/1/2 с подтверждениями),
PINGREQ/PINGRESP, DISCONNECT.

ВИДНО ПО ЛОГАМ ЖИВОЙ РОЗЕТКИ (SW-PLG01, прошивка 2.5.0, issue #1): события в
sys/event/* она публикует с QoS > 0 и ждёт подтверждения. Без него в её UART
«status not delivered!», а у нас был мусор из packet id перед текстом
(\\x17\\x02\\x01\\x01bssid …, а id 13 = '\\r' затирал строку в терминале).
"""

import socket
import struct
import threading
import time

CONNECT, CONNACK, PUBLISH, PUBACK = 1, 2, 3, 4
PUBREC, PUBREL, PUBCOMP = 5, 6, 7
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


def _printable(s: str) -> str:
    """Управляющие символы — в \\xNN, чтобы '\\r' и пр. не ломали строку лога."""
    return "".join(c if c.isprintable() else f"\\x{ord(c):02x}" for c in s)


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
        self._send_lock = threading.Lock()   # publish() из главного потока + ack из _serve
        self._pid = 0

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
                ptype, flags, payload = pkt
                if ptype == CONNECT:
                    self._on_connect(conn, payload)
                elif ptype == SUBSCRIBE:
                    self._on_subscribe(conn, payload)
                elif ptype == UNSUBSCRIBE:
                    self._on_unsubscribe(conn, payload)
                elif ptype == PUBLISH:
                    self._on_publish(conn, flags, payload)
                elif ptype == PUBREL:             # QoS 2, шаг 3 -> завершаем PUBCOMP
                    self._send(conn, bytes([PUBCOMP << 4]), payload[:2])
                elif ptype == PUBREC:             # QoS 2 на нашу публикацию
                    self._send(conn, bytes([PUBREL << 4 | 0x02]), payload[:2])
                elif ptype in (PUBACK, PUBCOMP):  # подтверждения наших публикаций
                    pass
                elif ptype == PINGREQ:
                    self._send(conn, bytes([PINGRESP << 4]), b"")
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
        ptype, flags = hdr[0] >> 4, hdr[0] & 0x0F
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
        return ptype, flags, body

    def _send(self, conn, hdr: bytes, body: bytes):
        with self._send_lock:
            conn.sendall(hdr + _enc_len(len(body)) + body)

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
            self._send(conn, bytes([CONNACK << 4]), b"\x00\x00\x00")
        else:
            self._send(conn, bytes([CONNACK << 4]), b"\x00\x00")
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
        self._send(conn, bytes([SUBACK << 4]), body)
        self.subscribed.set()

    def _on_unsubscribe(self, conn, p):
        i = 0
        pid = struct.unpack_from(">H", p, i)[0]; i += 2
        if self.version >= 5:
            plen, i = self._varint(p, i)
            i += plen
        codes = []
        while i < len(p):
            tlen = struct.unpack_from(">H", p, i)[0]; i += 2
            topic = p[i:i + tlen].decode("utf-8", "replace"); i += tlen
            if topic in self.subscriptions:
                self.subscriptions.remove(topic)
            codes.append(0x00)
            self.log(f"[mqtt] UNSUBSCRIBE: {topic}")
        body = struct.pack(">H", pid)
        if self.version >= 5:                 # у 3.1.1 в UNSUBACK только packet id
            body += b"\x00" + bytes(codes)
        self._send(conn, bytes([UNSUBACK << 4]), body)

    def _on_publish(self, conn, flags, p):
        qos = (flags >> 1) & 0x03
        tlen = struct.unpack_from(">H", p, 0)[0]
        topic = p[2:2 + tlen].decode("utf-8", "replace")
        i = 2 + tlen
        pid = None
        if qos:                               # у QoS 1/2 после топика идёт packet id
            pid = p[i:i + 2]; i += 2
        if self.version >= 5 and i < len(p):
            plen, i = self._varint(p, i)
            i += plen
        text = p[i:].decode("utf-8", "replace")
        self.messages.append((topic, text))
        self.log(f"[mqtt] <- [{topic}] {_printable(text)}")
        # Без подтверждения прошивка считает сообщение недоставленным.
        # Код причины в v5 опускаем: длина 2 = «успех» и для 3.1.1, и для 5.0.
        if qos == 1:
            self._send(conn, bytes([PUBACK << 4]), pid)
        elif qos == 2:
            self._send(conn, bytes([PUBREC << 4]), pid)

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
            self._pid = self._pid % 0xFFFF + 1
            body += struct.pack(">H", self._pid)
        if self.version >= 5:
            body += b"\x00"                   # пустой набор properties
        body += payload.encode()
        self._send(self.client, bytes([(PUBLISH << 4) | (qos << 1)]), body)
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
