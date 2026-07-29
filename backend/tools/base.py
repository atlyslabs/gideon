"""
BaseTool — abstract base class for all tools Claude can call.
Each tool defines its name, description, input schema, and execute method.
"""

from abc import ABC, abstractmethod
from typing import Any


class BaseTool(ABC):
    # Subclasses override this to True when the tool should only run for users
    # in a service-specific allowlist (enforced in tools/tool_executor.py).
    admin_only: bool = False

    @property
    @abstractmethod
    def name(self) -> str:
        """Tool name used in Claude tool_use (e.g. 'list_emails')."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description for Claude to understand when to use this tool."""
        ...

    @property
    @abstractmethod
    def input_schema(self) -> dict:
        """JSON Schema for the tool's input parameters."""
        ...

    @property
    @abstractmethod
    def provider(self) -> str:
        """Which integration provides this tool (e.g. 'google', 'slack', 'notion')."""
        ...

    @abstractmethod
    async def execute(self, params: dict[str, Any], access_token: str) -> dict[str, Any]:
        """Execute the tool with the given params and user's decrypted access token."""
        ...

    def to_claude_schema(self) -> dict:
        """Convert to Claude API tool format."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
