"""Run inside a disposable ZCode image; scripted local model, never a scored trial."""
import json
from pathlib import Path
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from trace_lab import zcode
from trace_lab.zcode_trace_check import inspect
from host_lab.compaction_trace_edit import summary_records

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
                   ('Bash', {'command': 'cat /workspace/smoke.txt'})] if phase == 'resume' else []
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
    zcode.initialize('z-ai/glm-5.3', str(Path.home()))
    skill = Path('/workspace/.zcode/skills/smoke-skill/SKILL.md')
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text('---\nname: smoke-skill\ndescription: Validate a local integration smoke test.\n---\n# Smoke Skill\nPreserve SMOKE_OK in the workspace.\n')
    server = HTTPServer(('127.0.0.1', 8080), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    sid = None
    results = {}
    try:
        for phase, prompt in [('task', 'Validate the local integration.'), ('resume', 'Read the same file.'),
                              ('compact', '/compact'), ('after_compact', 'Continue the same task.')]:
            request_count = 0
            command = zcode.native_command('z-ai/glm-5.3', '/workspace', sid, bool(sid)) + ['-p', prompt]
            process = subprocess.run(command, text=True, capture_output=True, timeout=90)
            events = [json.loads(line) for line in process.stdout.splitlines()]
            observed = zcode.session_id(events)
            assert not process.returncode and zcode.succeeded(events), (phase, process.stderr)
            assert observed and (sid is None or sid == observed), 'Session changed'
            sid = observed
            tools = list(zcode.tool_inputs(events))
            assert all(t[2]['_native_status'] == 'completed' for t in tools), tools
            if phase == 'task':
                assert any(t[1] == 'Skill' and 'Smoke Skill' in str(t[2]['_native_output']) for t in tools)
                assert 'smoke-skill' in json.dumps(requests[0]['messages']) or 'smoke-skill' in json.dumps(requests[0]['tools'])
            if phase == 'resume':
                assert any('SMOKE_OK' in str(t[2]['_native_output']) for t in tools)
            if phase == 'compact':
                assert any(e.get('payload', {}).get('boundaryId') and e.get('payload', {}).get('status') == 'completed' for e in events)
                assert summary_records(inspect(sid)['records'], 'zcode', sid)
            if phase == 'after_compact':
                assert 'SMOKE_OK' in json.dumps(requests[-1]['messages'])
            results[phase] = {'passed': True, 'session_id': sid, 'tool_count': len(tools)}
        assert (Path.home() / f'.zcode/cli/rollout/model-io-{sid}.jsonl').exists()
        print(json.dumps({'passed': True, 'phases': results, 'native_database': zcode.DB}))
    finally:
        server.shutdown(); server.server_close()


if __name__ == '__main__':
    main()
