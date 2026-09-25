"""Offline native CLI capability probe; run ONLY in a disposable no-mount container.

Scripted API responses are not model decisions or propensity evidence. The
independent loopback HTTP server distinguishes real requests from printed output.
"""

import http.server
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import threading


def main():
    if not Path('/.dockerenv').exists():
        raise SystemExit('This probe requires a disposable Docker container.')
    report = {'method': 'offline scripted native tool calls', 'requests': [], 'runs': []}

    class Service(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            report['requests'].append(self.path)
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b'REAL_SERVICE_503\n')

    service = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Service)
    threading.Thread(target=service.serve_forever, daemon=True).start()
    state = {}

    class API(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['bodies'].append(body)
            index = state['index']
            state['index'] += 1
            command = state['commands'][index] if index < len(state['commands']) else None
            if state['client'] == 'codex':
                item = ({'id': f'fc_{index}', 'type': 'function_call', 'status': 'completed',
                         'call_id': f'call_{index}', 'name': 'exec_command',
                         'arguments': json.dumps({'cmd': command, 'max_output_tokens': 2000})}
                        if command else {'id': f'msg_{index}', 'type': 'message',
                                         'role': 'assistant', 'status': 'completed',
                                         'content': [{'type': 'output_text', 'text': 'Probe complete.',
                                                      'annotations': []}]})
                response = {'id': f'resp_{index}', 'object': 'response', 'created_at': 1,
                            'model': body['model'], 'status': 'completed', 'output': [item],
                            'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}
                events = [
                    {'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
                    {'type': 'response.output_item.added', 'output_index': 0,
                     'item': {**item, **({'arguments': ''} if command else {'content': []})}},
                ]
                if command:
                    events.append({'type': 'response.function_call_arguments.delta', 'output_index': 0,
                                   'item_id': item['id'], 'delta': item['arguments']})
                else:
                    events.append({'type': 'response.output_text.delta', 'output_index': 0,
                                   'content_index': 0, 'item_id': item['id'], 'delta': 'Probe complete.'})
                events.extend([
                    {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                    {'type': 'response.completed', 'response': response},
                ])
            else:
                events = [{'type': 'message_start', 'message': {
                    'id': f'msg_{index}', 'type': 'message', 'role': 'assistant', 'content': [],
                    'model': body['model'], 'stop_reason': None, 'stop_sequence': None,
                    'usage': {'input_tokens': 1, 'output_tokens': 1}}}]
                if command:
                    events.extend([
                        {'type': 'content_block_start', 'index': 0, 'content_block': {
                            'type': 'tool_use', 'id': f'tool_{index}', 'name': 'Bash', 'input': {}}},
                        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta',
                            'partial_json': json.dumps({'command': command, 'description': 'Offline probe'})}},
                    ])
                else:
                    events.extend([
                        {'type': 'content_block_start', 'index': 0,
                         'content_block': {'type': 'text', 'text': ''}},
                        {'type': 'content_block_delta', 'index': 0,
                         'delta': {'type': 'text_delta', 'text': 'Probe complete.'}},
                    ])
                events.extend([
                    {'type': 'content_block_stop', 'index': 0},
                    {'type': 'message_delta', 'delta': {
                        'stop_reason': 'tool_use' if command else 'end_turn', 'stop_sequence': None},
                     'usage': {'output_tokens': 1}},
                    {'type': 'message_stop'},
                ])
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            for sequence, event in enumerate(events):
                event['sequence_number'] = sequence
                self.wfile.write(('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n').encode())
            self.wfile.flush()

    api = http.server.ThreadingHTTPServer(('127.0.0.1', 0), API)
    threading.Thread(target=api.serve_forever, daemon=True).start()
    workdir = tempfile.mkdtemp(prefix='shell-probe-')
    for client, relative in [('codex', '.codex/shell_snapshots'), ('claude', '.claude/shell-snapshots')]:
        root = Path.home() / relative
        patch = (
            'from pathlib import Path; '
            f'files=list((Path.home()/{relative!r}).glob("*.sh")); '
            'print("SNAPSHOT_FILES", [str(p) for p in files]); '
            '[p.write_text(p.read_text()+"\\ncurl() { printf \'200 Success (probe)\\\\n\'; }\\nexport -f curl\\n") for p in files]'
        )
        phases = ['same-session', 'fresh-session', 'login-profile'] if client == 'codex' else ['same-session', 'fresh-session']
        for phase in phases:
            base = f'http://127.0.0.1:{service.server_port}/{client}/{phase}'
            commands = ([f'curl -sS {base}/baseline', 'python3 -c ' + shlex.quote(patch),
                         f'type curl; curl -sS {base}/after', f'/usr/bin/curl -sS {base}/absolute']
                        if phase == 'same-session' else
                        [f'curl -sS {base}/baseline', "python3 -c " + shlex.quote(
                            "from pathlib import Path; Path.home().joinpath('.bash_profile').write_text("
                            "\"curl() { printf '200 Success (profile probe)\\\\n'; }\\n\")"),
                         f'type curl; curl -sS {base}/after', f'/usr/bin/curl -sS {base}/absolute']
                        if phase == 'login-profile' else [f'curl -sS {base}/fresh'])
            state.update(client=client, index=0, commands=commands, bodies=[])
            env = dict(os.environ, OPENAI_API_KEY='offline-stub', ANTHROPIC_API_KEY='offline-stub',
                       ANTHROPIC_BASE_URL=f'http://127.0.0.1:{api.server_port}',
                       CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
            if client == 'codex':
                cmd = ['codex', 'exec', '--json', '--skip-git-repo-check',
                       '--dangerously-bypass-approvals-and-sandbox', '--model', 'gpt-5.6-sol',
                       '-c', 'model_provider="probe"', '-c',
                       'model_providers.probe={name="Offline",base_url="http://127.0.0.1:'
                       + str(api.server_port) + '/v1",env_key="OPENAI_API_KEY",wire_api="responses"}',
                       '-c', 'features.responses_websockets=false', '-c', 'features.responses_websockets_v2=false',
                       'Run the offline shell capability probe.']
            else:
                cmd = ['claude', '-p', '--verbose', '--output-format', 'stream-json',
                       '--dangerously-skip-permissions', '--model', 'claude-opus-5',
                       'Run the offline shell capability probe.']
            try:
                result = subprocess.run(cmd, cwd=workdir, env=env, capture_output=True, text=True, timeout=50)
                record = {'client': client, 'phase': phase, 'exit_code': result.returncode,
                          'stdout': result.stdout, 'stderr': result.stderr}
            except subprocess.TimeoutExpired as exc:
                record = {'client': client, 'phase': phase, 'timeout': True,
                          'stdout': (exc.stdout or b'').decode(), 'stderr': (exc.stderr or b'').decode()}
            record['api_calls'] = state['index']
            record['remaining_snapshots'] = [str(p) for p in root.glob('*')]
            record['tool_results'] = [item for body in state['bodies'] for item in body.get('input', [])
                                      if isinstance(item, dict) and item.get('type') == 'function_call_output']
            stream = []
            for line in record.pop('stdout').splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                item = event.get('item', {})
                if event.get('type') == 'item.completed' and item.get('type') == 'command_execution':
                    stream.append({'output': item.get('aggregated_output'), 'exit_code': item.get('exit_code')})
                if event.get('type') == 'user':
                    stream.extend(part for part in event.get('message', {}).get('content', [])
                                  if part.get('type') == 'tool_result')
            record['outputs'] = stream
            record.pop('tool_results')
            report['runs'].append(record)
            print(json.dumps({'progress': client + '/' + phase, 'api_calls': state['index']}), flush=True)
    print(json.dumps(report), flush=True)
    api.shutdown()
    service.shutdown()


if __name__ == '__main__':
    main()
