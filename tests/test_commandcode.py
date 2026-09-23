import importlib.util, json, os, pathlib, tempfile, time, unittest
from unittest.mock import patch
spec = importlib.util.spec_from_file_location('commandcode_backend', pathlib.Path(__file__).parents[1] / 'backend/commandcode.py')
c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c)


def header(sid, cwd):
    return {'type': 'session', 'version': 3, 'id': sid, 'timestamp': '2026-09-23T08:40:56.097Z', 'cwd': cwd}


def user_entry(text='Fix the login bug'):
    return {'type': 'message', 'id': 'u', 'parentId': None, 'timestamp': '2026-09-23T08:41:38.808Z',
            'message': {'role': 'user', 'content': [{'type': 'text', 'text': text}], 'meta': {'source': 'user'}}}


def assistant_entry(content=None):
    return {'type': 'message', 'id': 'a', 'parentId': 'u', 'timestamp': '2026-09-23T08:42:00.000Z',
            'message': {'role': 'assistant', 'content': content or [{'type': 'text', 'text': 'Done'}],
                        'meta': {'source': 'model'}}}


def tool_result_entry():
    return {'type': 'message', 'id': 't', 'parentId': 'a', 'timestamp': '2026-09-23T08:42:30.000Z',
            'message': {'role': 'user', 'meta': {'source': 'tool'},
                        'content': [{'type': 'tool_result', 'tool_use_id': 'x', 'content': [{'type': 'text', 'text': 'ok'}]}]}}


def tool_use_entry():
    return {'type': 'message', 'id': 'a2', 'parentId': 'u', 'timestamp': '2026-09-23T08:43:00.000Z',
            'message': {'role': 'assistant', 'meta': {'source': 'model'},
                        'content': [{'type': 'text', 'text': 'reading'}, {'type': 'tool_use', 'id': 'x', 'name': 'read_file'}]}}


def make_session(root, slug, sid, entries, cwd='/tmp', meta=None):
    d = root / 'projects' / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / (sid + '.jsonl')).write_text('\n'.join(json.dumps(e) for e in [header(sid, cwd)] + entries))
    if meta is not None:
        (d / (sid + '.meta.json')).write_text(json.dumps(meta))
    return d


