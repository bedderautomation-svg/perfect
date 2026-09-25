"""Small Responses transport adaptation for Muse's OpenRouter connection.

Only tool identifiers and SSE envelopes change; model messages and native logs
remain untouched. The selected model is fixed by the gateway.
"""
import hashlib
import json


def catalog(model):
    return {'data': [{'id': model, 'model_id': model, 'provider_id': 'meta', 'profile_id': 'tbh',
                      'display_label': model, 'visibility': 'visible', 'display_order': 0,
                      'is_default': True, 'context_limit': 128000, 'output_limit': 32768}]}


def transform(value, mapping):
    if isinstance(value, list):
        return [transform(v, mapping) for v in value]
    if not isinstance(value, dict):
        return value
    return {k: mapping.get(v, v) if k == 'name' and isinstance(v, str) else transform(v, mapping)
            for k, v in value.items()}


def prepare(body):
    value = json.loads(body)
    names = set()
    def collect(v):
        if isinstance(v, dict):
            if isinstance(v.get('name'), str) and len(v['name']) > 64:
                names.add(v['name'])
            for child in v.values():
                collect(child)
        elif isinstance(v, list):
            for child in v:
                collect(child)
    collect(value.get('tools', []))
    mapping = {n: n[:49] + '_' + hashlib.sha256(n.encode()).hexdigest()[:14] for n in names}
    value = transform(value, mapping)
    value['store'] = False
    value['max_output_tokens'] = min(value.get('max_output_tokens') or 32768, 32768)
    return json.dumps(value).encode(), {v: k for k, v in mapping.items()}


def stream_events(response, mapping):
    """Bounded SSE framing also supplies missing event names/sequence numbers."""
    frame, sequence = [], 0
    while line := response.readline(16 * 1024 * 1024 + 1):
        if len(line) > 16 * 1024 * 1024:
            raise ValueError('Oversized SSE event')
        line = line.decode('utf-8').rstrip('\r\n')
        if line:
            frame.append(line)
            continue
        if not frame:
            continue
        data = '\n'.join(l[5:].lstrip() for l in frame if l.startswith('data:'))
        if data and data != '[DONE]':
            value = transform(json.loads(data), mapping)
            value.setdefault('sequence_number', sequence)
            sequence += 1
            yield ('event: ' + value.get('type', 'message') + '\ndata: ' + json.dumps(value) + '\n\n').encode()
        else:
            yield ('\n'.join(frame) + '\n\n').encode()
        frame = []
    if frame:
        raise ValueError('Truncated SSE event')
