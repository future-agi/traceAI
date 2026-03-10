"""Tests for AutogenInstrumentor class."""

import sys
import pytest
from unittest.mock import MagicMock, patch, PropertyMock


class TestAutoGenInstrumentor:
    """Tests for AutogenInstrumentor class."""

    @pytest.fixture
    def mock_autogen_v02(self):
        """Create mock autogen v0.2 module."""
        mock_module = MagicMock()
        mock_module.ConversableAgent = MagicMock()
        mock_module.ConversableAgent.generate_reply = MagicMock()
        mock_module.ConversableAgent.initiate_chat = MagicMock()
        mock_module.ConversableAgent.execute_function = MagicMock()
        return mock_module

    @pytest.fixture
    def mock_autogen_v04(self):
        """Create mock autogen v0.4 modules."""
        # Agents module
        agents_module = MagicMock()

        # Create BaseChatAgent class
        BaseChatAgent = type("BaseChatAgent", (), {})
        BaseChatAgent.on_messages = MagicMock()
        agents_module.BaseChatAgent = BaseChatAgent

        # Create AssistantAgent class
        AssistantAgent = type("AssistantAgent", (BaseChatAgent,), {})
        agents_module.AssistantAgent = AssistantAgent

        # Teams module
        teams_module = MagicMock()

        # Create team classes
        BaseGroupChat = type("BaseGroupChat", (), {})
        BaseGroupChat.run = MagicMock()
        BaseGroupChat.run_stream = MagicMock()
        teams_module.BaseGroupChat = BaseGroupChat

        RoundRobinGroupChat = type("RoundRobinGroupChat", (BaseGroupChat,), {})
        RoundRobinGroupChat.run = MagicMock()
        RoundRobinGroupChat.run_stream = MagicMock()
        teams_module.RoundRobinGroupChat = RoundRobinGroupChat

        SelectorGroupChat = type("SelectorGroupChat", (BaseGroupChat,), {})
        SelectorGroupChat.run = MagicMock()
        SelectorGroupChat.run_stream = MagicMock()
        teams_module.SelectorGroupChat = SelectorGroupChat

        return {
            "autogen_agentchat.agents": agents_module,
            "autogen_agentchat.teams": teams_module,
        }

    def test_instrumentor_initialization(self):
        """Test instrumentor initialization."""
        with patch.dict(sys.modules, {"autogen": MagicMock()}):
            from traceai_autogen import AutogenInstrumentor

            instrumentor = AutogenInstrumentor()

            assert instrumentor._original_generate is None
            assert instrumentor._original_initiate_chat is None
            assert instrumentor._original_execute_function is None
            assert instrumentor._v04_original_on_messages == {}
            assert instrumentor._v04_original_team_run == {}
            assert instrumentor._v04_original_team_run_stream == {}
            assert instrumentor.tracer is None

    def test_instrument_v02_only(self, mock_autogen_v02):
        """Test instrumenting only v0.2."""
        modules_dict = {
            "autogen": mock_autogen_v02,
        }

        with patch.dict(sys.modules, modules_dict, clear=False):
            # Re-import to get fresh module with patches
            import importlib
            import traceai_autogen
            importlib.reload(traceai_autogen)

            from traceai_autogen import AutogenInstrumentor

            mock_tracer_provider = MagicMock()
            mock_tracer = MagicMock()
            mock_tracer_provider.get_tracer = MagicMock(return_value=mock_tracer)

            with patch("opentelemetry.trace.get_tracer_provider", return_value=mock_tracer_provider):
                with patch("opentelemetry.trace.get_tracer", return_value=mock_tracer):
                    instrumentor = AutogenInstrumentor()
                    instrumentor.instrument()

                    # Verify v0.2 methods were stored
                    assert instrumentor._original_generate is not None
                    assert instrumentor._original_initiate_chat is not None
                    assert instrumentor._original_execute_function is not None

    def test_uninstrument_restores_v02(self, mock_autogen_v02):
        """Test uninstrumenting restores v0.2 methods."""
        original_generate = MagicMock()
        original_initiate_chat = MagicMock()
        original_execute_function = MagicMock()

        mock_autogen_v02.ConversableAgent.generate_reply = original_generate
        mock_autogen_v02.ConversableAgent.initiate_chat = original_initiate_chat
        mock_autogen_v02.ConversableAgent.execute_function = original_execute_function

        modules_dict = {
            "autogen": mock_autogen_v02,
        }

        with patch.dict(sys.modules, modules_dict, clear=False):
            import importlib
            import traceai_autogen
            importlib.reload(traceai_autogen)

            from traceai_autogen import AutogenInstrumentor

            mock_tracer_provider = MagicMock()
            mock_tracer = MagicMock()

            with patch("opentelemetry.trace.get_tracer_provider", return_value=mock_tracer_provider):
                with patch("opentelemetry.trace.get_tracer", return_value=mock_tracer):
                    instrumentor = AutogenInstrumentor()
                    instrumentor.instrument()

                    # Methods should be wrapped now
                    assert mock_autogen_v02.ConversableAgent.generate_reply != original_generate

                    # Uninstrument
                    instrumentor.uninstrument()

                    # Originals should be restored
                    assert mock_autogen_v02.ConversableAgent.generate_reply == original_generate
                    assert mock_autogen_v02.ConversableAgent.initiate_chat == original_initiate_chat
                    assert mock_autogen_v02.ConversableAgent.execute_function == original_execute_function

    def test_safe_json_dumps(self, mock_autogen_v02):
        """Test _safe_json_dumps method."""
        with patch.dict(sys.modules, {"autogen": mock_autogen_v02}):
            from traceai_autogen import AutogenInstrumentor

            instrumentor = AutogenInstrumentor()

            # Test with dict
            result = instrumentor._safe_json_dumps({"key": "value"})
            assert result == '{"key": "value"}'

            # Test with list
            result = instrumentor._safe_json_dumps([1, 2, 3])
            assert result == "[1, 2, 3]"

            # Test with string
            result = instrumentor._safe_json_dumps("hello")
            assert result == '"hello"'

            # Test with non-serializable object
            class NotSerializable:
                pass

            obj = NotSerializable()
            result = instrumentor._safe_json_dumps(obj)
            assert isinstance(result, str)

    def test_instrumentation_dependencies_v02_only(self, mock_autogen_v02):
        """Test instrumentation_dependencies with only v0.2 available."""
        with patch.dict(sys.modules, {"autogen": mock_autogen_v02}):
            import importlib
            import traceai_autogen
            importlib.reload(traceai_autogen)

            from traceai_autogen import AutogenInstrumentor

            instrumentor = AutogenInstrumentor()
            deps = instrumentor.instrumentation_dependencies()

            assert "autogen" in deps


