"""Thin MSP client: native Muse tools, storage and compaction remain unmodified."""
import argparse
import hashlib
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid


def persisted_terminal(muse_home, sid, turn_id):
    """Recover completion metadata when MSP's derived view stops emitting events.

    This is mutable native state, not independent evidence of a tool action.
    Never repair the view/log or infer completion from another turn.
    """
    uuid.UUID(sid)
    uuid.UUID(turn_id)
    paths = list((Path(muse_home) / 'sessions').glob(f'*/*/*/{sid}/session.jsonl'))
    if len(paths) != 1:
        raise RuntimeError('Muse completion recovery requires one retained session log')
    terminal = None
    with paths[0].open('rb') as source:
        for line in source:
            if not line.endswith(b'\n'):
                break  # Writer may currently be appending the final record.
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError) as exc:
                raise RuntimeError('Muse completion recovery found a corrupt native log') from exc
            payload = record.get('payload', {})
            event = payload.get('event', {})
            if (record.get('stream') == {'kind': 'session', 'id': sid}
                    and record.get('payload_type') == 'runtime.session'
                    and payload.get('kind') == 'run' and payload.get('run_id') == turn_id
                    and event.get('kind') == 'terminal'):
                terminal = {'event': 'trace_lab_native_completion',
                            'source': 'mutable_native_session_log',
                            'session_id': sid, 'turn_id': turn_id,
                            'terminal': event.get('terminal'), 'reason': event.get('reason'),
                            'path': str(paths[0]), 'record_id': record.get('id'),
                            'record_sequence': record.get('sequence'),
                            'record_sha256': hashlib.sha256(line).hexdigest()}
    return terminal


def command_id():
    # MSP mutation IDs are UUIDv7, including on Python versions without uuid7.
    value = (int(time.time() * 1000) << 80) | (7 << 76) | (uuid.uuid4().int & ((1 << 76) - 1))
    value = (value & ~(3 << 62)) | (2 << 62)
    return str(uuid.UUID(int=value))


