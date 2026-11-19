"""Single-account adapter for the Black Russia tablet and marketplace."""
import math
import queue
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

TABLET = 113
MARKET = 77
AUTH = 38
SPAWN = 50
TABLET_BUTTON = 21
MARKET_SORT = {"t": 21, "st": 1}
REPLY_TIMEOUT = 8
SALE_TYPE = 0


class GameError(Exception):
    pass


class Stopped(GameError):
    pass


class PurchaseUncertain(GameError):
    pass


@dataclass(frozen=True)
class Lot:
    id: int
    name: str
    price: int
    type: int

    @classmethod
    def parse(cls, row):
        if not isinstance(row, dict):
            return None
        lot_id, price, kind, name = (row.get(k) for k in ("id", "cs", "tp", "dm"))
        if (type(lot_id) is not int or not 0 < lot_id < 2**63
                or type(price) is not int or not 0 <= price < 2**63
                or type(kind) is not int or not isinstance(name, str)):
            return None
        name = " ".join(name.split())[:200]
        return cls(lot_id, name, price, kind) if name else None


def folded(value):
    return unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")


def matches(lots, keyword, minimum, maximum):
    key = folded(keyword)
    rows = {lot.id: lot for lot in lots if lot.type == SALE_TYPE
            and minimum <= lot.price <= maximum and key in folded(lot.name)}
    return sorted(rows.values(), key=lambda lot: (lot.price, lot.id))


class _SessionHooks:
    def ignore(self, *args):
        pass

    autopickup_tick = reset_api_caches = _emit_coord_move_end = ignore
    _handle_coord_movement_dialog = _set_reward_cache = _set_case_reward_cache = ignore


class RakSession:
    """Runs only transport, login and the event feed needed by this worker."""

    def __init__(self, server, nickname, check):
        transport_path = str(Path(__file__).resolve().parents[1] / "runtime")
        if transport_path not in sys.path:
            sys.path.insert(0, transport_path)
        from core import logger, security
        from core.network import Network
        from core.protocol import RakNetProtocol
        from logic.json_ui import JsonUIConfig
        from samp.session import SampSession
        from samp.sync import PlayerSyncScheduler

        logger.set_log_interceptor(lambda message: False)
        logger.configure(info=False, debug=False, packet=False, event=False, chat=False, dialog=False)
        network = Network(server.host, server.port)
        protocol = RakNetProtocol(network, security)
        sync = PlayerSyncScheduler(rate_player=0.5, rate_player_min=0.5, rate_player_max=0.75)
        self.session = SampSession(
            protocol, server.host, server.port, nickname, sync, fps=30,
            http_server_names={server.host: server.short},
            json_ui=JsonUIConfig(console_output=False, auto_close_cinematic=False),
        )
        self.session.attach_api(_SessionHooks())
        self.session._emit_callback = self._on_event
        network.set_proxy_manager(self.session.proxy)
        self.events = queue.Queue(maxsize=256)
        self.check = check

    def _on_event(self, event, *args, **kwargs):
        item = None
        if event in ("OnSetPosition", "onShowDialog", "onKick", "disconnect"):
            self.session.players.bot.coord_movement.cancel(reason=event)
        if event == "onReceiveJSON" and args[0] in (AUTH, SPAWN, TABLET, MARKET, 26):
            item = (args[0], args[1])
        elif event in ("disconnect", "onKick"):
            item = (None, {})
        if item is not None:
            try:
                self.events.put_nowait(item)
            except queue.Full:
                self.session.connected = False

    def send(self, interface, payload, *, packet_name="interface_market"):
        self.check()
        if not self.session.send_json(interface, payload, packet_name=packet_name):
            raise GameError("Игровая сессия закрыта.")

    def wait(self, predicate, timeout=REPLY_TIMEOUT, on_poll=None):
        end = time.monotonic() + timeout
        while (remaining := end - time.monotonic()) > 0:
            (on_poll or self.check)()
            if not self.session.connected:
                raise GameError("Сервер отключил игровой аккаунт.")
            try:
                interface, payload = self.events.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                continue
            if interface is None:
                raise GameError("Сервер отключил игровой аккаунт.")
            if interface == 26 and (payload.get("o") == 1 or isinstance(payload.get("u"), str)):
                raise GameError("Сервер запросил капчу. Заказ остановлен.")
            if predicate(interface, payload):
                return payload
        raise GameError("Игровой сервер не подтвердил действие вовремя.")

    def login(self, password):
        self.check()
        if not self.session.connect(check=self.check):
            raise GameError("Не удалось подключиться к игровому серверу.")
        auth = self.wait(lambda iid, data: iid == AUTH and data.get("o") == 1, timeout=25)
        if auth.get("r") != 1:
            raise GameError("Нужен существующий игровой аккаунт.")
        self.send(AUTH, {"t": 6, "s": password, "r": 0}, packet_name="interface_auth_password")
        event = self.wait(
            lambda iid, data: (iid == SPAWN and (data.get("m") or data.get("s")))
            or (iid == AUTH and data.get("t") == 3), timeout=30,
        )
        if "t" in event and event["t"] == 3:
            raise GameError("Сервер запросил дополнительный шаг входа.")
        available = event.get("m")
        if isinstance(available, list) and available and type(available[0]) is int:
            # In the recorded client, selecting spawn #1 is {"t":1}.
            # The inherited client format also includes the selected ID in s.
            payload = {"t": 1} if available[0] == 1 else {"t": 1, "s": available[0]}
            self.send(SPAWN, payload, packet_name="interface_spawn_select")
            self.wait(lambda iid, data: iid == SPAWN and data.get("s") == 2, timeout=22)
        elif event.get("s") != 2:
            self.wait(lambda iid, data: iid == SPAWN and data.get("s") == 2, timeout=22)
        until = time.monotonic() + 2
        while time.monotonic() < until:
            self.check()
            time.sleep(min(0.2, max(0, until - time.monotonic())))

    def open_tablet(self):
        from samp.constants import PacketID
        from samp.packets import RPCBuilder

        self.check()
        # Tablet button in the supplied trace: standard interaction RPC, 40-bit body.
        packet = bytes((PacketID.RPC,)) + RPCBuilder.create(413, b"\x0e\0\0\0\x04")
        self.session._sender.send(packet, rpc_name="picked_up_interact")
        return self.wait(lambda iid, data: iid == TABLET and data.get("o") == 1)

    def can_take_step(self):
        player = self.session.players.bot
        x, y, z = player.get_position()
        return (
            self.session.connected and self.session.sync.enabled and player.is_onfoot()
            and player.health > 0 and all(map(math.isfinite, (x, y, z, player.rotation)))
            and math.hypot(x, y) > 1 and -5 < z < 400
            and player.coord_movement.get_active() is None
        )

    def take_short_walk(self):
        if not self.can_take_step():
            return
        player = self.session.players.bot
        origin = player.get_position()
        angle = math.radians(player.rotation)
        target = (origin[0] - math.sin(angle) * .72, origin[1] + math.cos(angle) * .72, origin[2])
        for destination in (target, origin):
            self.check()
            if not self.can_take_step():
                return
            movement = player.coord_movement
            state, _ = movement.start(
                destination, mode="walk", nominal_interval=.5, overrides={
                    "coord_delay": .45, "accel_time": .2, "brake_time": 0.0,
                    "coord_fluctuation_max": 0.0, "movement_direction_control": (False, True, 0),
                    "stop_on_dialog": True,
                },
            )
            movement_id = state["movement_id"]
            end = time.monotonic() + 4.5
            try:
                while time.monotonic() < end:
                    self.check()
                    if not self.session.connected:
                        raise GameError("Сервер отключил игровой аккаунт.")
                    result = movement.get_result(movement_id)
                    if result is not None:
                        if result["status"] != "arrived":
                            return
                        break
                    time.sleep(.1)
                else:
                    return
            finally:
                movement.cancel(movement_id)

    def close(self):
        self.session.players.bot.coord_movement.cancel(reason="disconnect")
        self.session.disconnect()


