import ipaddress
import json
import os
import re
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

MAX_PRICE = 100_000_000
MIN_INTERVAL = 5
MAX_INTERVAL = 60
NICKNAME = re.compile(r"[A-Za-z0-9_]{3,20}\Z")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
DEFAULT_SERVERS_URL = "https://api.blackrussia.online/servers.json"


@dataclass(frozen=True)
class Server:
    id: int
    name: str
    short: str
    host: str
    port: int


def data_directory():
    return Path(os.environ.get("DATA_DIR") or Path(__file__).resolve().parents[1] / "data").resolve()


def public_https(value):
    result = urlsplit(value)
    if (result.scheme != "https" or not result.hostname or result.username
            or result.password or result.path not in ("", "/") or result.query or result.fragment):
        raise ValueError("PUBLIC_WORKER_URL / WORKER_URL должен быть публичным HTTPS origin без пути")
    return value.rstrip("/")


def validate_keyword(value):
    value = value.strip()
    if not 1 <= len(value) <= 50 or CONTROL.search(value):
        raise ValueError("Введи от 1 до 50 символов без переносов строк.")
    return value


def parse_integer(value, low, high):
    value = value.strip()
    if not re.fullmatch(r"[0-9]{1,12}", value):
        raise ValueError(f"Введи целое число от {low} до {high}.")
    number = int(value)
    if not low <= number <= high:
        raise ValueError(f"Введи целое число от {low} до {high}.")
    return number


def game_password(value):
    if not 1 <= len(value) <= 72 or CONTROL.search(value):
        raise ValueError("Пароль должен содержать от 1 до 72 символов без переносов строк.")
    return value


class ServerCatalog:
    def __init__(self):
        self._lock = threading.Lock()
        self._until = 0.0
        self._servers = {}

    def get(self):
        with self._lock:
            if time.monotonic() < self._until:
                return dict(self._servers)
            url = os.environ.get("BR_SERVERS_URL", DEFAULT_SERVERS_URL)
            if urlsplit(url).scheme != "https":
                raise RuntimeError("Каталог серверов должен быть доступен через HTTPS.")
            try:
                with urllib.request.urlopen(url, timeout=8) as reply:
                    payload = json.loads(reply.read(2_000_000))
                rows = payload if isinstance(payload, list) else payload.get("servers", [])
                parsed = {}
                for raw in rows:
                    try:
                        sid, port = int(raw["id"]), int(raw["port"])
                        host = str(raw["ip"])
                        ip = ipaddress.IPv4Address(host)
                        short = str(raw["firstname"]).strip()
                        name = str(raw.get("name") or short).strip()
                        if sid <= 0 or not ip.is_global or not 1 <= port <= 65535:
                            continue
                        if not re.fullmatch(r"[A-Za-z0-9-]{1,35}", short) or not name:
                            continue
                        parsed[sid] = Server(sid, name[:90], short, str(ip), port)
                    except (ValueError, TypeError, KeyError):
                        continue
                if not parsed:
                    raise ValueError("empty catalogue")
                self._servers = dict(sorted(parsed.items()))
                self._until = time.monotonic() + 300
            except (ValueError, TypeError, AttributeError, OSError):
                self._until = time.monotonic() + 20
                if not self._servers:
                    raise RuntimeError("Список серверов временно недоступен.") from None
            return dict(self._servers)
