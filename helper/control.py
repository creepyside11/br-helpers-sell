import html
import json
import os
import threading
from contextlib import asynccontextmanager
from urllib.parse import parse_qs
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route

from .common import MAX_PRICE, ServerCatalog, data_directory, public_https
from .credentials import load_cipher
from .db import Conflict, Database
from .miniapp import mini_routes
from .telegram import Controller, Telegram

SECRET_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
}
STYLE = """
body{font:16px system-ui;background:#f5f6fa;color:#18202d;margin:0;padding:24px}
main{max-width:440px;margin:5vh auto;background:white;border-radius:16px;padding:24px;box-shadow:0 6px 30px #161b2920}
h1{font-size:21px;margin:0 0 18px} label{display:block;margin:14px 0}
input{display:block;padding:10px;border:1px solid #b8bcc9;width:100%;box-sizing:border-box;border-radius:8px;margin-top:6px;font:inherit}
button{display:block;padding:10px 14px;background:#2455d7;color:white;border:0;border-radius:8px;font:inherit}
"""


def page(body, status=200):
    return HTMLResponse(
        f'<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" '
        f'content="width=device-width,initial-scale=1"><title>RakCRMP Helper</title>'
        f'<style>{STYLE}</style><main><h1>RakCRMP Helper</h1>{body}</main></html>',
        status_code=status, headers=SECRET_HEADERS,
    )


def form_html(kind):
    password = '<label>Пароль<input name="password" type="password" required maxlength="256" autocomplete="off"></label>'
    fields = ""
    if kind == "vps":
        fields = ('<label>Публичный IP VPS<input name="host" maxlength="45" required></label>'
                  '<label>SSH-логин<input name="username" maxlength="32" required></label>')
    return page(f'<form method="post" autocomplete="off">{fields}{password}<button>Отправить</button></form>')


async def body_bytes(request, max_bytes=4096):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise ValueError("Слишком длинный запрос.")
    return body


def number(data, key, low=1, high=2**63 - 1):
    v = data.get(key)
    if type(v) is not int or not low <= v <= high:
        raise ValueError(key)
    return v


def worker_action(db, op, token, data):
    if op == "claim":
        return {"order": db.claim(token)}
    order_id = number(data, "order_id")
    if op == "pulse":
        partial = data.get("partial")
        if partial is not None and type(partial) is not bool:
            raise ValueError("partial")
        return {"stop": db.pulse(token, order_id, partial)}
    if op == "finish":
        error = data.get("error")
        if error is not None and (not isinstance(error, str) or not 1 <= len(error) <= 220):
            raise ValueError("error")
        db.finish(token, order_id, error)
        return {"ok": True}
    lot = number(data, "lot_id")
    if op == "purchase":
        outcome = data.get("outcome")
        if outcome not in ("bought", "declined", "uncertain"):
            raise ValueError("outcome")
        db.purchase(token, order_id, lot, outcome)
        return {"ok": True}
    price = number(data, "price", 0, MAX_PRICE)
    if op == "reserve":
        return {"reserved": db.reserve(token, order_id, lot, price)}
    if op == "observe":
        name = data.get("name")
        if not isinstance(name, str) or not 1 <= len(name) <= 200 or any(ord(c) < 32 for c in name):
            raise ValueError("name")
        return {"created": db.observe(token, order_id, lot, name, price)}
    raise ValueError("unknown operation")


def create_app(controller, run_telegram=True, *, bot_token=None):
    db = controller.db

    @asynccontextmanager
    async def lifespan(app):
        await run_in_threadpool(db.initialize)
        stop = threading.Event()
        threads = []
        if run_telegram:
            threads = [threading.Thread(target=target, args=(stop,), daemon=True)
                       for target in (controller.poll, controller.alarms)]
            for thread in threads:
                thread.start()
        try:
            yield
        finally:
            stop.set()

    async def health(request):
        return PlainTextResponse("RakCRMP Helper")

    async def form(request):
        token = request.path_params["token"]
        if request.method == "GET":
            kind = await run_in_threadpool(controller.form_kind, token)
            return form_html(kind) if kind else page("<p>Ссылка истекла. Открой форму в Telegram заново.</p>", 404)
        try:
            if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
                raise ValueError("Неверный формат формы.")
            if request.headers.get("origin") not in (None, controller.public_url):
                raise ValueError("Открой форму по ссылке из Telegram.")
            values = parse_qs((await body_bytes(request)).decode(), keep_blank_values=True, max_num_fields=6)
            fields = {key: value[0] for key, value in values.items() if len(value) == 1}
            message = await run_in_threadpool(controller.submit_form, token, fields)
            return page(f"<p>{html.escape(message)}</p>")
        except (ValueError, ClientDisconnect) as exc:
            return page(f"<p>{html.escape(str(exc))}</p><p>Вернись к форме, чтобы попробовать ещё раз.</p>", 400)

    async def worker(request):
        token = request.headers.get("authorization", "")
        if not token.startswith("Bearer ") or len(token) != 55 or not token.isascii():
            return JSONResponse({"error": "unauthorized"}, status_code=401, headers=SECRET_HEADERS)
        try:
            data = json.loads(await body_bytes(request))
            if not isinstance(data, dict):
                raise ValueError("body")
            response = await run_in_threadpool(worker_action, db, request.path_params["op"], token[7:], data)
            return JSONResponse(response, headers=SECRET_HEADERS)
        except PermissionError:
            code = 401
        except Conflict:
            code = 409
        except (ValueError, ClientDisconnect):
            code = 400
        except Exception as exc:
            print(f"Worker handler failed ({type(exc).__name__})", flush=True)
            code = 503
        return JSONResponse({"error": "worker request failed"}, status_code=code, headers=SECRET_HEADERS)

    return Starlette(routes=[
        Route("/health", health),
        Route("/form/{token}", form, methods=["GET", "POST"]),
        Route("/worker/{op}", worker, methods=["POST"]),
        *mini_routes(controller, bot_token),
    ], lifespan=lifespan)


def main():
    import uvicorn

    token = os.environ["BOT_TOKEN"]
    if not token or "/" in token:
        raise ValueError("Некорректный BOT_TOKEN")
    db = Database(os.environ["DATABASE_URL"], None)
    cipher = load_cipher(data_directory(), os.environ.get("DATABASE_KEY"),
                         has_pending_passwords=db.has_pending_passwords)
    db.cipher = cipher
    controller = Controller(db, ServerCatalog(), cipher, Telegram(token), int(os.environ["ADMIN_ID"]),
                            public_https(os.environ["PUBLIC_WORKER_URL"]))
    uvicorn.run(create_app(controller, bot_token=token),
                host=os.environ.get("LISTEN_HOST", "127.0.0.1"),
                port=int(os.environ.get("PORT", "8080")), access_log=False)


if __name__ == "__main__":
    main()
