import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .common import MAX_PRICE, NICKNAME, game_password, parse_integer, validate_keyword
from .db import Conflict
from .ssh_install import install_worker, validate_ssh

NEW = "➕ Новый заказ"
ORDERS = "📋 Мои заказы"
PAGE_SIZE = 9
TTL = 15 * 60
START, MAX_ID = 0, 2**63 - 1
STATUS = {"queued": "В очереди", "running": "Работает", "stopped": "Остановлен", "error": "Ошибка"}
MODE = {"notify": "Уведомления", "buy": "Автопокупка"}


class TelegramError(Exception):
    def __init__(self, code, terminal=False):
        super().__init__(f"Telegram API error ({code})")
        self.terminal = terminal


class Telegram:
    def __init__(self, token):
        self.url = "https://api.telegram.org/bot" + token + "/"

    def call(self, method, http_timeout=10, **payload):
        req = urllib.request.Request(self.url + method, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as reply:
                data = json.loads(reply.read(2_000_000))
        except urllib.error.HTTPError as exc:
            raise TelegramError(exc.code, exc.code in (400, 403)) from None
        except (OSError, ValueError):
            raise TelegramError("transport") from None
        if not data.get("ok"):
            raise TelegramError(data.get("error_code", "response"))
        return data["result"]

    def send(self, chat, value, keyboard=None):
        options = {"reply_markup": keyboard} if keyboard else {}
        return self.call("sendMessage", chat_id=chat, text=value, **options)


def button(label, data):
    return {"text": label, "callback_data": data}


def buttons(*rows):
    return {"inline_keyboard": rows}


def money(number):
    return f"{number:,}".replace(",", " ")


@dataclass
class Draft:
    phase: str
    nonce: str = field(default_factory=lambda: secrets.token_hex(6))
    expiry: float = field(default_factory=lambda: time.monotonic() + TTL)
    settings: dict = field(default_factory=dict, repr=False)


class Controller:
    def __init__(self, db, catalog, cipher, telegram, admin_id, public_url):
        self.db, self.catalog, self.cipher, self.tg = db, catalog, cipher, telegram
        self.admin_id, self.public_url = admin_id, public_url
        self.drafts, self.forms = {}, {}
        self.lock = threading.RLock()
        self.install_lock = threading.Lock()

    def _sweep(self):
        now = time.monotonic()
        for owner, draft in list(self.drafts.items()):
            if draft.expiry <= now:
                self.drafts.pop(owner, None)
        for token_hash, record in list(self.forms.items()):
            if record[3] <= now:
                self.forms.pop(token_hash, None)

    def _form(self, owner, purpose, nonce=""):
        import hashlib

        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).digest()
        self._sweep()
        for key, record in list(self.forms.items()):
            if record[:2] == (owner, purpose):
                self.forms.pop(key)
        self.forms[digest] = (owner, purpose, nonce, time.monotonic() + TTL)
        return f"{self.public_url}/form/{token}"

    def _read_form(self, token):
        import hashlib

        self._sweep()
        if len(token) != 43:
            return None, None
        digest = hashlib.sha256(token.encode()).digest()
        return digest, self.forms.get(digest)

    def form_kind(self, token):
        with self.lock:
            _, record = self._read_form(token)
            return record[1] if record else None

    def submit_form(self, token, fields):
        with self.lock:
            digest, record = self._read_form(token)
            if record is None:
                raise ValueError("Ссылка истекла. Откройте форму в боте заново.")
            owner, purpose, nonce, _ = record
            if purpose == "game":
                draft = self.drafts.get(owner)
                if draft is None or draft.nonce != nonce or draft.phase != "password":
                    raise ValueError("Этот заказ больше не редактируется.")
                password = game_password(fields.get("password", ""))
                draft.settings["password_cipher"] = self.cipher.encrypt(password.encode())
                draft.phase = "keyword"
                draft.expiry = time.monotonic() + TTL
                self.forms.pop(digest)
            else:
                if owner != self.admin_id:
                    raise ValueError("Недостаточно прав.")
                creds = validate_ssh(*(fields.get(k, "") for k in ("host", "username", "password")))
                if not self.install_lock.acquire(blocking=False):
                    raise ValueError("Уже выполняется установка. Подожди её завершения.")
                self.forms.pop(digest)
                threading.Thread(target=self._install, args=(owner, creds), daemon=True).start()
        if purpose == "game":
            try:
                self.tg.send(owner, "Пароль сохранён для этого заказа. Введи название предмета или ключевое слово, например Емеля.")
            except TelegramError:
                pass
            return "Пароль сохранён. Вернись в Telegram и введи название предмета."
        return "Установка запущена. Результат придёт в Telegram."

    def _install(self, owner, credentials):
        worker = None
        try:
            worker, token = self.db.enroll(credentials[0])
            install_worker(*credentials, self.public_url, token, os.environ.get("BR_SERVERS_URL", ""))
        except (RuntimeError, OSError, ValueError) as exc:
            if worker is not None:
                self.db.disable(worker)
            message = str(exc) if isinstance(exc, RuntimeError) else "Не удалось установить воркер. Проверь доступ к VPS."
        except Exception:
            message = "Не удалось зарегистрировать воркер."
            if worker is not None:
                try:
                    self.db.disable(worker)
                except Exception:
                    pass
        else:
            message = "Воркер установлен и запущен. Статус подключения смотри в /admin."
        finally:
            self.install_lock.release()
        try:
            self.tg.send(owner, message)
        except TelegramError:
            pass

    def start(self, owner):
        self.tg.send(owner, "RakCRMP Helper\nСоздавай заказы и следи за лотами в приложении.", buttons(
            [{"text": "Открыть Mini App", "web_app": {"url": self.public_url + "/app"}}],
            [button(NEW, "new_order"), button(ORDERS, f"list:{MAX_ID}")],
        ))

    def servers(self, owner, draft, page):
        rows = list(self.catalog.get().values())
        if page < 0 or page * PAGE_SIZE >= len(rows):
            raise ValueError("Эта страница больше недоступна.")
        items = [[button(f"{item.id} · {item.short}", f"s:{draft.nonce}:{item.id}")]
                 for item in rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]]
        nav = []
        if page:
            nav.append(button("‹ Назад", f"pg:{draft.nonce}:{page - 1}"))
        if (page + 1) * PAGE_SIZE < len(rows):
            nav.append(button("Далее ›", f"pg:{draft.nonce}:{page + 1}"))
        self.tg.send(owner, "Выбери сервер Black Russia.", buttons(*items, *([nav] if nav else [])))

    def _order_text(self, row):
        status = STATUS[row["status"]]
        if row["status"] == "running" and row["stop_requested"]:
            status = "Останавливается"
        lines = [
            f"Заказ #{row['id']} · {status}", f"{row['server_name']} · {row['nickname']}",
            f"Поиск: {row['keyword']}", f"Цена: {money(row['min_price'])} – {money(row['max_price'])} ₽",
            f"{MODE[row['mode']]} · каждые {row['interval_seconds']} сек.",
            f"Найдено: {row['found']} · Куплено: {row['bought']}",
        ]
        if row["partial_results"]:
            lines.append("У запроса несколько страниц. Проверена только первая.")
        if row["error"]:
            lines.append(row["error"])
        return "\n".join(lines)

    def list_orders(self, owner, before=MAX_ID):
        rows = self.db.orders(owner, before, 9)
        if not rows:
            self.tg.send(owner, "Заказов на этой странице нет.")
            return
        keyboard = [[button(f"#{r['id']} · {r['keyword'][:27]} · {STATUS[r['status']]}", f"ord:{r['id']}")]
                    for r in rows[:8]]
        if len(rows) > 8:
            keyboard.append([button("Следующие заказы ›", f"list:{rows[7]['id']}")])
        self.tg.send(owner, "Мои заказы", buttons(*keyboard))

    def show_order(self, owner, order_id):
        row = self.db.order(owner, order_id)
        if not row:
            self.tg.send(owner, "Заказ не найден.")
            return
        keyboard = [[button("Обновить", f"ord:{order_id}"), button("Мои заказы", f"list:{MAX_ID}")]]
        if row["status"] in ("queued", "running") and not row["stop_requested"]:
            keyboard.append([button("Остановить", f"stop:{order_id}")])
        self.tg.send(owner, self._order_text(row), buttons(*keyboard))

    def admin(self, owner):
        if owner != self.admin_id:
            return
        workers = self.db.workers()
        text = ["Админка RakCRMP Helper", "", "VPS-воркеры"]
        for row in workers:
            state = "отключён" if not row["enabled"] else "в сети" if row["online"] else "нет сигнала"
            job = f" · заказ #{row['order_id']}" if row["order_id"] else ""
            text.append(f"{row['label']} · {state}{job}")
        if not workers:
            text.append("Воркеры пока не подключены.")
        self.tg.send(owner, "\n".join(text), buttons([button("Установить на VPS", "vps")],
                                                       [button("Обновить статус", "admin")]))

    def message(self, owner, raw, message_id):
        value = raw.strip()
        cmd = value.split(maxsplit=1)[0].split("@", 1)[0] if value.startswith("/") else ""
        if cmd == "/start":
            self.start(owner)
            return
        if cmd == "/cancel":
            self.drafts.pop(owner, None)
            self.tg.send(owner, "Черновик сброшен. Активные заказы доступны в «Мои заказы».")
            return
        if cmd == "/admin":
            self.admin(owner)
            return
        if cmd == "/orders" or value == ORDERS:
            self.list_orders(owner)
            return
        if cmd == "/new" or value == NEW:
            draft = self.drafts[owner] = Draft("server")
            self.servers(owner, draft, 0)
            return
        draft = self.drafts.get(owner)
        if draft is None:
            self.start(owner)
            return
        draft.expiry = time.monotonic() + TTL
        p = draft.phase
        if p == "nickname":
            if not NICKNAME.fullmatch(value):
                raise ValueError("Введи игровой ник латиницей, 3–20 символов. Допустимы цифры и подчёркивание.")
            draft.settings["nickname"] = value
            draft.phase = "password"
        if draft.phase == "password":
            # Delete an accidental password sent as a chat message.
            if p == "password":
                try:
                    self.tg.call("deleteMessage", chat_id=owner, message_id=message_id)
                except TelegramError:
                    pass
            url = self._form(owner, "game", draft.nonce)
            self.tg.send(owner, "Введи пароль от своего аккаунта 3+ уровня в одноразовой форме. Пароль будет передан воркеру на VPS для входа и удалён из очереди.",
                         buttons([{"text": "Ввести пароль", "url": url}]))
        elif p == "keyword":
            draft.settings["keyword"] = validate_keyword(value)
            draft.phase = "min"
            self.tg.send(owner, "Введи минимальную цену от 0 до 100 000 000 ₽, цифрами без пробелов.")
        elif p == "min":
            draft.settings["min_price"] = parse_integer(value, 0, MAX_PRICE)
            draft.phase = "max"
            self.tg.send(owner, "Введи максимальную цену, цифрами без пробелов.")
        elif p == "max":
            draft.settings["max_price"] = parse_integer(value, draft.settings["min_price"], MAX_PRICE)
            draft.phase = "mode"
            self.tg.send(owner, "Что делать при совпадении?", buttons(
                [button("Уведомлять", f"mode:{draft.nonce}:notify")],
                [button("Покупать автоматически", f"mode:{draft.nonce}:buy")],
            ))
        elif p == "interval":
            draft.settings["interval_seconds"] = parse_integer(value, 5, 60)
            draft.phase = "confirm"
            data = draft.settings
            text = (f"{data['server_name']} · {data['nickname']}\nПоиск: {data['keyword']}\n"
                    f"Цена: {money(data['min_price'])} – {money(data['max_price'])} ₽\n"
                    f"{MODE[data['mode']]} · каждые {data['interval_seconds']} сек.")
            if data["mode"] == "buy":
                text += "\nБот будет покупать по одной штуке каждого подходящего лота до остановки заказа."
            self.tg.send(owner, text, buttons([button("Запустить", f"go:{draft.nonce}:1")],
                                              [button("Отмена", f"cancel:{draft.nonce}:1")]))
        else:
            self.tg.send(owner, "Используй кнопки текущего шага или /cancel.")

    def callback(self, owner, data):
        if data == "new_order":
            self.message(owner, "/new", 0)
            return
        if data == "admin":
            self.admin(owner)
            return
        if data == "vps":
            if owner == self.admin_id:
                url = self._form(owner, "vps")
                self.tg.send(owner, "Заполни IP, SSH-логин и пароль от VPS.",
                             buttons([{"text": "Открыть форму VPS", "url": url}]))
            return
        parts = data.split(":")
        if len(parts) == 2 and parts[0] in ("list", "ord", "stop"):
            action, val = parts
            item_id = parse_integer(val, 1, MAX_ID) if len(val) <= 12 else int(val)
            if not 0 < item_id <= MAX_ID:
                raise ValueError("Недействительный номер заказа.")
            if action == "list":
                self.list_orders(owner, item_id)
            elif action == "ord":
                self.show_order(owner, item_id)
            else:
                self.db.stop(owner, item_id)
                self.show_order(owner, item_id)
            return
        if len(parts) != 3:
            return
        action, nonce, val = parts
        draft = self.drafts.get(owner)
        if draft is None or draft.nonce != nonce:
            raise ValueError("Этот черновик истёк. Начни новый заказ.")
        if action == "pg" and draft.phase == "server":
            self.servers(owner, draft, parse_integer(val, 0, 1000))
        elif action == "s" and draft.phase == "server":
            server = self.catalog.get().get(parse_integer(val, 1, 100_000))
            if server is None:
                raise ValueError("Сервер больше не доступен.")
            draft.settings.update(server_id=server.id, server_name=server.name)
            draft.phase = "nickname"
            self.tg.send(owner, "Введи ник аккаунта 3+ уровня на выбранном сервере.")
        elif action == "mode" and draft.phase == "mode" and val in MODE:
            draft.settings["mode"] = val
            draft.phase = "interval"
            self.tg.send(owner, "Введи интервал проверок в секундах, от 5 до 60.")
        elif action == "go" and draft.phase == "confirm":
            order = self.db.create(owner, draft.settings)
            self.drafts.pop(owner, None)
            self.show_order(owner, order)
        elif action == "cancel":
            self.drafts.pop(owner, None)
            self.tg.send(owner, "Черновик заказа удалён.")

    def handle(self, event):
        cb = event.get("callback_query")
        msg = cb.get("message", {}) if cb else event.get("message", {})
        chat = msg.get("chat", {})
        author = (cb or msg).get("from", {}).get("id")
        if cb:
            try:
                self.tg.call("answerCallbackQuery", callback_query_id=cb["id"])
            except TelegramError:
                pass
        if chat.get("type") != "private" or chat.get("id") != author:
            return
        try:
            with self.lock:
                self._sweep()
                if cb:
                    self.callback(author, cb.get("data", ""))
                elif isinstance(msg.get("text"), str):
                    self.message(author, msg["text"], msg["message_id"])
        except (ValueError, Conflict, RuntimeError) as exc:
            self.tg.send(author, str(exc))
        except TelegramError:
            raise
        except Exception as exc:
            print(f"Telegram handler failed ({type(exc).__name__})", flush=True)
            self.tg.send(author, "Временная ошибка сервиса. Попробуй снова.")

    def poll(self, stop):
        offset = None
        menu_pending = True
        menu_retry_at = 0.0
        stop.wait(.1)
        while not stop.is_set():
            if menu_pending and time.monotonic() >= menu_retry_at:
                try:
                    self.tg.call("setChatMenuButton", menu_button={
                        "type": "web_app", "text": "Маркет", "web_app": {"url": self.public_url + "/app"},
                    })
                    menu_pending = False
                except TelegramError as exc:
                    print(f"Mini App menu registration: {exc}", flush=True)
                    menu_retry_at = time.monotonic() + 60
            try:
                events = self.tg.call("getUpdates", http_timeout=22, timeout=15,
                                      offset=offset or 0, allowed_updates=["message", "callback_query"])
                for event in events:
                    try:
                        self.handle(event)
                    except TelegramError:
                        pass
                    offset = event["update_id"] + 1
            except TelegramError as exc:
                print(str(exc), flush=True)
                stop.wait(3)

    def alarms(self, stop):
        while not stop.is_set():
            try:
                self.db.reap()
                for notice in self.db.alerts():
                    try:
                        self.tg.send(notice["user_id"], notice["text"])
                    except TelegramError as exc:
                        if not exc.terminal:
                            break
                    self.db.delivered(notice["id"])
            except Exception as exc:
                print(f"Notifications unavailable ({type(exc).__name__})", flush=True)
            stop.wait(2)
