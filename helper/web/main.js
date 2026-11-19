"use strict";

const $ = (id) => document.getElementById(id);
const tg = window.Telegram?.WebApp;
const historyState = { rows: [], next: null, loading: false };
const statusNames = { queued: "В очереди", running: "Работает", stopped: "Остановлен", error: "Ошибка" };
const modeNames = { notify: "Уведомления", buy: "Автопокупка" };
const money = new Intl.NumberFormat("ru-RU");
let tab = "new";
let toastTimer;

function element(tag, text, className) {
  const item = document.createElement(tag);
  if (text !== undefined) item.textContent = text;
  if (className) item.className = className;
  return item;
}

async function api(path, options = {}) {
  const headers = { Authorization: "tma " + tg.initData };
  if (options.body) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch("/app/api/" + path, { ...options, headers, cache: "no-store" });
  } catch {
    throw new Error("Нет связи с сервисом. Попробуй ещё раз.");
  }
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error("Сервис временно недоступен.");
  }
  if (!response.ok) throw new Error(data.error || "Не удалось выполнить действие.");
  return data;
}

function status(id, message) {
  const target = $(id);
  target.textContent = message;
  target.hidden = !message;
}

function toast(message) {
  clearTimeout(toastTimer);
  status("toast", message);
  toastTimer = setTimeout(() => status("toast", ""), 3500);
}

function theme() {
  document.documentElement.dataset.theme = tg?.colorScheme === "light" ? "light" : "dark";
  const background = tg?.colorScheme === "light" ? "#f2f3ed" : "#111213";
  try {
    tg?.setHeaderColor?.(background);
    tg?.setBackgroundColor?.(background);
  } catch {
    // Older Telegram clients can still use the page theme.
  }
}

function show(which) {
  tab = which;
  for (const name of ["new", "orders"]) {
    $(name + "-tab").setAttribute("aria-pressed", String(which === name));
    $(name + "-panel").hidden = which !== name;
  }
  window.scrollTo(0, 0);
  if (which === "orders") void loadOrders();
}

async function loadServers() {
  const select = $("server");
  select.disabled = true;
  $("submit-order").disabled = true;
  $("retry-servers").hidden = true;
  select.replaceChildren(new Option("Загружаем серверы…", ""));
  try {
    const data = await api("servers");
    if (!data.servers.length) throw new Error("Серверы временно недоступны.");
    select.replaceChildren(new Option("Выбери сервер", ""));
    for (const server of data.servers) {
      select.add(new Option(server.id + " · " + server.short, String(server.id)));
    }
    select.disabled = false;
    $("submit-order").disabled = false;
    status("form-error", "");
  } catch (error) {
    select.replaceChildren(new Option("Список недоступен", ""));
    $("retry-servers").hidden = false;
    status("form-error", error.message);
  }
}

function confirmAction(title, detail, action) {
  const dialog = $("confirm-dialog");
  if (typeof dialog.showModal !== "function") {
    return Promise.resolve(window.confirm(title + "\n" + detail));
  }
  $("confirm-title").textContent = title;
  $("confirm-detail").textContent = detail;
  $("confirm-action").textContent = action;
  dialog.returnValue = "cancel";
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "yes"), { once: true });
    dialog.showModal();
  });
}

async function submit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $("submit-order");
  if (button.disabled) return;
  status("form-error", "");
  const data = Object.fromEntries(new FormData(form));
  for (const key of ["server_id", "min_price", "max_price", "interval_seconds"]) {
    data[key] = Number(data[key]);
  }
  data.nickname = data.nickname.trim();
  data.keyword = data.keyword.trim();
  if (!data.keyword || data.min_price > data.max_price) {
    status("form-error", "Проверь название предмета и границы цены.");
    return;
  }
  if (data.mode === "buy" && !await confirmAction(
    "Включить автопокупку?",
    "Бот будет покупать по одной штуке каждого подходящего лота в заданном диапазоне до остановки заказа.",
    "Подтвердить",
  )) return;
  button.disabled = true;
  button.textContent = "Создаём заказ…";
  try {
    const result = await api("orders", { method: "POST", body: JSON.stringify(data) });
    form.reset();
    $("password").type = "password";
    $("toggle-password").textContent = "Показать";
    $("toggle-password").setAttribute("aria-pressed", "false");
    $("interval-value").textContent = $("interval").value + " сек.";
    show("orders");
    toast("Заказ #" + result.id + " создан");
  } catch (error) {
    status("form-error", error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Запустить поиск";
  }
}

