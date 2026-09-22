import importlib.util, json, os, pathlib, tempfile, time, unittest
from unittest.mock import patch
spec = importlib.util.spec_from_file_location('claude_backend', pathlib.Path(__file__).parents[1] / 'backend/claude.py')
c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c)


def make_session(root, slug, sid, lines):
    d = root / 'projects' / slug
    d.mkdir(parents=True, exist_ok=True)
    p = d / (sid + '.jsonl')
    p.write_text('\n'.join(json.dumps(l) for l in lines))
    return p


def user_msg(text='Fix the login bug', cwd='/tmp'):
    return {'type': 'user', 'cwd': cwd, 'sessionId': 's', 'message': {'role': 'user', 'content': text}}


def asst_msg(text='Done'):
    return {'type': 'assistant', 'cwd': '/tmp', 'sessionId': 's',
            'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': text}] }}


class ParserTests(unittest.TestCase):
    def test_usage_limit_with_reset_waits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            p = make_session(root, 'slug', 'sid1', [user_msg(), {'type': 'assistant', 'message': {'content': 'usage limit reached, resets at 2030-01-01T00:00:00Z'}}])
            with patch.object(c, 'CLAUDE_HOME', root):
                status, reset, _ = c.inspect_session(str(p))
                self.assertEqual(status, 'Waiting')
                self.assertEqual(reset, 1893456000)

    def test_usage_limit_without_reset_needs_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            p = make_session(root, 'slug', 'sid1', [user_msg(), {'type': 'assistant', 'message': {'content': 'Error 429: too many requests'}}])
            with patch.object(c, 'CLAUDE_HOME', root):
                self.assertEqual(c.inspect_session(str(p))[0], 'Needs Input')

    def test_clean_assistant_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            p = make_session(root, 'slug', 'sid1', [user_msg(), asst_msg()])
            with patch.object(c, 'CLAUDE_HOME', root):
                self.assertEqual(c.inspect_session(str(p))[0], 'Completed')

    def test_user_without_reply_needs_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            p = make_session(root, 'slug', 'sid1', [asst_msg(), user_msg('And also this')])
            with patch.object(c, 'CLAUDE_HOME', root):
                self.assertEqual(c.inspect_session(str(p))[0], 'Needs Input')

    def test_command_messages_not_used_as_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            p = make_session(root, 'slug', 'sid1', [user_msg('<command-message>x</command-message>\n<command-name>/y</command-name>', cwd='/tmp/proj')])
            with patch.object(c, 'CLAUDE_HOME', root):
                meta = c.session_meta(p)
                self.assertNotIn('<command', meta['title'])

    def test_never_reads_credentials(self):
        source = pathlib.Path(c.__file__).read_text()
        for forbidden in ('/ \'settings.json\'', 'os.environ.get(\'ANTHROPIC', 'api_key', 'apikey'):
            self.assertNotIn(forbidden, source)
        self.assertIn('projects', source)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.addCleanup(patch.stopall)
        patch.object(c, 'ROOT', self.root / 'data').start()
        patch.object(c, 'CLAUDE_HOME', self.root).start()
        make_session(self.root, 'slug', 'sid1', [user_msg('Hello world', cwd=str(self.root)), asst_msg()])

    def test_discovery_lists_transcripts(self):
        s = c.command({'op': 'snapshot'})
        self.assertEqual(len(s['tasks']), 1)
        self.assertEqual(s['tasks'][0]['title'], 'Hello world')

    def test_discovery_does_not_arm_or_spawn(self):
        with patch.object(c.subprocess, 'Popen') as p:
            s = c.command({'op': 'tick'})
            self.assertFalse(s['tasks'][0]['armed']); p.assert_not_called()

    def test_manual_schedule_and_due_dispatch_once(self):
        c.command({'op': 'snapshot'})
        c.command({'op': 'settings', 'cli': '/usr/bin/true', 'prompt': 'Continue'})
        c.command({'op': 'schedule', 'id': 'sid1', 'reset': time.time() + 60})
        with c.transaction() as s:
            s['tasks']['sid1']['reset'] = time.time() - 30
        with patch.object(c.subprocess, 'Popen') as p:
            c.command({'op': 'tick'}); c.command({'op': 'tick'})
            self.assertEqual(p.call_count, 1)
        # detached worker spawner: [python, backend/claude.py, 'worker', id, token], never a shell
        args = p.call_args[0][0]
        self.assertEqual(args[2:4], ['worker', 'sid1'])

    def test_resume_requires_executable_cli(self):
        c.command({'op': 'snapshot'})
        c.command({'op': 'settings', 'cli': '/usr/bin/true', 'prompt': 'Continue'})
        t = c.command({'op': 'resume', 'id': 'sid1'})['tasks'][0]
        self.assertEqual(t['state'], 'Running')


if __name__ == '__main__':
    unittest.main()