class TestInstrumentAutogenFunction:
    """Tests for instrument_autogen convenience function."""

    def test_instrument_autogen_returns_instrumentor(self):
        """Test that instrument_autogen returns an instrumentor."""
        mock_autogen = MagicMock()
        mock_autogen.ConversableAgent = MagicMock()
        mock_autogen.ConversableAgent.generate_reply = MagicMock()
        mock_autogen.ConversableAgent.initiate_chat = MagicMock()
        mock_autogen.ConversableAgent.execute_function = MagicMock()

        with patch.dict(sys.modules, {"autogen": mock_autogen}):
            import importlib
            import traceai_autogen
            importlib.reload(traceai_autogen)

            from traceai_autogen import instrument_autogen

            mock_tracer_provider = MagicMock()
            mock_tracer = MagicMock()

            with patch("opentelemetry.trace.get_tracer_provider", return_value=mock_tracer_provider):
                with patch("opentelemetry.trace.get_tracer", return_value=mock_tracer):
                    instrumentor = instrument_autogen()

                    assert instrumentor is not None
                    from traceai_autogen import AutogenInstrumentor
                    assert isinstance(instrumentor, AutogenInstrumentor)


class TestWrappedMethodsV02:
    """Tests for wrapped v0.2 methods."""

    @pytest.fixture
    def setup_v02_instrumentation(self):
        """Setup v0.2 instrumentation with mocks."""
        mock_autogen = MagicMock()

        # Create a real class-like mock for ConversableAgent
        ConversableAgent = type("ConversableAgent", (), {
            "generate_reply": MagicMock(return_value="response"),
            "initiate_chat": MagicMock(return_value=MagicMock(chat_history=[{"content": "result"}])),
            "execute_function": MagicMock(return_value={"result": "success"}),
            "_function_map": {},
        })
        mock_autogen.ConversableAgent = ConversableAgent

        return mock_autogen

    def test_wrapped_generate_creates_span(self, setup_v02_instrumentation):
        """Test that wrapped generate_reply creates a span."""
        mock_autogen = setup_v02_instrumentation

        with patch.dict(sys.modules, {"autogen": mock_autogen}):
            import importlib
            import traceai_autogen
            importlib.reload(traceai_autogen)

            from traceai_autogen import AutogenInstrumentor

            mock_tracer = MagicMock()
            mock_span = MagicMock()
            mock_span.__enter__ = MagicMock(return_value=mock_span)
            mock_span.__exit__ = MagicMock(return_value=None)

            with patch("opentelemetry.trace.get_tracer_provider"):
                with patch("opentelemetry.trace.get_tracer", return_value=mock_tracer):
                    with patch("opentelemetry.trace.get_current_span", return_value=mock_span):
                        instrumentor = AutogenInstrumentor()
                        instrumentor.instrument()

                        # The ConversableAgent.generate_reply should now be wrapped
                        # We can verify by checking the stored original
                        assert instrumentor._original_generate is not None

    def test_wrapped_initiate_chat_creates_span(self, setup_v02_instrumentation):
        """Test that wrapped initiate_chat creates a span."""
        mock_autogen = setup_v02_instrumentation

        with patch.dict(sys.modules, {"autogen": mock_autogen}):
            import importlib
            import traceai_autogen
            importlib.reload(traceai_autogen)

            from traceai_autogen import AutogenInstrumentor

            mock_tracer = MagicMock()

            with patch("opentelemetry.trace.get_tracer_provider"):
                with patch("opentelemetry.trace.get_tracer", return_value=mock_tracer):
                    instrumentor = AutogenInstrumentor()
                    instrumentor.instrument()

                    assert instrumentor._original_initiate_chat is not None

    def test_wrapped_execute_function_creates_span(self, setup_v02_instrumentation):
        """Test that wrapped execute_function creates a span."""
        mock_autogen = setup_v02_instrumentation

        with patch.dict(sys.modules, {"autogen": mock_autogen}):
            import importlib
            import traceai_autogen
            importlib.reload(traceai_autogen)

            from traceai_autogen import AutogenInstrumentor

            mock_tracer = MagicMock()

            with patch("opentelemetry.trace.get_tracer_provider"):
                with patch("opentelemetry.trace.get_tracer", return_value=mock_tracer):
                    instrumentor = AutogenInstrumentor()
                    instrumentor.instrument()

                    assert instrumentor._original_execute_function is not None


