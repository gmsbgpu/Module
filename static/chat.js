const $ = (id) => document.getElementById(id);
const state = {
  user: null,
  rooms: [],
  selection: null,
  drafts: new Map(),
  socket: null,
  retry: 750,
  sending: false,
  noticeRequest: 0,
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

function newRequestId() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) =>
    byte.toString(16).padStart(2, "0")).join("");
}

function addRoom(room) {
  if (state.rooms.some((item) => item.id === room.id)) return;
  state.rooms.push(room);

  const button = element("button", "room-button");
  button.type = "button";
  button.dataset.roomId = room.id;
  button.title = room.title;
  button.setAttribute("aria-current", "false");
  const symbol = element("span", "room-symbol", room.is_private ? "🔒" : "#");
  symbol.setAttribute("aria-hidden", "true");
  button.append(symbol, element("span", "room-label", room.title));
  button.addEventListener("click", () => selectRoom(room.id));
  $("rooms").append(button);
}

function inviteFromUrl() {
  return new URLSearchParams(location.hash.slice(1)).get("invite");
}

function showJoinDialog() {
  if (inviteFromUrl() === null || $("join-dialog").open) return;
  $("join-error").textContent = "";
  $("join-dialog").showModal();
}

function openApp(user) {
  state.user = user;
  $("profile-name").textContent = user.name;
  $("profile-avatar").textContent = initials(user.name);
  $("welcome").hidden = true;
  $("app").hidden = false;

  const previous = localStorage.getItem("campus.room");
  selectRoom(state.rooms.find((room) => room.id === previous)?.id ?? state.rooms[0].id);
  showJoinDialog();
  refreshNotices();
  connect();
}

function selectRoom(roomId) {
  if (state.selection?.roomId === roomId) return;

  const room = state.rooms.find((item) => item.id === roomId);
  if (!room) return;
  const selection = { roomId, messages: new Map(), hasOlder: false, awaitingPresence: false };
  state.selection = selection;
  localStorage.setItem("campus.room", roomId);

  for (const button of $("rooms").children) {
    button.setAttribute("aria-current", String(button.dataset.roomId === roomId));
  }
  $("room-title").textContent = room.title;
  $("room-description").textContent = room.description;
  $("open-invite").hidden = !room.can_invite;
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
    refreshNotices();
    refreshRooms().catch(() => {});
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
    if (data.type === "room_available") addRoom(data.room);
    if (data.type === "notification") {
      showSystemNotice(data.message);
      refreshNotices();
    }
    if (data.type === "notifications_changed") refreshNotices();
  });
}

function messageUrl(roomId) {
  return `/api/rooms/${encodeURIComponent(roomId)}/messages`;
}

async function refreshRooms() {
  const { rooms } = await api("/api/bootstrap");
  rooms.forEach(addRoom);
}

const createRoomForm = $("create-room-form");
let roomRequestId;
let shareRequest;

