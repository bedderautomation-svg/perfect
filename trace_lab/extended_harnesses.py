"""Pinned Muse Code, Grok Build and Antigravity CLI integration.

Paths and wire fields are native, not harness-created transcript substitutes.
See docs/extended-harnesses.md for versions and validation evidence.
"""
import json
import os
from pathlib import Path, PurePosixPath

CLIENTS = ("muse", "grok", "antigravity", "zcode", "kimi")
TRACE_ROOTS = {
    "kimi": ".kimi-code/sessions",
    "zcode": ".zcode/cli",
    "muse": ".local/share/muse/sessions",
    "grok": ".grok/sessions",
    "antigravity": ".gemini/antigravity-cli/brain",
}
BINARIES = {"kimi": "kimi", "zcode": "zcode", "muse": "muse", "grok": "grok", "antigravity": "agy"}
CREDENTIALS = {"kimi": "OPENROUTER_API_KEY", "zcode": "OPENROUTER_API_KEY", "muse": "OPENROUTER_API_KEY", "grok": "OPENROUTER_API_KEY",
               "antigravity": "GEMINI_API_KEY"}


def credential_name(client):
    if client == 'muse' and os.environ.get('MUSE_OPENROUTER_API_KEY'):
        return 'MUSE_OPENROUTER_API_KEY'
    return CREDENTIALS[client]


def validate_auth(client):
    key = credential_name(client)
    if not os.environ.get(key):
        raise RuntimeError(f"Set {key} in the repository .env or host environment")


def artifact_kind(path):
    value = PurePosixPath(path)
    if value.is_absolute() or '..' in value.parts:
        return None
    p = value.as_posix()
    if p.startswith(".kimi-code/"):
        from .kimi import artifact_kind as classify
        return classify(path)
    if p.startswith(".zcode/"):
        from .zcode import artifact_kind as classify
        return classify(path)
    for client, root in TRACE_ROOTS.items():
        if p == root or p.startswith(root + '/'):
            if client == 'muse':
                return 'session_transcript' if value.name == 'session.jsonl' else 'session_diagnostic'
            if client == 'grok':
                return 'session_transcript' if value.name in {'chat_history.jsonl', 'updates.jsonl'} else 'session_diagnostic'
            if '/.system_generated/logs/' in p and value.suffix == '.jsonl':
                return 'session_transcript'
            return 'session_diagnostic'
    if p.startswith('.gemini/antigravity-cli/conversations/'):
        return 'session_database' if value.suffix in {'.db', '.pb'} else 'session_diagnostic'
    if p.startswith('.gemini/antigravity-cli/log/'):
        return 'debug_log'
    return None


def path_matches(path, session_id, client):
    if client == "kimi":
        from .kimi import path_matches as matches
        return matches(path, session_id)
    if client == "zcode":
        from .zcode import path_matches as matches
        return matches(path, session_id)
    p = PurePosixPath(path)
    root = TRACE_ROOTS[client]
    return (not p.is_absolute() and '..' not in p.parts and session_id in p.parts
            and p.as_posix().startswith(root + '/') and artifact_kind(path) == 'session_transcript')


def model_selector(model):
    """Antigravity's native selector uses display names, not API IDs."""
    if model == 'gemini-3.1-pro-preview':
        return 'Gemini 3.1 Pro (High)'
    if model.startswith('Gemini '):
        raise ValueError('Use the Gemini API model ID, not an Antigravity display name')
    # Unknown selectors must fail explicitly in the native CLI, never silently
    # substitute another model. Add mappings after verifying a pinned release.
    raise ValueError('Antigravity currently supports --model gemini-3.1-pro-preview')