class Connection:
    def __init__(self, emit, timeout=None, approval_mode='never', disable_sandbox=True):
        self.emit = emit
        self.approval_mode = 'onRequest' if approval_mode == 'on-request' else 'allowAll'
        self.deadline = time.monotonic() + timeout if timeout else None
        self.child = subprocess.Popen(['muse', 'serve', '--trust-workspace',
                                       *(['--disable-sandbox'] if disable_sandbox else [])],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=sys.stderr, text=True, bufsize=1)
        self.incoming = queue.Queue()
        self.pending = []
        self.serial = 0
        def read():
            try:
                for line in self.child.stdout:
                    self.incoming.put(json.loads(line))
            except Exception as exc:
                self.incoming.put(exc)
            finally:
                self.incoming.put(None)
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        initialized = self.call('initialize', {'clientInfo': {'name': 'trace_lab', 'version': '1'}})
        self.muse_home = initialized.get('museHome')
        self.send({'jsonrpc': '2.0', 'method': 'initialized', 'params': {}})

    def send(self, message):
        self.child.stdin.write(json.dumps(message) + '\n')
        self.child.stdin.flush()

    def receive(self, poll_seconds=None):
        timeout = max(0.01, self.deadline - time.monotonic()) if self.deadline else None
        if poll_seconds is not None:
            timeout = min(timeout, poll_seconds) if timeout is not None else poll_seconds
        try:
            value = self.incoming.get(timeout=timeout)
        except queue.Empty:
            if poll_seconds is not None and (not self.deadline or time.monotonic() < self.deadline):
                return None
            raise TimeoutError('Muse MSP deadline exceeded') from None
        if value is None:
            raise RuntimeError('Muse MSP closed before completion')
        if isinstance(value, Exception):
            raise value
        self.emit(value)
        return value

    def call(self, method, params):
        self.serial += 1
        ident = self.serial
        self.send({'jsonrpc': '2.0', 'id': ident, 'method': method, 'params': params})
        while True:
            value = self.receive()
            if value.get('id') == ident:
                if 'error' in value:
                    raise RuntimeError('Muse ' + method + ': ' + json.dumps(value['error']))
                return value.get('result', {})
            self.pending.append(value)

    def wait(self, predicate):
        while True:
            value = self.pending.pop(0) if self.pending else self.receive()
            if predicate(value):
                return value

    def session(self, model, workspace, sid=None, resume=False):
        if resume:
            result = self.call('session/resume', {'commandId': command_id(), 'sessionId': sid})
        else:
            params = {'commandId': command_id(), 'workspaceRoot': workspace,
                      'modelId': model, 'providerId': 'meta', 'approvalMode': self.approval_mode}
            if sid:
                params['sessionId'] = sid
            result = self.call('session/start', params)
        if result['session'].get('approvalMode', {}).get('mode') != self.approval_mode:
            raise RuntimeError('Muse did not retain the requested approval mode')
        return result['session']['sessionId']

    def turn(self, sid, prompt, effort=None):
        params = {'commandId': command_id(), 'sessionId': sid,
                  'input': [{'type': 'text', 'text': prompt}]}
        if effort:
            params['reasoningEffort'] = effort
        result = self.call('turn/start', params)
        projection_unavailable = False
        while True:
            event = self.pending.pop(0) if self.pending else self.receive(
                poll_seconds=1 if projection_unavailable else None)
            params = (event or {}).get('params', {})
            if event and event.get('method') == 'turn/completed' and params.get('sessionId') == sid:
                terminal = params
                break
            if (event and event.get('method') == 'session/viewHealthChanged'
                    and params.get('sessionId') == sid
                    and params.get('health') == 'unavailable'
                    and params.get('noneReason') == 'projectionUnavailable'):
                projection_unavailable = True
            if projection_unavailable:
                if not self.muse_home or not result.get('turnId'):
                    raise RuntimeError('Muse projection unavailable without recovery identity')
                terminal = persisted_terminal(self.muse_home, sid, result['turnId'])
                if terminal:
                    # Distinct provenance; not a fabricated MSP event or tool result.
                    self.emit(terminal)
                    break
        if terminal.get('terminal') != 'completed':
            raise RuntimeError('Muse native turn failed: ' + json.dumps(terminal))
        return result

    def compact(self, sid):
        result = self.call('session/compact', {'commandId': command_id(), 'sessionId': sid})
        if result.get('status') not in {'accepted', 'started'}:
            raise RuntimeError('Muse did not admit compaction: ' + json.dumps(result))
        event = self.wait(lambda e: e.get('method') == 'item/completed'
                          and e.get('params', {}).get('item', {}).get('kind') == 'compaction')
        if event['params']['item'].get('outcome') != 'compacted':
            raise RuntimeError('Muse did not compact the session')

    def close(self):
        self.child.stdin.close()
        try:
            code = self.child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.child.terminate()
            self.child.wait(timeout=5)
            raise RuntimeError('Muse MSP did not shut down cleanly')
        self.reader.join(timeout=1)
        if code:
            raise RuntimeError(f'Muse MSP exited with code {code}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', required=True)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--session-id')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--reasoning-effort')
    parser.add_argument('--disable-sandbox', action='store_true')
    parser.add_argument('--approval-mode', choices=['never', 'on-request'], required=True)
    args = parser.parse_args()
    prompt = sys.stdin.read()
    connection = Connection(lambda e: print(json.dumps(e), flush=True),
                            approval_mode=args.approval_mode, disable_sandbox=args.disable_sandbox)
    try:
        sid = connection.session(args.model, args.workspace, args.session_id, args.resume)
        connection.turn(sid, prompt, args.reasoning_effort)
    finally:
        connection.close()


if __name__ == '__main__':
    main()
