"""Thin ACP client; official Kimi owns tools, context and trace persistence."""
import argparse
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading


class Connection:
    def __init__(self, emit):
        self.emit = emit
        self.phase = 'initialize'
        self.compaction_completed = False
        self.child = subprocess.Popen(['kimi', 'acp'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=sys.stderr, text=True, bufsize=1)
        self.incoming = queue.Queue()
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
        self.call('initialize', {'protocolVersion': 1, 'clientInfo': {'name': 'trace-lab', 'version': '1'},
                                 'clientCapabilities': {}})

    def send(self, value):
        self.emit({'kimi_rpc': value, 'direction': 'sent', 'phase': self.phase})
        self.child.stdin.write(json.dumps(value) + '\n'); self.child.stdin.flush()

    def call(self, method, params):
        self.send({'jsonrpc': '2.0', 'id': method, 'method': method, 'params': params})
        while True:
            value = self.incoming.get()
            if value is None:
                raise RuntimeError('Kimi ACP closed before completion')
            if isinstance(value, Exception):
                raise value
            self.emit({'kimi_rpc': value, 'direction': 'received', 'phase': self.phase})
            if self.phase == 'compaction':
                text = value.get('params', {}).get('update', {}).get('content', {}).get('text', '')
                self.compaction_completed |= text.startswith('Compaction completed.\n- Messages compacted:')
            if value.get('method') and 'id' in value:
                # Native full-access mode should not need interactive approval.
                # Fail visibly if the configuration doesn't actually take effect.
                if value['method'] == 'session/request_permission':
                    self.send({'jsonrpc': '2.0', 'id': value['id'], 'result': {'outcome': {'outcome': 'cancelled'}}})
                    raise RuntimeError('Kimi unexpectedly requested approval in full-access mode')
                self.send({'jsonrpc': '2.0', 'id': value['id'], 'error': {'code': -32601, 'message': 'Client capability not advertised'}})
            if value.get('id') == method and not value.get('method'):
                if 'error' in value:
                    raise RuntimeError('Kimi ' + method + ': ' + json.dumps(value['error']))
                return value.get('result', {})

    def session(self, workspace, sid=None):
        self.phase = 'session'
        params = {'cwd': workspace, 'mcpServers': []}
        if sid:
            self.call('session/resume', {**params, 'sessionId': sid})
        else:
            sid = self.call('session/new', params)['sessionId']
        # Despite old ACP UI labels, mode `auto` maps to native Never Ask;
        # `yolo` maps to Ask When Needed. See pinned modes.ts implementation.
        self.call('session/set_mode', {'sessionId': sid, 'modeId': 'auto'})
        return sid

    def turn(self, sid, prompt):
        self.phase = 'prompt'
        result = self.call('session/prompt', {'sessionId': sid, 'prompt': [{'type': 'text', 'text': prompt}]})
        # ACP 2.0.2 maps non-auth provider errors to end_turn. Inspect retained
        # native completion metadata only to surface failures; absence after
        # an agent deletion is allowed and is never evidence of tool success.
        paths = list((Path.home() / '.kimi-code/sessions').glob('*/' + sid + '/agents/main/wire.jsonl'))
        if len(paths) == 1:
            terminal = None
            try:
                with paths[0].open('rb') as source:
                    source.seek(0, 2); size = source.tell()
                    start = max(0, size - 2 * 1024 * 1024); source.seek(start)
                    lines = source.read().decode('utf-8', errors='replace').splitlines()
                for line in lines[1:] if start else lines:
                    record = json.loads(line)
                    if record.get('type') == 'turn.ended' and record.get('agentId') == 'main':
                        terminal = record
            except (OSError, ValueError):
                terminal = None
            if terminal and terminal.get('reason') == 'failed':
                self.emit({'kimi_native_turn_check': terminal, 'source': 'mutable_native_trace'})
                raise RuntimeError('Kimi native turn failed: ' + str(terminal.get('error', {})))
        if result.get('stopReason') not in {'end_turn', 'refusal'}:
            raise RuntimeError('Kimi turn did not complete: ' + json.dumps(result))

    def compact(self, sid):
        self.phase = 'compaction'
        self.compaction_completed = False
        self.call('session/prompt', {'sessionId': sid, 'prompt': [{'type': 'text', 'text': '/compact'}]})
        while not self.compaction_completed:
            value = self.incoming.get()
            if value is None or isinstance(value, Exception):
                raise RuntimeError('Kimi closed during native compaction')
            self.emit({'kimi_rpc': value, 'direction': 'received', 'phase': self.phase})
            u = value.get('params', {}).get('update', {})
            text = u.get('content', {}).get('text', '')
            if u.get('sessionUpdate') == 'agent_message_chunk' and text.startswith('Compaction completed.\n- Messages compacted:'):
                return
            if 'compaction failed' in text.lower() or 'compaction cancelled' in text.lower():
                raise RuntimeError(text)

    def close(self):
        self.child.stdin.close()
        try:
            code = self.child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.child.terminate(); self.child.wait(timeout=5)
            raise RuntimeError('Kimi ACP did not shut down cleanly')
        self.reader.join(timeout=1)
        if code:
            raise RuntimeError(f'Kimi ACP exited with code {code}')


def main():
    p = argparse.ArgumentParser(); p.add_argument('--workspace', required=True)
    p.add_argument('--session-id'); p.add_argument('--permissions', choices=['full'], default='full')
    args = p.parse_args()
    prompt = sys.stdin.read(1024 * 1024)
    c = Connection(lambda event: print(json.dumps(event), flush=True))
    try:
        sid = c.session(args.workspace, args.session_id)
        c.turn(sid, prompt)
    finally:
        c.close()


if __name__ == '__main__':
    main()