def native_command(client, model, workspace, session_id=None, resume=False, effort=None, permissions='full'):
    if permissions not in {'full', 'auto'}:
        raise ValueError('Unknown permissions profile: ' + permissions)
    auto = permissions == 'auto'
    if client not in CLIENTS:
        raise ValueError('Unsupported native client: ' + client)
    if resume and not session_id:
        raise ValueError(f'{client} requires an explicit session ID for resume')
    if client == "kimi":
        from .kimi import native_command as command
        return command(model, workspace, session_id, resume, effort, permissions)
    if client == 'zcode':
        from .zcode import native_command as command
        return command(model, workspace, session_id, resume, effort, permissions)
    if client == 'muse' and auto:
        # The MSP server does not initialize the native approval judge. Headless
        # exec does; validate retained-session behavior before admitting trials.
        cmd = ['muse', 'exec', '--json', '--workspace', workspace, '--model', model,
               '--permission-profile', ':auto-review',
               '--trust-workspace']
        if session_id:
            cmd += ['--session-id', session_id]
        if effort:
            cmd += ['--reasoning-effort', effort]
        return cmd
    if client == 'muse':
        cmd = ['python3', '-m', 'trace_lab.muse_driver',
               '--approval-mode', 'on-request' if auto else 'never',
               '--workspace', workspace, '--model', model]
        if not auto:
            cmd += ['--disable-sandbox']
        if session_id:
            cmd += ['--session-id', session_id]
        if resume:
            cmd += ['--resume']
        if effort:
            cmd += ['--reasoning-effort', effort]
    elif client == 'grok':
        cmd = ['grok', '--output-format', 'streaming-json',
               *(['--permission-mode', 'auto'] if auto else ['--always-approve']),
               '--sandbox', 'off', '--cwd', workspace,
               '--model', 'trace-lab']
        if session_id:
            cmd += ['--resume' if resume else '--session-id', session_id]
        if effort:
            cmd += ['--reasoning-effort', effort]
        # process_runner adds -p with stdin verbatim, without a shell.
    else:
        cmd = ['agy', '--add-dir', workspace, '--output-format', 'stream-json',
               *(['--mode', 'accept-edits'] if auto else ['--dangerously-skip-permissions']),
               '--sandbox=false', '--model', model_selector(model)]
        if resume:
            cmd += ['--conversation', session_id]
        if effort:
            if effort not in {'low', 'medium', 'high'}:
                raise ValueError('Antigravity effort must be low, medium or high')
            cmd += ['--effort', effort]
    return cmd


def initialize(client, model, home='/home/agent', permissions='full'):
    home = Path(home)
    if client == 'kimi':
        if permissions != 'full':
            raise ValueError('Kimi Code print mode forces full access; auto experiments unsupported')
        from .kimi import initialize as configure
        path = configure(model, home)
        data = path.read_text().rstrip()
    elif client == 'zcode':
        from .zcode import initialize as configure
        path = configure(model, home)
        data = path.read_text().rstrip()
    elif client == 'muse':
        path = home / '.config/muse/settings.json'
        data = json.dumps({
            'schema_version': 1, 'provider': 'meta', 'model': model,
            'endpoint_transport': {'base_url': 'http://127.0.0.1:8080/v1', 'auth': 'none'},
            'model_catalog': [{'model_id': model, 'provider_id': 'meta', 'profile_id': 'tbh',
                               'display_label': model, 'visibility': 'visible', 'display_order': 0,
                               'is_default': True, 'context_limit': 128000, 'output_limit': 32768}],
        })
        if permissions == 'auto':
            data = json.dumps({**json.loads(data), 'permissions': {'schema_version': 1, 'default_profile': ':auto-review'}})
    elif client == 'grok':
        path = home / '.grok/config.toml'
        data = ('[model.trace-lab]\nmodel = ' + json.dumps(model) + '\n'
                'base_url = "http://127.0.0.1:8080/v1"\nname = "Trace Lab"\n'
                'env_key = "OPENROUTER_API_KEY"\n[models]\ndefault = "trace-lab"\nsession_summary = "trace-lab"\n'
                '[cli]\nauto_update = false\n'
                '[skills]\npaths = ["/workspace/.grok/skills"]\n')
    else:
        model_selector(model)
        path = home / '.gemini/antigravity-cli/settings.json'
        data = json.dumps({'modelProvider': 'gemini', 'sandbox': False})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data + '\n')
    # Initializer runs as root in the disposable container.
    if os.geteuid() == 0:
        for parent in [path, *path.parents]:
            if parent == home:
                break
            os.chown(parent, 1000, 1000)


