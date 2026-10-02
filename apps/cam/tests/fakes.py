"""In-memory stand-ins for Postgres and MediaMTX.

FakeStore implements the same methods as app.store.Store, so the app runs
unchanged on top of it - in the tests, and in devserver.py for looking at
the pages without a database or a camera.
"""
import datetime

from app.store import Invite, User, hash_token, new_token, now


class FakeStore:
    def __init__(self):
        self.users: dict[int, User] = {}
        self.sessions: dict[bytes, tuple[int, datetime.datetime]] = {}
        self.invites: dict[bytes, tuple[Invite, int | None]] = {}
        self.views: list[dict] = []
        self._ids = iter(range(1, 10**6))

    async def close(self):
        pass

    async def ping(self):
        pass

    async def user_by_name(self, username):
        return next((u for u in self.users.values() if u.username.lower() == username.lower()), None)

    async def user_by_id(self, user_id):
        return self.users.get(user_id)

    async def list_users(self):
        return sorted(self.users.values(), key=lambda u: u.username)

    async def count_active_admins(self):
        return sum(1 for u in self.users.values() if u.is_admin and not u.disabled and u.password_hash)

    async def set_password(self, user_id, password_hash):
        self.users[user_id].password_hash = password_hash

    async def set_disabled(self, user_id, disabled):
        self.users[user_id].disabled = disabled
        if disabled:
            self.sessions = {k: v for k, v in self.sessions.items() if v[0] != user_id}

    async def delete_user(self, user_id):
        self.users.pop(user_id, None)
        self.sessions = {k: v for k, v in self.sessions.items() if v[0] != user_id}

    async def create_session(self, user_id, days):
        token = new_token()
        self.sessions[hash_token(token)] = (user_id, now() + datetime.timedelta(days=days))
        self.users[user_id].last_seen = now()
        return token

    async def session_user(self, token, days):
        entry = self.sessions.get(hash_token(token))
        if not entry or entry[1] <= now():
            return None
        user = self.users.get(entry[0])
        return None if not user or user.disabled else user

    async def delete_session(self, token):
        self.sessions.pop(hash_token(token), None)

    async def delete_other_sessions(self, user_id, keep_token):
        keep = hash_token(keep_token) if keep_token else None
        self.sessions = {k: v for k, v in self.sessions.items() if v[0] != user_id or k == keep}

    async def create_invite(self, username, is_admin, created_by, days):
        token = new_token()
        invite = Invite(next(self._ids), username, is_admin, now(), now() + datetime.timedelta(days=days))
        self.invites[hash_token(token)] = (invite, created_by)
        return invite, token

    async def invite_by_token(self, token):
        entry = self.invites.get(hash_token(token))
        return entry[0] if entry and entry[0].expires_at > now() else None

    async def list_invites(self):
        return [i for i, _ in self.invites.values() if i.expires_at > now()]

    async def delete_invite(self, invite_id):
        self.invites = {k: v for k, v in self.invites.items() if v[0].id != invite_id}

    async def accept_invite(self, token, password_hash):
        entry = self.invites.pop(hash_token(token), None)
        if not entry or entry[0].expires_at <= now():
            return None
        invite = entry[0]
        user = await self.user_by_name(invite.username)
        if user:
            user.password_hash = password_hash
        else:
            user = User(next(self._ids), invite.username, invite.is_admin, False, now(), None, password_hash)
            self.users[user.id] = user
        self.sessions = {k: v for k, v in self.sessions.items() if v[0] != user.id}
        return user

    async def log_view(self, user, ip):
        self.views.append({"username": user.username, "started_at": now().isoformat(), "ip": ip})

    async def recent_views(self, limit=50):
        return list(reversed(self.views))[:limit]


class FakeResponse:
    def __init__(self, status_code, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}


class FakeMediaMTX:
    """Answers WHEP like MediaMTX: 201, an SDP answer, a session Location."""

    def __init__(self):
        self.offers = []
        self.deleted = []
        self.fail_with = None      # an exception to raise, e.g. httpx.ConnectError
        self.status = 201

    async def post(self, url, content=None, headers=None):
        if self.fail_with:
            raise self.fail_with
        self.offers.append((url, content))
        if self.status != 201:
            return FakeResponse(self.status)
        return FakeResponse(201, b"v=0\r\nfake-answer\r\n",
                            {"location": "/cam/whep/5f0c2b7e-1111-2222-3333-444455556666"})

    async def delete(self, url):
        self.deleted.append(url)
        return FakeResponse(200)

    async def aclose(self):
        pass
