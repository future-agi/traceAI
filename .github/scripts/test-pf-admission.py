"""Pure parser regressions; never runs pfctl or changes a host."""
import hashlib
from pathlib import Path
import runpy
import unittest

HERE = Path(__file__).resolve().parent
API = runpy.run_path(str(HERE / 'pf-admission.py'))
SCRUB, FILTER = API['SCRUB'], API['FILTER']
DEFAULT = SCRUB + '\n' + FILTER + '\n'


class AdmissionTests(unittest.TestCase):
    def test_exact_two_hosted_captures(self):
        for node in [20, 24]:
            with self.subTest(node=node):
                raw = (HERE / f'fixtures/pf-admission/run41963831029-node{node}-root-rules-before.log').read_text()
                # The old recorder concatenated stdout + stderr. The expected
                # exact warning suffix is a fixture fact, not parser behavior.
                self.assertEqual(raw, DEFAULT + 'No ALTQ support in kernel\nALTQ related functions disabled\n')
                stdout = raw[:-len('No ALTQ support in kernel\nALTQ related functions disabled\n')]
                self.assertEqual(hashlib.sha256(stdout.encode()).hexdigest(), '80a4aba0003d04855da31ea24b6e63ec4aee91da2570b488596a80d8beca202f')
                self.assertEqual(API['admit_root'](stdout)['normalization_hook'], SCRUB)
                with self.assertRaises(ValueError):
                    API['admit_root'](raw)

    def test_required_filter_without_normalization(self):
        self.assertIsNone(API['admit_root'](FILTER)['normalization_hook'])

    def test_ordinary_layout_whitespace(self):
        self.assertEqual(API['admit_root']('\n\t' + SCRUB + '\n  ' + FILTER + '\n'), API['admit_root'](DEFAULT))

    def test_reject_root_missing_duplicate_or_changed_hooks(self):
        fixtures = ['', SCRUB, FILTER + '\n' + FILTER, DEFAULT + SCRUB,
                    FILTER + '\n' + SCRUB, DEFAULT.replace('scrub-anchor', 'scrub'),
                    DEFAULT.replace('fragment reassemble', 'fragment crop'),
                    DEFAULT.replace('anchor "com.apple/*" all\n', 'anchor "com.apple/*" quick all\n'),
                    DEFAULT.replace('com.apple/*', 'com.apple'),
                    DEFAULT + '\nanchor "other/*" all',
                    DEFAULT + '\nscrub-anchor "other/*" all fragment reassemble',
                    DEFAULT + '\nnat-anchor "com.apple/*" all',
                    DEFAULT + '\n# comment', DEFAULT + '\x00', DEFAULT + '\x1b[0m',
                    DEFAULT + '\n}', 'anchor "com.apple/*" all {\npass all\n}']
        for text in fixtures:
            with self.subTest(text=text), self.assertRaises(ValueError):
                API['admit_root'](text)

    def test_reject_global_rules_even_if_filter_hook_present(self):
        for rule in ['pass all', 'pass out quick all keep state', 'block drop all',
                     'block return out quick proto tcp user 61000', 'match all',
                     'set skip on lo0', 'set state-policy floating', 'table <bypass> persist',
                     'load anchor "com.apple/evil" from "/tmp/evil"',
                     'rdr pass on en0 proto tcp to any -> 127.0.0.1',
                     'dummynet out all pipe 1']:
            for text in [rule + '\n' + DEFAULT, DEFAULT + rule]:
                with self.subTest(text=text), self.assertRaises(ValueError):
                    API['admit_root'](text)

    def test_recursive_empty_default(self):
        for text in [DEFAULT, SCRUB + '\n' + FILTER + ' {\n}\n', FILTER + ' {\n}\n']:
            self.assertEqual(API['admit_recursive'](text)['filter_hook'], FILTER)

    def test_recursive_fail_closed(self):
        for nested in ['pass all', 'block all', 'pass out quick all keep state',
                       'anchor "unexpected" all {\n}', 'scrub all', 'set skip on lo0',
                       '}', 'nat-anchor "unexpected" all']:
            text = SCRUB + '\n' + FILTER + ' {\n' + nested + '\n}'
            with self.subTest(nested=nested), self.assertRaises(ValueError):
                API['admit_recursive'](text)
        for text in [FILTER + ' {', FILTER + ' {\n}\n}', FILTER + ' quick {\n}', SCRUB + '\n{}']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                API['admit_recursive'](text)

    def test_translation_empty_defaults(self):
        for text in ['nat-anchor "com.apple/*" all\nrdr-anchor "com.apple/*" all',
                     'nat-anchor "com.apple/*" all {\n}\nrdr-anchor "com.apple/*" all {\n}']:
            self.assertIsNone(API['admit_translation'](text))

    def test_translation_reject_active_or_ambiguous(self):
        for text in ['', 'nat-anchor "other/*" all',
                     'nat-anchor "com.apple/*" all {\nnat on en0 from any to any -> en0\n}',
                     'nat-anchor "com.apple/*" all {\n',
                     'nat-anchor "com.apple/*" all\nrdr-anchor "com.apple/*" all\npass all']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                API['admit_translation'](text)

    def test_packet_counts_attributed_by_rule_label(self):
        labels = ['traceai-inet-tcp', 'traceai-inet-udp', 'traceai-inet6-tcp', 'traceai-inet6-udp']
        text = ''.join(f'@{index} block return out quick inet proto tcp all label "{label}"\n  [ Evaluations: 12 Packets: {index} Bytes: 0 States: 0 ]\n' for index, label in enumerate(labels))
        self.assertEqual(API['packet_counts'](text), dict(zip(labels, range(4))))
        for invalid in ['', text.replace('traceai-inet6-udp', 'unexpected'),
                        text.replace('traceai-inet6-udp', 'traceai-inet-tcp'),
                        text.replace('Packets:', 'Unknown:')]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                API['packet_counts'](invalid)


if __name__ == '__main__':
    unittest.main(verbosity=2)
