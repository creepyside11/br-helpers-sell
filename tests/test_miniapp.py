import hashlib
import hmac
import json
import time
import unittest
from urllib.parse import urlencode

from cryptography.fernet import Fernet
from starlette.testclient import TestClient

from helper.control import create_app
from helper.miniapp import telegram_user
from helper.telegram import Controller
from helper.worker import OrderRunner, WALK_INTERVAL
from test_helper import Catalog, FakeDB, FakeTelegram, ReplayGame

TOKEN = "1234:fake-token-for-tests"


def init_data(owner=42, when=None):
    fields = {
        "user": json.dumps({"id": owner, "first_name": "Demo User"}, separators=(",", ":")),
        "auth_date": str(int(time.time()) if when is None else when),
        "query_id": "demo",
        "signature": "webapp-signature-placeholder",
    }
    secret = hmac.digest(b"WebAppData", TOKEN.encode(), "sha256")
    message = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    fields["hash"] = hmac.new(secret, message.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def headers(owner):
    return {"Authorization": "tma " + init_data(owner)}


class MiniDB(FakeDB):
    def orders(self, owner, before, limit):
        return [row for oid in sorted(self.saved, reverse=True)
                if oid < before and (row := self.order(owner, oid))][:limit]

    def order(self, owner, oid):
        row = super().order(owner, oid)
        if row and self.saved[oid][1].get("miniapp_stopped"):
            row.update(status="stopped", stop_requested=True)
        return row

    def stop(self, owner, oid):
        row = self.order(owner, oid)
        if row is None or row["status"] == "stopped":
            return None
        self.saved[oid][1]["miniapp_stopped"] = True
        return {"status": "stopped"}


class MiniAppTests(unittest.TestCase):
    def test_signed_identity_and_expiry(self):
        self.assertEqual(telegram_user(init_data(), TOKEN), 42)
        for invalid in (
            init_data().replace("42", "43", 1),
            init_data(when=int(time.time()) - 3601),
            init_data(when=int(time.time()) + 120),
            init_data() + "&user=x",
        ):
            with self.assertRaises(PermissionError):
                telegram_user(invalid, TOKEN)

    def test_ui_routes_and_order_ownership(self):
        db = MiniDB()
        cipher = Fernet(Fernet.generate_key())
        tg = FakeTelegram()
        controller = Controller(db, Catalog(), cipher, tg, 2, "https://helper.example.com")
        controller.start(42)
        button = tg.transcript[-1][2]["inline_keyboard"][0][0]
        self.assertEqual(button["web_app"]["url"], "https://helper.example.com/app")
        payload = {"server_id": 7, "nickname": "Demo_Player", "password": "test-password",
                   "keyword": "Емеля", "min_price": 0, "max_price": 300000, "mode": "notify",
                   "interval_seconds": 5}
        with TestClient(create_app(controller, run_telegram=False, bot_token=TOKEN)) as app:
            self.assertIn("Маркет", app.get("/app").text)
            self.assertEqual(app.get("/app/style.css").status_code, 200)
            self.assertEqual(app.get("/app/api/servers").status_code, 401)
            servers = app.get("/app/api/servers", headers=headers(42)).json()["servers"]
            self.assertEqual(servers, [{"id": 7, "name": "BLACK RUSSIA | BLUE", "short": "BLUE"}])
            self.assertEqual(app.post("/app/api/orders", headers={**headers(42), "Origin": "https://other.test"},
                                      json=payload).status_code, 401)
            self.assertEqual(app.post("/app/api/orders", headers=headers(42),
                                      json={**payload, "interval_seconds": True}).status_code, 400)
            created = app.post("/app/api/orders", headers=headers(42), json=payload)
            self.assertEqual(created.status_code, 201)
            self.assertEqual(created.json(), {"id": "1"})
            listing = app.get("/app/api/orders", headers=headers(42))
            self.assertNotIn("password", listing.text)
            self.assertEqual(listing.json()["orders"][0]["keyword"], "Емеля")
            self.assertEqual(app.get("/app/api/orders", headers=headers(99)).json()["orders"], [])
            self.assertEqual(app.post("/app/api/orders/1/stop", headers=headers(99)).status_code, 404)
            self.assertEqual(app.post("/app/api/orders/1/stop", headers=headers(42)).status_code, 200)
            self.assertEqual(app.get("/app/api/orders", headers=headers(42)).json()["orders"][0]["status"], "stopped")
        self.assertEqual(cipher.decrypt(db.saved[1][1]["password_cipher"]).decode(), payload["password"])


class IdleMovementTests(unittest.TestCase):
    def test_short_walk_between_searches_resorts_market(self):
        now = 0
        walks = []
        sessions = []

        class API:
            def call(self, op, **kwargs):
                return {"stop": False, "created": False}

        class Session(ReplayGame):
            def can_take_step(self):
                return True

            def take_short_walk(self):
                self.check()
                walks.append(now)
                self.assert_market_closed()

            def assert_market_closed(self):
                assert self.sent[-1] == (77, {"c": 1})

        def factory(*args):
            game = Session(*args)
            sessions.append(game)
            return game

        def sleep(seconds):
            nonlocal now
            now += seconds

        runner = OrderRunner(API(), Catalog(), lambda: now >= 670, game_factory=factory,
                             clock=lambda: now, sleep=sleep)
        runner.run({"id": 1, "server_id": 7, "nickname": "Test_Player", "password": "demo",
                    "keyword": "емеля", "min_price": 0, "max_price": 300000,
                    "mode": "notify", "interval_seconds": 30})
        self.assertEqual(walks, [WALK_INTERVAL, 2 * WALK_INTERVAL])
        self.assertEqual(sessions[0].sent.count((77, {"t": 21, "st": 1})), 3)
        self.assertTrue(sessions[0].closed)
