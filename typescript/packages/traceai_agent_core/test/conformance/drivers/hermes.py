"""Synthetic Hermes hook driver. The production exporter sends OTLP/protobuf to loopback.
No host session files, model keys, or external endpoints are used.
"""
import concurrent.futures
import json
import sys
import time
from traceai_hermes_agent import register

request = json.load(sys.stdin)

class Context:
    def __init__(self):
        self.hooks = {}
    def register_hook(self, name, callback):
        self.hooks.setdefault(name, []).append(callback)
    # Match the native PluginContext API: the gateway's process cwd and an
    # arbitrary hook payload cannot establish the active client's workspace.
    def session_surface(self):
        return 'cli'
    def session_cwd(self):
        return request['project']
    def fire(self, name, **kwargs):
        kwargs.setdefault('telemetry_schema_version', 'hermes.observer.v1')
        for callback in self.hooks.get(name, []):
            callback(**kwargs)

ctx = Context()
register(ctx)
for s in request['turns']:
    base = dict(session_id=s['session'], turn_id=s['id'], task_id='task', platform='cli', cwd=request['project'])
    def emit(name, **kwargs):
        ctx.fire(name, **base, **kwargs)
    emit('pre_llm_call', user_message=s['input'] + ' ' + s['secret'], conversation_history=[], is_first_turn=True, model='conf-model')
    for i in range(s['calls']):
        now = time.time()
        messages = [{'role': 'user', 'content': s['input'] + ' ' + s['secret']}, {'role': 'assistant', 'content': s['output']}, {'role': 'tool', 'isError': s['outcome'] == 'error', 'content': s['error'] if s['outcome'] == 'error' else s['tool']}]
        emit('pre_api_request', api_request_id=str(i), model='conf-model', provider='mock', request_messages=messages, started_at=now, api_call_count=i + 1)
        emit('post_api_request', api_request_id=str(i), model='conf-model', response_model='conf-model', provider='mock', started_at=now, ended_at=now + 0.001, usage={'input_tokens': 10, 'prompt_tokens': 10, 'output_tokens': 4, 'total_tokens': 14}, assistant_message={'content': s['output']}, response={'content': s['output']})
    def tool(i):
        emit('post_tool_call', tool_name='read', tool_call_id=str(i), api_request_id=str(min(i, s['calls'] - 1)), args={'path': s['input'], 'password': 'SYNTH_PASSWORD_19275'}, result=s['tool'], duration_ms=1, status='ok')
    if s.get('overlap'):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(tool, range(s['tools'])))
    else:
        for i in range(s['tools']):
            tool(i)
    emit('post_llm_call', user_message=s['input'], assistant_response=s['output'], model='conf-model')
    if s.get('lateMs'):
        time.sleep(s['lateMs'] / 1000)
    emit('on_session_end', completed=s['outcome'] == 'success', failed=s['outcome'] == 'error', interrupted=s['outcome'] == 'interrupted', turn_exit_reason=s['error'] if s['outcome'] == 'error' else 'text_response', model='conf-model')
ctx.fire('on_session_finalize')
