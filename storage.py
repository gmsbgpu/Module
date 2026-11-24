import hashlib
import secrets
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path


ROOMS = (
    ("diploma", "Дипломники", "Вопросы по выпускным работам и консультациям"),
    ("dorm", "Общежитие №2", "Бытовые вопросы и объявления для жильцов"),
    ("events", "Мероприятия", "События, встречи и жизнь кампуса"),
)

MESSAGE_SELECT = """
    SELECT m.id, m.room_id, m.sender_id, u.name AS sender_name, m.text, m.created_at
    FROM messages AS m JOIN users AS u ON u.id = m.sender_id
"""

ROOM_SELECT = """
    SELECT r.id, r.title, r.description, p.room_id IS NOT NULL AS is_private,
           COALESCE(p.owner_id = ?, 0) AS can_invite
    FROM rooms AS r
    LEFT JOIN private_rooms AS p ON p.room_id = r.id
    LEFT JOIN room_members AS m ON m.room_id = r.id AND m.user_id = ?
    WHERE (p.room_id IS NULL OR m.user_id IS NOT NULL)
"""

ADMIN_LISTS = {
    "rooms": """
        SELECT r.id, r.title,
               CASE WHEN p.room_id IS NULL THEN 'Общий чат' ELSE 'Владелец: ' || u.name END AS detail,
               r.description AS text
        FROM rooms r LEFT JOIN private_rooms p ON p.room_id = r.id LEFT JOIN users u ON u.id = p.owner_id
        ORDER BY r.rowid DESC
    """,
    "messages": """
        SELECT m.id, u.name AS title, r.title || ' · #' || m.id AS detail, m.text
        FROM messages m JOIN users u ON u.id = m.sender_id JOIN rooms r ON r.id = m.room_id
        ORDER BY m.id DESC
    """,
    "notifications": """
        SELECT n.user_id || '-' || n.message_id AS id, u.name AS title,
               r.title || ' · #' || m.id || CASE WHEN n.read_at IS NULL
                   THEN ' · непрочитано' ELSE ' · прочитано' END AS detail, m.text
        FROM notifications n JOIN users u ON u.id = n.user_id
        JOIN messages m ON m.id = n.message_id JOIN rooms r ON r.id = m.room_id
        ORDER BY n.message_id DESC, n.user_id DESC
    """,
    "users": "SELECT id, name AS title, 'Профиль #' || id AS detail FROM users ORDER BY id DESC",
}


