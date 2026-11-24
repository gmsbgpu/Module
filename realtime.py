import asyncio
from dataclasses import dataclass, field

from fastapi import WebSocket, WebSocketDisconnect


@dataclass(eq=False)
class Client:
    socket: WebSocket
    user_id: int
    room_id: str | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Hub:
    def __init__(self):
        self.clients: set[Client] = set()

    async def publish(self, event, *, room_id=None, user_id=None, exclude_user_id=None):
        recipients = [
            client for client in self.clients
            if (room_id is None or client.room_id == room_id)
            and (user_id is None or client.user_id == user_id)
            and client.user_id != exclude_user_id
        ]
        await asyncio.gather(*(self._send(client, event) for client in recipients))

    async def publish_presence(self, room_id):
        users = {client.user_id for client in self.clients if client.room_id == room_id}
        await self.publish({"type": "presence", "room_id": room_id, "count": len(users)}, room_id=room_id)

    async def _send(self, client, event):
        try:
            async with client.send_lock:
                await client.socket.send_json(event)
        except (WebSocketDisconnect, RuntimeError, OSError):
            self.clients.discard(client)
