import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from trace_lab.muse_driver import Connection, persisted_terminal
from trace_lab.extended_harnesses import succeeded, tool_inputs

SID = '01a0c868-753e-7172-8636-4ccf853b65e4'
TURN = '01a0c869-d634-7076-a19d-c316895f3b91'
OTHER = '01a0c869-d634-7076-a19d-c316895f3b92'


def record(turn=TURN, terminal='completed'):
    return {'id': 'terminal-record', 'sequence': 1432,
            'stream': {'kind': 'session', 'id': SID}, 'payload_type': 'runtime.session',
            'payload': {'kind': 'run', 'run_id': turn,
                        'event': {'kind': 'terminal', 'terminal': terminal}}}


class MuseCompletionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.path = self.home / 'sessions/2026/09/22' / SID / 'session.jsonl'
        self.path.parent.mkdir(parents=True)

    def write(self, rows, tail=''):
        self.path.write_text(''.join(json.dumps(x)+'\n' for x in rows)+tail)

    def test_completed_native_turn_recovers_after_projection_failure(self):
        self.write([record()])
        connection = Connection.__new__(Connection)
        connection.muse_home = str(self.home)
        connection.pending = [{'method': 'session/viewHealthChanged', 'params': {
            'sessionId': SID, 'health': 'unavailable', 'noneReason': 'projectionUnavailable'}}]
        emitted = []
        connection.emit = emitted.append
        with patch.object(connection, 'call', return_value={'turnId': TURN}), \
                patch.object(connection, 'receive', side_effect=AssertionError('must not hang')):
            connection.turn(SID, 'unchanged task')
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0]['source'], 'mutable_native_session_log')
        self.assertTrue(succeeded('muse', emitted))
        self.assertEqual(list(tool_inputs(emitted)), [])

    def test_other_turn_and_partial_last_record_cannot_complete_this_turn(self):
        self.write([record(OTHER)], json.dumps(record())[:-3])
        self.assertIsNone(persisted_terminal(self.home, SID, TURN))
        self.write([record(OTHER), record()])
        self.assertEqual(persisted_terminal(self.home, SID, TURN)['turn_id'], TURN)

    def test_missing_ambiguous_and_corrupt_logs_are_not_success(self):
        with self.assertRaises(RuntimeError):
            persisted_terminal(self.home, SID, TURN)
        self.path.write_text('invalid-json\n')
        with self.assertRaisesRegex(RuntimeError, 'corrupt'):
            persisted_terminal(self.home, SID, TURN)
        self.write([record()])
        duplicate = self.home / 'sessions/2026/09/23' / SID / 'session.jsonl'
        duplicate.parent.mkdir(parents=True)
        duplicate.write_text(self.path.read_text())
        with self.assertRaisesRegex(RuntimeError, 'one retained'):
            persisted_terminal(self.home, SID, TURN)

    def test_native_failed_terminal_is_not_success_and_last_outcome_wins(self):
        self.write([record(terminal='failed')])
        failure = persisted_terminal(self.home, SID, TURN)
        self.assertFalse(succeeded('muse', [failure]))
        self.write([record()])
        recovered = persisted_terminal(self.home, SID, TURN)
        failed_msp = {'method': 'turn/completed', 'params': {'terminal': 'failed'}}
        self.assertFalse(succeeded('muse', [recovered, failed_msp]))
        self.assertTrue(succeeded('muse', [failed_msp, recovered]))

    def test_wrong_session_record_and_path_traversal_are_rejected(self):
        row = record();row['stream']['id'] = OTHER
        self.write([row])
        self.assertIsNone(persisted_terminal(self.home, SID, TURN))
        with self.assertRaises(ValueError):
            persisted_terminal(self.home, '../elsewhere', TURN)

    def test_healthy_msp_does_not_read_mutable_session_log(self):
        connection = Connection.__new__(Connection)
        connection.pending = [{'method': 'turn/completed', 'params': {
            'sessionId': SID, 'terminal': 'completed'}}]
        with patch.object(connection, 'call', return_value={'turnId': TURN}), \
                patch('trace_lab.muse_driver.persisted_terminal', side_effect=AssertionError):
            connection.turn(SID, 'unchanged task')


if __name__ == '__main__':
    unittest.main()
