import json
import os
from pathlib import Path
import socket
import threading
import time

import uvicorn
from cryptography.fernet import Fernet
from playwright.sync_api import sync_playwright, expect

from helper.control import create_app
from helper.telegram import Controller
from test_helper import Catalog, FakeTelegram
from test_miniapp import MiniDB, TOKEN, init_data

s = socket.socket()
s.bind(("127.0.0.1", 0))
port = s.getsockname()[1]
url = f"http://127.0.0.1:{port}"
cipher = Fernet(Fernet.generate_key())
controller = Controller(MiniDB(), Catalog(), cipher, FakeTelegram(), 2, url)
controller.message(84, "/new", 1)
draft = controller.drafts[84]
controller.callback(84, f"s:{draft.nonce}:7")
controller.message(84, "Demo_Player", 2)
game_password_url = controller.tg.transcript[-1][2]["inline_keyboard"][0][0]["url"]
server = uvicorn.Server(uvicorn.Config(create_app(controller, run_telegram=False, bot_token=TOKEN),
                                       access_log=False, log_level="error"))
thread = threading.Thread(target=server.run, kwargs={"sockets": [s]}, daemon=True)
thread.start()
for _ in range(100):
    if server.started:
        break
    time.sleep(.02)

try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        screenshots = Path(os.environ.get("MINIAPP_SCREENSHOTS", "/tmp/rakhelper-miniapp"))
        screenshots.mkdir(parents=True, exist_ok=True)
        ctx = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda ex: errors.append(str(ex)))
        tg = {"initData": init_data(42), "colorScheme": "dark"}
        page.route("https://telegram.org/js/telegram-web-app.js",
                   lambda route: route.fulfill(body="window.Telegram=" + json.dumps({"WebApp": tg}) + ";", content_type="text/javascript"))
        page.goto(url + "/app")
        expect(page.locator("#server")).to_be_enabled()
        page.screenshot(path=screenshots / "mobile.png", full_page=True)
        page.get_by_label("Предмет или ключевое слово").fill("Емеля")
        page.get_by_label("Цена до").fill("300000")
        page.get_by_label("Сервер", exact=True).select_option("7")
        page.get_by_label("Ник", exact=True).fill("Demo_Player")
        page.get_by_label("Пароль от аккаунта").fill("fixture-password")
        page.get_by_role("button", name="Показать").click()
        expect(page.locator("#password")).to_have_attribute("type", "text")
        page.get_by_role("button", name="Скрыть").click()
        page.get_by_text("Автопокупка", exact=True).click()
        page.get_by_role("button", name="Запустить поиск").click()
        expect(page.get_by_role("dialog")).to_be_visible()
        page.get_by_role("button", name="Подтвердить").click()
        expect(page.get_by_role("heading", name="Емеля")).to_be_visible()
        page.get_by_role("button", name="Остановить", exact=True).click()
        page.get_by_role("dialog").get_by_role("button", name="Остановить", exact=True).click()
        expect(page.get_by_text("Остановлен", exact=True)).to_be_visible()
        page.screenshot(path=screenshots / "orders.png", full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth === innerWidth")
        assert not errors, errors
        print("Chromium mobile: каталог, пароль, диалог автопокупки, создание заказа и остановка прошли")
        desktop = browser.new_page(viewport={"width": 1280, "height": 950})
        tg["colorScheme"] = "light"
        desktop.route("https://telegram.org/js/telegram-web-app.js",
                      lambda route: route.fulfill(body="window.Telegram=" + json.dumps({"WebApp": tg}) + ";", content_type="text/javascript"))
        desktop.goto(url + "/app")
        expect(desktop.locator("#server")).to_be_enabled()
        desktop.screenshot(path=screenshots / "desktop.png", full_page=True)
        expect(desktop.locator("html")).to_have_attribute("data-theme", "light")
        desktop.set_viewport_size({"width": 320, "height": 790})
        assert desktop.evaluate("document.documentElement.scrollWidth === innerWidth")
        print("Chromium desktop and 320 px: light theme and no horizontal overflow")
        form_page = ctx.new_page()
        form_page.goto(game_password_url)
        form_page.get_by_label("Пароль").fill("fixture-game-password")
        with form_page.expect_response(lambda response: response.request.method == "POST"
                                       and response.url == game_password_url) as submitted:
            form_page.get_by_role("button", name="Отправить").click()
        assert submitted.value.request.header_value("origin") == "null"
        assert submitted.value.status == 200
        expect(form_page.get_by_text("Пароль сохранён. Вернись в Telegram и введи название предмета.")).to_be_visible()
        assert draft.phase == "keyword"
        print("Chromium game account form: native POST with Origin null saved the password")
        browser.close()
finally:
    server.should_exit = True
    thread.join(3)
