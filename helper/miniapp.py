import hashlib
import hmac
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl

from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from .common import MAX_INTERVAL, MAX_PRICE, MIN_INTERVAL, NICKNAME, game_password, validate_keyword
from .db import Conflict

WEB = Path(__file__).with_name("web")
MAX_ORDER_ID = 2**63 - 1
PAGE_SIZE = 12
INIT_MAX_AGE = 3600
API_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
WEB_HEADERS = {
    **API_HEADERS, "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; script-src 'self' https://telegram.org; "
    "style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'self' https://web.telegram.org",
}


def telegram_user(raw, bot_token, *, now=None):
    """Verify Telegram's signature and the age of the init data."""
    if not bot_token or not 1 <= len(raw) <= 8192:
        raise PermissionError
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=30)
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError
        signed_hash = fields.pop("hash")
        if not re.fullmatch(r"[a-f0-9]{64}", signed_hash):
            raise ValueError
        message = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
        secret = hmac.digest(b"WebAppData", bot_token.encode(), "sha256")
        expected = hmac.new(secret, message.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signed_hash, expected):
            raise ValueError
        timestamp = int(fields["auth_date"])
        age = (time.time() if now is None else now) - timestamp
        if not -30 <= age <= INIT_MAX_AGE:
            raise ValueError
        owner = json.loads(fields["user"])["id"]
        if type(owner) is not int or not 0 < owner < 2**52:
            raise ValueError
        return owner
    except (ValueError, KeyError, TypeError, RecursionError):
        raise PermissionError from None


def _bounded(data, key, low, high):
    value = data.get(key)
    if type(value) is not int or not low <= value <= high:
        raise ValueError
    return value


def _order_payload(controller, data):
    if not isinstance(data, dict) or data.get("mode") not in ("notify", "buy"):
        raise ValueError
    sid = _bounded(data, "server_id", 1, 100_000)
    low = _bounded(data, "min_price", 0, MAX_PRICE)
    high = _bounded(data, "max_price", low, MAX_PRICE)
    interval = _bounded(data, "interval_seconds", MIN_INTERVAL, MAX_INTERVAL)
    nickname = data.get("nickname")
    keyword = data.get("keyword")
    password = data.get("password")
    if not all(isinstance(v, str) for v in (nickname, keyword, password)) or not NICKNAME.fullmatch(nickname):
        raise ValueError
    keyword = validate_keyword(keyword)
    password = game_password(password)
    server = controller.catalog.get().get(sid)
    if server is None:
        raise ValueError
    return {
        "server_id": server.id, "server_name": server.name, "nickname": nickname,
        "password_cipher": controller.cipher.encrypt(password.encode()), "keyword": keyword,
        "min_price": low, "max_price": high, "interval_seconds": interval, "mode": data["mode"],
    }


def _public_order(row):
    public = {key: row[key] for key in (
        "server_name", "nickname", "keyword", "min_price", "max_price", "mode",
        "interval_seconds", "status", "stop_requested", "partial_results", "found", "bought", "error",
    )}
    public["id"] = str(row["id"])
    return public


def mini_routes(controller, bot_token):
    def response(data, code=200):
        return JSONResponse(data, status_code=code, headers=API_HEADERS)

    def authenticate(request):
        auth = request.headers.get("authorization", "")
        if not auth.startswith("tma "):
            raise PermissionError
        return telegram_user(auth[4:], bot_token)

    async def asset(request):
        path = {"/": "index.html", "/app": "index.html", "/app/": "index.html",
                "/app/style.css": "style.css", "/app/main.js": "main.js"}
        return FileResponse(WEB / path[request.url.path], headers=WEB_HEADERS)

    async def api(request):
        try:
            owner = authenticate(request)
            if request.method == "POST" and request.headers.get("origin") not in (None, controller.public_url):
                raise PermissionError
            action = request.path_params["action"]
            if action == "servers" and request.method == "GET":
                catalog = await run_in_threadpool(controller.catalog.get)
                return response({"servers": [{"id": s.id, "name": s.name, "short": s.short}
                                             for s in catalog.values()]})
            if action == "orders" and request.method == "GET":
                before = int(request.query_params.get("before", MAX_ORDER_ID))
                if not 0 < before <= MAX_ORDER_ID:
                    raise ValueError
                rows = await run_in_threadpool(controller.db.orders, owner, before, PAGE_SIZE + 1)
                return response({"orders": [_public_order(row) for row in rows[:PAGE_SIZE]],
                                 "next": str(rows[PAGE_SIZE - 1]["id"]) if len(rows) > PAGE_SIZE else None})
            if action == "orders" and request.method == "POST":
                if request.headers.get("content-type", "").split(";")[0] != "application/json":
                    raise ValueError
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 4096:
                        raise ValueError
                data = json.loads(body)
                settings = await run_in_threadpool(_order_payload, controller, data)
                order_id = await run_in_threadpool(controller.db.create, owner, settings)
                return response({"id": str(order_id)}, 201)
            return response({"error": "Действие недоступно."}, 404)
        except PermissionError:
            return response({"error": "Заново открой Mini App из Telegram."}, 401)
        except (ValueError, ClientDisconnect, RecursionError):
            return response({"error": "Проверь поля заказа."}, 400)
        except Conflict as exc:
            return response({"error": str(exc)}, 409)
        except Exception as exc:
            print(f"Mini App request failed ({type(exc).__name__})", flush=True)
            return response({"error": "Сервис временно недоступен. Попробуй позже."}, 503)

    async def stop(request):
        try:
            owner = authenticate(request)
            if request.headers.get("origin") not in (None, controller.public_url):
                raise PermissionError
            order = request.path_params["order_id"]
            if not 0 < order <= MAX_ORDER_ID:
                raise ValueError
            row = await run_in_threadpool(controller.db.stop, owner, order)
            if row is None:
                return response({"error": "Активный заказ не найден."}, 404)
            return response({"ok": True})
        except PermissionError:
            return response({"error": "Заново открой Mini App из Telegram."}, 401)
        except ValueError:
            return response({"error": "Заказ не найден."}, 404)
        except Exception as exc:
            print(f"Mini App stop failed ({type(exc).__name__})", flush=True)
            return response({"error": "Сервис временно недоступен. Попробуй позже."}, 503)

    return [
        *(Route(path, asset) for path in ("/", "/app", "/app/", "/app/style.css", "/app/main.js")),
        Route("/app/api/{action}", api, methods=["GET", "POST"]),
        Route("/app/api/orders/{order_id:int}/stop", stop, methods=["POST"]),
    ]