class ParserTests(unittest.TestCase):
    def inspect(self, entries):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            d = make_session(root, 'slug', 'sid1', entries)
            with patch.object(c, 'COMMANDCODE_HOME', root):
                return c.inspect_session(str(d / 'sid1.jsonl'))

    def test_clean_assistant_completes(self):
        self.assertEqual(self.inspect([user_entry(), assistant_entry()])[0], 'Completed')

    def test_user_without_reply_needs_input(self):
        self.assertEqual(self.inspect([assistant_entry(), user_entry('And also this')])[0], 'Needs Input')

    def test_trailing_tool_result_needs_input(self):
        # A tool result is carried in a user-role entry, so it is not a human prompt
        # and it does not mean the turn finished.
        self.assertEqual(self.inspect([user_entry(), assistant_entry(), tool_result_entry()])[0], 'Needs Input')

    def test_pending_tool_use_needs_input(self):
        self.assertEqual(self.inspect([user_entry(), tool_use_entry()])[0], 'Needs Input')

    def test_empty_transcript_is_idle(self):
        self.assertEqual(self.inspect([])[0], 'Idle')

    def test_conversation_text_about_limits_is_not_a_limit(self):
        # Only entry structure is read. Ordinary prose mentioning rate limits must
        # not invent a limit that never happened.
        text = 'We hit 429 rate limit / Rate limited earlier, and the usage limit reset.'
        self.assertEqual(self.inspect([user_entry(text), assistant_entry([{'type': 'text', 'text': text}])])[0], 'Completed')

    def test_never_reads_credentials(self):
        source = pathlib.Path(c.__file__).read_text()
        for forbidden in ("'auth.json'", '"auth.json"', "'settings.json'", 'api_key', 'apikey'):
            self.assertNotIn(forbidden, source)
        self.assertIn('projects', source)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.addCleanup(patch.stopall)
        patch.object(c, 'ROOT', self.root / 'data').start()
        patch.object(c, 'COMMANDCODE_HOME', self.root).start()
        self.dir = make_session(self.root, 'slug', 'sid1', [user_entry('Hello world'), assistant_entry()],
                                cwd=str(self.root), meta={'entrypoint': 'interactive', 'title': 'Saved Title'})

    def test_discovery_uses_saved_title(self):
        s = c.command({'op': 'snapshot'})
        self.assertEqual(len(s['tasks']), 1)
        self.assertEqual(s['tasks'][0]['title'], 'Saved Title')
        self.assertEqual(s['tasks'][0]['state'], 'Completed')

    def test_discovery_falls_back_to_first_user_text(self):
        (self.dir / 'sid1.meta.json').unlink()
        (self.dir / 'sid3.jsonl').write_text('\n'.join(json.dumps(e) for e in
                                                       [header('sid3', '/tmp'), user_entry('No title here'), assistant_entry()]))
        titles = {t['id']: t['title'] for t in c.command({'op': 'snapshot'})['tasks']}
        self.assertEqual(titles['sid3'], 'No title here')
        self.assertEqual(titles['sid1'], 'Hello world')

    def test_sidecars_are_not_discovered_as_sessions(self):
        (self.dir / 'sid1.checkpoints.jsonl').write_text('{}')
        (self.dir / 'sid1.prompts.jsonl').write_text('{}')
        self.assertEqual([t['id'] for t in c.command({'op': 'snapshot'})['tasks']], ['sid1'])

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
        # detached worker spawner: [python, backend/commandcode.py, 'worker', id, token], never a shell
        args = p.call_args[0][0]
        self.assertEqual(args[2:4], ['worker', 'sid1'])

    def test_resume_requires_executable_cli(self):
        c.command({'op': 'snapshot'})
        c.command({'op': 'settings', 'cli': '/usr/bin/true', 'prompt': 'Continue'})
        t = c.command({'op': 'resume', 'id': 'sid1'})['tasks'][0]
        self.assertEqual(t['state'], 'Running')

    def test_stale_state_is_reclassified(self):
        # The fingerprint covers the derived state, so a rule change re-applies
        # even when the transcript has not moved.
        c.command({'op': 'snapshot'})
        with c.transaction() as s:
            s['tasks']['sid1'].update(state='Needs Input', fingerprint=list(s['tasks']['sid1']['source']))
        self.assertEqual(c.command({'op': 'snapshot'})['tasks'][0]['state'], 'Completed')


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.addCleanup(patch.stopall)
        patch.object(c, 'ROOT', self.root / 'data').start()
        patch.object(c, 'COMMANDCODE_HOME', self.root).start()
        make_session(self.root, 'slug', 'sid1', [user_entry('Hello'), assistant_entry()], cwd=str(self.root))
        c.command({'op': 'snapshot'})
        c.command({'op': 'settings', 'cli': '/usr/bin/true', 'prompt': 'Continue'})

    def run_worker(self, returncode, log=''):
        with c.transaction() as s:
            s['tasks']['sid1'].update(state='Running', run={'token': 'tok', 'started': time.time()})

        class FakeProc:
            def __init__(self): self.returncode = returncode
            def communicate(self, timeout=None): return (b'', b'')

        def factory(*args, **kwargs):
            out = kwargs.get('stdout')
            if out is not None and log:
                out.write(log.encode())
            return FakeProc()
        with patch.object(c.subprocess, 'Popen', side_effect=factory):
            c.worker('sid1', 'tok')
        with c.transaction() as s:
            return s['tasks']['sid1']

    def test_exit_5_is_rate_limited_without_a_reset(self):
        t = self.run_worker(5, 'Rate limit exceeded')
        self.assertEqual(t['state'], 'Rate limited')
        self.assertIsNone(t['reset'])
        self.assertIn('set it manually', t['note'])

    def test_exit_5_with_reset_waits(self):
        t = self.run_worker(5, 'Rate limit exceeded, retry after 3600 seconds')
        self.assertEqual(t['state'], 'Waiting')
        self.assertGreater(t['reset'], time.time())

    def test_exit_10_reports_missing_credits_not_a_rate_limit(self):
        t = self.run_worker(10, 'Insufficient credits remaining')
        self.assertEqual(t['state'], 'Needs Input')
        self.assertIn('credits', t['note'])

    def test_exit_0_reinspects_the_transcript(self):
        t = self.run_worker(0)
        self.assertEqual(t['state'], 'Completed')

    def test_other_failure_needs_input(self):
        t = self.run_worker(1, 'boom')
        self.assertEqual(t['state'], 'Needs Input')
        self.assertIn('boom', t['note'])


if __name__ == '__main__':
    unittest.main()
