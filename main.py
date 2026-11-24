import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Cookie, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints

from admin import AdminAuth, router as admin_router
from realtime import Client, Hub
from storage import Storage


ROOT = Path(__file__).resolve().parent
COOKIE = "campus_profile"
storage = Storage(Path(os.environ.get("CAMPUS_DB_PATH", ROOT / "data" / "campus.sqlite3")))
hub = Hub()


@asynccontextmanager
async def lifespan(_app):
    storage.initialize()
    _app.state.admin = AdminAuth.load(storage.path.with_name(storage.path.name + ".admin-key"))
    yield


app = FastAPI(title="Кампус БГПУ", lifespan=lifespan)
app.state.storage = storage
app.state.hub = hub
app.include_router(admin_router)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

ProfileToken = Annotated[str | None, Cookie(alias=COOKIE)]
RequestId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]


class ProfileInput(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=40)]


class MessageInput(BaseModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    request_id: RequestId


class RoomInput(BaseModel):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=60)]
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=160)] = ""
    request_id: RequestId


class InviteInput(BaseModel):
    token: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{43}$")]


class ReadInput(BaseModel):
    message_id: Annotated[int | None, Field(gt=0, le=2**63 - 1)] = None


def require_user(token):
    user = storage.find_user(token)
    if user is None:
        raise HTTPException(401, "Сначала откройте профиль")
    return user


def require_room(room_id, user_id):
    if not storage.can_access_room(room_id, user_id):
        raise HTTPException(404, "Чат не найден")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/bootstrap")
async def bootstrap(response: Response, token: ProfileToken = None):
    response.headers["Cache-Control"] = "no-store"
    user = storage.find_user(token)
    return {"user": user, "rooms": storage.rooms(user["id"] if user else None)}


@app.post("/api/profile", status_code=201)
async def create_profile(data: ProfileInput, request: Request, response: Response, token: ProfileToken = None):
    user = storage.find_user(token)
    if user:
        return user
    user, token = storage.create_user(data.name)
    response.set_cookie(
        COOKIE, token, max_age=60 * 60 * 24 * 365, httponly=True,
        secure=request.url.scheme == "https", samesite="lax",
    )
    return user


@app.post("/api/rooms", status_code=201)
async def create_room(data: RoomInput, token: ProfileToken = None):
    user = require_user(token)
    room = storage.create_room(user["id"], data.title, data.description, data.request_id)
    if room["title"] != data.title or room["description"] != data.description:
        raise HTTPException(409, "Идентификатор запроса уже использован")
    await hub.publish({"type": "room_available", "room": room}, user_ids={user["id"]})
    return room


@app.get("/api/rooms/{room_id}/invite")
async def invite(room_id: str, response: Response, token: ProfileToken = None):
    user = require_user(token)
    invite_token = storage.room_invite(room_id, user["id"])
    if invite_token is None:
        raise HTTPException(404, "Приглашение недоступно")
    response.headers["Cache-Control"] = "no-store"
    return {"token": invite_token}


@app.delete("/api/rooms/{room_id}", status_code=204)
async def delete_room(room_id: str, token: ProfileToken = None):
    user = require_user(token)
    if not storage.delete_room(room_id, owner_id=user["id"]):
        raise HTTPException(404, "Чат недоступен для удаления")
    await hub.publish({"type": "content_changed"})


@app.post("/api/rooms/join")
async def join_room(data: InviteInput, token: ProfileToken = None):
    user = require_user(token)
    room = storage.join_room(data.token, user["id"])
    if room is None:
        raise HTTPException(404, "Приглашение не найдено. Проверьте ссылку.")
    await hub.publish({"type": "room_available", "room": room}, user_ids={user["id"]})
    return room


@app.get("/api/rooms/{room_id}/messages")
async def history(
    room_id: str,
    response: Response,
    token: ProfileToken = None,
    before: Annotated[int | None, Query(gt=0, le=2**63 - 1)] = None,
):
    user = require_user(token)
    require_room(room_id, user["id"])
    response.headers["Cache-Control"] = "no-store"
    return storage.history(room_id, before)


@app.post("/api/rooms/{room_id}/messages", status_code=201)
async def create_message(room_id: str, data: MessageInput, token: ProfileToken = None):
    user = require_user(token)
    require_room(room_id, user["id"])
    message, created, recipients = storage.add_message(room_id, user["id"], data.request_id, data.text)
    if message["room_id"] != room_id or message["text"] != data.text:
        raise HTTPException(409, "Идентификатор отправки уже использован")
    if created:
        await hub.publish({"type": "notification", "message": message}, user_ids=recipients)
        await hub.publish({"type": "message", "message": message}, room_id=room_id)
    return message


@app.get("/api/notifications")
async def notifications(response: Response, token: ProfileToken = None):
    user = require_user(token)
    response.headers["Cache-Control"] = "no-store"
    return storage.notifications(user["id"])


@app.post("/api/notifications/read")
async def mark_notifications_read(data: ReadInput, token: ProfileToken = None):
    user = require_user(token)
    storage.mark_notifications_read(user["id"], data.message_id)
    await hub.publish({"type": "notifications_changed"}, user_ids={user["id"]})
    return storage.notifications(user["id"])


@app.websocket("/ws")
async def events(socket: WebSocket):
    origin = socket.headers.get("origin")
    host = socket.headers.get("host")
    if origin is not None and origin not in (f"http://{host}", f"https://{host}"):
        await socket.close(code=1008)
        return

    user = storage.find_user(socket.cookies.get(COOKIE))
    if user is None:
        await socket.close(code=1008)
        return

    await socket.accept()
    if storage.find_user(socket.cookies.get(COOKIE)) is None:
        await socket.close(code=4001)
        return
    client = Client(socket, user["id"])
    hub.clients.add(client)
    try:
        while True:
            room_id = await socket.receive_text()
            if room_id == client.room_id or len(room_id) > 32 or not storage.can_access_room(room_id, user["id"]):
                continue
            previous = client.room_id
            client.room_id = room_id
            if previous is not None:
                await hub.publish_presence(previous)
            await hub.publish_presence(room_id)
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.discard(client)
        if client.room_id is not None:
            await hub.publish_presence(client.room_id)
