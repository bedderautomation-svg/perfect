"""Read-only prediction of the pinned Gemini 0.60.0 native history selector.

The actual gateway request remains the authoritative exposure check. This
helper never changes history, the compactor's prompt, or its generated output.
"""

import json


def serialized_chars(content):
    # Native JSON.stringify(...).length counts UTF-16 code units, not UTF-8
    # bytes or Python code points. Use compact, non-ASCII-escaped JSON.
    value = json.dumps(content, ensure_ascii=False, separators=(',', ':'))
    return len(value.encode('utf-16-le')) // 2


def gemini_split_point(contents, fraction=0.7):
    if not 0 < fraction < 1:
        raise ValueError('Fraction must be between 0 and 1')
    counts = [serialized_chars(content) for content in contents]
    target = sum(counts) * fraction
    last = cumulative = 0
    for index, content in enumerate(contents):
        if content.get('role') == 'user' and not any(part.get('functionResponse') for part in content.get('parts', [])):
            if cumulative >= target:
                return index
            last = index
        cumulative += counts[index]
    if contents and contents[-1].get('role') == 'model' and not any(part.get('functionCall') for part in contents[-1].get('parts', [])):
        return len(contents)
    return last


def visible_texts(contents):
    for content in contents:
        for part in content.get('parts', []):
            if isinstance(part.get('text'), str):
                yield part['text']
            response = part.get('functionResponse', {}).get('response', {})
            if isinstance(response, dict):
                for key in ('output', 'content'):
                    if isinstance(response.get(key), str):
                        yield response[key]


def gemini_scope(contents, payload):
    split = gemini_split_point(contents)
    return {'split_point': split, 'history_length': len(contents),
            'payload_in_history': any(payload in text for text in visible_texts(contents)),
            'payload_in_summarized_head': any(payload in text for text in visible_texts(contents[:split])),
            'payload_in_retained_tail': any(payload in text for text in visible_texts(contents[split:]))}
