"""Observation-only hook: never returns instructions or changes model context."""

import json
from pathlib import Path
import sys
import time


def main():
    value = json.load(sys.stdin)
    value['observed_ns'] = time.time_ns()
    with Path('/tmp/trace-lab-gemini-compaction-hooks.jsonl').open('a') as output:
        output.write(json.dumps(value) + '\n')
    print('{}')


if __name__ == '__main__':
    main()
