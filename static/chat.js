const $ = (id) => document.getElementById(id);
const state = {
  user: null,
  rooms: [],
  selection: null,
  drafts: new Map(),
  socket: null,
  retry: 750,
  sending: false,
};
const timeFormat = new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" });
const dateFormat = new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeStyle: "short" });

async function api(url, body) {
  const options = body ? {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  } : {};
  const response = await fetch(url, options);
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(typeof error.detail === "string" ? error.detail : "Не удалось выполнить запрос");
  }
  return response.json();
}

function element(tag, className, value) {
  const node = document.createElement(tag);
  node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}

function initials(name) {
  return name.split(/\s+/).slice(0, 2).map((part) => Array.from(part)[0]).join("").toUpperCase();
}

function openApp(user) {
  state.user = user;
  $("profile-name").textContent = user.name;
  $("profile-avatar").textContent = initials(user.name);
  $("welcome").hidden = true;
  $("app").hidden = false;

  for (const room of state.rooms) {
    const button = element("button", "room-button");
    button.type = "button";
    button.dataset.roomId = room.id;
    const symbol = element("span", "room-symbol", "#");
    symbol.setAttribute("aria-hidden", "true");
    button.append(symbol, element("span", "", room.title));
    button.addEventListener("click", () => selectRoom(room.id));
    $("rooms").append(button);
  }
  const previous = localStorage.getItem("campus.room");
  selectRoom(state.rooms.find((room) => room.id === previous)?.id ?? state.rooms[0].id);
  connect();
}

function selectRoom(roomId) {
  if (state.selection?.roomId === roomId) return;

  const room = state.rooms.find((item) => item.id === roomId);
  const selection = { roomId, messages: new Map(), hasOlder: false, awaitingPresence: false };
  state.selection = selection;
  localStorage.setItem("campus.room", roomId);

  for (const button of $("rooms").children) {
    button.setAttribute("aria-current", String(button.dataset.roomId === roomId));
  }
  $("room-title").textContent = room.title;
  $("room-description").textContent = room.description;
  $("chat-error").textContent = "";
  $("message").value = state.drafts.get(roomId)?.text ?? "";
  $("messages").replaceChildren();
  $("older").hidden = true;
  $("older").disabled = false;
  $("empty").hidden = false;
  $("empty").textContent = "Загружаем сообщения…";
  $("timeline").scrollTop = 0;
  setPresence("Подключение…");
  if (!subscribe()) loadLatest(selection);
}

function setPresence(text, connected = false) {
  $("presence").textContent = text;
  $("presence").classList.toggle("connected", connected);
}

function subscribe() {
  if (state.socket?.readyState !== WebSocket.OPEN) return false;
  state.selection.awaitingPresence = true;
  state.socket.send(state.selection.roomId);
  return true;
}

function connect() {
  const url = new URL("/ws", location.href);
  url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(url);
  state.socket = socket;

  socket.addEventListener("open", () => {
    state.retry = 750;
    subscribe();
  });
  socket.addEventListener("close", () => {
    setPresence("Переподключение…");
    setTimeout(connect, state.retry);
    state.retry = Math.min(state.retry * 2, 8000);
  });
  socket.addEventListener("message", (event) => {
    const data = JSON.parse(event.data);
    const selection = state.selection;
    if (data.type === "presence" && data.room_id === selection.roomId) {
      setPresence(`В чате: ${data.count}`, true);
      if (selection.awaitingPresence) {
        selection.awaitingPresence = false;
        loadLatest(selection);
      }
    }
    if (data.type === "message") receiveMessage(data.message);
  });
}

function messageUrl(roomId) {
  return `/api/rooms/${encodeURIComponent(roomId)}/messages`;
}

function receiveMessage(message) {
  const selection = state.selection;
  if (selection.roomId !== message.room_id || selection.messages.has(message.id)) return;

  const timeline = $("timeline");
  const nearBottom = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 90;
  selection.messages.set(message.id, message);
  renderMessages(selection);
  if (nearBottom || message.sender_id === state.user.id) timeline.scrollTop = timeline.scrollHeight;
}

