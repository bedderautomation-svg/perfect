"""Run inside a disposable Kimi image; scripted local model, never a scored trial."""
import json
import os
from pathlib import Path
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from trace_lab import kimi

phase = 'task'
request_count = 0
requests = []

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        global request_count
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        requests.append(body)
        request_count += 1
        planned = [('Skill', {'skill': 'smoke-skill'}),
                   ('Bash', {'command': 'printf SMOKE_OK > /workspace/smoke.txt'})] if phase == 'task' else [
                   ('Bash', {'command': 'cat /workspace/smoke.txt'})] if phase == 'resume' else [('Bash', {'command': 'exit 7'})] if phase == 'failure' else [('Bash', {'command': 'printf %s "$KIMI_SMOKE"'})] if phase == 'shell' else []
        if request_count <= len(planned):
            name, arguments = planned[request_count - 1]
            delta = {'role': 'assistant', 'tool_calls': [{'index': 0, 'id': f'call_{phase}_{request_count}',
                     'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}]}
            finish = 'tool_calls'
        else:
            delta = {'role': 'assistant', 'content': 'The task is to preserve SMOKE_OK in /workspace/smoke.txt.'}
            finish = 'stop'
        self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
        for part, reason in [(delta, None), ({}, finish)]:
            data = {'id': 'chatcmpl-smoke', 'object': 'chat.completion.chunk', 'created': int(time.time()),
                    'model': body['model'], 'choices': [{'index': 0, 'delta': part, 'finish_reason': reason}]}
            self.wfile.write(('data: ' + json.dumps(data) + '\n\n').encode())
        self.wfile.write(b'data: [DONE]\n\n')


def main():
    global phase, request_count
    kimi.initialize('moonshotai/kimi-k3', str(Path.home()))
    skill = Path('/workspace/.kimi-code/skills/smoke-skill/SKILL.md')
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text('---\nname: smoke-skill\ndescription: Validate a local integration smoke test.\n---\n# Smoke Skill\nPreserve SMOKE_OK in the workspace.\n')
    from trace_lab.shell_tampering_fixture import initialize as initialize_shell
    initialize_shell(Path('/workspace'), Path.home(), 'kimi')
    envfile = Path('/workspace/smoke-env.sh'); envfile.write_text('export KIMI_SMOKE=inherited-state\n')
    os.environ['BASH_ENV'] = str(envfile)
    os.environ['SHELL'] = '/bin/bash'
    server = HTTPServer(('127.0.0.1', 8080), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    from trace_lab.kimi_driver import Connection
    from host_lab.compaction_trace_edit import summary_records
    collected = []
    c = Connection(collected.append)
    results = {}
    try:
        sid = c.session('/workspace')
        for phase, prompt in [('task', 'Validate the local integration.'), ('resume', 'Read the same file.'),
                              ('failure', 'Try a failing command.'), ('shell', 'Read the shell environment.'), ('compact', None),
                              ('after_compact', 'Continue the task.')]:
            request_count = 0
            start = len(collected)
            if phase == 'compact':
                c.compact(sid)
            else:
                c.turn(sid, prompt)
            events = collected[start:]
            Path('/tmp/kimi-' + phase + '.json').write_text(json.dumps(events, indent=2))
            Path('/tmp/kimi-requests.json').write_text(json.dumps(requests, indent=2))
            tools = list(kimi.tool_inputs(events))
            assert all(t[2]['_native_status'] == ('error' if phase == 'failure' else 'completed') for t in tools), tools
            if phase == 'task':
                assert any(t[1] == 'Skill' for t in tools), tools
                assert 'smoke-skill' in json.dumps(requests[0])
            if phase == 'resume':
                assert any('SMOKE_OK' in str(t[2]['_native_output']) for t in tools), tools
            targets = list(Path.home().glob('.kimi-code/sessions/*/' + sid + '/agents/main/wire.jsonl'))
            assert len(targets) == 1
            if phase == 'compact':
                rows = [json.loads(l) for l in targets[0].read_text().splitlines()]
                assert summary_records(rows, 'kimi', sid), [r for r in rows if 'compact' in r.get('type', '')]
            if phase == 'after_compact':
                assert 'SMOKE_OK' in json.dumps(requests[-1]['messages'])
            results[phase] = {'passed': True, 'tool_count': len(tools)}
            if phase == 'shell':
                results[phase]['bash_env_sourced'] = any('inherited-state' in t[2]['_native_output'] for t in tools)
                assert results[phase]['bash_env_sourced'], tools
        c.close()
        c = Connection(collected.append)
        assert c.session('/workspace', sid) == sid
        phase = 'resume'; request_count = 0
        c.turn(sid, 'Resume and read the file.')
        c.close()
        print(json.dumps({'passed': True, 'phases': results, 'session_id': sid,
                          'native_paths': [str(p.relative_to(Path.home())) for p in targets]}))
    finally:
        server.shutdown(); server.server_close()


if __name__ == '__main__':
    main()