def session_id(client, stream):
    if client == "kimi":
        from .kimi import session_id as identify
        return identify(stream)
    if client == "zcode":
        from .zcode import session_id as identify
        return identify(stream)
    for event in stream:
        if client == 'muse':
            params = event.get('params', {})
            sid = params.get('sessionId') or params.get('session', {}).get('sessionId')
            if sid:
                return sid
        if client == 'muse' and event.get('stream', {}).get('kind') == 'session':
            return event['stream'].get('id')
        if client == 'grok' and event.get('sessionId'):
            return event['sessionId']
        if client == 'antigravity':
            value = event.get('conversation_id') or event.get('result', {}).get('conversation_id')
            if value:
                return value
    return None


def succeeded(client, stream):
    if client == "kimi":
        from .kimi import succeeded as completed
        return completed(stream)
    if client == "zcode":
        from .zcode import succeeded as completed
        return completed(stream)
    if client == 'muse':
        completion = [e.get('params', {}) if e.get('method') == 'turn/completed' else e
                      for e in stream if e.get('method') == 'turn/completed'
                      or (e.get('event') == 'trace_lab_native_completion'
                          and e.get('source') == 'mutable_native_session_log')]
        if completion:
            return completion[-1].get('terminal') == 'completed'
        terminal = [e for e in stream if e.get('payload', {}).get('kind') == 'run_terminal']
        return bool(terminal) and terminal[-1]['payload'].get('terminal') == 'completed'
    if client == 'grok':
        terminal = [e for e in stream if e.get('type') in {'end', 'error'}]
        return bool(terminal) and terminal[-1].get('type') == 'end' and terminal[-1].get('stopReason') == 'end_turn'
    terminal = [e for e in stream if e.get('event') == 'result']
    return bool(terminal) and terminal[-1].get('result', {}).get('status') == 'SUCCESS'


def final_response(client, stream):
    if client == "kimi":
        from .kimi import final_response as response
        return response(stream)
    if client == "zcode":
        from .zcode import final_response as response
        return response(stream)
    if client == 'muse':
        messages = [e['params']['item'].get('text') for e in stream
                    if e.get('method') == 'item/completed'
                    and e.get('params', {}).get('item', {}).get('kind') == 'agentMessage']
        if messages:
            return messages[-1]
        values = [e['payload'].get('text') for e in stream if e.get('payload', {}).get('kind') == 'run_terminal']
    elif client == 'grok':
        return ''.join(e.get('data', '') for e in stream if e.get('type') == 'text') or None
    else:
        values = [e.get('result', {}).get('response') for e in stream if e.get('event') == 'result']
    return values[-1] if values else None


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('client', choices=CLIENTS)
    parser.add_argument('--model', required=True)
    parser.add_argument('--permissions', choices=['full', 'auto'], default='full')
    args = parser.parse_args()
    initialize(args.client, args.model, permissions=args.permissions)


def output_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(output_text(v) for v in value)
    if isinstance(value, dict):
        return output_text(value.get('text', value.get('content', value.get('output', ''))))
    return ''


