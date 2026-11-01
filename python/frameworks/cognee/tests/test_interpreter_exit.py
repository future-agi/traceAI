"""Check delivery and exporter closure, not just a successful process exit."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROGRAM = r'''
import atexit
import json
from pathlib import Path
import sys
import time
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult

mode, filename = sys.argv[1:]
output = Path(filename)

class Sink(SpanExporter):
    def __init__(self, label):
        self.label = label

    def record(self, operation, names=None):
        with output.open('a') as stream:
            stream.write(json.dumps([self.label, operation, names]) + '\n')

    def export(self, spans):
        # Simulate buffered I/O that cannot finish before atexit returns unless
        # shutdown actually waits. No real service or credentials are needed.
        time.sleep(0.2 if self.label != 'foreign' else 0)
        self.record('export', [span.name for span in spans])
        return SpanExportResult.SUCCESS

    def shutdown(self):
        self.record('shutdown')

if mode == 'baseline':
    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(Sink('baseline')))
else:
    from traceai_cognee import configure
    options = {}
    if mode == 'existing-provider':
        options['provider'] = TracerProvider()
    handle = configure(project_name='synthetic', exporter=Sink('adapter'), **options)
    provider = handle.provider
    if mode in ('late-shutdown', 'closed-generation'):
        # Registered later, this callback closes the generation after Python's
        # normal thread-join phase but before the generation's own exit hook.
        atexit.register(handle.shutdown)

provider.add_span_processor(BatchSpanProcessor(Sink('foreign')))
with provider.get_tracer('exit-regression').start_as_current_span('finished-before-exit'):
    pass
if mode == 'closed-generation':
    handle.shutdown()
# No force_flush or explicit provider.shutdown: use normal interpreter teardown.
'''


class InterpreterExitTests(unittest.TestCase):
    def check_delivery(self, mode):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'delivery.jsonl'
            result = subprocess.run(
                [sys.executable, '-I', '-c', PROGRAM, mode, str(output)],
                cwd=directory, capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, '')
            events = [json.loads(line) for line in output.read_text().splitlines()] if output.exists() else []
        label = 'baseline' if mode == 'baseline' else 'adapter'
        for exporter in (label, 'foreign'):
            self.assertEqual(
                [event[1:] for event in events if event[0] == exporter],
                [['export', ['finished-before-exit']], ['shutdown', None]],
                (mode, events),
            )

    def test_sdk_baseline_delivers_and_closes(self):
        self.check_delivery('baseline')

    def test_default_provider_delivers_and_closes(self):
        self.check_delivery('default-provider')

    def test_existing_provider_delivers_and_closes(self):
        self.check_delivery('existing-provider')

    def test_shutdown_from_later_exit_callback_delivers_and_closes(self):
        self.check_delivery('late-shutdown')

    def test_closed_generation_is_not_closed_twice_at_exit(self):
        self.check_delivery('closed-generation')


if __name__ == '__main__':
    unittest.main()