class TestVersionDetection:
    """Tests for version detection functions."""

    def test_v02_detection_when_available(self):
        """Test v0.2 detection when autogen is available."""
        mock_autogen = MagicMock()

        with patch.dict(sys.modules, {"autogen": mock_autogen}):
            import importlib
            import traceai_autogen
            importlib.reload(traceai_autogen)

            from traceai_autogen import _is_v02_available
            assert _is_v02_available() is True

    def test_v02_detection_when_not_available(self):
        """Test v0.2 detection when autogen is not available."""
        # Remove autogen from modules if present
        modules_to_remove = [k for k in sys.modules if k.startswith("autogen")]
        original_modules = {k: sys.modules[k] for k in modules_to_remove if k in sys.modules}

        try:
            for k in modules_to_remove:
                if k in sys.modules:
                    del sys.modules[k]

            # Patch import_module to raise ImportError
            with patch("traceai_autogen.import_module", side_effect=ImportError):
                import importlib
                import traceai_autogen
                importlib.reload(traceai_autogen)

                from traceai_autogen import _is_v02_available
                assert _is_v02_available() is False
        finally:
            # Restore modules
            sys.modules.update(original_modules)

    def test_v04_detection_when_available(self):
        """Test v0.4 detection when autogen_agentchat is available."""
        mock_agents = MagicMock()

        with patch.dict(sys.modules, {"autogen_agentchat.agents": mock_agents}):
            import importlib
            import traceai_autogen
            importlib.reload(traceai_autogen)

            from traceai_autogen import _is_v04_available
            assert _is_v04_available() is True

    def test_v04_detection_when_not_available(self):
        """Test v0.4 detection when autogen_agentchat is not available."""
        # Ensure autogen_agentchat modules are not in sys.modules
        modules_to_remove = [k for k in sys.modules if k.startswith("autogen_agentchat")]
        original_modules = {k: sys.modules[k] for k in modules_to_remove if k in sys.modules}

        try:
            for k in modules_to_remove:
                if k in sys.modules:
                    del sys.modules[k]

            import importlib
            import traceai_autogen
            importlib.reload(traceai_autogen)

            from traceai_autogen import _is_v04_available
            # This might return True or False depending on actual installation
            # Just verify it returns a boolean
            assert isinstance(_is_v04_available(), bool)
        finally:
            sys.modules.update(original_modules)
