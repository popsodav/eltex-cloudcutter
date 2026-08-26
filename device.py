"""Профили устройств: описание железа/протокола данными, а не кодом.

Добавление нового устройства = новый JSON в profiles/, без правки исходников.
Схема и смысл полей — profiles/_schema.md.
"""

import fnmatch
import json
import os

PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles")

# Обязательный минимум: без этого заливать нечего и некуда.
REQUIRED = [
    ("provisioning", "port"),
    ("provisioning", "magic"),
    ("mqtt", "plain_port"),
    ("mqtt", "cmd_topics"),
    ("ota", "payloads"),
]


class ProfileError(Exception):
    pass


class Profile:
    def __init__(self, data, path=None):
        self.data = data
        self.path = path
        self.name = data.get("name") or os.path.basename(path or "?")
        self._validate()

    # ---------- доступ ----------
    def get(self, *keys, default=None):
        cur = self.data
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    def _validate(self):
        missing = [".".join(k) for k in REQUIRED if self.get(*k) is None]
        if missing:
            raise ProfileError(
                f"профиль «{self.name}» неполный, нет полей: {', '.join(missing)}")
        if not isinstance(self.get("ota", "payloads"), list):
            raise ProfileError(f"профиль «{self.name}»: ota.payloads должен быть списком")
        if not isinstance(self.get("mqtt", "cmd_topics"), list):
            raise ProfileError(f"профиль «{self.name}»: mqtt.cmd_topics должен быть списком")

    # ---------- удобные свойства ----------
    @property
    def display(self):
        return self.data.get("display_name", self.name)

    @property
    def prov_port(self):
        return int(self.get("provisioning", "port"))

    @property
    def magic(self):
        return self.get("provisioning", "magic")

    @property
    def device_ip(self):
        return self.get("ap", "device_ip")

    @property
    def ssid_glob(self):
        return self.get("ap", "ssid_glob", default="*")

    @property
    def plain_port(self):
        return int(self.get("mqtt", "plain_port"))

    @property
    def slot_size(self):
        return self.get("image", "slot_size")

    @property
    def min_size(self):
        return self.get("image", "min_size", default=100000)

    def cmd_topics(self, node_id):
        return [t.format(node_id=node_id) for t in self.get("mqtt", "cmd_topics")]

    def payloads(self, url):
        return [p.format(url=url) for p in self.get("ota", "payloads")]

    def rejects(self):
        """[(offset, bytes, описание)] — сигнатуры заведомо негодных файлов."""
        out = []
        for r in self.get("image", "reject", default=[]) or []:
            out.append((int(r.get("offset", 0)),
                        bytes.fromhex(r["hex"]),
                        r.get("what", "неподходящий формат")))
        return out

    def matches_ssid(self, ssid):
        return fnmatch.fnmatch(ssid.upper(), self.ssid_glob.upper())


# ---------- загрузка ----------

def load(name_or_path):
    path = name_or_path
    if not os.path.isfile(path):
        path = os.path.join(PROFILE_DIR, name_or_path)
        if not path.endswith(".json"):
            path += ".json"
    if not os.path.isfile(path):
        raise ProfileError(
            f"профиль «{name_or_path}» не найден. Доступные: {', '.join(available()) or 'нет'}")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ProfileError(f"профиль «{path}» — некорректный JSON: {e}")
    return Profile(data, path)


def available():
    if not os.path.isdir(PROFILE_DIR):
        return []
    return sorted(os.path.splitext(f)[0] for f in os.listdir(PROFILE_DIR)
                  if f.endswith(".json") and not f.startswith("_"))


def load_all():
    out = []
    for n in available():
        try:
            out.append(load(n))
        except ProfileError:
            continue
    return out


def match_ssid(ssid):
    """Какому профилю соответствует найденная точка доступа."""
    for p in load_all():
        if p.matches_ssid(ssid):
            return p
    return None


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        p = load(sys.argv[1])
        print(json.dumps(p.data, ensure_ascii=False, indent=2))
    else:
        for p in load_all():
            st = p.data.get("status", "?")
            print(f"  {p.name:24} {p.display}  [{st}]")
