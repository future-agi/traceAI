"""MCP (Model Context Protocol) server tracking for Claude Agent SDK.

This module provides tracking for MCP server tool attribution,
helping identify which external tool provider handles each tool call.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from opentelemetry.trace import Span

from ._attributes import ClaudeAgentAttributes, ClaudeAgentSpanKind

logger = logging.getLogger(__name__)

# Pattern for MCP tool names: mcp__<server>__<tool>
MCP_TOOL_PATTERN = re.compile(r"^mcp__([^_]+)__(.+)$")


@dataclass
class MCPServerInfo:
    """Information about an MCP server."""

    name: str
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    url: Optional[str] = None  # For remote MCP servers

    # Discovered tools from this server
    tools: Set[str] = field(default_factory=set)

    # Usage tracking
    tool_calls: int = 0
    total_duration_ms: float = 0.0


class MCPTracker:
    """Track MCP server tool attribution.

    This class manages MCP server configurations and attributes
    tool calls to their source servers.

    Usage:
        tracker = MCPTracker()

        # Register servers from options
        tracker.register_servers(mcp_servers_config)

        # Check if tool is from MCP
        server_name = tracker.get_tool_server("mcp__playwright__click")

        # Annotate span with MCP info
        tracker.annotate_tool_span(span, tool_name)
    """

    def __init__(self):
        """Initialize the MCP tracker."""
        self._servers: Dict[str, MCPServerInfo] = {}
        self._tool_to_server: Dict[str, str] = {}

    def register_servers(self, mcp_servers: Optional[Dict[str, Any]]) -> None:
        """Register MCP servers from configuration.

        Args:
            mcp_servers: Dictionary of server name to config
                Example:
                {
                    "playwright": {
                        "command": "npx",
                        "args": ["-y", "@anthropic/mcp-playwright"]
                    },
                    "puppeteer": {
                        "command": "node",
                        "args": ["puppeteer-server.js"]
                    }
                }
        """
        if not mcp_servers:
            return

        for name, config in mcp_servers.items():
            if isinstance(config, dict):
                info = MCPServerInfo(
                    name=name,
                    command=config.get("command"),
                    args=config.get("args"),
                    env=config.get("env"),
                    url=config.get("url"),
                )
            else:
                # Simple config, just the name
                info = MCPServerInfo(name=name)

            self._servers[name] = info
            logger.debug(f"Registered MCP server: {name}")

    def register_tool(self, tool_name: str, server_name: str) -> None:
        """Register a tool as belonging to a server.

        Args:
            tool_name: Full tool name
            server_name: Name of the MCP server
        """
        self._tool_to_server[tool_name] = server_name

        if server_name in self._servers:
            self._servers[server_name].tools.add(tool_name)

    def get_tool_server(self, tool_name: str) -> Optional[str]:
        """Get the server name for a tool.

        Args:
            tool_name: Tool name to look up

        Returns:
            Server name or None if not an MCP tool
        """
        # Check explicit registration first
        if tool_name in self._tool_to_server:
            return self._tool_to_server[tool_name]

        # Try to parse from tool name pattern
        match = MCP_TOOL_PATTERN.match(tool_name)
        if match:
            server_name = match.group(1)
            # Verify server is registered
            if server_name in self._servers:
                return server_name
            # Return anyway for attribution
            return server_name

        return None

    def parse_tool_name(self, tool_name: str) -> Optional[Dict[str, str]]:
        """Parse an MCP tool name into components.

        Args:
            tool_name: Full tool name (e.g., "mcp__playwright__click")

        Returns:
            Dict with 'server' and 'tool' keys, or None
        """
        match = MCP_TOOL_PATTERN.match(tool_name)
        if match:
            return {
                "server": match.group(1),
                "tool": match.group(2),
            }
        return None

    def is_mcp_tool(self, tool_name: str) -> bool:
        """Check if a tool is an MCP tool.

        Args:
            tool_name: Tool name to check

        Returns:
            True if it's an MCP tool
        """
        return self.get_tool_server(tool_name) is not None

    def annotate_tool_span(
        self,
        span: Span,
        tool_name: str,
        duration_ms: Optional[float] = None,
    ) -> bool:
        """Annotate a span with MCP server information.

        Args:
            span: Span to annotate
            tool_name: Tool name
            duration_ms: Optional tool duration for tracking

        Returns:
            True if tool was attributed to an MCP server
        """
        server_name = self.get_tool_server(tool_name)
        if not server_name:
            return False

        # Get server info
        server_info = self._servers.get(server_name)

        # Set MCP-specific attributes
        span.set_attribute(ClaudeAgentAttributes.SPAN_KIND, ClaudeAgentSpanKind.MCP_TOOL.value)
        span.set_attribute(ClaudeAgentAttributes.MCP_SERVER_NAME, server_name)

        if server_info:
            if server_info.command:
                span.set_attribute(
                    ClaudeAgentAttributes.MCP_SERVER_COMMAND,
                    server_info.command,
                )
            if server_info.args:
                span.set_attribute(
                    ClaudeAgentAttributes.MCP_SERVER_ARGS,
                    " ".join(server_info.args),
                )

            # Update usage tracking
            server_info.tool_calls += 1
            if duration_ms:
                server_info.total_duration_ms += duration_ms

        # Parse and set the actual tool name (without prefix)
        parsed = self.parse_tool_name(tool_name)
        if parsed:
            span.set_attribute("claude_agent.mcp.tool_name", parsed["tool"])

        return True

    def get_server_info(self, server_name: str) -> Optional[MCPServerInfo]:
        """Get information about an MCP server.

        Args:
            server_name: Name of the server

        Returns:
            MCPServerInfo or None
        """
        return self._servers.get(server_name)

    def get_server_stats(self, server_name: str) -> Dict[str, Any]:
        """Get usage statistics for a server.

        Args:
            server_name: Name of the server

        Returns:
            Dict with usage statistics
        """
        info = self._servers.get(server_name)
        if not info:
            return {}

        return {
            "name": info.name,
            "command": info.command,
            "tool_count": len(info.tools),
            "tools": list(info.tools),
            "total_calls": info.tool_calls,
            "total_duration_ms": info.total_duration_ms,
        }

    def get_all_server_stats(self) -> Dict[str, Dict[str, Any]]:
        """Get usage statistics for all servers.

        Returns:
            Dict of server name to stats
        """
        return {
            name: self.get_server_stats(name)
            for name in self._servers
        }

    def annotate_conversation_span(self, span: Span) -> None:
        """Annotate a conversation span with MCP server info.

        Args:
            span: Conversation span to annotate
        """
        if not self._servers:
            return

        # List of configured servers
        server_names = list(self._servers.keys())
        span.set_attribute("claude_agent.mcp.servers", ",".join(server_names))
        span.set_attribute("claude_agent.mcp.server_count", len(server_names))

        # Count of tools by server
        tool_counts = {
            name: len(info.tools)
            for name, info in self._servers.items()
        }
        if any(tool_counts.values()):
            span.set_attribute(
                "claude_agent.mcp.discovered_tools",
                sum(tool_counts.values()),
            )

    def clear(self) -> None:
        """Clear all MCP server tracking."""
        self._servers.clear()
        self._tool_to_server.clear()
        logger.debug("Cleared all MCP server tracking")

    @property
    def server_count(self) -> int:
        """Number of registered servers."""
        return len(self._servers)

    @property
    def server_names(self) -> List[str]:
        """List of registered server names."""
        return list(self._servers.keys())
