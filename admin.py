import hmac
import os
import re
import secrets
import time
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, StringConstraints


router = APIRouter()
COOKIE = "campus_admin"
COOKIE_PATH = "/api/admin"
SESSION_SECONDS = 3600
Kind = Literal["rooms", "messages", "notifications", "users"]
SessionToken = Annotated[str | None, Cookie(alias=COOKIE)]


class AdminAuth:
    def __init__(self, key: str):
        self.key = key.encode()

    @classmethod
    def load(cls, path: Path):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as key_file:
                key_file.write(secrets.token_urlsafe(32) + "\n")
        except FileExistsError:
            pass
        key = path.read_text(encoding="ascii").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", key):
            raise RuntimeError(f"Invalid admin key file: {path}")
        return cls(key)

    def _signature(self, value: str):
        return hmac.digest(self.key, ("campus-admin:" + value).encode(), "sha256").hex()

    def login(self, key: str):
        if not hmac.compare_digest(self.key, key.encode()):
            raise HTTPException(401, "Неверный ключ администратора")
        value = f"{int(time.time()) + SESSION_SECONDS}.{secrets.token_hex(16)}"
        return f"{value}.{self._signature(value)}"

    def valid(self, token):
        if not token or not re.fullmatch(r"[0-9]{1,12}\.[a-f0-9]{32}\.[a-f0-9]{64}", token):
            return False
        value, signature = token.rsplit(".", 1)
        expires = int(value.split(".", 1)[0])
        return expires > time.time() and hmac.compare_digest(signature, self._signature(value))


class LoginInput(BaseModel):
    key: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


def require_admin(request: Request, token: SessionToken = None):
    if not request.app.state.admin.valid(token):
        raise HTTPException(401, "Войдите в панель администратора")


@router.get("/admin", include_in_schema=False)
async def admin_page():
    return FileResponse(Path(__file__).resolve().parent / "static" / "admin.html")


@router.get("/api/admin/session")
async def admin_session(request: Request, response: Response, token: SessionToken = None):
    response.headers["Cache-Control"] = "no-store"
    return {"signed_in": request.app.state.admin.valid(token)}


@router.post("/api/admin/session", status_code=204)
async def admin_login(data: LoginInput, request: Request):
    token = request.app.state.admin.login(data.key)
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    response.set_cookie(
        COOKIE, token, max_age=SESSION_SECONDS, httponly=True, samesite="strict",
        secure=request.url.scheme == "https", path=COOKIE_PATH,
    )
    return response


@router.delete("/api/admin/session", status_code=204)
async def admin_logout(request: Request):
    response = Response(status_code=204)
    response.delete_cookie(COOKIE, path=COOKIE_PATH, secure=request.url.scheme == "https", samesite="strict", httponly=True)
    return response


@router.get("/api/admin/{kind}", dependencies=[Depends(require_admin)])
async def admin_list(
    kind: Kind, request: Request, response: Response,
    offset: Annotated[int, Query(ge=0, le=2**31 - 1)] = 0,
):
    response.headers["Cache-Control"] = "no-store"
    return request.app.state.storage.admin_list(kind, offset)


def record_ids(record: str, count: int):
    parts = record.split("-")
    if len(parts) != count or any(not re.fullmatch(r"[1-9][0-9]{0,18}", part) for part in parts):
        raise HTTPException(404, "Запись не найдена")
    ids = tuple(map(int, parts))
    if any(value > 2**63 - 1 for value in ids):
        raise HTTPException(404, "Запись не найдена")
    return ids


@router.delete("/api/admin/{kind}/{record}", status_code=204, dependencies=[Depends(require_admin)])
async def admin_delete(kind: Kind, record: str, request: Request):
    storage = request.app.state.storage
    hub = request.app.state.hub
    if kind == "rooms":
        deleted = storage.delete_room(record)
    elif kind == "messages":
        deleted = storage.delete_message(*record_ids(record, 1))
    elif kind == "notifications":
        deleted = storage.delete_notification(*record_ids(record, 2))
    else:
        (user_id,) = record_ids(record, 1)
        deleted = storage.delete_user(user_id)
        if deleted:
            await hub.disconnect_user(user_id)
    if not deleted:
        raise HTTPException(404, "Запись не найдена")
    if kind == "notifications":
        (user_id, _) = record_ids(record, 2)
        await hub.publish({"type": "notifications_changed"}, user_ids={user_id})
    else:
        await hub.publish({"type": "content_changed"})
