"""Send one OTLP/HTTP JSON export request to the Future AGI collector.

Uncompressed JSON, traces only, ``X-Api-Key`` / ``X-Secret-Key`` headers, the
same ``{FI_BASE_URL}/tracer/v1/traces`` endpoint ``fi_instrumentation`` uses.
Synchronous on purpose: the webhook is acknowledged only after the collector
accepted the batch.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit


class ExportError(Exception):
    """The collector did not accept the batch. ``detail`` never contains credentials or content."""

    def __init__(self, detail: str, status: Optional[int] = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


def traces_endpoint(base_url: str) -> str:
    """``https://api.futureagi.com`` -> ``https://api.futureagi.com/tracer/v1/traces``."""
    parts = urlsplit(base_url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("FI_BASE_URL must be an http(s) URL")
    return urlunsplit((parts.scheme, parts.netloc, "/tracer/v1/traces", "", ""))


class OTLPJsonExporter:
    def __init__(self, endpoint: str, api_key: str, secret_key: str, timeout: float = 30.0) -> None:
        if not api_key or not secret_key:
            raise ValueError("FI_API_KEY and FI_SECRET_KEY are required")
        self.endpoint = endpoint
        self._headers = {
            "Content-Type": "application/json",
            "X-Api-Key": api_key,
            "X-Secret-Key": secret_key,
        }
        self.timeout = timeout

    def export(self, export_request: Mapping[str, Any]) -> Dict[str, Any]:
        body = json.dumps(export_request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(self.endpoint, data=body, headers=self._headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as error:
            raise ExportError("collector returned HTTP {0}".format(error.code), error.code) from None
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise ExportError("collector unreachable ({0})".format(type(reason).__name__)) from None
        if not 200 <= status < 300:
            raise ExportError("collector returned HTTP {0}".format(status), status)
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            parsed = {}
        partial = parsed.get("partialSuccess") if isinstance(parsed, dict) else None
        if isinstance(partial, dict):
            try:
                rejected = int(partial.get("rejectedSpans") or 0)
            except (TypeError, ValueError):
                rejected = 0
            if rejected:
                raise ExportError("collector rejected {0} span(s)".format(rejected), status)
        return parsed if isinstance(parsed, dict) else {}
