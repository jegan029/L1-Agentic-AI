"""Roles and permissions (ADR 0006).

Routes name a permission, never a role. Roles come from identity provider
groups through a per-tenant mapping; the role-to-permission table is fixed in
code so that it changes only through review.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, FrozenSet, Iterable


class Role(str, Enum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    APPROVER = "approver"
    SOP_AUTHOR = "sop_author"
    SOP_APPROVER = "sop_approver"
    TENANT_ADMIN = "tenant_admin"
    AUDITOR = "auditor"


class Permission(str, Enum):
    INCIDENTS_READ = "incidents.read"
    SYSTEM_READ = "system.read"
    METRICS_READ = "metrics.read"
    AUDIT_READ = "audit.read"
    TOKENS_MANAGE = "tokens.manage"
    ROLES_MANAGE = "roles.manage"
    CONNECTORS_MANAGE = "connectors.manage"
    SOPS_AUTHOR = "sops.author"
    SOPS_APPROVE = "sops.approve"
    # Declared now so the role table is complete; their routes arrive in Phase 4.
    RUNS_OPERATE = "runs.operate"
    ACTIONS_APPROVE = "actions.approve"


P = Permission

ROLE_PERMISSIONS: Dict[Role, FrozenSet[Permission]] = {
    Role.VIEWER: frozenset({P.INCIDENTS_READ}),
    Role.OPERATOR: frozenset({P.INCIDENTS_READ, P.SYSTEM_READ, P.METRICS_READ, P.RUNS_OPERATE}),
    Role.APPROVER: frozenset({P.INCIDENTS_READ, P.ACTIONS_APPROVE}),
    Role.SOP_AUTHOR: frozenset({P.INCIDENTS_READ, P.SOPS_AUTHOR}),
    Role.SOP_APPROVER: frozenset({P.INCIDENTS_READ, P.SOPS_APPROVE}),
    # An administrator manages access. Reading the audit log is the auditor's job,
    # and approving actions or SOPs is not an administrator's by default.
    Role.TENANT_ADMIN: frozenset(
        {
            P.INCIDENTS_READ,
            P.SYSTEM_READ,
            P.METRICS_READ,
            P.TOKENS_MANAGE,
            P.ROLES_MANAGE,
            P.CONNECTORS_MANAGE,
        }
    ),
    Role.AUDITOR: frozenset({P.INCIDENTS_READ, P.AUDIT_READ}),
}


def valid_roles(names: Iterable[str]) -> list:
    """The known roles among `names`, in a stable order. Unknown names are dropped."""
    known = {r.value for r in Role}
    return sorted({n for n in names if n in known})


def permissions_for(roles: Iterable[str]) -> FrozenSet[Permission]:
    granted: set = set()
    for name in valid_roles(roles):
        granted |= ROLE_PERMISSIONS[Role(name)]
    return frozenset(granted)


def valid_permissions(names: Iterable[str]) -> list:
    known = {p.value for p in Permission}
    return sorted({n for n in names if n in known})