$("new-room").addEventListener("click", () => {
  $("create-room-error").textContent = "";
  $("create-room-dialog").showModal();
});
createRoomForm.addEventListener("input", () => { roomRequestId = null; });
createRoomForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = createRoomForm.querySelector('button[type="submit"]');
  const title = $("new-room-title").value.trim();
  $("create-room-error").textContent = "";
  if (Array.from(title).length < 2) {
    $("create-room-error").textContent = "Введите название из двух или более символов.";
    return;
  }
  button.disabled = true;
  roomRequestId ??= newRequestId();
  try {
    const room = await api("/api/rooms", {
      title,
      description: $("new-room-description").value.trim(),
      request_id: roomRequestId,
    });
    addRoom(room);
    selectRoom(room.id);
    createRoomForm.reset();
    roomRequestId = null;
    $("create-room-dialog").close();
    shareInvite(room.id);
  } catch (error) {
    $("create-room-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

async function shareInvite(roomId) {
  const request = Symbol();
  shareRequest = request;
  $("invite-url").value = "";
  $("copy-invite").disabled = true;
  $("share-status").textContent = "Загружаем ссылку…";
  $("share-dialog").showModal();
  try {
    const { token } = await api(`/api/rooms/${encodeURIComponent(roomId)}/invite`);
    if (shareRequest !== request || !$("share-dialog").open) return;
    const url = new URL("/", location.href);
    url.hash = new URLSearchParams({ invite: token }).toString();
    $("invite-url").value = url.href;
    $("copy-invite").disabled = false;
    $("share-status").textContent = "";
  } catch (error) {
    if (shareRequest === request) $("share-status").textContent = error.message;
  }
}

$("open-invite").addEventListener("click", () => shareInvite(state.selection.roomId));
$("copy-invite").addEventListener("click", async () => {
  const input = $("invite-url");
  try {
    await navigator.clipboard.writeText(input.value);
    $("share-status").textContent = "Ссылка скопирована.";
  } catch {
    input.focus();
    input.select();
    $("share-status").textContent = "Скопируйте выделенную ссылку вручную.";
  }
});

$("join-dialog").addEventListener("close", () => {
  if (inviteFromUrl() !== null) history.replaceState(null, "", location.pathname + location.search);
});
$("join-room").addEventListener("click", async () => {
  const token = inviteFromUrl();
  $("join-error").textContent = "";
  if (!token || !/^[A-Za-z0-9_-]{43}$/.test(token)) {
    $("join-error").textContent = "Проверьте ссылку-приглашение.";
    return;
  }
  $("join-room").disabled = true;
  try {
    const room = await api("/api/rooms/join", { token });
    addRoom(room);
    selectRoom(room.id);
    $("join-dialog").close();
  } catch (error) {
    $("join-error").textContent = error.message;
  } finally {
    $("join-room").disabled = false;
  }
});
window.addEventListener("hashchange", () => {
  if (state.user) showJoinDialog();
  else $("welcome-invite").hidden = inviteFromUrl() === null;
});

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
    if (!overlaps) selection.hasOlder = data.has_older;
    for (const message of data.messages) selection.messages.set(message.id, message);
    renderMessages(selection);
    if ($("chat-error").textContent === "Не удалось загрузить историю.") $("chat-error").textContent = "";
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

function renderNotices({ items, unread }) {
  $("notice-count").hidden = unread === 0;
  $("notice-count").textContent = unread > 99 ? "99+" : String(unread);
  $("open-notifications").setAttribute("aria-label", unread ? `Уведомления, непрочитанных: ${unread}` : "Уведомления");
  $("read-all").disabled = unread === 0;
  $("notice-empty").hidden = items.length > 0;
  $("notice-list").replaceChildren(...items.map((notice) => {
    const row = element("li", "");
    const button = element("button", notice.read_at ? "notice" : "notice unread");
    button.type = "button";
    const top = element("span", "notice-top");
    const stamp = element("time", "", dateFormat.format(new Date(notice.created_at)));
    stamp.dateTime = notice.created_at;
    top.append(element("strong", "", notice.room_title), stamp);
    button.append(top, element("span", "notice-preview", `${notice.sender_name}: ${notice.text}`));
    if (!notice.read_at) button.append(element("span", "visually-hidden", "Непрочитанное уведомление"));
    button.addEventListener("click", () => openNotice(notice.room_id, notice.message_id));
    row.append(button);
    return row;
  }));
}

async function refreshNotices() {
  const request = ++state.noticeRequest;
  try {
    const data = await api("/api/notifications");
    if (request !== state.noticeRequest) return;
    $("notice-error").textContent = "";
    renderNotices(data);
  } catch {
    if (request === state.noticeRequest) $("notice-error").textContent = "Не удалось загрузить уведомления.";
  }
}

async function markRead(messageId = null) {
  try {
    await api("/api/notifications/read", { message_id: messageId });
    await refreshNotices();
  } catch {
    const error = $("notifications").open ? $("notice-error") : $("chat-error");
    error.textContent = "Не удалось отметить уведомление как прочитанное.";
  }
}

async function openNotice(roomId, messageId) {
  $("notifications").close();
  if (!state.rooms.some((room) => room.id === roomId)) {
    try {
      await refreshRooms();
    } catch {
      $("chat-error").textContent = "Не удалось открыть чат.";
      return;
    }
  }
  selectRoom(roomId);
  markRead(messageId);
}

function supportsSystemNotices() {
  return window.isSecureContext && "Notification" in window;
}

function systemNoticesEnabled() {
  return supportsSystemNotices() && Notification.permission === "granted"
    && localStorage.getItem("campus.systemNotices") === "on";
}

function renderSystemSettings() {
  const button = $("system-notices");
  const supported = supportsSystemNotices();
  const denied = supported && Notification.permission === "denied";
  const enabled = systemNoticesEnabled();
  button.disabled = !supported || denied;
  button.textContent = enabled ? "Отключить системные" : "Включить системные";
  if (!supported) {
    $("system-status").textContent = "Системные уведомления доступны в поддерживаемом браузере на localhost или через HTTPS.";
  } else if (denied) {
    $("system-status").textContent = "Разрешите уведомления для этого сайта в настройках браузера.";
  } else {
    $("system-status").textContent = enabled
      ? "Включены. Баннеры приходят, пока чат открыт."
      : "Включите баннеры о новых сообщениях, пока чат открыт.";
  }
}

function showSystemNotice(message) {
  if (!systemNoticesEnabled()) return;
  const room = state.rooms.find((item) => item.id === message.room_id);
  if (!room) return;
  const preview = Array.from(message.text);
  const body = preview.length > 140 ? preview.slice(0, 139).join("") + "…" : message.text;
  try {
    const notice = new Notification(`Кампус БГПУ · ${room.title}`, {
      body: `${message.sender_name}: ${body}`,
      tag: `campus-message-${message.id}`,
    });
    notice.onclick = () => {
      window.focus();
      openNotice(message.room_id, message.id);
      notice.close();
    };
  } catch {
    $("system-status").textContent = "Браузер не смог показать системный баннер.";
  }
}

$("open-notifications").addEventListener("click", () => {
  renderSystemSettings();
  $("notifications").showModal();
  refreshNotices();
});
$("read-all").addEventListener("click", () => markRead());
$("system-notices").addEventListener("click", async () => {
  if (systemNoticesEnabled()) localStorage.removeItem("campus.systemNotices");
  else {
    try {
      const permission = await Notification.requestPermission();
      if (permission === "granted") localStorage.setItem("campus.systemNotices", "on");
    } catch {
      $("system-status").textContent = "Не удалось запросить разрешение браузера.";
      return;
    }
  }
  renderSystemSettings();
});
window.addEventListener("focus", () => {
  if ($("notifications").open) renderSystemSettings();
});

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
  draft.requestId ??= newRequestId();
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
    data.rooms.forEach(addRoom);
    if (data.user) openApp(data.user);
    else {
      $("welcome-invite").hidden = inviteFromUrl() === null;
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
