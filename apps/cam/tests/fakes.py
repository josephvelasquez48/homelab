"""In-memory stand-ins for Postgres, MediaMTX and a passkey authenticator.

FakeStore implements the same methods as app.store.Store, so the app runs
unchanged on top of it - in the tests, and in devserver.py for looking at
the pages without a database or a camera. SoftAuthenticator makes real
WebAuthn registrations and assertions (P-256, "none" attestation), so the
tests exercise py_webauthn's actual verification, not a stub of it.
"""
import datetime
import hashlib
import json
import os
import struct

import asyncpg
import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

from app.store import Invite, Passkey, User, hash_token, new_token, now


class FakeStore:
    def __init__(self):
        self.users: dict[int, User] = {}
        self.passkeys: dict[bytes, Passkey] = {}
        self.sessions: dict[bytes, tuple[int, datetime.datetime]] = {}
        self.invites: dict[bytes, tuple[Invite, int | None]] = {}
        self.views: list[dict] = []
        self._ids = iter(range(1, 10**6))

    async def close(self):
        pass

    async def ping(self):
        pass

    def _with_count(self, user):
        user.passkey_count = sum(1 for p in self.passkeys.values() if p.user_id == user.id)
        return user

    async def user_by_name(self, username):
        return next((u for u in self.users.values() if u.username.lower() == username.lower()), None)

    async def user_by_id(self, user_id):
        return self.users.get(user_id)

    async def list_users(self):
        return [self._with_count(u) for u in sorted(self.users.values(), key=lambda u: u.username)]

    async def set_disabled(self, user_id, disabled):
        self.users[user_id].disabled = disabled
        if disabled:
            self.sessions = {k: v for k, v in self.sessions.items() if v[0] != user_id}

    async def set_webauthn_id(self, user_id, webauthn_id):
        user = self.users[user_id]
        user.webauthn_id = user.webauthn_id or webauthn_id
        return user.webauthn_id

    async def delete_user(self, user_id):
        self.users.pop(user_id, None)
        self.passkeys = {k: v for k, v in self.passkeys.items() if v.user_id != user_id}
        self.sessions = {k: v for k, v in self.sessions.items() if v[0] != user_id}

    async def passkeys_for(self, user_id):
        return sorted((p for p in self.passkeys.values() if p.user_id == user_id), key=lambda p: p.created_at)

    async def passkey_with_user(self, credential_id):
        passkey = self.passkeys.get(credential_id)
        if not passkey:
            return None
        user = self.users.get(passkey.user_id)
        return None if not user or user.disabled else (passkey, user)

    async def add_passkey(self, user_id, credential_id, public_key, sign_count, name, backed_up):
        if credential_id in self.passkeys:
            raise asyncpg.UniqueViolationError("duplicate passkey")
        self.passkeys[credential_id] = Passkey(credential_id, user_id, public_key, sign_count, name,
                                               backed_up, now(), None)

    async def passkey_used(self, credential_id, sign_count):
        self.passkeys[credential_id].sign_count = sign_count
        self.passkeys[credential_id].last_used = now()

    async def delete_passkey(self, user_id, credential_id):
        mine = [p for p in self.passkeys.values() if p.user_id == user_id]
        if len(mine) <= 1 or credential_id not in {p.id for p in mine}:
            return False
        del self.passkeys[credential_id]
        return True

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

    async def create_invite(self, username, is_admin, created_by, hours):
        token = new_token()
        invite = Invite(next(self._ids), username, is_admin, now(), now() + datetime.timedelta(hours=hours))
        self.invites[hash_token(token)] = (invite, created_by)
        return invite, token

    async def invite_by_token(self, token):
        entry = self.invites.get(hash_token(token))
        return entry[0] if entry and entry[0].expires_at > now() else None

    async def list_invites(self):
        return [i for i, _ in self.invites.values() if i.expires_at > now()]

    async def delete_invite(self, invite_id):
        self.invites = {k: v for k, v in self.invites.items() if v[0].id != invite_id}

    async def accept_invite(self, token, webauthn_id, credential_id, public_key, sign_count, name, backed_up):
        entry = self.invites.get(hash_token(token))
        if not entry or entry[0].expires_at <= now():
            return None
        if credential_id in self.passkeys:
            raise asyncpg.UniqueViolationError("duplicate passkey")
        del self.invites[hash_token(token)]
        invite = entry[0]
        user = await self.user_by_name(invite.username)
        if user:
            user.webauthn_id = webauthn_id
        else:
            user = User(next(self._ids), invite.username, invite.is_admin, False, now(), None, webauthn_id)
            self.users[user.id] = user
        self.passkeys = {k: v for k, v in self.passkeys.items() if v.user_id != user.id}
        self.sessions = {k: v for k, v in self.sessions.items() if v[0] != user.id}
        self.passkeys[credential_id] = Passkey(credential_id, user.id, public_key, sign_count, name,
                                               backed_up, now(), None)
        return user

    async def log_view(self, user, ip):
        self.views.append({"username": user.username, "started_at": now().isoformat(), "ip": ip})

    async def recent_views(self, limit=50):
        return list(reversed(self.views))[:limit]


class SoftAuthenticator:
    """A passkey authenticator in software: one P-256 key, like a phone's."""

    # Flags: user present, user verified, backup eligible, backed up.
    UP, UV, BE, BS, AT = 0x01, 0x04, 0x08, 0x10, 0x40

    def __init__(self, origin: str, rp_id: str):
        self.origin = origin
        self.rp_id = rp_id
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.user_handle = b""
        self.sign_count = 0

    def _client_data(self, kind: str, challenge: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": self.origin,
                           "crossOrigin": False}).encode()

    def _cose_key(self) -> bytes:
        numbers = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"),
                            -3: numbers.y.to_bytes(32, "big")})

    def create(self, options: dict, user_verified: bool = True) -> dict:
        """navigator.credentials.create(), answered."""
        self.user_handle = base64url_to_bytes(options["user"]["id"])
        flags = self.UP | self.BE | self.BS | self.AT | (self.UV if user_verified else 0)
        auth_data = (hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", 0)
                     + bytes(16) + struct.pack(">H", len(self.credential_id)) + self.credential_id
                     + self._cose_key())
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        cid = bytes_to_base64url(self.credential_id)
        return {
            "id": cid, "rawId": cid, "type": "public-key", "clientExtensionResults": {},
            "response": {
                "clientDataJSON": bytes_to_base64url(self._client_data("webauthn.create", options["challenge"])),
                "attestationObject": bytes_to_base64url(attestation),
                "transports": ["internal", "hybrid"],
            },
        }

    def get(self, options: dict) -> dict:
        """navigator.credentials.get(), answered."""
        self.sign_count += 1
        flags = self.UP | self.UV | self.BE | self.BS
        auth_data = hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", self.sign_count)
        client_data = self._client_data("webauthn.get", options["challenge"])
        signature = self.key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        cid = bytes_to_base64url(self.credential_id)
        return {
            "id": cid, "rawId": cid, "type": "public-key", "clientExtensionResults": {},
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "authenticatorData": bytes_to_base64url(auth_data),
                "signature": bytes_to_base64url(signature),
                "userHandle": bytes_to_base64url(self.user_handle),
            },
        }


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
