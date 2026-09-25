"""Official Kimi Code 2.0.2 (native v2 engine), not legacy kimi-cli."""
import json
from pathlib import Path, PurePosixPath

TRACE_ROOT = '.kimi-code/sessions'
DEFAULT_MODEL = 'moonshotai/kimi-k3'


def artifact_kind(path):
    p = PurePosixPath(path)
    if p.is_absolute() or '..' in p.parts:
        return None
    s = p.as_posix()
    if s.startswith(TRACE_ROOT + '/.index-dirty/') or '.tmp.' in p.name:
        return None
    if s == TRACE_ROOT or s.startswith(TRACE_ROOT + '/'):
        if p.name == 'wire.jsonl' and len(p.parts) == 7 and p.parts[4] == 'agents':
            return 'session_transcript' if p.parts[5] == 'main' else 'subagent_transcript'
        return 'session_diagnostic'
    if s.startswith('.kimi-code/logs/'):
        return 'debug_log'
    if s == '.kimi-code/session_index.jsonl':
        return 'session_index'
    return None


def path_matches(path, sid):
    p = PurePosixPath(path)
    return bool(sid and artifact_kind(path) == 'session_transcript' and p.parts[3] == sid)


def native_command(model, workspace, sid=None, resume=False, effort=None, permissions='full'):
    if permissions != 'full':
        raise ValueError('Kimi Code restricted auto-mode experiments are not validated')
    if effort:
        raise ValueError('Kimi Code effort override is not validated; omit --effort')
    cmd = ['python3', '-m', 'trace_lab.kimi_driver', '--workspace', workspace, '--permissions', 'full']
    if resume:
        if not sid:
            raise ValueError('Kimi resume requires the native session ID')
        cmd += ['--session-id', sid]
    return cmd


def initialize(model, home):
    path = Path(home) / '.kimi-code/config.toml'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('default_model = "trace-lab"\ntelemetry = false\n'
                    '[providers.trace-lab]\ntype = "openai"\n'
                    'base_url = "http://127.0.0.1:8080/v1"\napi_key = "trace-lab-placeholder"\n'
                    '[models.trace-lab]\nprovider = "trace-lab"\nmodel = ' + json.dumps(model) + '\n'
                    'max_context_size = 1048576\ncapabilities = ["tool_use", "thinking"]\n')
    return path


def session_id(stream):
    for e in stream:
        r = e.get('kimi_rpc', {})
        if e.get('direction') == 'sent' and r.get('method') == 'session/prompt':
            return r.get('params', {}).get('sessionId')
        if e.get('direction') == 'received' and r.get('id') == 'session/new':
            return r.get('result', {}).get('sessionId')
    return None


def succeeded(stream):
    if any(e.get('kimi_native_turn_check', {}).get('reason') == 'failed' for e in stream):
        return False
    terminal = [e['kimi_rpc'] for e in stream if e.get('direction') == 'received'
                and e.get('kimi_rpc', {}).get('id') == 'session/prompt']
    return bool(terminal and terminal[-1].get('result', {}).get('stopReason') in {'end_turn', 'refusal'})


def final_response(stream):
    return ''.join(e.get('kimi_rpc', {}).get('params', {}).get('update', {}).get('content', {}).get('text', '')
                   for e in stream if e.get('direction') == 'received' and e.get('phase') == 'prompt'
                   and e.get('kimi_rpc', {}).get('params', {}).get('update', {}).get('sessionUpdate') == 'agent_message_chunk') or None


def tool_inputs(stream):
    calls = {}
    for e in stream:
        if e.get('direction') != 'received' or e.get('phase') != 'prompt':
            continue
        p = e.get('kimi_rpc', {}).get('params', {})
        u = p.get('update', {})
        if u.get('sessionUpdate') not in {'tool_call', 'tool_call_update'} or not u.get('toolCallId'):
            continue
        key = (p.get('sessionId'), u['toolCallId'])
        item = calls.setdefault(key, {})
        if 'title' in u and 'name' not in item:
            item['name'] = u['title'].split('(')[0].strip()
        item.update(u)
    for (_, cid), u in calls.items():
        args = u.get('rawInput', {})
        if not isinstance(args, dict):
            continue
        ok = u.get('status') == 'completed'
        yield cid, u.get('name', ''), {**args, '_native_status': 'completed' if ok else 'error',
                                      '_native_output': u.get('rawOutput', ''),
                                      '_native_error': None if ok else u.get('rawOutput') or 'Tool completion missing'}
