"""Authentication — pluggable identity backend.

Gideon does not ship its own user database or password store. Instead it
resolves the caller's identity through one of the backends below, chosen with
the `AUTH_MODE` environment variable:

    single_user  (default) — no authentication. Every request resolves to one
                             local user, configured via SINGLE_USER_* env vars.
                             This is the self-hosting default: clone, run, and
                             you have your own assistant with no identity
                             provider to stand up. Do NOT expose an instance in
                             this mode to a network you do not control.

    oidc                   — validate the caller's bearer token against any
                             OpenID Connect provider (Google, Okta, Auth0,
                             Keycloak, Entra) by calling its userinfo endpoint,
                             set via OIDC_USERINFO_URL. Using userinfo rather
                             than local JWT verification keeps this dependency
                             free and provider agnostic, and it honours token
                             revocation immediately.

To add a backend (a company SSO, a bespoke gateway), implement a resolver that
returns an `AuthUser` and register it in `verify_headers`. Everything upstream
depends only on `middleware.auth.get_current_user`, so nothing else changes.

`roles` / `access` carry whatever your provider returns and are what the
permission checks elsewhere read (see `tools/analytics_tool.py`).
"""

import os
from dataclasses import dataclass
from typing import Optional

import httpx
import structlog

logger = structlog.get_logger()

AUTH_MODE = os.environ.get("AUTH_MODE", "single_user").strip().lower()

# --- single_user mode -------------------------------------------------------
# SINGLE_USER_ID must be set to a random UUID in your .env — do not rely on
# this fallback in any real deployment. Generate one with:
#   python -c "import uuid; print(uuid.uuid4())"
# The all-zeros default exists only so `poetry run pytest` boots without a
# .env file. It must never be used against a persistent database.
SINGLE_USER_ID = os.environ.get("SINGLE_USER_ID") or "00000000-0000-0000-0000-000000000001"
if SINGLE_USER_ID == "00000000-0000-0000-0000-000000000001":
    import warnings
    warnings.warn(
        "SINGLE_USER_ID is using the insecure default UUID. "
        "Set a random UUID in your .env: python -c \"import uuid; print(uuid.uuid4())\"",
        stacklevel=1,
    )
SINGLE_USER_EMAIL = os.environ.get("SINGLE_USER_EMAIL", "you@example.com")
SINGLE_USER_NAME = os.environ.get("SINGLE_USER_NAME", "Gideon User")

# --- oidc mode --------------------------------------------------------------
OIDC_USERINFO_URL = os.environ.get("OIDC_USERINFO_URL", "")
# Claim names vary by provider; these defaults match the OIDC core spec.
OIDC_ID_CLAIM = os.environ.get("OIDC_ID_CLAIM", "sub")
OIDC_EMAIL_CLAIM = os.environ.get("OIDC_EMAIL_CLAIM", "email")
OIDC_ROLES_CLAIM = os.environ.get("OIDC_ROLES_CLAIM", "roles")

# Optional allow-list. Comma-separated bare domains, e.g. "example.com,corp.io".
# Empty (default) accepts any authenticated email.
ALLOWED_EMAIL_DOMAINS = [
    d.strip().lower().lstrip("@")
    for d in os.environ.get("ALLOWED_EMAIL_DOMAINS", "").split(",")
    if d.strip()
]


@dataclass
class AuthUser:
    id: str
    email: str
    first_name: Optional[str]
    last_name: Optional[str]
    username: str
    roles: list[str]
    access: list[str]
    associated_teams: dict
    associated_groups: Optional[dict] = None
    google_id: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> "AuthUser":
        return cls(
            id=data["id"],
            email=data["email"],
            first_name=data.get("first_name"),
            last_name=data.get("last_name"),
            username=data.get("username", ""),
            roles=data.get("roles", []),
            access=data.get("access", []),
            associated_teams=data.get("associated_teams", {}),
            associated_groups=data.get("associated_groups"),
            google_id=data.get("google_id"),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "username": self.username,
            "roles": self.roles,
            "access": self.access,
            "associated_teams": self.associated_teams,
            "associated_groups": self.associated_groups,
            "google_id": self.google_id,
        }


def email_domain_allowed(email: str) -> bool:
    """True when `email` passes the ALLOWED_EMAIL_DOMAINS allow-list.
    Always True when the allow-list is empty (the default)."""
    if not ALLOWED_EMAIL_DOMAINS:
        return True
    domain = (email or "").rsplit("@", 1)[-1].lower()
    return domain in ALLOWED_EMAIL_DOMAINS


def single_user() -> AuthUser:
    """The one local identity used when AUTH_MODE=single_user."""
    name = SINGLE_USER_NAME.strip()
    first, _, last = name.partition(" ")
    return AuthUser(
        id=SINGLE_USER_ID,
        email=SINGLE_USER_EMAIL,
        first_name=first or None,
        last_name=last or None,
        username=SINGLE_USER_EMAIL.split("@")[0],
        roles=["owner"],
        access=[],
        associated_teams={},
    )


async def _verify_oidc(authorization: str) -> Optional[AuthUser]:
    """Resolve the bearer token via the provider's userinfo endpoint.
    Returns None when the token is rejected; raises on transport failure so the
    caller can distinguish "not authenticated" from "auth provider is down"."""
    if not OIDC_USERINFO_URL:
        logger.error("AUTH_MODE=oidc but OIDC_USERINFO_URL is unset")
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                OIDC_USERINFO_URL,
                headers={"Authorization": authorization},
            )
        if response.status_code in (401, 403):
            return None
        if response.status_code != 200:
            logger.error("OIDC userinfo returned unexpected status", status_code=response.status_code)
            return None
        claims = response.json()
    except httpx.TimeoutException:
        logger.error("OIDC userinfo request timed out")
        raise
    except Exception:
        logger.exception("OIDC userinfo verification failed")
        raise

    email = claims.get(OIDC_EMAIL_CLAIM, "")
    if not email_domain_allowed(email):
        logger.warning("Rejected sign-in: email domain not in ALLOWED_EMAIL_DOMAINS")
        return None

    roles = claims.get(OIDC_ROLES_CLAIM) or []
    if isinstance(roles, str):
        roles = [r.strip() for r in roles.split(",") if r.strip()]

    return AuthUser(
        id=str(claims.get(OIDC_ID_CLAIM, "")),
        email=email,
        first_name=claims.get("given_name"),
        last_name=claims.get("family_name"),
        username=claims.get("preferred_username") or (email.split("@")[0] if email else ""),
        roles=list(roles),
        access=[],
        associated_teams={},
    )


async def verify_headers(authorization: str) -> Optional[AuthUser]:
    """Resolve the caller identity for the configured AUTH_MODE.
    Returns None when the credentials are invalid."""
    if AUTH_MODE == "single_user":
        return single_user()
    if AUTH_MODE == "oidc":
        return await _verify_oidc(authorization)
    logger.error("Unknown AUTH_MODE; refusing all requests", mode=AUTH_MODE)
    return None
