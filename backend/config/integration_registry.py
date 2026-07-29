"""
Integration Registry — single source of truth for integration metadata.

Keys here are the values stored on `workflows.integrations_used` and
`skills.integrations_used`. For each key we record:

- `label`: human-readable name (currently FE-owned; duplicated here so the
  manifest endpoint can return one shape that's useful without joining)
- `provider`: the underlying connection provider. For Google sub-tools
  (gmail, google_calendar, …) this is `"google"` because they all share one
  OAuth connection. For everything else it equals the key.
- `internal`: True when the deployment supplies one shared credential for
  everyone (Grafana, Freshdesk, Freshchat, Semrush) instead of a per-user
  OAuth connection. These don't appear in a user's `integrations` table —
  there's nothing for the user to connect — so callers must treat them as
  always-available.

Consumers:
- `routers/workflows_router.py` derives `VALID_INTEGRATIONS` from the keys.
- `routers/integrations_router.py` exposes the registry at
  `GET /v1/integrations/manifest` for the frontend.
"""

from typing import TypedDict


class IntegrationMeta(TypedDict):
    label: str
    provider: str
    internal: bool


INTEGRATION_REGISTRY: dict[str, IntegrationMeta] = {
    # --- Google (one OAuth connection, many sub-tools) ---
    "google": {"label": "Google", "provider": "google", "internal": False},
    "gmail": {"label": "Gmail", "provider": "google", "internal": False},
    "google_calendar": {"label": "Google Calendar", "provider": "google", "internal": False},
    "google_drive": {"label": "Google Drive", "provider": "google", "internal": False},
    "google_sheets": {"label": "Google Sheets", "provider": "google", "internal": False},
    "google_docs": {"label": "Google Docs", "provider": "google", "internal": False},

    # --- Per-user OAuth ---
    "slack": {"label": "Slack", "provider": "slack", "internal": False},
    "notion": {"label": "Notion", "provider": "notion", "internal": False},
    "linear": {"label": "Linear", "provider": "linear", "internal": False},
    "github": {"label": "GitHub", "provider": "github", "internal": False},
    "ramp": {"label": "Ramp", "provider": "ramp", "internal": False},
    "posthog": {"label": "PostHog", "provider": "posthog", "internal": False},
    "figma": {"label": "Figma", "provider": "figma", "internal": False},
    "granola": {"label": "Granola", "provider": "granola", "internal": False},
    "meta_ads": {"label": "Meta Ads", "provider": "meta_ads", "internal": False},

    # --- Per-user form-based connect (not OAuth) ---
    "recruitee": {"label": "Recruitee", "provider": "recruitee", "internal": False},

    # --- Internal / shared-key (no per-user connect) ---
    "grafana": {"label": "Grafana", "provider": "grafana", "internal": True},
    "freshdesk": {"label": "Freshdesk", "provider": "freshdesk", "internal": True},
    "freshchat": {"label": "Freshchat", "provider": "freshchat", "internal": True},
    "semrush": {"label": "Semrush", "provider": "semrush", "internal": True},
}


VALID_INTEGRATIONS: set[str] = set(INTEGRATION_REGISTRY.keys())
INTERNAL_INTEGRATIONS: set[str] = {k for k, v in INTEGRATION_REGISTRY.items() if v["internal"]}


def provider_for(integration: str) -> str:
    """Map an integration key to its underlying connection provider.

    Returns the input unchanged for keys not in the registry so callers can
    feed in arbitrary strings (e.g. preview-step tool names) without crashing.
    """
    meta = INTEGRATION_REGISTRY.get(integration)
    return meta["provider"] if meta else integration


def is_internal(integration: str) -> bool:
    return integration in INTERNAL_INTEGRATIONS
