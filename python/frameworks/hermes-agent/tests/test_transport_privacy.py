"""No credential-bearing exception or response text may reach host/SDK logs."""
import logging

import pytest
import requests

from traceai_hermes_agent import FutureAGIHermesPlugin, build_tracer_provider


@pytest.mark.parametrize('value', ['synthetic-invalid-secret\n', ' bad-key', 'bad-☃'])
def test_bad_environment_headers_disable_without_traceback(value, tmp_path, monkeypatch, caplog):
    env = {'FI_API_KEY': value, 'FI_SECRET_KEY': 'synthetic-secret',
           'FI_AGENT_CONFIG_DIR': str(tmp_path / 'config'), 'HERMES_HOME': str(tmp_path / 'host')}
    monkeypatch.setattr('traceai_hermes_agent._setting', env.get)
    with caplog.at_level(logging.DEBUG):
        plugin = FutureAGIHermesPlugin(register_atexit=False)
        plugin.callback('pre_llm_call')(session_id='test', turn_id='test')
    assert plugin._provider is None and plugin._disabled
    assert 'tracing disabled' in caplog.text
    assert value not in caplog.text and 'synthetic-secret' not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_direct_provider_rejects_headers_with_constant_error():
    with pytest.raises(ValueError, match='^INVALID_HEADER_VALUE$'):
        build_tracer_provider(api_key='invented-key\n', secret_key='invented-secret',
                              base_url='http://127.0.0.1:9', project_name='test')


@pytest.mark.parametrize('failure', ['exception', 'response'])
def test_export_failure_never_logs_credential_bearing_text(failure, monkeypatch, caplog):
    marker = 'synthetic-private-key-marker'

    def request(*args, **kwargs):
        if failure == 'exception':
            try:
                raise ValueError(marker)
            except ValueError as cause:
                raise requests.exceptions.InvalidHeader(marker) from cause
        response = requests.Response()
        response.status_code = 401
        response._content = marker.encode()
        response.reason = marker
        return response

    monkeypatch.setattr(requests.Session, 'request', request)
    provider = build_tracer_provider(api_key=marker, secret_key='synthetic-secret',
        base_url='http://127.0.0.1:9', project_name='test')
    try:
        with caplog.at_level(logging.DEBUG):
            provider.get_tracer('test').start_span('test').end()
            provider.force_flush(timeout_millis=1000)
        assert marker not in caplog.text and 'synthetic-secret' not in caplog.text
        assert all(record.exc_info is None for record in caplog.records)
        assert 'EXPORT_FAILED' in caplog.text
    finally:
        provider.shutdown(timeout_millis=1000)
