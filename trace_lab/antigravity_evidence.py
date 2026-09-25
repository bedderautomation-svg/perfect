"""Recover shell exit status omitted by Antigravity's stream-json output.

The protected API gateway captures the native function result before forwarding
it. The host matches it to an observed native tool step; a result alone is never
an executed tool, and unmatched shell steps receive no grading credit.
"""
import json
import re


def shell_results(body):
    calls = {}
    for message in body.get('contents', []):
        for part in message.get('parts', []):
            call = part.get('functionCall', {})
            if call.get('name') == 'run_command' and call.get('id'):
                calls[call['id']] = call
            response = part.get('functionResponse', {})
            call = calls.get(response.get('id'))
            if not call or response.get('name') != 'run_command':
                continue
            output = response.get('response', {}).get('output', '')
            match = re.search(r'The command exited with code (-?\d+)\.', output)
            yield {'call_id': response['id'], 'command': call.get('args', {}).get('CommandLine'),
                   'exit_code': int(match[1]) if match else None, 'output': output}


def attach(directory, stream_name, started_ns):
    """Append provenance-labelled evidence after a supervised native stage."""
    log = directory / 'gateway.log'
    if not log.exists():
        return
    previous, results = set(), {}
    for line in log.read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get('kind') != 'antigravity_shell_result':
            continue
        cid = event['call_id']
        if event['observed_ns'] < started_ns:
            previous.add(cid)
        else:
            results[cid] = event
    results = [r for cid, r in results.items() if cid not in previous]
    path = directory / stream_name
    steps, used = {}, set()
    for line in path.read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get('event') == 'init':
            steps.clear()
        if event.get('event') == 'trace_lab_tool_evidence':
            used.add((event.get('conversation_id'), event.get('step_index')))
        step = event.get('step_update', {})
        if step.get('tool_name') == 'run_command' and step.get('state') == 'DONE':
            steps[(step.get('conversation_id'), step.get('step_index'))] = step
    additions = []
    for key, step in steps.items():
        if key in used:
            continue
        command = step.get('tool_info', {}).get('parameters', {}).get('CommandLine')
        match = next((r for r in results if r.get('command') == command), None)
        truncated = False
        if match is None and isinstance(command, str) and len(command) == 513 and command.endswith('…'):
            # Native stream-json renders long command arguments as 512 chars
            # plus an ellipsis. Bind only a unique full gateway call in this
            # stage; never choose between commands sharing the displayed prefix.
            candidates = [r for r in results if isinstance(r.get('command'), str)
                          and r['command'].startswith(command[:-1])]
            if len(candidates) == 1:
                match, truncated = candidates[0], True
        if match is not None:
            results.remove(match)
            additions.append({'event': 'trace_lab_tool_evidence', 'conversation_id': key[0],
                              'step_index': key[1], 'source': 'protected_gateway', **match,
                              'native_display_command': command,
                              'binding': 'unique_512_character_prefix' if truncated else 'exact'})
    with path.open('a') as output:
        for event in additions:
            output.write(json.dumps(event) + '\n')
