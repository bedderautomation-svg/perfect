"""Bind protected gateway arguments to native Muse exec tool results by call ID."""
import json


def attach(directory, stream_name, started_ns):
    gateway = directory / 'gateway.log'
    if not gateway.exists():
        return
    calls = {}
    for line in gateway.read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get('kind') == 'muse_tool_call' and event.get('observed_ns', 0) >= started_ns:
            calls.setdefault(event['call_id'], []).append(event)
    path = directory / stream_name
    native = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    used = {e.get('call_id') for e in native if e.get('event') == 'trace_lab_muse_tool_evidence'}
    with path.open('a') as output:
        for event in native:
            if event.get('payload_type') != 'tool.result':
                continue
            p = event['payload']; cid = p.get('call_id')
            candidates = calls.get(cid, [])
            if cid in used or len(candidates) != 1:
                continue
            call = candidates[0]
            if call.get('name') != p.get('correlation_facts', {}).get('tool_name'):
                continue
            output.write(json.dumps({**call, 'event': 'trace_lab_muse_tool_evidence',
                                    'source': 'protected_gateway', 'binding': 'exact_native_call_id_and_name'}) + '\n')
            used.add(cid)
