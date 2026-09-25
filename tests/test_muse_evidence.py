import json
from pathlib import Path
import tempfile
import unittest

from trace_lab.muse_evidence import attach
from trace_lab.extended_harnesses import tool_inputs


class MuseEvidenceTests(unittest.TestCase):
    def test_arguments_require_matching_native_result_and_keep_denial(self):
        for outcome in ('success', 'failure'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                proof = {'kind': 'muse_tool_call', 'observed_ns': 20, 'call_id': 'call1',
                         'name': 'write_file', 'arguments': json.dumps({'path': '/tmp/example', 'content': 'text'})}
                native = {'payload_type': 'tool.result', 'payload': {'call_id': 'call1',
                          'correlation_facts': {'outcome': outcome, 'tool_name': 'write_file'},
                          'text': 'wrote file' if outcome == 'success' else 'permission denied'}}
                (root/'gateway.log').write_text(json.dumps(proof)+'\n')
                (root/'muse.jsonl').write_text(json.dumps(native)+'\n')
                attach(root, 'muse.jsonl', 10)
                attach(root, 'muse.jsonl', 10)
                rows = [json.loads(s) for s in (root/'muse.jsonl').read_text().splitlines()]
                self.assertEqual(len(rows), 2)
                tools = list(tool_inputs(rows))
                self.assertEqual(len(tools), 1)
                self.assertEqual(tools[0][2]['content'], 'text')
                self.assertEqual(tools[0][2]['_native_status'], 'completed' if outcome == 'success' else 'error')

    def test_unbound_or_ambiguous_gateway_calls_do_not_become_actions(self):
        for variant in ('missing', 'wrong_name', 'duplicate', 'old'):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                proof = {'kind': 'muse_tool_call', 'observed_ns': 5 if variant == 'old' else 20,
                         'call_id': 'call1', 'name': 'read_file' if variant == 'wrong_name' else 'write_file',
                         'arguments': '{"path":"/tmp/example"}'}
                native = {'payload_type': 'tool.result', 'payload': {'call_id': 'call1',
                          'correlation_facts': {'outcome': 'success', 'tool_name': 'write_file'}, 'text': 'done'}}
                (root/'gateway.log').write_text((json.dumps(proof)+'\n') * (2 if variant == 'duplicate' else 1))
                (root/'muse.jsonl').write_text('' if variant == 'missing' else json.dumps(native)+'\n')
                attach(root, 'muse.jsonl', 10)
                rows = [json.loads(s) for s in (root/'muse.jsonl').read_text().splitlines()]
                self.assertFalse(any(r.get('event') == 'trace_lab_muse_tool_evidence' for r in rows))
                self.assertTrue(all('path' not in t[2] for t in tool_inputs(rows)))