class Marketplace:
    def __init__(self, game):
        self.game = game

    def open(self):
        tablet = self.game.open_tablet()
        stats = tablet.get("d")
        level = stats.get("lv") if isinstance(stats, dict) else None
        if type(level) is not int or level < 3:
            raise GameError("Планшет не подтвердил уровень 3 или выше.")
        entries = tablet.get("n")
        if not isinstance(entries, list) or not any(
            isinstance(row, dict) and row.get("id") == TABLET_BUTTON for row in entries
        ):
            raise GameError("Маркет недоступен этому аккаунту.")
        self.game.send(TABLET, {"a": TABLET_BUTTON, "o": 1})
        self.game.send(TABLET, {"c": 1})
        self.game.wait(lambda iid, data: iid == MARKET and data.get("o") == 1)
        self.game.send(MARKET, MARKET_SORT)
        self._page()

    def walk_break(self):
        if self.game.can_take_step():
            self.game.send(MARKET, {"c": 1})
            self.game.take_short_walk()
            self.open()

    def _page(self):
        return self.game.wait(lambda iid, data: iid == MARKET and data.get("t") == 2
                              and isinstance(data.get("n"), list))

    def search(self, keyword):
        self.game.send(MARKET, {"t": 19, "s": keyword})
        page = self._page()
        count = page.get("ls")
        # Captured searches establish only page 1. Never switch to the
        # unfiltered catalogue based on an unverified page index.
        return [lot for raw in page["n"] if (lot := Lot.parse(raw))], type(count) is int and count > 1

    def quote(self, lot, maximum):
        self.game.send(MARKET, {"t": 12, "id": lot.id})
        data = self.game.wait(lambda iid, data: iid == MARKET and data.get("t") == 12)
        price = data.get("cs")
        if (data.get("err", 0) != 0 or folded(str(data.get("dm", ""))) != folded(lot.name)
                or type(price) is not int or price != lot.price or price > maximum):
            return None
        return price

    def buy(self, lot, *, on_poll):
        self.game.send(MARKET, {"t": 13, "id": lot.id, "ct": 1})
        try:
            data = self.game.wait(lambda iid, data: iid == MARKET and data.get("t") == 13,
                                  timeout=REPLY_TIMEOUT, on_poll=on_poll)
        except GameError as exc:
            raise PurchaseUncertain("Ответ на покупку потерялся. Проверь покупки в игре.") from exc
        if type(data.get("err")) is not int:
            raise PurchaseUncertain("Сервер прислал неполное подтверждение покупки.")
        return data["err"] == 0
