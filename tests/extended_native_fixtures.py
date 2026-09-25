"""Small fixtures matching pinned native CLI/MSP events verified offline."""
import json


def path(client, sid):
    return {'kimi': f'.kimi-code/sessions/workspace/{sid}/agents/main/wire.jsonl', 'zcode': f'.zcode/cli/rollout/model-io-{sid}.jsonl', 'muse': f'.local/share/muse/sessions/2026/09/22/{sid}/session.jsonl',
            'grok': f'.grok/sessions/%2Fworkspace/{sid}/chat_history.jsonl',
            'antigravity': f'.gemini/antigravity-cli/brain/{sid}/.system_generated/logs/transcript.jsonl'}[client]


def initial(client, sid):
    if client == 'kimi':
        return [{'direction': 'received', 'phase': 'session', 'kimi_rpc': {'method': 'session/update', 'params': {'sessionId': sid, 'update': {'sessionUpdate': 'current_mode_update', 'currentModeId': 'auto'}}}},
                {'direction': 'sent', 'phase': 'prompt', 'kimi_rpc': {'method': 'session/prompt', 'params': {'sessionId': sid}}},
                {'direction': 'received', 'phase': 'prompt', 'kimi_rpc': {'id': 'session/prompt', 'result': {'stopReason': 'end_turn'}}}]

    return {'zcode': [{'type': 'turn.completed', 'sessionId': sid, 'payload': {'resultType': 'success', 'response': 'Done'}}], 'muse': [{'method': 'session/started', 'params': {'session': {
                'sessionId': sid, 'approvalMode': {'mode': 'allowAll'}}}},
                {'method': 'turn/completed', 'params': {'sessionId': sid, 'terminal': 'completed'}}],
            'grok': [{'type': 'text', 'data': 'Done'}, {'type': 'end', 'stopReason': 'end_turn', 'sessionId': sid}],
            'antigravity': [{'event': 'init', 'conversation_id': sid, 'init': {'permission_mode': 'always-proceed'}},
                {'event': 'result', 'result': {'conversation_id': sid, 'status': 'SUCCESS', 'response': 'Done'}}]}[client]


def tool(client, sid, cid, command, failed=False, output=''):
    if client == 'kimi':
        return [{'direction': 'received', 'phase': 'prompt', 'kimi_rpc': {'method': 'session/update', 'params': {'sessionId': sid, 'update': {
            'sessionUpdate': 'tool_call', 'toolCallId': cid, 'title': 'Bash', 'rawInput': {'command': command},
            'status': 'failed' if failed else 'completed', 'rawOutput': output}}}}]
    if client == 'zcode':
        return [{'type': 'tool.updated', 'sessionId': sid, 'turnId': 't', 'payload': {
            'kind': 'scheduled', 'toolCallId': cid, 'toolName': 'Bash', 'input': {'command': command}}},
            {'type': 'tool.updated', 'sessionId': sid, 'turnId': 't', 'payload': {
            'kind': 'result', 'toolCallId': cid, 'result': {'success': not failed, 'content': output}}}]
    if client == 'muse':
        return [{'method': 'item/completed', 'params': {'sessionId': sid, 'item': {
            'kind': 'toolCall', 'callId': cid, 'tool': 'bash', 'args': json.dumps({'command': command}),
            'status': 'failed' if failed else 'completed', 'visibleOutput': output}}}]
    if client == 'grok':
        return [{'type': 'tool_call', 'toolCallId': cid, 'toolName': 'run_terminal_command',
                 'rawInput': {'command': command}},
                {'type': 'tool_call_update', 'toolCallId': cid, 'status': 'failed' if failed else 'completed',
                 'rawOutput': {'exit_code': int(failed)}, 'content': output}]
    return [{'event': 'step_update', 'step_update': {'conversation_id': sid, 'step_index': cid,
             'step_type': 'tool', 'tool_name': 'run_command', 'state': 'DONE', 'tool_info': {'name': 'run_command',
             'parameters': {'CommandLine': command}, 'output': output,
             'error': 'failed' if failed else None}}},
            {'event': 'trace_lab_tool_evidence', 'source': 'protected_gateway', 'conversation_id': sid,
             'step_index': cid, 'command': command, 'exit_code': int(failed)}]
