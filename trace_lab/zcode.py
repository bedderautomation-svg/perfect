"""Official ZCode Agent 0.16.9 from ZCode Desktop 3.14.3.

The native rollout JSONL is model-I/O evidence; SQLite is the resumable history.
Neither is replaced with the supervisor's externally captured stream.
"""
import json
from pathlib import Path, PurePosixPath

TRACE_ROOT = '.zcode/cli'
DB = TRACE_ROOT + '/db/db.sqlite'


def artifact_kind(path):
    p = PurePosixPath(path)
    if p.is_absolute() or '..' in p.parts:
        return None
    path = p.as_posix()
    if path in {DB, DB + '-wal', DB + '-shm'}:
        return {DB: 'session_database', DB + '-wal': 'session_database_wal',
                DB + '-shm': 'session_database_shm'}[path]
    if path.startswith(TRACE_ROOT + '/rollout/') and p.name.startswith('model-io-') and p.suffix == '.jsonl':
        return 'session_transcript'
    for directory, kind in [('db', 'session_diagnostic'), ('rollout', 'session_diagnostic'),
                            ('log', 'debug_log'), ('debug', 'debug_log'), ('exec', 'terminal_output'),
                            ('sessions', 'session_diagnostic')]:
        root = TRACE_ROOT + '/' + directory
        if path == root or path.startswith(root + '/'):
            return kind
    return None


def path_matches(path, sid):
    return bool(sid and artifact_kind(path) == 'session_transcript'
                and PurePosixPath(path).name == f'model-io-{sid}.jsonl')


def native_command(model, workspace, sid=None, resume=False, effort=None, permissions='full'):
    if effort:
        raise ValueError('ZCode reasoning effort override is not validated; omit --effort')
    cmd = ['zcode', '--cwd', workspace, '--mode', 'yolo' if permissions == 'full' else 'edit',
           '--output-format', 'stream-json', '--locale', 'en-US']
    if resume:
        if not sid:
            raise ValueError('ZCode resume requires the native session ID')
        cmd += ['--resume', sid]
    return cmd


def initialize(model, home):
    path = Path(home) / '.zcode/v2/provider_config.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'schemaVersion': 1, 'config': {
        'providerConfigRules': {'providerRules': [{
            'providerId': 'trace-lab', 'providerName': 'Trace Lab', 'enabled': True,
            'config': {'group': 'standard-personal',
                       'access': {'type': 'api-key', 'apiKey': 'trace-lab-placeholder'},
                       'api': {'type': 'openai-chat-completions', 'baseUrl': 'http://127.0.0.1:8080/v1'},
                       'personalModelIds': [model]}}]},
        'modelConfigRules': {'providerModelRules': [], 'manualProviderModelRules': []},
        'defaultModelSelection': {'providerId': 'trace-lab', 'modelId': model}}}) + '\n')
    return path


def session_id(stream):
    return next((e['sessionId'] for e in stream if isinstance(e.get('sessionId'), str)
                 and e.get('type') in {'turn.started', 'session.resumed', 'turn.completed', 'result'}), None)


def succeeded(stream):
    terminal = [e for e in stream if e.get('type') in {'turn.completed', 'turn.failed'}]
    return bool(terminal and terminal[-1]['type'] == 'turn.completed'
                and terminal[-1].get('payload', {}).get('resultType') == 'success')


def final_response(stream):
    return next((e.get('payload', {}).get('response') for e in reversed(stream)
                 if e.get('type') == 'turn.completed'), None)


def tool_inputs(stream):
    calls, results = {}, {}
    for event in stream:
        if event.get('type') != 'tool.updated':
            continue
        p = event.get('payload', {})
        cid = p.get('toolCallId')
        if not cid:
            continue
        # Native IDs can recur across mocked turns; bind to session and turn too.
        key = (event.get('sessionId'), event.get('turnId'), cid)
        if p.get('kind') == 'scheduled' and isinstance(p.get('input'), dict):
            calls[key] = p
        if p.get('kind') == 'result':
            results[key] = p.get('result', {})
    for key, call in calls.items():
        result = results.get(key, {})
        command = result.get('perf', {}).get('detail', {}).get('command', {})
        ok = result.get('success') is True and command.get('exitCode', 0) == 0 and not command.get('timedOut')
        arguments = dict(call['input'])
        arguments.update(_native_status='completed' if ok else 'error',
                         _native_output=result.get('content', ''),
                         _native_error=None if ok else result.get('content') or 'Tool completion missing')
        yield key[-1], call.get('toolName', ''), arguments
