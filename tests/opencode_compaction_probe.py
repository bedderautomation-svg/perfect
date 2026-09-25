"""Unpaid pinned-runtime API probe, with no provider credentials or requests."""
import http.client
import json
import os
import subprocess
import time

environment = dict(os.environ, OPENCODE_DISABLE_AUTOUPDATE='1', OPENCODE_DISABLE_DEFAULT_PLUGINS='1',
                   OPENCODE_DISABLE_MODELS_FETCH='1', OPENCODE_DISABLE_LSP_DOWNLOAD='1',
                   OPENAI_API_KEY='offline-placeholder',
                   OPENCODE_CONFIG_CONTENT=json.dumps({'permission': 'allow', 'share': 'disabled',
                      'compaction': {'auto': False, 'prune': False, 'tail_turns': 0, 'preserve_recent_tokens': 0},
                      'provider': {'trace_lab': {'npm': '@ai-sdk/openai', 'options': {'baseURL': 'http://127.0.0.1:8080/v1', 'apiKey': '{env:OPENAI_API_KEY}'},
                                                'models': {'google/gemini-3.1-pro-preview': {'name': 'google/gemini-3.1-pro-preview'}}}}}))
child = subprocess.Popen(['opencode', '--pure', '--print-logs', '--log-level', 'DEBUG', 'serve', '--port', '4096'], env=environment)
try:
    time.sleep(2)
    for method, path, body in [('GET', '/global/health', None), ('GET', '/doc', None), ('POST', '/api/session', {}),
                                ('POST', '/session?directory=/workspace', {}), ('GET', '/config?directory=/workspace', None)]:
        connection = http.client.HTTPConnection('127.0.0.1', 4096, timeout=8)
        try:
            connection.request(method, path, None if body is None else json.dumps(body), {'Content-Type': 'application/json'})
            response = connection.getresponse()
            raw = response.read().decode()
            if path == '/doc':
                doc = json.loads(raw)
                assert '/session/{sessionID}/summarize' in doc['paths']
                print('Native summarization route available', flush=True)
            elif path.startswith('/config'):
                config = json.loads(raw)
                assert config['compaction']['tail_turns'] == 0, config
                print('Native full-history config:', config['compaction'], flush=True)
            else:
                print(path, response.status, raw[:2000], flush=True)
        except OSError as exc:
            print(path, type(exc).__name__, flush=True)
        finally:
            connection.close()
finally:
    child.terminate()
    child.wait(timeout=10)
