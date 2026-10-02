"""Accounts, sessions, invites and the viewing log, in Postgres.

The schema is created at startup with IF NOT EXISTS rather than migrated:
four small tables in their own database, owned by their own role. If it
ever needs an ALTER, that's the point to bring in Alembic like the api.

Tokens (sessions and invites) are stored as SHA-256 hashes, so a read of
the database - or of a backup of it - can't be replayed as a login.
"""
from dataclasses import dataclass
import datetime
import hashlib
import secrets

import asyncpg

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            bigserial PRIMARY KEY,
    username      text NOT NULL UNIQUE,
    password_hash text,
    is_admin      boolean NOT NULL DEFAULT false,
    disabled      boolean NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_seen     timestamptz
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash bytea PRIMARY KEY,
    user_id    bigint NOT NULL REFERENCES users ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
    id         bigserial PRIMARY KEY,
    token_hash bytea NOT NULL UNIQUE,
    username   text NOT NULL,
    is_admin   boolean NOT NULL DEFAULT false,
    created_by bigint REFERENCES users ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS views (
    id         bigserial PRIMARY KEY,
    user_id    bigint REFERENCES users ON DELETE SET NULL,
    username   text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    ip         text
);
"""


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


@dataclass
class User:
    id: int
    username: str
    is_admin: bool
    disabled: bool
    created_at: datetime.datetime
    last_seen: datetime.datetime | None
    password_hash: str | None = None

    def public(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "is_admin": self.is_admin,
            "disabled": self.disabled,
            "created_at": self.created_at.isoformat(),
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
        }


@dataclass
class Invite:
    id: int
    username: str
    is_admin: bool
    created_at: datetime.datetime
    expires_at: datetime.datetime

    def public(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "is_admin": self.is_admin,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }


_USER_COLS = "id, username, is_admin, disabled, created_at, last_seen, password_hash"


class Store:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> "Store":
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
        async with pool.acquire() as conn:
            await conn.execute(SCHEMA)
        return cls(pool)

    async def close(self) -> None:
        await self.pool.close()

    async def ping(self) -> None:
        async with self.pool.acquire() as conn:
            await conn.fetchval("SELECT 1")

    # Users

    async def user_by_name(self, username: str) -> User | None:
        row = await self.pool.fetchrow(
            f"SELECT {_USER_COLS} FROM users WHERE lower(username) = lower($1)", username)
        return User(**row) if row else None

    async def user_by_id(self, user_id: int) -> User | None:
        row = await self.pool.fetchrow(f"SELECT {_USER_COLS} FROM users WHERE id = $1", user_id)
        return User(**row) if row else None

    async def list_users(self) -> list[User]:
        rows = await self.pool.fetch(f"SELECT {_USER_COLS} FROM users ORDER BY lower(username)")
        return [User(**r) for r in rows]

    async def count_active_admins(self) -> int:
        return await self.pool.fetchval(
            "SELECT count(*) FROM users WHERE is_admin AND NOT disabled AND password_hash IS NOT NULL")

    async def set_password(self, user_id: int, password_hash: str) -> None:
        await self.pool.execute("UPDATE users SET password_hash = $2 WHERE id = $1", user_id, password_hash)

    async def set_disabled(self, user_id: int, disabled: bool) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("UPDATE users SET disabled = $2 WHERE id = $1", user_id, disabled)
            if disabled:
                await conn.execute("DELETE FROM sessions WHERE user_id = $1", user_id)

    async def delete_user(self, user_id: int) -> None:
        await self.pool.execute("DELETE FROM users WHERE id = $1", user_id)

    # Sessions

    async def create_session(self, user_id: int, days: int) -> str:
        token = new_token()
        await self.pool.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES ($1, $2, $3)",
            hash_token(token), user_id, now() + datetime.timedelta(days=days))
        await self.pool.execute("UPDATE users SET last_seen = now() WHERE id = $1", user_id)
        return token

    async def session_user(self, token: str, days: int) -> User | None:
        """The signed-in user, extending the session while it's in use.

        Sliding: each use pushes expiry back out to `days`, but only once the
        session is past its first day, so a page load isn't a write.
        """
        cols = ", ".join("u." + c for c in _USER_COLS.split(", "))
        row = await self.pool.fetchrow(
            f"SELECT {cols}, s.expires_at AS session_expires FROM sessions s "
            "JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = $1 AND s.expires_at > now() AND NOT u.disabled",
            hash_token(token))
        if not row:
            return None
        row = dict(row)
        expires = row.pop("session_expires")
        if expires - now() < datetime.timedelta(days=days - 1):
            await self.pool.execute(
                "UPDATE sessions SET expires_at = $2 WHERE token_hash = $1",
                hash_token(token), now() + datetime.timedelta(days=days))
            await self.pool.execute("UPDATE users SET last_seen = now() WHERE id = $1", row["id"])
        return User(**row)

    async def delete_session(self, token: str) -> None:
        await self.pool.execute("DELETE FROM sessions WHERE token_hash = $1", hash_token(token))

    async def delete_other_sessions(self, user_id: int, keep_token: str | None) -> None:
        await self.pool.execute(
            "DELETE FROM sessions WHERE user_id = $1 AND token_hash IS DISTINCT FROM $2",
            user_id, hash_token(keep_token) if keep_token else None)

    # Invites - also how a password is reset: an invite for an existing name

    async def create_invite(self, username: str, is_admin: bool, created_by: int | None,
                            days: int) -> tuple[Invite, str]:
        token = new_token()
        row = await self.pool.fetchrow(
            "INSERT INTO invites (token_hash, username, is_admin, created_by, expires_at) "
            "VALUES ($1, $2, $3, $4, $5) "
            "RETURNING id, username, is_admin, created_at, expires_at",
            hash_token(token), username, is_admin, created_by, now() + datetime.timedelta(days=days))
        return Invite(**row), token

    async def invite_by_token(self, token: str) -> Invite | None:
        row = await self.pool.fetchrow(
            "SELECT id, username, is_admin, created_at, expires_at FROM invites "
            "WHERE token_hash = $1 AND expires_at > now()", hash_token(token))
        return Invite(**row) if row else None

    async def list_invites(self) -> list[Invite]:
        rows = await self.pool.fetch(
            "SELECT id, username, is_admin, created_at, expires_at FROM invites "
            "WHERE expires_at > now() ORDER BY created_at DESC")
        return [Invite(**r) for r in rows]

    async def delete_invite(self, invite_id: int) -> None:
        await self.pool.execute("DELETE FROM invites WHERE id = $1", invite_id)

    async def accept_invite(self, token: str, password_hash: str) -> User | None:
        """Create the account, or set a new password on an existing one.

        One transaction, and the invite row is locked and deleted in it, so
        a link opened twice at once still makes one account.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            invite = await conn.fetchrow(
                "DELETE FROM invites WHERE token_hash = $1 AND expires_at > now() "
                "RETURNING username, is_admin", hash_token(token))
            if not invite:
                return None
            row = await conn.fetchrow(
                "INSERT INTO users (username, password_hash, is_admin) VALUES ($1, $2, $3) "
                "ON CONFLICT (username) DO UPDATE SET password_hash = EXCLUDED.password_hash "
                f"RETURNING {_USER_COLS}",
                invite["username"], password_hash, invite["is_admin"])
            # A reset signs the old password out everywhere.
            await conn.execute("DELETE FROM sessions WHERE user_id = $1", row["id"])
            return User(**row)

    # Viewing log

    async def log_view(self, user: User, ip: str | None) -> None:
        await self.pool.execute(
            "INSERT INTO views (user_id, username, ip) VALUES ($1, $2, $3)", user.id, user.username, ip)

    async def recent_views(self, limit: int = 50) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT username, started_at, ip FROM views ORDER BY started_at DESC LIMIT $1", limit)
        return [{"username": r["username"], "started_at": r["started_at"].isoformat(), "ip": r["ip"]}
                for r in rows]
