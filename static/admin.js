const $ = (id) => document.getElementById(id);
const pageSize = 50;
const sections = {
  rooms: ["Удалить чат?", "Переписка, уведомления и приглашение этого чата будут удалены."],
  messages: ["Удалить сообщение?", "Сообщение и связанные уведомления будут удалены."],
  notifications: ["Удалить уведомление?", "Уведомление исчезнет у получателя. Сообщение останется в чате."],
  users: ["Удалить профиль?", "Профиль, его сообщения, созданные им чаты и связанные уведомления будут удалены."],
};
const navButtons = [...document.querySelectorAll(".admin-nav button")];
let kind = "rooms";
let offset = 0;
let revision = 0;
let pending;

async function request(path, method = "GET", body) {
  const response = await fetch("/api/admin" + path, {
    method,
    ...(body && { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
  if (response.status === 204) return null;
  const data = await response.json();
  if (response.ok) return data;
  const error = new Error(typeof data.detail === "string" ? data.detail : "Не удалось выполнить запрос.");
  error.status = response.status;
  throw error;
}

function showLogin(message = "") {
  revision++;
  $("admin-confirm").close();
  $("admin-shell").hidden = true;
  $("admin-list").replaceChildren();
  $("admin-welcome").hidden = false;
  $("login-error").textContent = message;
  $("admin-key").focus();
}

function showPanel() {
  $("admin-welcome").hidden = true;
  $("admin-shell").hidden = false;
  load();
}

async function load() {
  const current = ++revision;
  $("admin-error").textContent = "";
  $("admin-status").textContent = "Загружаем записи…";
  $("admin-list").replaceChildren();
  $("admin-prev").disabled = true;
  $("admin-next").disabled = true;
  try {
    const { items, has_more: hasMore } = await request(`/${kind}?offset=${offset}`);
    if (current !== revision) return;
    if (!items.length && offset > 0) {
      offset -= pageSize;
      load();
      return;
    }
    $("admin-status").textContent = items.length ? "" : "В этом разделе пока нет записей.";
    $("admin-list").replaceChildren(...items.map(renderRow));
    $("admin-prev").disabled = offset === 0;
    $("admin-next").disabled = !hasMore;
  } catch (error) {
    if (current !== revision) return;
    $("admin-status").textContent = "";
    if (error.status === 401) showLogin(error.message);
    else $("admin-error").textContent = error.message;
  }
}

function renderRow(item) {
  const row = document.createElement("li");
  row.className = "admin-row";
  const info = document.createElement("div");
  const name = document.createElement("strong");
  name.textContent = item.title;
  const detail = document.createElement("p");
  detail.textContent = item.detail;
  info.append(name, detail);
  if (item.text) {
    const text = document.createElement("p");
    text.className = "admin-preview";
    text.textContent = item.text;
    info.append(text);
  }
  const remove = document.createElement("button");
  remove.className = "secondary danger-secondary";
  remove.type = "button";
  remove.textContent = "Удалить";
  remove.setAttribute("aria-label", `Удалить запись: ${item.title}`);
  remove.addEventListener("click", () => {
    pending = { kind, id: item.id };
    const [title, summary] = sections[kind];
    $("admin-confirm-title").textContent = title;
    $("admin-confirm-summary").textContent = summary;
    $("admin-confirm-name").textContent = item.title;
    $("admin-confirm-error").textContent = "";
    $("admin-confirm").showModal();
  });
  row.append(info, remove);
  return row;
}

for (const button of navButtons) {
  button.addEventListener("click", () => {
    kind = button.dataset.kind;
    offset = 0;
    for (const item of navButtons) item.setAttribute("aria-pressed", String(item === button));
    load();
  });
}
$("admin-prev").addEventListener("click", () => { offset -= pageSize; load(); });
$("admin-next").addEventListener("click", () => { offset += pageSize; load(); });
$("admin-login").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  $("login-error").textContent = "";
  try {
    await request("/session", "POST", { key: $("admin-key").value });
    $("admin-key").value = "";
    showPanel();
  } catch (error) {
    $("login-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
$("admin-logout").addEventListener("click", async () => {
  try {
    await request("/session", "DELETE");
    showLogin();
  } catch (error) {
    $("admin-error").textContent = error.message;
  }
});
$("admin-delete").addEventListener("click", async () => {
  const button = $("admin-delete");
  button.disabled = true;
  try {
    await request(`/${pending.kind}/${encodeURIComponent(pending.id)}`, "DELETE");
    $("admin-confirm").close();
    load();
  } catch (error) {
    if (error.status === 401) showLogin(error.message);
    else if (error.status === 404) {
      $("admin-confirm").close();
      await load();
      $("admin-status").textContent = "Запись уже удалена.";
    } else $("admin-confirm-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

request("/session")
  .then(({ signed_in: signedIn }) => { if (signedIn) showPanel(); })
  .catch(() => { $("login-error").textContent = "Не удалось проверить вход в админку."; });
