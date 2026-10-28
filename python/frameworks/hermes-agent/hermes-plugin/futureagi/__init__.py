"""Hermes directory-plugin wrapper for traceAI-hermes-agent.

Hermes installs ``traceAI-hermes-agent`` from this directory's pyproject.toml
when the plugin is enabled; all of the plugin's code lives in that package.
"""

from traceai_hermes_agent import register

__all__ = ["register"]
