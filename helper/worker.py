import json
import os
import signal
import time
import urllib.error
import urllib.request

from .common import ServerCatalog, public_https
from .game import GameError, Marketplace, PurchaseUncertain, RakSession, Stopped, matches


class WorkerAPI:
    def __init__(self, url, token):
        self.url = public_https(url)
        self.token = token

    def call(self, endpoint, **body):
        request = urllib.request.Request(
            self.url + "/worker/" + endpoint,
            data=json.dumps(body, ensure_ascii=False).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        # Never forward the bearer token to a redirected host.
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=5) as response:
            return json.loads(response.read(65536))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


WALK_INTERVAL = 5 * 60 + 30


class OrderRunner:
    def __init__(self, api, catalog, shutting_down=lambda: False, game_factory=RakSession,
                 *, clock=time.monotonic, sleep=time.sleep):
        self.api = api
        self.catalog = catalog
        self.shutting_down = shutting_down
        self.game_factory = game_factory
        self.clock = clock
        self.sleep = sleep

    def run(self, order):
        order_id = order["id"]
        pulse_at = 0.0
        session = None
        error = None

        def call(endpoint, **body):
            return self.api.call(endpoint, order_id=order_id, **body)

        def heartbeat(ignore_stop=False, force=False, partial=None):
            nonlocal pulse_at
            if self.shutting_down() and not ignore_stop:
                raise Stopped()
            if force or self.clock() >= pulse_at:
                try:
                    stop = call("pulse", partial=partial)["stop"]
                except (OSError, ValueError, KeyError) as exc:
                    if ignore_stop:
                        return
                    raise GameError("Связь с панелью потеряна. Заказ остановлен.") from exc
                pulse_at = self.clock() + 2
                if stop and not ignore_stop:
                    raise Stopped()

        def walk_if_due():
            nonlocal next_walk
            if self.clock() >= next_walk:
                heartbeat(force=True)
                market.walk_break()
                next_walk = self.clock() + WALK_INTERVAL

        try:
            heartbeat(force=True)
            server = self.catalog.get().get(order["server_id"])
            if server is None:
                raise GameError("Игровой сервер больше не найден в каталоге.")
            session = self.game_factory(server, order["nickname"], heartbeat)
            session.login(order.pop("password"))
            market = Marketplace(session)
            market.open()
            next_walk = self.clock() + WALK_INTERVAL
            while True:
                heartbeat(force=True)
                walk_if_due()
                next_search = self.clock() + order["interval_seconds"]
                lots, partial = market.search(order["keyword"])
                heartbeat(force=True, partial=partial)
                for lot in matches(lots, order["keyword"], order["min_price"], order["max_price"]):
                    heartbeat()
                    new = call("observe", lot_id=lot.id, name=lot.name, price=lot.price)["created"]
                    if not new or order["mode"] != "buy":
                        continue
                    price = market.quote(lot, order["max_price"])
                    if price is None:
                        continue
                    if not call("reserve", lot_id=lot.id, price=price)["reserved"]:
                        continue
                    try:
                        success = market.buy(lot, on_poll=lambda: heartbeat(ignore_stop=True))
                    except PurchaseUncertain:
                        call("purchase", lot_id=lot.id, outcome="uncertain")
                        raise
                    except Stopped:
                        call("purchase", lot_id=lot.id, outcome="declined")
                        raise
                    call("purchase", lot_id=lot.id, outcome="bought" if success else "declined")
                while self.clock() < next_search:
                    heartbeat()
                    walk_if_due()
                    self.sleep(min(0.25, max(0.0, next_search - self.clock())))
        except Stopped:
            pass
        except GameError as exc:
            error = str(exc)
        except (OSError, ValueError, KeyError, RuntimeError):
            error = "Ошибка VPS или подключения. Проверь состояние заказа и покупки в игре."
        finally:
            order.pop("password", None)
            try:
                if session is not None:
                    session.close()
            finally:
                call("finish", error=error)


def main():
    api = WorkerAPI(os.environ["WORKER_URL"], os.environ["WORKER_TOKEN"])
    catalog = ServerCatalog()
    stopped = False

    def quit_worker(*_args):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, quit_worker)
    signal.signal(signal.SIGINT, quit_worker)
    runner = OrderRunner(api, catalog, lambda: stopped)
    interval = 2
    while not stopped:
        try:
            job = api.call("claim")["order"]
            if job:
                runner.run(job)
            interval = 2
        except (OSError, ValueError, KeyError) as exc:
            print(f"Worker control unavailable ({type(exc).__name__})", flush=True)
            interval = min(interval * 2, 30)
        deadline = time.monotonic() + interval
        while not stopped and time.monotonic() < deadline:
            time.sleep(.25)


if __name__ == "__main__":
    main()
