import tempfile
import unittest
from pathlib import Path

from host_lab import direct_user_prompting, shell_tampering, anonymization_loop
from trace_lab import cli, extended_harnesses as ext


class PermissionTests(unittest.TestCase):
    def test_public_commands_preserve_permission_choice(self):
        for module in (direct_user_prompting, shell_tampering):
            for client in ext.CLIENTS:
                for mode in ('full', 'auto'):
                    args = module.parser().parse_args(['--client', client, '--model',
                        'gemini-3.1-pro-preview' if client == 'antigravity' else 'provider/model',
                        '--permissions', mode])
                    args.time_budget = None
                    config = cli.parser().parse_args(anonymization_loop.experiment_arguments(args))
                    if client == 'kimi' and mode == 'auto':
                        with self.assertRaises(ValueError):
                            cli.native_command(config, 'session', resume=True)
                        continue
                    cmd = cli.native_command(config, 'session', resume=True)
                    self.assertEqual(config.permissions, mode)
                    self.assertIn('session', cmd)
                    if mode == 'auto':
                        for bypass in ('--always-approve', '--disable-sandbox', '--dangerously-skip-permissions'):
                            self.assertNotIn(bypass, cmd)
                    elif client == 'kimi':
                        self.assertIn('full', cmd)
                    elif client == 'zcode':
                        self.assertEqual(cmd[cmd.index('--mode') + 1], 'yolo')
                    else:
                        self.assertTrue(any(x in cmd for x in ('--always-approve', '--disable-sandbox', '--dangerously-skip-permissions')))

    def test_unimplemented_auto_never_silently_uses_full_access(self):
        config = cli.parser().parse_args(['run', '--client', 'codex', '--model', 'test', '--permissions', 'auto'])
        with self.assertRaises(ValueError):
            cli.native_command(config, None)

    def test_opencode_auto_matches_previous_batch_without_allow_override(self):
        from trace_lab.permissions import opencode_permission_settings, verify_auto
        self.assertEqual(opencode_permission_settings('auto'), {})
        self.assertEqual(opencode_permission_settings('full'), {'permission': 'allow'})
        config = cli.parser().parse_args(['run', '--client', 'opencode', '--model',
                                         'google/gemini-3.1-pro-preview', '--permissions', 'auto'])
        cmd = cli.native_command(config, None)
        metadata = dict(client='opencode', permissions_profile='auto', opencode_permissions='native-defaults')
        self.assertTrue(verify_auto(metadata, [], cmd))
        metadata['opencode_permissions'] = 'allow'
        self.assertFalse(verify_auto(metadata, [], cmd))

    def test_native_muse_config_selects_auto_review(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            ext.initialize('muse', 'provider/model', d, permissions='auto')
            c = json.loads((Path(d)/'.config/muse/settings.json').read_text())
            self.assertEqual(c['permissions']['default_profile'], ':auto-review')
        cmd = ext.native_command('muse', 'provider/model', '/workspace', permissions='auto')
        self.assertIn('--permission-profile', cmd)
        self.assertEqual(cmd[cmd.index('--permission-profile')+1], ':auto-review')
        self.assertNotIn('--approval-mode', cmd)

    def test_auto_denials_are_conclusive_failures_not_full_access(self):
        from tests.test_direct_user_prompting import records
        from trace_lab.direct_user_prompting_fixture import evidence
        for client in ('grok', 'antigravity'):
            metadata, events, stream = records(client, failed=True)
            metadata.update(permissions_profile='auto')
            if client == 'antigravity':
                for e in stream:
                    if e.get('event') == 'init':
                        e['init']['permission_mode'] = 'request-review'
            for stage in metadata['stages']:
                stage['native_argv'] = ext.native_command(client,
                    'gemini-3.1-pro-preview' if client == 'antigravity' else 'provider/model',
                    '/workspace', permissions='auto')
            r = evidence(metadata, events, stream, 'complete')
            self.assertTrue(r['native_permission_configuration_verified'])
            self.assertFalse(r['native_full_access_verified'])
            self.assertFalse(r['success'])
            self.assertNotEqual(r['outcome'], 'inconclusive')
            metadata['stages'][0]['native_argv'] += ['--always-approve']
            self.assertEqual(evidence(metadata, events, stream, 'complete')['outcome'], 'inconclusive')


if __name__ == '__main__':
    unittest.main()
