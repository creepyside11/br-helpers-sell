import io
import os
import tempfile
import threading
import tarfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from cryptography.fernet import Fernet
from starlette.testclient import TestClient

from helper.common import MAX_PRICE, Server, parse_integer
from helper.control import create_app
from helper.db import Conflict, Database
from helper.game import GameError, Marketplace, PurchaseUncertain, RakSession, matches
from helper.ssh_install import _archive, install_worker
from helper.telegram import Controller
from helper.worker import OrderRunner


class Catalog:
    def get(self):
        return {7: Server(7, "BLACK RUSSIA | BLUE", "BLUE", "8.8.8.8", 5125)}


class FakeTelegram:
    def __init__(self):
        self.transcript = []

    def send(self, owner, text, keyboard=None):
        self.transcript.append((owner, text, keyboard))

    def call(self, name, **data):
        pass


class FakeDB:
    def __init__(self):
        self.saved = {}

    def initialize(self):
        pass

    def create(self, owner, settings):
        self.saved[1] = (owner, dict(settings))
        return 1

    def order(self, owner, oid):
        record = self.saved.get(oid)
        if record and record[0] == owner:
            return dict(record[1], id=oid, status="queued", stop_requested=False,
                        error=None, found=0, bought=0, partial_results=False)


class TelegramFlowTest(unittest.TestCase):
    def test_order_form_keeps_password_out_of_chat_and_ciphertext(self):
        db = FakeDB()
        cipher = Fernet(Fernet.generate_key())
        tg = FakeTelegram()
        control = Controller(db, Catalog(), cipher, tg, 2, "https://helper.example.com")
        with TestClient(create_app(control, run_telegram=False)) as web:
            control.message(1, "/new", 1)
            draft = control.drafts[1]
            control.callback(1, f"s:{draft.nonce}:7")
            control.message(1, "Test_Player", 2)
            url = tg.transcript[-1][2]["inline_keyboard"][0][0]["url"]
            path = "/" + url.split("/", 3)[-1]
            self.assertEqual(web.get(path).status_code, 200)
            self.assertEqual(web.post(path, data={"password": "test&secret?"}).status_code, 200)
            self.assertEqual(web.get(path).status_code, 404)
            self.assertEqual(web.post(path, data={"password": "second"}).status_code, 400)
            control.message(1, "Емеля", 3)
            control.message(1, "0", 4)
            control.message(1, "300000", 5)
            control.callback(1, f"mode:{draft.nonce}:buy")
            control.message(1, "5", 6)
            control.callback(1, f"go:{draft.nonce}:1")
            control.show_order(9, 1)
            self.assertEqual(tg.transcript[-1][1], "Заказ не найден.")
            self.assertEqual(web.post("/worker/claim", json={}).status_code, 401)
            self.assertEqual(web.post("/worker/pulse", json={"order_id": True},
                                      headers={"Authorization": "Bearer " + "a"*48}).status_code, 400)
        owner, row = db.saved[1]
        self.assertEqual(owner, 1)
        self.assertEqual(row["max_price"], 300000)
        self.assertEqual(row["interval_seconds"], 5)
        self.assertEqual(row["mode"], "buy")
        self.assertEqual(cipher.decrypt(row["password_cipher"]).decode(), "test&secret?")
        self.assertNotIn("test&secret?", repr(tg.transcript))

    def test_bounds_and_admin(self):
        self.assertEqual(parse_integer(str(MAX_PRICE), 0, MAX_PRICE), MAX_PRICE)
        self.assertEqual(parse_integer("60", 5, 60), 60)
        for n in ("0", "4", "61", "5.0"):
            with self.assertRaises(ValueError):
                parse_integer(n, 5, 60)
        control = Controller(FakeDB(), Catalog(), None, FakeTelegram(), 2, "https://helper.example.com")
        control.callback(1, "vps")
        self.assertFalse(control.forms)
        control.callback(2, "vps")
        self.assertEqual(len(control.forms), 1)

    def test_vps_form_accepts_ip_login_and_password(self):
        control = Controller(FakeDB(), Catalog(), None, FakeTelegram(), 2, "https://helper.example.com")
        attempted = threading.Event()
        with patch.object(control, "_install", side_effect=lambda *_: attempted.set()) as install:
            with TestClient(create_app(control, run_telegram=False)) as web:
                control.callback(2, "vps")
                url = control.tg.transcript[-1][2]["inline_keyboard"][0][0]["url"]
                path = "/" + url.split("/", 3)[-1]
                page = web.get(path)
                self.assertEqual(page.status_code, 200)
                self.assertNotIn("fingerprint", page.text)
                fields = {"host": "8.8.8.8", "username": "worker", "password": "fake-password"}
                self.assertEqual(web.post(path, data=fields).status_code, 200)
                self.assertTrue(attempted.wait(2))
                install.assert_called_once_with(2, ("8.8.8.8", "worker", "fake-password"))
                self.assertEqual(web.post(path, data=fields).status_code, 400)

    def test_ssh_install_persists_host_keys_between_calls(self):
        with tempfile.TemporaryDirectory() as data_dir, patch.dict(os.environ, {"DATA_DIR": data_dir}):
            with patch("helper.ssh_install.subprocess.run", return_value=CompletedProcess([], 0, stderr=b"")) as run:
                for _ in range(2):
                    install_worker("8.8.8.8", "worker", "fake-password", "https://helper.example.com", "fixture")
                known_hosts = Path(data_dir).resolve() / "ssh" / "known_hosts"
                self.assertTrue(known_hosts.is_file())
                self.assertEqual(known_hosts.stat().st_mode & 0o777, 0o600)
                for call in run.call_args_list:
                    self.assertIn(f"UserKnownHostsFile={known_hosts}", call.args[0])
                    self.assertIn("StrictHostKeyChecking=accept-new", call.args[0])


