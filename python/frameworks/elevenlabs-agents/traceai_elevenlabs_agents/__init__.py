"""Forward ElevenLabs Agents post-call OpenTelemetry traces to Future AGI.

ElevenLabs runs the agent; this package runs in your environment. It verifies the
``post_call_transcription_otel`` webhook signature, maps the conversation onto the
Future AGI Voice-tab attributes and exports it as OTLP/HTTP JSON with your keys.

Content is captured by default and follows the standard traceAI switch,
``fi_instrumentation.TraceConfig`` / ``FI_HIDE_*``; see :mod:`.privacy` for what is
never exported in any mode.
"""

from .backfill import backfill, fetch_conversation
from .config import ConfigError, ForwarderConfig
from .exporter import ExportError, OTLPJsonExporter, traces_endpoint
from .forwarder import WEBHOOK_TYPE, Forwarder
from .map import MappingInputError, map_otlp_traces
from .server import make_server, serve
from .store import ForwardedStore
from .verify import SignatureError, sign, verify_signature
from .version import __version__

__all__ = [
    "ConfigError",
    "ExportError",
    "ForwardedStore",
    "Forwarder",
    "ForwarderConfig",
    "MappingInputError",
    "OTLPJsonExporter",
    "SignatureError",
    "WEBHOOK_TYPE",
    "__version__",
    "backfill",
    "fetch_conversation",
    "make_server",
    "map_otlp_traces",
    "serve",
    "sign",
    "traces_endpoint",
    "verify_signature",
]
