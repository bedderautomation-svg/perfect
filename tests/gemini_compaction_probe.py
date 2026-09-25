"""Unpaid real CLI/PTY smoke test against a deterministic local model stub."""
import http.server
import json
import os
from pathlib import Path
import threading

from trace_lab import compaction_skill_fixture as fixture
from trace_lab.gemini_config import initialize
from trace_lab.native_compaction_driver import Driver
from trace_lab import native_compaction_driver


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if ':countTokens' in self.path:
            value = {'totalTokens': 1000}
        else:
            prompt = json.dumps(body.get('contents', []))
            text = ('<state_snapshot><overall_goal>Fix invoice rounding.</overall_goal><next_steps>Use Decimal and test.</next_steps></state_snapshot>'
                    if 'state_snapshot' in prompt else 'Diagnosis complete. Ready to continue.')
            value = {'candidates': [{'content': {'role': 'model', 'parts': [{'text': text}]}, 'finishReason': 'STOP'}],
                     'usageMetadata': {'promptTokenCount': 100000, 'candidatesTokenCount': 30, 'totalTokenCount': 100030}}
        payload = json.dumps(value).encode()
        stream = ':streamGenerateContent' in self.path
        if stream:
            payload = b'data: ' + payload + b'\n\n'
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream' if stream else 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


server = http.server.ThreadingHTTPServer(('127.0.0.1', 8080), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ.update(HOME='/home/agent', GEMINI_API_KEY='offline-placeholder', GOOGLE_GEMINI_BASE_URL='http://127.0.0.1:8080')
initialize()
fixture.initialize(Path('/workspace'), 'gemini')
original_emit = native_compaction_driver.emit
native_compaction_driver.emit = lambda kind, **fields: original_emit(kind, **fields) if kind != 'native_ui' else None
try:
    Driver({'client': 'gemini', 'model': 'gemini-3.1-pro-preview', 'timeout': 90,
            'history_policy': 'ensure-exposure', 'review_prompts': fixture.GEMINI_REVIEW_PROMPTS,
            'task_prompt': fixture.TASK_PROMPT, 'prepare_prompt': fixture.PREPARE_PROMPT,
            'continue_prompt': fixture.CONTINUE_PROMPT}).run()
finally:
    server.shutdown()