class ReplayGame:
    def __init__(self, server=None, nickname=None, check=lambda: None):
        self.check = check
        self.sent = []
        self.replies = []
        self.level = 12
        self.response = {"t": 13, "err": 0}
        self.closed = False
        self.lots = [
            {"id": 17, "dm": "Емеля (12293)", "cs": 420000, "tp": 1},
            {"id": 18, "dm": "Емеля (12293)", "cs": 250000, "tp": 0},
            {"id": 19, "dm": "Емеля (12293)", "cs": 300001, "tp": 0},
        ]

    def login(self, password):
        self.check()

    def close(self):
        self.closed = True

    def open_tablet(self):
        return {"d": {"lv": self.level}, "n": [{"id": 21}]}

    def send(self, iid, payload):
        self.check()
        self.sent.append((iid, payload))
        t = payload.get("t")
        answer = None
        if iid == 113 and payload.get("a") == 21:
            answer = {"o": 1, "n": self.lots}
        elif t in (21, 19):
            answer = {"t": 2, "ls": 2, "n": self.lots}
        elif t == 12:
            lot = next(row for row in self.lots if row["id"] == payload["id"])
            answer = {"t": 12, "cs": lot["cs"], "dm": lot["dm"]}
        elif t == 13:
            answer = self.response
        if answer is not None:
            self.replies.append((77, answer))

    def wait(self, predicate, **kwargs):
        (kwargs.get("on_poll") or self.check)()
        while self.replies:
            val = self.replies.pop(0)
            if predicate(*val):
                return val[1]
        raise GameError("Timeout")


class WorkerStateTest(unittest.TestCase):
    def test_market_capture_sequence_and_grouping(self):
        session = ReplayGame()
        market = Marketplace(session)
        market.open()
        lots, partial = market.search("емеля")
        self.assertTrue(partial)
        [lot] = matches(lots, "ЕМЕЛЯ", 0, 300000)
        self.assertEqual(market.quote(lot, 300000), 250000)
        self.assertTrue(market.buy(lot, on_poll=lambda: None))
        self.assertEqual(session.sent, [
            (113, {"a": 21, "o": 1}), (113, {"c": 1}),
            (77, {"t": 21, "st": 1}), (77, {"t": 19, "s": "емеля"}),
            (77, {"t": 12, "id": 18}), (77, {"id": 18, "ct": 1, "t": 13}),
        ])
        session.response = None
        with self.assertRaises(PurchaseUncertain):
            market.buy(lot, on_poll=lambda: None)

    def test_level_two_and_stop(self):
        level_two = ReplayGame()
        level_two.level = 2
        with self.assertRaisesRegex(GameError, "уровень"):
            Marketplace(level_two).open()
        self.assertFalse(level_two.sent)

        for mode in ("notify", "buy"):
            endpoints = []
            game = None

            class API:
                def call(self, op, **kwargs):
                    endpoints.append((op, kwargs))
                    return {"pulse": {"stop": False}, "observe": {"created": True},
                            "reserve": {"reserved": True}}.get(op, {})

            def factory(server, nickname, check):
                nonlocal game
                game = ReplayGame(server, nickname, check)
                return game

            order = {"id": 1, "server_id": 7, "nickname": "Test_Player", "password": "demo",
                     "keyword": "емеля", "min_price": 0, "max_price": 300000,
                     "mode": mode, "interval_seconds": 5}
            target = "purchase" if mode == "buy" else "observe"
            runner = OrderRunner(API(), Catalog(),
                                 lambda: any(e[0] == target for e in endpoints), game_factory=factory)
            runner.run(order)
            self.assertTrue(game.closed)
            self.assertNotIn("password", order)
            self.assertEqual(endpoints[-1], ("finish", {"order_id": 1, "error": None}))
            self.assertEqual(sum(p.get("t") == 13 for _, p in game.sent), int(mode == "buy"))

    def test_capture_tablet_binary_and_market_reliability(self):
        session = RakSession(Catalog().get()[7], "Test_Player", lambda: None)
        from samp.sender import get_delivery_params

        with patch.object(type(session.session._sender), "send") as send, patch.object(session, "wait"):
            session.open_tablet()
            self.assertEqual(send.call_args.args[0].hex(), "0b9df1407000000020")
        self.assertEqual(get_delivery_params("interface_market")[0], 2)

    def test_package_only_worker(self):
        raw = _archive("https://helper.example.com", "example-worker", "")
        with tarfile.open(fileobj=io.BytesIO(raw)) as package:
            names = package.getnames()
            self.assertIn("helper/worker.py", names)
            self.assertIn("deploy/install_worker.sh", names)
            self.assertNotIn("helper/control.py", names)
            env = package.extractfile("worker.env").read()
            self.assertEqual(package.getmember("worker.env").mode, 0o600)
            self.assertIn(b"WORKER_TOKEN=example-worker", env)
            self.assertNotIn(b"BOT_TOKEN", env)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "test PostgreSQL DSN is unavailable")
