"""Sessions, API tokens, users and role mappings in the database.

A credential carries its tenant: `<tenant id>.<secret>` for a session cookie and
`l1t_<tenant id>_<secret>` for an API token. The tenant is set on the
connection before the lookup, so a credential is only ever looked up among its
own tenant's rows, and a forged tenant prefix finds nothing. Only a SHA-256 of
the credential is stored; the secret part is 256 random bits.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import FrozenSet, Iterable, List, Optional, Tuple

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from src.l1_agent.audit import log as audit
from src.l1_agent.audit.log import Actor
from src.l1_agent.auth.permissions import (
    Permission,
    permissions_for,
    valid_permissions,
    valid_roles,
)
from src.l1_agent.db.engine import Database, utcnow
from src.l1_agent.db.models import (
    api_tokens,
    login_states,
    role_mappings,
    sessions,
    users,
    webhook_nonces,
)
from src.l1_agent.service.runs import insert_ignoring_conflict

TOKEN_PREFIX = "l1t_"
MAX_TOKEN_DAYS = 365
LOGIN_STATE_MINUTES = 10


@dataclass(frozen=True)
class Principal:
    """An authenticated caller, always inside exactly one tenant."""

    tenant_id: uuid.UUID
    kind: str  # user | token | demo
    id: str
    display: str
    roles: Tuple[str, ...]
    permissions: FrozenSet[Permission]
    session_id: Optional[uuid.UUID] = None
    csrf_token: str = ""

    @property
    def actor(self) -> Actor:
        return Actor("token" if self.kind == "token" else "user", self.id)

    def has(self, permission: Permission) -> bool:
        return permission in self.permissions


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _tenant_of(hex_part: str) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(hex=hex_part) if len(hex_part) == 32 else None
    except ValueError:
        return None


# ── Users and roles ───────────────────────────────────────────────────


async def upsert_user(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    issuer: str,
    subject: str,
    email: str = "",
    display_name: str = "",
) -> uuid.UUID:
    now = utcnow()
    found = (
        await conn.execute(
            sa.select(users.c.id).where(
                users.c.tenant_id == tenant_id,
                users.c.issuer == issuer,
                users.c.subject == subject,
            )
        )
    ).scalar()
    if found:
        await conn.execute(
            sa.update(users)
            .where(users.c.id == found)
            .values(email=email[:320], display_name=display_name[:200], last_login_at=now)
        )
        return found
    user_id = uuid.uuid4()
    await conn.execute(
        sa.insert(users).values(
            id=user_id,
            tenant_id=tenant_id,
            issuer=issuer[:500],
            subject=subject[:500],
            email=email[:320],
            display_name=display_name[:200],
            created_at=now,
            last_login_at=now,
        )
    )
    return user_id


async def roles_for_groups(
    conn: AsyncConnection, tenant_id: uuid.UUID, groups: Iterable[str]
) -> List[str]:
    """Roles the tenant's mapping grants to these identity provider groups."""
    wanted = [str(g) for g in groups][:500]
    if not wanted:
        return []
    rows = await conn.execute(
        sa.select(role_mappings.c.role).where(
            role_mappings.c.tenant_id == tenant_id, role_mappings.c.idp_group.in_(wanted)
        )
    )
    return valid_roles(r.role for r in rows)


async def list_role_mappings(db: Database, tenant_id: uuid.UUID) -> List[dict]:
    async with db.tenant(tenant_id) as conn:
        rows = await conn.execute(
            sa.select(role_mappings.c.idp_group, role_mappings.c.role)
            .where(role_mappings.c.tenant_id == tenant_id)
            .order_by(role_mappings.c.idp_group, role_mappings.c.role)
        )
        return [{"group": r.idp_group, "role": r.role} for r in rows]


async def set_role_mapping(
    db: Database, tenant_id: uuid.UUID, group: str, role: str, actor: Actor, *, grant: bool
) -> bool:
    """Grant or remove one group-to-role mapping. Returns False if nothing changed."""
    if not valid_roles([role]):
        raise ValueError(f"Unknown role: {role}")
    group = group.strip()
    if not group or len(group) > 300:
        raise ValueError("Group name must be 1 to 300 characters")
    async with db.tenant(tenant_id) as conn:
        if grant:
            result = await conn.execute(
                insert_ignoring_conflict(conn, role_mappings)
                .values(id=uuid.uuid4(), tenant_id=tenant_id, idp_group=group, role=role)
                .returning(role_mappings.c.id)
            )
            changed = result.first() is not None
        else:
            result = await conn.execute(
                sa.delete(role_mappings).where(
                    role_mappings.c.tenant_id == tenant_id,
                    role_mappings.c.idp_group == group,
                    role_mappings.c.role == role,
                )
            )
            changed = bool(result.rowcount)
        if changed:
            await audit.append(
                conn,
                tenant_id,
                actor,
                "roles.mapping_granted" if grant else "roles.mapping_removed",
                {"group": group, "role": role},
            )
    return changed