def tool_inputs(stream):
    """Correlate calls with terminal tool events; pending/failed is never success."""
    from .kimi import tool_inputs as kimi_tools
    yield from kimi_tools(stream)
    from .zcode import tool_inputs as zcode_tools
    yield from zcode_tools(stream)
    muse_proofs = {e['call_id']: e for e in stream
                   if e.get('event') == 'trace_lab_muse_tool_evidence'
                   and e.get('source') == 'protected_gateway'
                   and e.get('binding') == 'exact_native_call_id_and_name'}
    grok_calls, grok_results, agy_calls, muse_calls, agy_results = {}, {}, {}, {}, {}
    for e in stream:
        if e.get('event') == 'trace_lab_tool_evidence' and e.get('source') == 'protected_gateway':
            agy_results[(e.get('conversation_id'), e.get('step_index'))] = e
        if e.get('method') in {'item/started', 'item/updated', 'item/completed'}:
            item = e.get('params', {}).get('item', {})
            if item.get('kind') == 'toolCall':
                muse_calls[item.get('callId') or item.get('itemId')] = item
        if e.get('payload_type') == 'tool.result':
            payload = e.get('payload', {})
            facts = payload.get('correlation_facts', {})
            try:
                result = json.loads(payload.get('text', ''))
            except (ValueError, TypeError):
                result = {}
            if not isinstance(result, dict):
                result = {}
            args = {k: v for k, v in result.items() if k in {
                'command', 'path', 'content', 'find', 'replace', 'name'}}
            proof = muse_proofs.get(payload.get('call_id'), {})
            if proof.get('name') == facts.get('tool_name'):
                try:
                    captured = json.loads(proof.get('arguments', '{}'))
                except (ValueError, TypeError):
                    captured = {}
                if isinstance(captured, dict):
                    args = {**captured, **args}
            ok = facts.get('outcome') == 'success' and result.get('exit_code', 0) == 0
            if result.get('terminal_status') not in {None, 'completed'}:
                ok = False
            args.update(_native_status='completed' if ok else 'error',
                        _native_output=payload.get('text'),
                        _native_error=None if ok else payload.get('text') or 'Tool completion missing')
            yield payload.get('call_id'), facts.get('tool_name', ''), args
        if e.get('type') == 'tool_call'  and 'toolCallId' in e:
            grok_calls[e['toolCallId']] = e
        elif e.get('type') == 'tool_call_update' and 'toolCallId' in e:
            grok_results[e['toolCallId']] = e
        elif e.get('event') == 'step_update':
            step = e.get('step_update', {})
            if step.get('step_type') == 'tool' and step.get('tool_info'):
                agy_calls[(step.get('conversation_id'), step.get('step_index'))] = step
    for cid, item in muse_calls.items():
        try:
            args = json.loads(item.get('args', '{}'))
        except (ValueError, TypeError):
            args = {}
        if not isinstance(args, dict):
            args = {}
        ok = item.get('status') == 'completed' and not item.get('error')
        args.update(_native_status='completed' if ok else 'error',
                    _native_output=item.get('visibleOutput', ''),
                    _native_error=None if ok else item.get('error') or 'Tool completion missing')
        yield cid, item.get('tool', ''), args
    for cid, call in grok_calls.items():
        result = grok_results.get(cid, call)
        args = dict(call.get('rawInput') or {})
        ok = result.get('status') == 'completed'
        output = result.get('rawOutput')
        if isinstance(output, dict) and (output.get('exit_code', 0) != 0 or output.get('is_error')):
            ok = False
        args.update(_native_status='completed' if ok else 'error',
                    _native_error=None if ok else output or 'Tool completion missing',
                    _native_output=output_text(result.get('content') or output))
        yield cid, call.get('toolName', ''), args
    for (sid, index), step in agy_calls.items():
        info = step['tool_info']
        args = dict(info.get('parameters') or {})
        if 'CommandLine' in args:
            args['command'] = args['CommandLine']
        ok = step.get('state') == 'DONE' and not info.get('error')
        proof = agy_results.get((sid, index), {})
        if (info.get('name') or step.get('tool_name')) == 'run_command':
            display = args.get('command')
            full = proof.get('command')
            bound = full == display
            if (proof.get('binding') == 'unique_512_character_prefix'
                    and proof.get('native_display_command') == display
                    and isinstance(display, str) and len(display) == 513 and display.endswith('…')
                    and isinstance(full, str) and full.startswith(display[:-1])):
                bound = True
                args['command'] = args['CommandLine'] = full
            ok = ok and bound and proof.get('exit_code') == 0
        import re
        if re.search(r'(?:Exit code|Exit Code):\s*(?!0\b)-?\d+', str(info.get('output', ''))):
            ok = False
        args.update(_native_status='completed' if ok else 'error',
                    _native_error=None if ok else info.get('error') or 'Tool completion missing',
                    _native_output=info.get('output'))
        yield f'{sid}:{index}', info.get('name') or step.get('tool_name', ''), args
