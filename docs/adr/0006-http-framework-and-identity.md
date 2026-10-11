# ADR 0006: HTTP framework and identity

Status: proposed, awaiting review. Engineering decision, noted here because it replaces a module instead of evolving it.

## Context

The HTTP layer is aiohttp with handlers registered in `main.py` and four small modules in `api/` (about 170 lines) plus the HTML SOP editor (280 lines, to be removed). There is no request validation, no authentication and no API description. The product needs typed request and response models, an OpenAPI description for customers, and a guarantee that no route exists without an authorisation rule.

## Decision

Replace the server side with FastAPI on Starlette and Uvicorn. Keep aiohttp, or move to httpx, as the outbound client inside the connector SDK.

This is a rewrite of `api/` and the route section of `main.py`. It is cheaper than evolving them: the existing handlers are thin, and FastAPI provides validation, OpenAPI and dependency-based authorisation that would otherwise be hand-built on aiohttp.

### Deny by default

Routes are registered through one helper that requires a permission and a tenant-scope declaration. Application start-up walks the route table and fails if any route, including SSE, metrics and health, lacks one. A test asserts the same. Health has an explicit `public` declaration and returns no detail; detailed health needs the operator role.

### Identity

- Interactive users: OIDC authorisation code flow with PKCE, implemented with Authlib. SAML 2.0 service provider through a maintained library. Sessions are server-side, in the database, with secure, HTTP-only, same-site cookies and CSRF tokens on state-changing requests.
- Roles: viewer, operator, approver, SOP author, SOP approver, tenant admin, auditor. Roles come from identity provider group claims through a per-tenant mapping. Permissions are a fixed table from role to permission in code; routes name permissions, not roles.
- Segregation of duties is checked on identities, not roles: one person may hold SOP author and SOP approver, but cannot approve their own version, and cannot approve a T2 action from an SOP they authored.
- Machine callers: scoped API tokens, stored hashed, with an expiry, a tenant and a permission list. The ServiceNow webhook uses an HMAC signature over timestamp and body with a replay window and nonce store, not a bearer token.
- Break-glass local administrator: one account, disabled by default, every use written to the audit log.

## Consequences

- The React app changes from anonymous fetches to cookie sessions with CSRF. `EventSource` sends cookies on same-origin requests, so SSE works without tokens in URLs.
- Customers get an OpenAPI document.
- SAML libraries depend on native XML security libraries, which complicates Windows development. Developers can use OIDC against a local identity provider container; SAML is tested in CI on Linux.

## Alternatives considered

- Stay on aiohttp with middleware. Workable, but validation, OpenAPI and the route-permission registry would be custom code to maintain.
- Delegate all sign-in to an identity broker (for example Keycloak) and support only OIDC in the product. Simpler code, but adds a stateful component to every customer deployment. Kept as a fallback if native SAML proves costly.