# ── Sessions ──────────────────────────────────────────────────────────


async def create_session(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    roles: List[str],
    auth_method: str,
    hours: int,
) -> Tuple[str, uuid.UUID]:
    """Create a server-side session. Returns the cookie value and the session id."""
    cookie = f"{tenant_id.hex}.{secrets.token_urlsafe(32)}"
    session_id = uuid.uuid4()
    now = utcnow()
    await conn.execute(
        sa.insert(sessions).values(
            id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            token_hash=_sha256(cookie),
            csrf_token=secrets.token_urlsafe(32),
            roles=valid_roles(roles),
            auth_method=auth_method,
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(hours=hours),
        )
    )
    return cookie, session_id


async def load_session(db: Database, cookie: str, idle_minutes: int) -> Optional[Principal]:
    tenant_hex, _, secret = cookie.partition(".")
    tenant_id = _tenant_of(tenant_hex)
    if tenant_id is None or not secret:
        return None
    now = utcnow()
    async with db.tenant(tenant_id) as conn:
        row = (
            await conn.execute(
                sa.select(sessions, users.c.subject, users.c.issuer, users.c.display_name)
                .join(users, users.c.id == sessions.c.user_id)
                .where(
                    sessions.c.tenant_id == tenant_id,
                    sessions.c.token_hash == _sha256(cookie),
                    sessions.c.revoked_at.is_(None),
                    sessions.c.expires_at > now,
                    sessions.c.last_seen_at > now - timedelta(minutes=idle_minutes),
                )
            )
        ).first()
        if row is None:
            return None
        if now - row.last_seen_at > timedelta(seconds=60):
            await conn.execute(
                sa.update(sessions).where(sessions.c.id == row.id).values(last_seen_at=now)
            )
    roles = tuple(valid_roles(row.roles))
    return Principal(
        tenant_id=tenant_id,
        kind="user",
        id=f"{row.issuer}|{row.subject}",
        display=row.display_name or row.subject,
        roles=roles,
        permissions=permissions_for(roles),
        session_id=row.id,
        csrf_token=row.csrf_token,
    )


async def revoke_session(db: Database, principal: Principal) -> None:
    if principal.session_id is None:
        return
    async with db.tenant(principal.tenant_id) as conn:
        await conn.execute(
            sa.update(sessions)
            .where(
                sessions.c.tenant_id == principal.tenant_id, sessions.c.id == principal.session_id
            )
            .values(revoked_at=utcnow())
        )
        await audit.append(conn, principal.tenant_id, principal.actor, "auth.logout", {})


# ── Sign-in state (between redirect and callback) ─────────────────────


async def create_login_state(db: Database, tenant_id: uuid.UUID) -> Tuple[str, str, str]:
    """Returns (state, nonce, code_verifier). Only a hash of the state is stored."""
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    now = utcnow()
    async with db.tenant(tenant_id) as conn:
        await conn.execute(
            sa.delete(login_states).where(
                login_states.c.tenant_id == tenant_id, login_states.c.expires_at < now
            )
        )
        await conn.execute(
            sa.insert(login_states).values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                state_hash=_sha256(state),
                nonce=nonce,
                code_verifier=verifier,
                expires_at=now + timedelta(minutes=LOGIN_STATE_MINUTES),
            )
        )
    return state, nonce, verifier


async def bind_login_state(db: Database, tenant_id: uuid.UUID, state: str, request_id: str) -> None:
    """Remember which SAML request a pending sign-in sent. Returned by consume as the nonce."""
    async with db.tenant(tenant_id) as conn:
        await conn.execute(
            sa.update(login_states)
            .where(
                login_states.c.tenant_id == tenant_id,
                login_states.c.state_hash == _sha256(state),
            )
            .values(nonce=request_id[:64])
        )


async def consume_login_state(
    db: Database, tenant_id: uuid.UUID, state: str
) -> Optional[Tuple[str, str]]:
    """Returns (nonce, code_verifier) once. A state cannot be used twice."""
    async with db.tenant(tenant_id) as conn:
        row = (
            await conn.execute(
                sa.delete(login_states)
                .where(
                    login_states.c.tenant_id == tenant_id,
                    login_states.c.state_hash == _sha256(state),
                    login_states.c.expires_at > utcnow(),
                )
                .returning(login_states.c.nonce, login_states.c.code_verifier)
            )
        ).first()
    return (row.nonce, row.code_verifier) if row else None


# ── API tokens ────────────────────────────────────────────────────────