async function loadLatest(selection) {
  const request = Symbol();
  selection.latestRequest = request;
  const previousIds = new Set(selection.messages.keys());
  try {
    const data = await api(messageUrl(selection.roomId));
    if (selection !== state.selection || selection.latestRequest !== request) return;

    const overlaps = data.messages.some((message) => previousIds.has(message.id));
    if (!overlaps && data.has_older) {
      for (const id of previousIds) selection.messages.delete(id);
    }
    if (!overlaps || !previousIds.size) selection.hasOlder = data.has_older;
    for (const message of data.messages) selection.messages.set(message.id, message);
    renderMessages(selection);
    $("timeline").scrollTop = $("timeline").scrollHeight;
  } catch {
    if (selection !== state.selection || selection.latestRequest !== request) return;
    if (!selection.messages.size) $("empty").textContent = "История пока недоступна.";
    $("chat-error").textContent = "Не удалось загрузить историю.";
  }
}

$("older").addEventListener("click", async () => {
  const selection = state.selection;
  const timeline = $("timeline");
  $("older").disabled = true;
  try {
    const oldest = [...selection.messages.keys()].reduce((first, id) => Math.min(first, id), Infinity);
    const data = await api(`${messageUrl(selection.roomId)}?before=${oldest}`);
    if (selection !== state.selection) return;

    const firstMessage = $("messages").firstElementChild;
    const previousTop = firstMessage.getBoundingClientRect().top;
    for (const message of data.messages) selection.messages.set(message.id, message);
    selection.hasOlder = data.has_older;
    renderMessages(selection);
    const anchor = $("messages").querySelector(`[data-message-id="${oldest}"]`);
    timeline.scrollTop += anchor.getBoundingClientRect().top - previousTop;
  } catch {
    if (selection === state.selection) $("chat-error").textContent = "Не удалось загрузить предыдущие сообщения.";
  } finally {
    if (selection === state.selection) $("older").disabled = false;
  }
});

function renderMessages(selection) {
  const messages = [...selection.messages.values()].sort((a, b) => a.id - b.id);
  $("empty").hidden = messages.length > 0;
  $("empty").textContent = "Здесь пока нет сообщений. Начните разговор.";
  $("older").hidden = !selection.hasOlder;
  $("messages").replaceChildren(...messages.map(renderMessage));
}

function renderMessage(message) {
  const own = message.sender_id === state.user.id;
  const row = element("li", own ? "message-row own" : "message-row");
  row.dataset.messageId = message.id;
  const avatar = element("span", "avatar", initials(message.sender_name));
  avatar.setAttribute("aria-hidden", "true");
  const card = element("div", "message-card");
  const meta = element("div", "message-meta");
  const date = new Date(message.created_at);
  const stamp = element("time", "", timeFormat.format(date));
  stamp.dateTime = message.created_at;
  stamp.title = dateFormat.format(date);
  meta.append(element("strong", "", own ? "Вы" : message.sender_name), stamp);
  card.append(meta, element("p", "message-text", message.text));
  if (!own) row.append(avatar);
  row.append(card);
  return row;
}

$("message").addEventListener("input", () => {
  state.drafts.set(state.selection.roomId, { text: $("message").value });
});
$("message").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $("composer").requestSubmit();
  }
});
$("composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  const selection = state.selection;
  const input = $("message");
  const text = input.value.trim();
  if (!text || state.sending) return;

  const draft = state.drafts.get(selection.roomId) ?? { text: input.value };
  draft.requestId ??= Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) =>
    byte.toString(16).padStart(2, "0")).join("");
  state.drafts.set(selection.roomId, draft);
  state.sending = true;
  $("send").disabled = true;
  $("chat-error").textContent = "";
  try {
    const message = await api(messageUrl(selection.roomId), { text, request_id: draft.requestId });
    receiveMessage(message);
    if (state.drafts.get(selection.roomId) === draft) {
      state.drafts.delete(selection.roomId);
      if (selection.roomId === state.selection.roomId) input.value = "";
    }
  } catch (error) {
    if (selection === state.selection) $("chat-error").textContent = error.message;
  } finally {
    state.sending = false;
    $("send").disabled = false;
  }
});

$("profile-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button");
  const name = $("name").value.trim();
  $("startup-error").textContent = "";
  if (name.length < 2) {
    $("startup-error").textContent = "Укажите имя из двух или более символов.";
    return;
  }
  button.disabled = true;
  try {
    openApp(await api("/api/profile", { name }));
  } catch (error) {
    $("startup-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

async function start() {
  try {
    const data = await api("/api/bootstrap");
    state.rooms = data.rooms;
    if (data.user) openApp(data.user);
    else {
      $("profile-form").hidden = false;
      $("name").focus();
    }
  } catch {
    $("startup-error").textContent = "Не удалось открыть приложение. Обновите страницу, чтобы повторить попытку.";
  } finally {
    $("starting").hidden = true;
  }
}

start();
