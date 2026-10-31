"""Small stdlib-only attribution/protocol tests; no real provider or agent runs."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.dont_write_bytecode = True
sys.path[:0] = [str(Path(__file__).resolve().parent)]
from profile_compare import (STAGES, TARGETS, competitive_budget_context, node_stages,
                             observer_effect, python_tag, render, summarize)


class ProfileTests(unittest.TestCase):
    def test_python_privacy_config_is_not_wrapper(self):
        self.assertEqual(python_tag('/x/fi_instrumentation/instrumentation/config.py', 'mask'), 'privacy_S5_S6')
        self.assertEqual(python_tag('/x/fi_instrumentation/auto/wire/_privacy.py', 'observe_request'), 'privacy_S1')
        self.assertEqual(python_tag('/x/fi_instrumentation/auto/wire/_core.py', 'parse_sse'), 'sse_reassembly')

    def node_fixture(self, url, name, parent=None):
        rows = [{'id': 1, 'callFrame': {'functionName': name, 'url': url, 'lineNumber': 0, 'columnNumber': 1}, 'children': []}]
        if parent:
            rows[0]['children'] = [2]
            rows.append({'id': 2, 'callFrame': {'functionName': parent, 'url': 'file:///x/harness.mjs', 'lineNumber': 0, 'columnNumber': 0}, 'children': []})
        profile = {'nodes': rows, 'samples': [2 if parent else 1], 'timeDeltas': [100]}
        with tempfile.TemporaryDirectory(dir='/Volumes/nikm4/futureagi/tmp/bench-compare') as work:
            raw = Path(work) / 'sample.cpuprofile'; raw.write_text(json.dumps(profile))
            return node_stages(raw, {'line_starts': [0], 'ranges': [{'start': 0, 'end': 10, 'stage': 'privacy_S5_S6', 'ours': True}], 'labels': {'ab': 'scrubValue'}}, 2, 'traceai')

    def node_chain_fixture(self, frames, tool='braintrust'):
        nodes = [{'id': i + 1, 'callFrame': {'url': url, 'functionName': name, 'lineNumber': 0, 'columnNumber': 1},
                  'children': [i + 2] if i + 1 < len(frames) else []}
                 for i, (url, name) in enumerate(frames)]
        with tempfile.TemporaryDirectory(dir='/Volumes/nikm4/futureagi/tmp/bench-compare') as work:
            raw = Path(work) / 'sample.cpuprofile'
            raw.write_text(json.dumps({'nodes': nodes, 'samples': [len(nodes)], 'timeDeltas': [100]}))
            return node_stages(raw, {'line_starts': [0], 'ranges': [
                {'start': 0, 'end': 10, 'stage': 'privacy_S5_S6', 'ours': True}]}, 2, tool)

    def test_provider_sdk_work_cannot_inherit_braintrust_wrapper(self):
        bt = 'file:///x/node_modules/braintrust/dist/index.js'
        sdk = 'file:///x/node_modules/openai/internal/parse.mjs'
        for function in ('request', 'APIPromise', 'parseResponse'):
            with self.subTest(function=function):
                stages, _, info = self.node_chain_fixture([(bt, 'traceStreamingCall'), (sdk, function)])
                self.assertEqual(stages, {'unattributed': 50})
                self.assertEqual(info['boundary_stopped_samples'], 1)
                self.assertEqual(info['boundary_stopped_us_per_call'], 50)

    def test_provider_sdk_builtin_work_cannot_inherit_either_agent(self):
        sdk = 'file:///x/node_modules/openai/internal/parse.mjs'
        harness = 'file:///x/profile_node.mjs'
        for tool, root in (('braintrust', ('file:///x/node_modules/braintrust/index.js', 'traceStreamingCall')),
                           ('traceai', ('file:///x/runtime.cjs', 'scrubValue'))):
            for shim in ('profileJSONParse', 'profileJSONStringify', 'profileBodyCopy'):
                with self.subTest(tool=tool, shim=shim):
                    stages, _, _ = self.node_chain_fixture([root, (sdk, 'parseResponse'), (harness, shim)], tool)
                    self.assertEqual(stages, {'unattributed': 50})

    def test_harness_and_other_provider_boundaries_stop_propagation(self):
        bt = 'file:///x/node_modules/braintrust/index.js'
        for url in ('file:///x/node-openai_chat-braintrust-cpu.mjs', 'file:///x/app.mjs',
                    'file:///x/node_modules/@anthropic-ai/sdk/core.js',
                    'file:///x/node_modules/@google/genai/index.js',
                    'file:///x/node_modules/undici/index.js', 'node:internal/deps/undici/undici'):
            with self.subTest(url=url):
                stages, _, _ = self.node_chain_fixture([(bt, 'traceStreamingCall'), (url, 'invoke'),
                                                        ('file:///x/profile_node.mjs', 'profileJSONParse')])
                self.assertEqual(stages, {'unattributed': 50})

    def test_builtin_can_inherit_without_crossing_boundary(self):
        bt = 'file:///x/node_modules/braintrust/index.js'
        stages, _, _ = self.node_chain_fixture([(bt, 'deepCopy'), ('node:buffer', 'from'),
                                                ('file:///x/profile_node.mjs', 'profileBodyCopy')])
        self.assertEqual(stages, {'body_copy': 50})

    def test_generic_stream_wrappers_are_not_reassembly(self):
        for name in ('traceStreamingCall', 'wrapStream', 'generator', 'streamingCall'):
            with self.subTest(name=name):
                stages, _, _ = self.node_chain_fixture([('file:///x/node_modules/braintrust/index.js', name)])
                self.assertEqual(stages, {'wrapper': 50})

    def test_reviewed_chunk_aggregation_is_reassembly(self):
        stages, _, _ = self.node_chain_fixture([('file:///x/node_modules/braintrust/index.js', 'aggregateChatCompletionChunks')])
        self.assertEqual(stages, {'sse_reassembly': 50})

    def test_app_filename_does_not_make_braintrust_attribution(self):
        stages, _, _ = self.node_fixture('file:///x/node-braintrust-cpu.mjs', 'main')
        self.assertEqual(stages, {'unattributed': 50})

    def test_braintrust_package_is_attributed(self):
        stages, _, _ = self.node_fixture('file:///x/node_modules/braintrust/dist/index.js', 'startSpan')
        self.assertEqual(stages, {'span': 50})

    def test_node_private_range_and_symbol_label(self):
        stages, hot, _ = self.node_fixture('file:///x/runtime.cjs', 'ab')
        self.assertEqual(stages, {'privacy_S5_S6': 50})
        self.assertEqual(hot[0]['function'], 'scrubValue')
        self.assertEqual(hot[0]['line'], 1)
        self.assertEqual(hot[0]['column'], 2)

    def test_codec_only_charged_with_instrumentation_ancestor(self):
        stages, _, _ = self.node_fixture('file:///x/runtime.cjs', 'ab', 'profileJSONParse')
        self.assertEqual(stages, {'json_codec': 50})
        stages, _, _ = self.node_fixture('file:///x/app.mjs', 'main', 'profileJSONParse')
        self.assertEqual(stages, {'unattributed': 50})

    def test_idle_is_not_cpu_or_wrapper(self):
        stages, _, info = self.node_fixture('file:///x/node_modules/braintrust/index.js', '(idle)')
        self.assertFalse(stages)
        self.assertEqual(info['idle_program_us'], 100)

    def test_plain_baseline_pair_retains_raw_but_withholds_estimates(self):
        def row(tool, mode, latency, cpu, stages=None):
            return {'language': 'python', 'scenario': 'openai_chat', 'tool': tool,
                    'processor': 'batch' if tool == 'traceai' else 'native', 'mode': mode, 'repetition': 0,
                    'calls': [{'latency_us': latency, 'cpu_us': cpu, 'response': {'ok': True}}],
                    'errors': [], 'stages_us': stages or {}, 'top_functions': []}
        runs = [row('baseline', 'plain', 1000, 500), row('traceai', 'plain', 1400, 800),
                row('traceai', 'cpu', 9000, 8500, {'privacy_S5_S6': 2000, 'attributes': 1000, 'unattributed': 4000})]
        breakdown, _, overhead = summarize(runs)
        value = breakdown['python/traceai']
        self.assertEqual(value['unprofiled_added_mean_cpu_us'], 300)
        self.assertIsNone(value['stages']['privacy_S5_S6']['estimated_added_cpu_us'])
        self.assertFalse(value['normalization_reliable'])
        self.assertEqual(value['normalization_status'], 'withheld_uncalibrated_observer_effect')
        self.assertEqual(value['stages']['privacy_S5_S6']['us_per_call'], 2000)
        self.assertEqual(overhead[0]['added_us']['p50'], 400)
        self.assertTrue(overhead[0]['parity'])
        self.assertAlmostEqual(sum(s['percent_of_attributed'] or 0 for s in value['stages'].values()), 100)

    def test_nonpositive_anchor_abstains(self):
        base = {'language': 'node', 'scenario': 'embeddings', 'mode': 'plain', 'repetition': 0,
                'calls': [{'cpu_us': 500, 'latency_us': 900, 'response': {}}], 'errors': [], 'stages_us': {}, 'top_functions': []}
        runs = [dict(base, tool='baseline', processor='native'), dict(base, tool='traceai', processor='simple'),
                dict(base, tool='traceai', processor='simple', mode='cpu', stages_us={'span': 100})]
        breakdown, _, _ = summarize(runs)
        self.assertIsNone(breakdown['node/traceai']['stages']['span']['estimated_added_cpu_us'])

    def test_positive_but_noisy_anchor_abstains(self):
        rows = []
        for rep, cpu in ((0, 600), (1, 450)):
            common = {'language': 'node', 'scenario': 'embeddings', 'mode': 'plain', 'repetition': rep,
                      'errors': [], 'stages_us': {}, 'top_functions': []}
            rows.append(dict(common, tool='baseline', processor='native', calls=[{'cpu_us': 500, 'latency_us': 900, 'response': {}}]))
            rows.append(dict(common, tool='traceai', processor='simple', calls=[{'cpu_us': cpu, 'latency_us': 1000, 'response': {}}]))
        rows += [dict(rows[0], mode='cpu'), dict(rows[1], mode='cpu', stages_us={'span': 100})]
        breakdown, _, _ = summarize(rows)
        value = breakdown['node/traceai']
        self.assertGreater(value['unprofiled_added_mean_cpu_us'], 0)
        self.assertFalse(value['normalization_reliable'])
        self.assertIsNone(value['stages']['span']['estimated_added_cpu_us'])

    def test_stable_positive_anchor_is_not_a_calibration(self):
        for language, policy in (('python', 'batch'), ('node', 'simple')):
            with self.subTest(language=language):
                rows = []
                for rep in (0, 1):
                    common = {'language': language, 'scenario': 'openai_chat', 'repetition': rep,
                              'errors': [], 'stages_us': {}, 'top_functions': [], 'profile': {}}
                    for mode, base_latency, agent_latency in (('plain', 500, 1345), ('cpu', 4062, 16487)):
                        rows.append(dict(common, mode=mode, tool='baseline', processor='native', calls=[
                            {'cpu_us': 500, 'latency_us': base_latency, 'response': {}}]))
                        rows.append(dict(common, mode=mode, tool='traceai', processor=policy,
                                         stages_us={'privacy_S5_S6': 15000, 'attributes': 298} if mode == 'cpu' else {},
                                         calls=[{'cpu_us': 1354, 'latency_us': agent_latency, 'response': {}}]))
                breakdown, hot, overhead = summarize(rows)
                value = breakdown[language + '/traceai']
                self.assertEqual(value['unprofiled_added_mean_cpu_us'], 854)
                self.assertEqual(value['unprofiled_cpu_cluster_se_us'], 0)
                self.assertFalse(value['normalization_reliable'])
                self.assertTrue(all(s['estimated_added_cpu_us'] is None for s in value['stages'].values()))
                audit = observer_effect(rows)
                agent = next(r for r in audit if r['tool'] == 'traceai')
                self.assertAlmostEqual(agent['cpu_profile_to_plain_p50_ratio'], 16487 / 1345)
                self.assertAlmostEqual(agent['raw_profile_to_unprofiled_added_cpu_ratio'], 15298 / 854)
                self.assertFalse(agent['unprofiled_stage_composition_validated'])
                report = render({'created_at': 'fixture', 'config': {'n': 1, 'warmup': 1, 'repetitions': 2},
                                 'runs': rows, 'breakdown': breakdown, 'hot_functions': hot, 'overhead': overhead,
                                 'observer_effect': audit, 'competitive_budget_context': competitive_budget_context(rows),
                                 'observations': [], 'work': '/fixture', 'provenance': {}})
                self.assertIn('Unprofiled stage partition: WITHHELD', report)
                self.assertNotIn('Estimated native-policy cost partition', report)
                self.assertIn('Achievable p50 / p99', report)
                self.assertIn('unresolved | none', report)

    def test_candidate_budget_gap_is_disclosed_not_approved(self):
        rows = []
        for language, p50 in (('python', 54), ('node', 184)):
            common = {'language': language, 'scenario': 'openai_chat', 'mode': 'plain', 'repetition': 0, 'errors': []}
            rows.append(dict(common, tool='baseline', processor='native', calls=[{'latency_us': 500}]))
            rows.append(dict(common, tool='braintrust', processor='native', calls=[{'latency_us': 500 + p50}]))
        contexts = competitive_budget_context(rows)
        for row in contexts:
            t = TARGETS[row['language']]
            self.assertIsNone(t['estimated_added_p50_us'])
            self.assertIsNone(t['estimated_added_p99_us'])
            self.assertIsNone(t['release_budget_added_p50_us'])
            self.assertIsNone(t['release_budget_added_p99_us'])
            self.assertIsNone(row['approved_privacy_surcharge_us'])
            self.assertEqual(row['implied_unapproved_surcharge_us'],
                             t['candidate_ceiling_added_p50_us'] - row['braintrust_added_p50_us'])
        self.assertAlmostEqual(contexts[1]['candidate_to_braintrust_ratio'], 500 / 54)

    def test_minified_label_collisions_abstain(self):
        import shutil
        import subprocess
        node = shutil.which('node')
        deps = Path('/Volumes/nikm4/futureagi/tmp/bench-compare/profile-deps')
        if not node or not (deps / 'node_modules/acorn').is_dir():
            self.skipTest('Optional scratch Node parser prerequisites absent')
        with tempfile.TemporaryDirectory(dir='/Volumes/nikm4/futureagi/tmp/bench-compare') as work:
            fixture = Path(work) / 'bundle.cjs'
            fixture.write_text('var a=(f,n)=>f; function one(){const e=()=>{};a(e,"selectColor")} function ds(e=()=>Object.keys({})){return e()} a(ds,"discoverSecrets"); function two(){const e=()=>{};a(e,"unrelated")}')
            result = subprocess.check_output([node, str(Path(__file__).with_name('profile_ranges.cjs')), str(deps), str(fixture)],
                                             env={'HTTP_PROXY': 'http://127.0.0.1:9', 'HTTPS_PROXY': 'http://127.0.0.1:9', 'ALL_PROXY': 'http://127.0.0.1:9', 'NO_PROXY': '127.0.0.1,localhost'}, text=True)
        parsed = json.loads(result)
        self.assertNotIn('e', parsed['labels'])
        self.assertEqual(parsed['labels']['ds'], 'discoverSecrets')
        self.assertTrue(any(r['name'] == 'discoverSecrets' and r['stage'] == 'privacy_S1' for r in parsed['ranges']))

    def test_process_clock_has_no_negative_self_times(self):
        import cProfile
        import pstats
        import threading
        import time
        profile = cProfile.Profile(timer=time.process_time_ns, timeunit=1e-9)
        def work():
            return sum(i * i for i in range(300))
        thread = threading.Thread(target=lambda: [work() for _ in range(10)])
        profile.enable()
        thread.start()
        for _ in range(10):
            work()
        thread.join()
        profile.disable()
        self.assertTrue(all(v[2] >= 0 for v in pstats.Stats(profile).stats.values()))

    def test_internal_timers_ignore_background_threads(self):
        from profile_python import Timers
        import threading
        class Target:
            @staticmethod
            def call():
                return 42
        timer = Timers()
        timer.patch(Target, 'call', 'wrapper')
        thread = threading.Thread(target=Target.call)
        thread.start(); thread.join()
        self.assertEqual(dict(timer.calls), {})
        self.assertEqual(Target.call(), 42)
        self.assertEqual(timer.calls['wrapper'], 1)
        for obj, name, old in reversed(timer.originals):
            setattr(obj, name, old)

    def test_stage_names_unique(self):
        self.assertEqual(len(STAGES), len(set(STAGES)))

    def test_credential_check_scans_whole_exported_bodies(self):
        # A canary outside span attributes (e.g. a resource attribute) must still be caught.
        import base64
        import profile_compare
        from mocks import FI_KEY
        body = b'\x0a\x10resource' + FI_KEY.encode() + b'\x12spans-without-it'
        exports = [{'body_b64': base64.b64encode(body).decode()}]
        self.assertTrue(profile_compare.privacy(exports)['credentials_in_payload'])
        clean = [{'body_b64': base64.b64encode(b'no canary here').decode()}]
        self.assertFalse(profile_compare.privacy(clean)['credentials_in_payload'])


if __name__ == '__main__':
    unittest.main()