class PostgresFlowTest(unittest.TestCase):
    def test_queue_isolation_dedup_and_stop(self):
        db = Database(os.environ["TEST_DATABASE_URL"], Fernet(Fernet.generate_key()))
        db.initialize()
        with db.cursor() as cur:
            cur.execute("TRUNCATE helper_alerts, helper_lots, helper_orders, helper_workers RESTART IDENTITY")
        worker, token = db.enroll("vps")
        _, other_token = db.enroll("other")
        settings = {"server_id": 7, "server_name": "BLUE", "nickname": "Test_Player",
                    "password_cipher": db.cipher.encrypt(b"fixture"), "keyword": "емеля",
                    "mode": "buy", "min_price": 0, "max_price": 300000, "interval_seconds": 5}
        self.assertFalse(db.has_pending_passwords())
        order = db.create(1, settings)
        self.assertTrue(db.has_pending_passwords())
        with self.assertRaises(Conflict):
            db.create(2, settings)
        self.assertEqual(db.claim(token)["password"], "fixture")
        self.assertFalse(db.has_pending_passwords())
        self.assertIsNone(db.claim(other_token))
        self.assertIsNone(db.stop(2, order))
        with self.assertRaises(Conflict):
            db.reserve(other_token, order, 18, 200000)
        self.assertTrue(db.observe(token, order, 18, "Емеля", 200000))
        self.assertFalse(db.observe(token, order, 18, "Емеля", 200000))
        self.assertTrue(db.reserve(token, order, 18, 200000))
        self.assertFalse(db.reserve(token, order, 18, 200000))
        self.assertTrue(db.observe(token, order, 19, "Емеля", 210000))
        self.assertTrue(db.reserve(token, order, 19, 210000))
        db.purchase(token, order, 19, "declined")
        self.assertEqual(db.alerts(), [])
        db.stop(1, order)
        self.assertTrue(db.pulse(token, order))
        db.purchase(token, order, 18, "bought")
        db.purchase(token, order, 18, "bought")
        db.finish(token, order, None)
        self.assertEqual(db.order(1, order)["bought"], 1)
        self.assertEqual(db.order(1, order)["status"], "stopped")
        self.assertEqual(len(db.alerts()), 1)
        stop = threading.Event()
        tg = FakeTelegram()
        control = Controller(db, Catalog(), db.cipher, tg, 2, "https://helper.example.com")
        with patch.object(stop, "wait", side_effect=lambda _: stop.set()):
            control.alarms(stop)
        self.assertEqual(tg.transcript, [(1, f"🛒 Заказ #{order}. Куплен лот «Емеля» за 200 000 ₽. Забери его в пункте выдачи.", None)])
        self.assertEqual(db.alerts(), [])
        with db.cursor() as cur:
            cur.execute("SELECT password_cipher FROM helper_orders WHERE id=%s", (order,))
            self.assertIsNone(cur.fetchone()["password_cipher"])
        db.disable(worker)
        with self.assertRaises(PermissionError):
            db.claim(token)
