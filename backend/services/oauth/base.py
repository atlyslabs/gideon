"""
OAuth Provider — abstract base class for all OAuth integrations.
Each provider implements: get_auth_url, handle_callback, refresh_token.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class OAuthTokens:
    """Result of a successful OAuth callback."""
    access_token: str
    refresh_token: Optional[str] = None
    bot_token: Optional[str] = None
    token_expiry: Optional[datetime] = None
    scopes: list[str] | None = None
    extra_data: dict | None = None


class OAuthProvider(ABC):
    """Base class for OAuth providers."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """e.g. 'google', 'slack', 'notion'"""
        ...

    @abstractmethod
    def get_auth_url(self, state: str) -> str:
        """Generate the OAuth authorization URL. State is for CSRF protection."""
        ...

    @abstractmethod
    async def handle_callback(self, code: str) -> OAuthTokens:
        """Exchange authorization code for tokens."""
        ...

    async def refresh_access_token(self, refresh_token: str) -> OAuthTokens:
        """Refresh an expired access token. Not all providers support this."""
        raise NotImplementedError(f"{self.provider_name} does not support token refresh")
