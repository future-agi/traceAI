"""Voice fixtures reuse the core's synthetic transport and real TLS server."""

import importlib.util
import sys
from pathlib import Path

_CORE_TESTS = Path(__file__).resolve().parents[1] / "tests" / "attachments"
sys.path.insert(0, str(_CORE_TESTS))
_spec = importlib.util.spec_from_file_location("attachment_test_support", _CORE_TESTS / "conftest.py")
_support = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_support)
TransportDouble = _support.TransportDouble
clean_attachment_env = _support.clean_attachment_env
manager = _support.manager
transport = _support.transport
from test_http_contract import server  # noqa: E402,F401