class Storage:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS rooms (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS private_rooms (
                    room_id TEXT PRIMARY KEY REFERENCES rooms(id),
                    owner_id INTEGER NOT NULL REFERENCES users(id),
                    invite_token TEXT NOT NULL UNIQUE,
                    request_id TEXT NOT NULL,
                    UNIQUE (owner_id, request_id)
                );
                CREATE TABLE IF NOT EXISTS room_members (
                    room_id TEXT NOT NULL REFERENCES private_rooms(room_id),
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    PRIMARY KEY (room_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY,
                    room_id TEXT NOT NULL REFERENCES rooms(id),
                    sender_id INTEGER NOT NULL REFERENCES users(id),
                    request_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    UNIQUE (sender_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS messages_room_id ON messages(room_id, id);
                CREATE TABLE IF NOT EXISTS notifications (
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    message_id INTEGER NOT NULL REFERENCES messages(id),
                    read_at TEXT,
                    PRIMARY KEY (user_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS notifications_unread
                    ON notifications(user_id, read_at, message_id);
                CREATE INDEX IF NOT EXISTS notifications_message ON notifications(message_id);
                CREATE INDEX IF NOT EXISTS room_members_user ON room_members(user_id);
                CREATE TABLE IF NOT EXISTS id_counters (
                    name TEXT PRIMARY KEY,
                    last_id INTEGER NOT NULL
                );
            """)
            db.execute("BEGIN IMMEDIATE")
            if db.execute("PRAGMA user_version").fetchone()[0] == 0:
                db.executemany("INSERT OR IGNORE INTO rooms VALUES (?, ?, ?)", ROOMS)
                db.execute("PRAGMA user_version = 1")
            db.execute("INSERT OR IGNORE INTO id_counters SELECT 'users', COALESCE(MAX(id), 0) FROM users")
            db.execute("INSERT OR IGNORE INTO id_counters SELECT 'messages', COALESCE(MAX(id), 0) FROM messages")

    @staticmethod
    def _next_id(db, name):
        return db.execute(
            "UPDATE id_counters SET last_id = last_id + 1 WHERE name = ? RETURNING last_id", (name,)
        ).fetchone()[0]

    def create_user(self, name):
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as db:
            user = db.execute(
                "INSERT INTO users(id, name, token_hash) VALUES (?, ?, ?) RETURNING id, name",
                (self._next_id(db, "users"), name, token_hash),
            ).fetchone()
        return dict(user), token

    def find_user(self, token):
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as db:
            row = db.execute("SELECT id, name FROM users WHERE token_hash = ?", (token_hash,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _room(row):
        return {**dict(row), "is_private": bool(row["is_private"]), "can_invite": bool(row["can_invite"])}

    def rooms(self, user_id=None):
        with self.connect() as db:
            rows = db.execute(ROOM_SELECT + " ORDER BY r.rowid", (user_id, user_id))
            return [self._room(row) for row in rows]

    def can_access_room(self, room_id, user_id):
        with self.connect() as db:
            return db.execute(ROOM_SELECT + " AND r.id = ?", (user_id, user_id, room_id)).fetchone() is not None

    def create_room(self, owner_id, title, description, request_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                ROOM_SELECT + " AND p.owner_id = ? AND p.request_id = ?",
                (owner_id, owner_id, owner_id, request_id),
            ).fetchone()
            if row is None:
                room_id = secrets.token_hex(16)
                db.execute("INSERT INTO rooms VALUES (?, ?, ?)", (room_id, title, description))
                db.execute(
                    "INSERT INTO private_rooms VALUES (?, ?, ?, ?)",
                    (room_id, owner_id, secrets.token_urlsafe(32), request_id),
                )
                db.execute("INSERT INTO room_members VALUES (?, ?)", (room_id, owner_id))
                row = db.execute(ROOM_SELECT + " AND r.id = ?", (owner_id, owner_id, room_id)).fetchone()
            return self._room(row)

    def room_invite(self, room_id, user_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT invite_token FROM private_rooms WHERE room_id = ? AND owner_id = ?", (room_id, user_id)
            ).fetchone()
        return row["invite_token"] if row else None

    def join_room(self, invite_token, user_id):
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO room_members(room_id, user_id)
                SELECT room_id, ? FROM private_rooms WHERE invite_token = ?
                ON CONFLICT(room_id, user_id) DO NOTHING
                """,
                (user_id, invite_token),
            )
            row = db.execute(
                ROOM_SELECT + " AND p.invite_token = ?", (user_id, user_id, invite_token)
            ).fetchone()
        return self._room(row) if row else None

    def history(self, room_id, before=None, limit=50):
        with self.connect() as db:
            rows = db.execute(
                MESSAGE_SELECT + " WHERE m.room_id = ? AND m.id < ? ORDER BY m.id DESC LIMIT ?",
                (room_id, before if before is not None else 2**63 - 1, limit + 1),
            ).fetchall()
        return {"messages": [dict(row) for row in reversed(rows[:limit])], "has_older": len(rows) > limit}

    def add_message(self, room_id, sender_id, request_id, text):
        recipients = set()
        with self.connect() as db:
            inserted = db.execute(
                """
                INSERT INTO messages(id, room_id, sender_id, request_id, text) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(sender_id, request_id) DO NOTHING
                RETURNING id
                """,
                (self._next_id(db, "messages"), room_id, sender_id, request_id, text),
            ).fetchone()
            message = db.execute(
                MESSAGE_SELECT + " WHERE m.sender_id = ? AND m.request_id = ?", (sender_id, request_id)
            ).fetchone()
            if inserted:
                rows = db.execute(
                    """
                    INSERT INTO notifications(user_id, message_id)
                    SELECT u.id, ? FROM users AS u
                    WHERE u.id != ? AND (
                        NOT EXISTS (SELECT 1 FROM private_rooms WHERE room_id = ?)
                        OR EXISTS (SELECT 1 FROM room_members WHERE room_id = ? AND user_id = u.id)
                    )
                    RETURNING user_id
                    """,
                    (message["id"], sender_id, room_id, room_id),
                )
                recipients = {row["user_id"] for row in rows}
        return dict(message), inserted is not None, recipients

    def notifications(self, user_id):
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT n.message_id, n.read_at, m.room_id, m.text, m.created_at,
                       u.name AS sender_name, r.title AS room_title
                FROM notifications AS n
                JOIN messages AS m ON m.id = n.message_id
                JOIN users AS u ON u.id = m.sender_id
                JOIN rooms AS r ON r.id = m.room_id
                WHERE n.user_id = ?
                ORDER BY n.read_at IS NOT NULL, n.message_id DESC
                LIMIT 50
                """,
                (user_id,),
            ).fetchall()
            unread = db.execute(
                "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read_at IS NULL", (user_id,)
            ).fetchone()[0]
        return {"items": [dict(row) for row in rows], "unread": unread}

    def mark_notifications_read(self, user_id, message_id=None):
        update = """
            UPDATE notifications
            SET read_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE user_id = ? AND read_at IS NULL
        """
        with self.connect() as db:
            if message_id is None:
                db.execute(update, (user_id,))
            else:
                db.execute(update + " AND message_id = ?", (user_id, message_id))

    def admin_list(self, kind, offset, limit=50):
        with self.connect() as db:
            rows = db.execute(ADMIN_LISTS[kind] + " LIMIT ? OFFSET ?", (limit + 1, offset)).fetchall()
        return {"items": [dict(row) for row in rows[:limit]], "has_more": len(rows) > limit}

    @staticmethod
    def _delete_messages(db, *, room_id=None, sender_id=None, message_id=None):
        if room_id is not None:
            clause, value = "room_id = ?", room_id
        elif sender_id is not None:
            clause, value = "sender_id = ?", sender_id
        else:
            clause, value = "id = ?", message_id
        db.execute("DELETE FROM notifications WHERE message_id IN (SELECT id FROM messages WHERE " + clause + ")", (value,))
        return db.execute("DELETE FROM messages WHERE " + clause, (value,)).rowcount

    def _delete_room(self, db, room_id):
        self._delete_messages(db, room_id=room_id)
        db.execute("DELETE FROM room_members WHERE room_id = ?", (room_id,))
        db.execute("DELETE FROM private_rooms WHERE room_id = ?", (room_id,))
        return bool(db.execute("DELETE FROM rooms WHERE id = ?", (room_id,)).rowcount)

    def delete_room(self, room_id, owner_id=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if owner_id is not None and not db.execute(
                "SELECT 1 FROM private_rooms WHERE room_id = ? AND owner_id = ?", (room_id, owner_id)
            ).fetchone():
                return False
            return self._delete_room(db, room_id)

    def delete_message(self, message_id):
        with self.connect() as db:
            return bool(self._delete_messages(db, message_id=message_id))

    def delete_notification(self, user_id, message_id):
        with self.connect() as db:
            return bool(db.execute(
                "DELETE FROM notifications WHERE user_id = ? AND message_id = ?", (user_id, message_id)
            ).rowcount)

    def delete_user(self, user_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owned = db.execute("SELECT room_id FROM private_rooms WHERE owner_id = ?", (user_id,)).fetchall()
            for row in owned:
                self._delete_room(db, row["room_id"])
            self._delete_messages(db, sender_id=user_id)
            db.execute("DELETE FROM notifications WHERE user_id = ?", (user_id,))
            db.execute("DELETE FROM room_members WHERE user_id = ?", (user_id,))
            return bool(db.execute("DELETE FROM users WHERE id = ?", (user_id,)).rowcount)