async def create_api_token(
    db: Database,
    tenant_id: uuid.UUID,
    name: str,
    permissions: Iterable[str],
    days: int,
    actor: Actor,
) -> Tuple[str, uuid.UUID]:
    """Create a scoped token. The token itself is returned once and never stored."""
    granted = valid_permissions(permissions)
    if not granted or set(granted) != set(permissions):
        raise ValueError("Name at least one permission, and only known permissions")
    if not 1 <= days <= MAX_TOKEN_DAYS:
        raise ValueError(f"A token must expire in 1 to {MAX_TOKEN_DAYS} days")
    name = name.strip()
    if not name or len(name) > 100:
        raise ValueError("Token name must be 1 to 100 characters")
    token = f"{TOKEN_PREFIX}{tenant_id.hex}_{secrets.token_urlsafe(32)}"
    token_id = uuid.uuid4()
    now = utcnow()
    async with db.tenant(tenant_id) as conn:
        await conn.execute(
            sa.insert(api_tokens).values(
                id=token_id,
                tenant_id=tenant_id,
                name=name,
                token_hash=_sha256(token),
                permissions=granted,
                created_by=actor.id[:200],
                created_at=now,
                expires_at=now + timedelta(days=days),
            )
        )
        await audit.append(
            conn,
            tenant_id,
            actor,
            "token.created",
            {"token_id": str(token_id), "name": name, "permissions": granted, "days": days},
        )
    return token, token_id


async def load_api_token(db: Database, token: str) -> Optional[Principal]:
    if not token.startswith(TOKEN_PREFIX):
        return None
    tenant_hex, _, secret = token[len(TOKEN_PREFIX) :].partition("_")
    tenant_id = _tenant_of(tenant_hex)
    if tenant_id is None or not secret:
        return None
    now = utcnow()
    async with db.tenant(tenant_id) as conn:
        row = (
            await conn.execute(
                sa.select(api_tokens).where(
                    api_tokens.c.tenant_id == tenant_id,
                    api_tokens.c.token_hash == _sha256(token),
                    api_tokens.c.revoked_at.is_(None),
                    api_tokens.c.expires_at > now,
                )
            )
        ).first()
        if row is None:
            return None
        if row.last_used_at is None or now - row.last_used_at > timedelta(seconds=60):
            await conn.execute(
                sa.update(api_tokens).where(api_tokens.c.id == row.id).values(last_used_at=now)
            )
    granted = frozenset(Permission(p) for p in valid_permissions(row.permissions))
    return Principal(
        tenant_id=tenant_id,
        kind="token",
        id=f"token:{row.id}",
        display=row.name,
        roles=(),
        permissions=granted,
    )


async def list_api_tokens(db: Database, tenant_id: uuid.UUID) -> List[dict]:
    async with db.tenant(tenant_id) as conn:
        rows = await conn.execute(
            sa.select(api_tokens)
            .where(api_tokens.c.tenant_id == tenant_id)
            .order_by(api_tokens.c.created_at.desc())
        )
        return [
            {
                "id": str(r.id),
                "name": r.name,
                "permissions": r.permissions,
                "created_by": r.created_by,
                "created_at": r.created_at.isoformat(),
                "expires_at": r.expires_at.isoformat(),
                "revoked": r.revoked_at is not None,
                "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
            }
            for r in rows
        ]


async def revoke_api_token(
    db: Database, tenant_id: uuid.UUID, token_id: uuid.UUID, actor: Actor
) -> bool:
    async with db.tenant(tenant_id) as conn:
        result = await conn.execute(
            sa.update(api_tokens)
            .where(
                api_tokens.c.tenant_id == tenant_id,
                api_tokens.c.id == token_id,
                api_tokens.c.revoked_at.is_(None),
            )
            .values(revoked_at=utcnow())
        )
        if result.rowcount:
            await audit.append(conn, tenant_id, actor, "token.revoked", {"token_id": str(token_id)})
    return bool(result.rowcount)


# ── Webhook replay protection ─────────────────────────────────────────


async def use_webhook_signature(
    db: Database, tenant_id: uuid.UUID, signature: str, ttl_seconds: int
) -> bool:
    """Record a signature. Returns False if it was already used: a replay.

    Stored in the database so that every process behind a load balancer sees it.
    """
    now = utcnow()
    async with db.tenant(tenant_id) as conn:
        await conn.execute(
            sa.delete(webhook_nonces).where(
                webhook_nonces.c.tenant_id == tenant_id, webhook_nonces.c.expires_at < now
            )
        )
        inserted = await conn.execute(
            insert_ignoring_conflict(conn, webhook_nonces)
            .values(
                tenant_id=tenant_id,
                signature_hash=_sha256(signature),
                expires_at=now + timedelta(seconds=ttl_seconds),
            )
            .returning(webhook_nonces.c.signature_hash)
        )
        return inserted.first() is not None
