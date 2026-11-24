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
            """)
            db.executemany("INSERT OR IGNORE INTO rooms VALUES (?, ?, ?)", ROOMS)

    def create_user(self, name):
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as db:
            user = db.execute(
                "INSERT INTO users(name, token_hash) VALUES (?, ?) RETURNING id, name",
                (name, token_hash),
            ).fetchone()
        return dict(user), token

    def find_user(self, token):
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as db:
            row = db.execute("SELECT id, name FROM users WHERE token_hash = ?", (token_hash,)).fetchone()
        return dict(row) if row else None

    def rooms(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT id, title, description FROM rooms ORDER BY rowid")]

    def room_exists(self, room_id):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM rooms WHERE id = ?", (room_id,)).fetchone() is not None

    def history(self, room_id, before=None, limit=50):
        with self.connect() as db:
            rows = db.execute(
                MESSAGE_SELECT + " WHERE m.room_id = ? AND m.id < ? ORDER BY m.id DESC LIMIT ?",
                (room_id, before if before is not None else 2**63 - 1, limit + 1),
            ).fetchall()
        return {"messages": [dict(row) for row in reversed(rows[:limit])], "has_older": len(rows) > limit}

    def add_message(self, room_id, sender_id, request_id, text):
        with self.connect() as db:
            inserted = db.execute(
                """
                INSERT INTO messages(room_id, sender_id, request_id, text) VALUES (?, ?, ?, ?)
                ON CONFLICT(sender_id, request_id) DO NOTHING
                RETURNING id
                """,
                (room_id, sender_id, request_id, text),
            ).fetchone()
            message = db.execute(
                MESSAGE_SELECT + " WHERE m.sender_id = ? AND m.request_id = ?", (sender_id, request_id)
            ).fetchone()
            if inserted:
                db.execute(
                    "INSERT INTO notifications(user_id, message_id) SELECT id, ? FROM users WHERE id != ?",
                    (message["id"], sender_id),
                )
        return dict(message), inserted is not None

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
