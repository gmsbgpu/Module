from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from datetime import datetime
import json

app = FastAPI()

# chat_id -> {user_id: WebSocket}
rooms: dict[str, dict[str, WebSocket]] = {}

HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Кампус: чат</title>
<style>
  body { font-family: sans-serif; max-width: 760px; margin: 20px auto; }
  #top { display: flex; gap: 10px; align-items: center; margin-bottom: 10px; }
  #chat { border: 1px solid #ccc; height: 320px; overflow-y: auto; padding: 10px; margin-bottom: 10px; background: #fafafa; }
  .msg { margin: 4px 0; }
  .me { color: #0a5; }
  .other { color: #333; }
  .system { color: #999; font-style: italic; font-size: 13px; }
  .time { color: #aaa; font-size: 11px; margin-right: 6px; }
  #input-row { display: flex; gap: 8px; }
  #input { flex: 1; padding: 8px; }
  button { padding: 8px 14px; }
  #online { font-size: 13px; color: #555; }
</style>
</head>
<body>
<h3>Кампус БГПУ — чат</h3>
<div id="top">
  <label>Чат:
    <select id="room">
      <option value="diploma">Дипломники</option>
      <option value="dorm">Общежитие №2</option>
      <option value="events">Мероприятия</option>
    </select>
  </label>
  <span id="online"></span>
</div>
<div id="chat"></div>
<div id="input-row">
  <input id="input" placeholder="Введите сообщение..." autofocus>
  <button onclick="send()">Отправить</button>
</div>

<script>
const myId = "user-" + Math.floor(Math.random() * 1000);
let ws = null;
let currentRoom = "diploma";

const chat = document.getElementById("chat");
const input = document.getElementById("input");
const roomSelect = document.getElementById("room");
const online = document.getElementById("online");

function now() {
  return new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

function addMsg(text, cls, ts) {
  const div = document.createElement("div");
  div.className = "msg " + cls;
  const t = document.createElement("span");
  t.className = "time";
  t.textContent = ts ? `[${ts}]` : `[${now()}]`;
  div.appendChild(t);
  div.appendChild(document.createTextNode(text));
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}

function connect(room) {
  if (ws) ws.close();
  chat.innerHTML = "";
  currentRoom = room;
  ws = new WebSocket(`ws://${location.host}/ws/${room}/${myId}`);

  ws.onmessage = (e) => {
    const data = JSON.parse(e.data);
    if (data.type === "system") addMsg(data.text, "system", data.ts);
    else if (data.type === "online") online.textContent = "онлайн: " + data.users.join(", ");
    else if (data.sender === myId) addMsg("Вы: " + data.text, "me", data.ts);
    else addMsg(data.sender + ": " + data.text, "other", data.ts);
  };
}

function send() {
  const text = input.value.trim();
  if (!text || !ws) return;
  ws.send(JSON.stringify({ text }));
  input.value = "";
  input.focus();
}

input.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
roomSelect.addEventListener("change", () => connect(roomSelect.value));

connect(currentRoom);
</script>
</body>
</html>
"""

@app.get("/")
async def root():
    return HTMLResponse(HTML)

def ts():
    return datetime.now().strftime("%H:%M")

@app.websocket("/ws/{chat_id}/{user_id}")
async def ws_endpoint(ws: WebSocket, chat_id: str, user_id: str):
    await ws.accept()
    rooms.setdefault(chat_id, {})[user_id] = ws

    await broadcast(chat_id, {"type": "system", "text": f"{user_id} подключился", "ts": ts()})
    await send_online(chat_id)

    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            await broadcast(chat_id, {
                "type": "message",
                "sender": user_id,
                "text": data["text"],
                "ts": ts()
            })
    except WebSocketDisconnect:
        rooms.get(chat_id, {}).pop(user_id, None)
        await broadcast(chat_id, {"type": "system", "text": f"{user_id} отключился", "ts": ts()})
        await send_online(chat_id)

async def broadcast(chat_id: str, payload: dict):
    msg = json.dumps(payload, ensure_ascii=False)
    for c in list(rooms.get(chat_id, {}).values()):
        try:
            await c.send_text(msg)
        except Exception:
            pass

async def send_online(chat_id: str):
    users = list(rooms.get(chat_id, {}).keys())
    await broadcast(chat_id, {"type": "online", "users": users})