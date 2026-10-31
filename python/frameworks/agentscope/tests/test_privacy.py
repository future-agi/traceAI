import pytest


def test_unknown_objects_do_not_invoke_metaclass_or_instance_getters(tracing):
    instrumentor, _, _ = tracing
    calls = []

    class Meta(type):
        def __getattribute__(cls, name):
            if name in ("__name__", "__module__"):
                calls.append(name)
                raise AssertionError("must bypass user metaclass")
            return super().__getattribute__(name)

        def __eq__(cls, other):
            calls.append("equality")
            raise AssertionError("must compare exact types by identity")

    class Hostile(metaclass=Meta):
        @property
        def __dict__(self):
            calls.append("dict")
            raise AssertionError("must bypass user descriptor")

        @property
        def content(self):
            calls.append("content")
            raise AssertionError("must not call getters")

        def __str__(self):
            calls.append("str")
            raise AssertionError("must not stringify")

    value = Hostile()
    attrs = instrumentor._attributes
    assert attrs.json({"unknown": value}) == '{"unknown":"<Hostile>"}'
    assert attrs.content(value) == ""
    assert (
        attrs.request("LLM", value, (), {"messages": [value]})["gen_ai.operation.name"]
        == "chat"
    )
    assert calls == []


def test_request_projection_never_discovers_client_or_mapping_credentials(tracing):
    from collections.abc import Mapping
    from types import MappingProxyType

    instrumentor, _, _ = tracing
    calls = []

    class UnknownHeaders(Mapping):
        def __iter__(self):
            calls.append("iter")
            raise AssertionError("must not iterate unknown mappings")

        def __len__(self):
            calls.append("len")
            raise AssertionError("must not size unknown mappings")

        def __getitem__(self, key):
            calls.append("getitem")
            raise AssertionError("must not read unknown mappings")

        def items(self):
            calls.append("items")
            raise AssertionError("must not call unknown mapping methods")

    class Client:
        def __getattribute__(self, name):
            calls.append(name)
            raise AssertionError("must not inspect an SDK client")

    class Model:
        def __init__(self, headers):
            self.client = Client()
            self.credential = Client()
            self.generate_kwargs = {"extra_headers": headers}

    attrs = instrumentor._attributes
    headers = UnknownHeaders()
    for value in (headers, MappingProxyType(headers)):
        projected = attrs.request("LLM", Model(value), (), {"extra_headers": value})
        assert projected["gen_ai.request.parameters"] == "{}"
        assert "Client" not in str(projected)
    assert calls == []


async def test_explicit_core_registration_scrubs_later_manual_model_span(tracing):
    from agentscope import model
    from fi_instrumentation import register_secret

    from .conftest import response
    from .test_instrumentor import spans_by_kind

    # An extra value known outside a transport can be explicitly registered by
    # the application. AgentScope itself no longer probes credential holders.
    secret = "explicit-out-of-band-credential-20261030"
    register_secret(secret)
    instrumentor, _, exporter = tracing
    value = response("echoed: " + secret)

    class LocalModel(model.ChatModelBase):
        async def __call__(self, *args, **kwargs):
            return value

    instance = LocalModel.__new__(LocalModel)
    assert await instance([]) is value
    (span,) = spans_by_kind(exporter, "LLM")
    assert span.attributes["output.value"] == "echoed: <redacted>"
    assert instrumentor._attributes.content(value) == "echoed: " + secret


@pytest.mark.parametrize("hidden", [False, True])
async def test_manual_unobserved_credentials_need_registration_or_hide_controls(retrace, hidden):
    from agentscope import model
    from fi_instrumentation import _secrets

    from .conftest import response
    from .test_instrumentor import spans_by_kind

    _, _, exporter = retrace(hide_outputs=hidden)
    secret = "manual-unobserved-sdk-credential-20261030"
    value = response("echoed: " + secret)

    class LocalModel(model.ChatModelBase):
        async def __call__(self, *args, **kwargs):
            return value

    instance = LocalModel.__new__(LocalModel)
    instance.api_key = secret
    assert await instance([]) is value
    assert secret not in _secrets.snapshot()
    (span,) = spans_by_kind(exporter, "LLM")
    # ROUND5 B2 OTHER: standalone instrumentors do not discover SDK credentials.
    if hidden:
        assert secret not in str(dict(span.attributes))
    else:
        assert span.attributes["output.value"] == "echoed: " + secret