function orderCard(order) {
  const card = element("article", undefined, "card order-card");
  const stopping = order.status === "running" && order.stop_requested;
  const top = element("div", undefined, "order-top");
  const heading = element("div");
  heading.append(element("span", "Заказ #" + order.id, "order-id"), element("h2", order.keyword));
  const statusClass = order.status === "error" ? " error-status" : order.status === "running" ? " running" : "";
  top.append(heading, element("span", stopping ? "Останавливается" : statusNames[order.status], "status" + statusClass));
  card.append(top,
    element("p", order.server_name + " · " + order.nickname, "hint"),
    element("p", money.format(order.min_price) + " – " + money.format(order.max_price) + " ₽", undefined),
    element("p", modeNames[order.mode] + " · каждые " + order.interval_seconds + " сек.", "hint"));
  const statistics = element("div", undefined, "order-stats");
  for (const [count, label] of [[order.found, "Найдено"], [order.bought, "Куплено"]]) {
    const item = element("span");
    item.append(element("b", money.format(count)), document.createTextNode(label));
    statistics.append(item);
  }
  card.append(statistics);
  if (order.partial_results) card.append(element("p", "Проверяется первая страница поиска.", "hint"));
  if (order.error) card.append(element("p", order.error, "error"));
  if (["queued", "running"].includes(order.status) && !order.stop_requested) {
    const stop = element("button", "Остановить", "secondary stop");
    stop.type = "button";
    stop.addEventListener("click", async () => {
      if (!await confirmAction("Остановить заказ #" + order.id + "?", "Воркер завершит работу по этому заказу.", "Остановить")) return;
      stop.disabled = true;
      try {
        await api("orders/" + order.id + "/stop", { method: "POST" });
        toast("Запрошена остановка");
        await loadOrders();
      } catch (error) {
        toast(error.message);
      } finally {
        stop.disabled = false;
      }
    });
    card.append(stop);
  }
  return card;
}

function renderOrders() {
  const list = $("order-list");
  const rows = historyState.rows;
  list.replaceChildren(...rows.map(orderCard));
  if (!rows.length) {
    const empty = element("div", undefined, "card empty");
    const button = element("button", "Создать заказ", "primary");
    button.type = "button";
    button.addEventListener("click", () => show("new"));
    empty.append(element("h2", "Заказов пока нет"), element("p", "Добавь предмет, чтобы начать поиск."), button);
    list.append(empty);
  }
  $("more-orders").hidden = !historyState.next;
}

async function loadOrders(more = false, quiet = false) {
  if (historyState.loading || (more && !historyState.next)) return;
  historyState.loading = true;
  $("refresh-orders").disabled = true;
  $("more-orders").disabled = true;
  if (!quiet) status("orders-feedback", "Обновляем заказы…");
  try {
    const data = await api(more ? "orders?before=" + historyState.next : "orders");
    historyState.rows = more ? historyState.rows.concat(data.orders) : data.orders;
    historyState.next = data.next;
    renderOrders();
    status("orders-feedback", "");
  } catch (error) {
    status("orders-feedback", error.message);
  } finally {
    historyState.loading = false;
    $("refresh-orders").disabled = false;
    $("more-orders").disabled = false;
  }
}

theme();
if (tg?.initData) {
  tg.ready?.();
  tg.expand?.();
  tg.onEvent?.("themeChanged", theme);
  $("gate").hidden = true;
  $("workspace").hidden = false;
  $("new-tab").addEventListener("click", () => show("new"));
  $("orders-tab").addEventListener("click", () => show("orders"));
  $("retry-servers").addEventListener("click", loadServers);
  $("order-form").addEventListener("submit", submit);
  $("refresh-orders").addEventListener("click", () => loadOrders());
  $("more-orders").addEventListener("click", () => loadOrders(true));
  $("interval").addEventListener("input", () => {
    $("interval-value").textContent = $("interval").value + " сек.";
  });
  $("toggle-password").addEventListener("click", () => {
    const field = $("password");
    const reveal = field.type === "password";
    field.type = reveal ? "text" : "password";
    $("toggle-password").textContent = reveal ? "Скрыть" : "Показать";
    $("toggle-password").setAttribute("aria-pressed", String(reveal));
  });
  void loadServers();
  setInterval(() => {
    const active = document.activeElement;
    if (tab === "orders" && !document.hidden && !$("confirm-dialog").open &&
        !$("order-list").contains(active) && historyState.rows.length <= 12) {
      void loadOrders(false, true);
    }
  }, 7000);
}
