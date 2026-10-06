import importlib.util, json, os, pathlib, sqlite3, tempfile, time, unittest
from unittest.mock import patch
spec = importlib.util.spec_from_file_location('cursor_backend', pathlib.Path(__file__).parents[1] / 'backend/cursor.py')
u = importlib.util.module_from_spec(spec); spec.loader.exec_module(u)

OLD = time.time() - 1000


def backdate(path):
    os.utime(path, (OLD, OLD))


def user_line(text='Fix the login bug'):
    return {'role': 'user', 'message': {'content': [{'type': 'text', 'text': '<user_query>\n\n' + text + '\n</user_query>'}]}}


def assistant_line(text='Done'):
    return {'role': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}}


def make_transcript(root, slug, sid, lines, cwd=None):
    d = root / 'projects' / slug / 'agent-transcripts' / sid
    d.mkdir(parents=True, exist_ok=True)
    if cwd is None:
        cwd = root / 'work'
        cwd.mkdir(exist_ok=True)
    path = d / (sid + '.jsonl')
    path.write_text('\n'.join(json.dumps(e) for e in lines))
    backdate(path)
    return path


def slug_for(path):
    """Workspace slug for a real directory (absolute path, '/' -> '-')."""
    return '-'.join(str(path).lstrip('/').split('/'))


def make_chat(root, ws, sid, meta, blobs):
    d = root / 'chats' / ws / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / 'meta.json').write_text(json.dumps(meta))
    backdate(d / 'meta.json')
    db = d / 'store.db'
    c = sqlite3.connect(db)
    c.execute('create table blobs (id text primary key, data blob)')
    c.execute('create table meta (key text primary key, value text)')
    for i, blob in enumerate(blobs):
        data = json.dumps(blob).encode()
        c.execute('insert into blobs values (?, ?)', ('b%d' % i, data))
    c.commit(); c.close()
    backdate(db)
    return d


def chat_meta(title='Saved Chat', cwd='/tmp', updated_ms=None):
    return {'schemaVersion': 1, 'createdAtMs': 1791208000000, 'hasConversation': True,
            'title': title, 'updatedAtMs': updated_ms or int(OLD * 1000), 'cwd': cwd}


class ParserTests(unittest.TestCase):
    def inspect_transcript(self, lines):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            path = make_transcript(root, 'slug', 'sid1', lines)
            with patch.object(u, 'CURSOR_HOME', root):
                return u.inspect_session({'kind': 'transcript', 'path': str(path)})

    def inspect_chat(self, meta, blobs):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            d = make_chat(root, 'ws', 'sid1', meta, blobs)
            with patch.object(u, 'CURSOR_HOME', root):
                return u.inspect_session({'kind': 'chat', 'path': str(d)})

    def test_clean_assistant_completes(self):
        self.assertEqual(self.inspect_transcript([user_line(), assistant_line()])[0], 'Completed')

    def test_user_without_reply_needs_input(self):
        self.assertEqual(self.inspect_transcript([assistant_line(), user_line('And also this')])[0], 'Needs Input')

    def test_empty_transcript_is_idle(self):
        self.assertEqual(self.inspect_transcript([])[0], 'Idle')

    def test_chat_assistant_last_completes(self):
        meta = chat_meta()
        blobs = [{'role': 'user', 'content': 'hi'}, {'role': 'assistant', 'content': 'hello'}]
        self.assertEqual(self.inspect_chat(meta, blobs)[0], 'Completed')

    def test_chat_user_last_needs_input(self):
        meta = chat_meta()
        blobs = [{'role': 'assistant', 'content': 'hello'}, {'role': 'user', 'content': 'more'}]
        self.assertEqual(self.inspect_chat(meta, blobs)[0], 'Needs Input')

    def test_assistant_limit_with_reset_waits(self):
        meta = chat_meta()
        blobs = [{'role': 'user', 'content': 'go'},
                 {'role': 'assistant', 'content': 'You have reached your limit. Resets at 2030-01-01T00:00:00Z'}]
        status, reset, _ = self.inspect_chat(meta, blobs)
        self.assertEqual(status, 'Waiting')
        self.assertGreater(reset, time.time())

    def test_assistant_limit_without_reset_is_rate_limited(self):
        meta = chat_meta()
        blobs = [{'role': 'user', 'content': 'go'},
                 {'role': 'assistant', 'content': 'Rate limit exceeded, try again later.'}]
        status, reset, note = self.inspect_chat(meta, blobs)
        self.assertEqual(status, 'Rate limited')
        self.assertIsNone(reset)
        self.assertIn('set it manually', note)

    def test_user_prose_about_limits_is_not_a_limit(self):
        # Only assistant-role text is matched: user prompts and tool output
        # often discuss rate limits without one ever happening.
        text = 'We hit 429 rate limit earlier, usage limit discussion.'
        meta = chat_meta()
        blobs = [{'role': 'user', 'content': text}, {'role': 'assistant', 'content': 'Done, noted.'}]
        self.assertEqual(self.inspect_chat(meta, blobs)[0], 'Completed')

    def test_hot_store_counts_as_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            path = make_transcript(root, 'slug', 'sid1', [user_line(), assistant_line()])
            os.utime(path, None)  # fresh mtime: Cursor is writing now
            with patch.object(u, 'CURSOR_HOME', root):
                self.assertEqual(u.inspect_session({'kind': 'transcript', 'path': str(path)})[0], 'Running')

    def test_slug_decode_accepts_only_real_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = pathlib.Path(tmp) / 'proj'
            real.mkdir()
            # A slug is the absolute path with '/' replaced by '-'.
            slug = '-'.join(str(real).lstrip('/').split('/'))
            self.assertEqual(u.decode_slug(slug), str(real))
            self.assertEqual(u.decode_slug('Users-no-such-dir-xyz'), '')

    def test_never_reads_credentials(self):
        source = pathlib.Path(u.__file__).read_text()
        for forbidden in ("'auth.json'", '"auth.json"', 'statsig', 'api_key', 'apikey'):
            self.assertNotIn(forbidden, source)
        # The module documents the stores it avoids alongside the ones it reads.
        self.assertIn('Never touches', source)
        self.assertIn('chats', source)
        self.assertIn('agent-transcripts', source)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.addCleanup(patch.stopall)
        patch.object(u, 'ROOT', self.root / 'data').start()
        patch.object(u, 'CURSOR_HOME', self.root).start()
        self.work = self.root / 'work'; self.work.mkdir()
        make_chat(self.root, 'ws', 'chat1', chat_meta(title='Saved Chat', cwd=str(self.work)),
                  [{'role': 'user', 'content': 'hi'}, {'role': 'assistant', 'content': 'hello'}])
        make_transcript(self.root, slug_for(self.work), 'tr1', [user_line('Hello world'), assistant_line()])

    def test_discovery_finds_chats_and_transcripts(self):
        tasks = {t['id']: t for t in u.command({'op': 'snapshot'})['tasks']}
        self.assertEqual(set(tasks), {'chat1', 'tr1'})
        self.assertEqual(tasks['chat1']['title'], 'Saved Chat')
        self.assertEqual(tasks['tr1']['title'], 'Hello world')
        self.assertEqual(tasks['chat1']['state'], 'Completed')
        self.assertEqual(tasks['tr1']['state'], 'Completed')

    def test_discovery_does_not_arm_or_spawn(self):
        with patch.object(u.subprocess, 'Popen') as p:
            s = u.command({'op': 'tick'})
            self.assertFalse(any(t['armed'] for t in s['tasks'])); p.assert_not_called()

    def test_manual_schedule_and_due_dispatch_once(self):
        u.command({'op': 'snapshot'})
        u.command({'op': 'settings', 'cli': '/usr/bin/true', 'prompt': 'Continue'})
        u.command({'op': 'schedule', 'id': 'tr1', 'reset': time.time() + 60})
        with u.transaction() as s:
            s['tasks']['tr1']['reset'] = time.time() - 30
        with patch.object(u.subprocess, 'Popen') as p:
            u.command({'op': 'tick'}); u.command({'op': 'tick'})
            self.assertEqual(p.call_count, 1)
        # detached worker spawner: [python, backend/cursor.py, 'worker', id, token], never a shell
        args = p.call_args[0][0]
        self.assertEqual(args[2:4], ['worker', 'tr1'])

    def test_dead_cli_path_falls_back(self):
        u.command({'op': 'snapshot'})
        u.command({'op': 'settings', 'cli': '/usr/bin/true', 'prompt': 'Continue'})
        with u.transaction() as s:
            s['cli'] = '/nonexistent/cursor-agent'
        with patch.object(u.shutil, 'which', return_value='/usr/bin/true'):
            with patch.object(u, 'CLI_CANDIDATES', ()):
                t = u.command({'op': 'resume', 'id': 'tr1'})['tasks']
                resumed = [x for x in t if x['id'] == 'tr1'][0]
        self.assertEqual(resumed['state'], 'Running')

    def test_missing_cli_everywhere_still_reports_error(self):
        u.command({'op': 'snapshot'})
        with u.transaction() as s:
            s['cli'] = '/nonexistent/cursor-agent'
        with patch.object(u.shutil, 'which', return_value=None):
            with patch.object(u, 'CLI_CANDIDATES', ()):
                with self.assertRaises(ValueError) as ctx:
                    u.command({'op': 'resume', 'id': 'tr1'})
        self.assertIn('Cursor executable was not found', str(ctx.exception))


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.addCleanup(patch.stopall)
        patch.object(u, 'ROOT', self.root / 'data').start()
        patch.object(u, 'CURSOR_HOME', self.root).start()
        self.work = self.root / 'work'; self.work.mkdir()
        make_transcript(self.root, slug_for(self.work), 'sid1', [user_line('Hello'), assistant_line()])
        u.command({'op': 'snapshot'})
        u.command({'op': 'settings', 'cli': '/usr/bin/true', 'prompt': 'Continue'})

    def run_worker(self, returncode, log=''):
        with u.transaction() as s:
            s['tasks']['sid1'].update(state='Running', run={'token': 'tok', 'started': time.time()})

        class FakeProc:
            def __init__(self): self.returncode = returncode
            def communicate(self, timeout=None): return (b'', b'')

        def factory(*args, **kwargs):
            out = kwargs.get('stdout')
            if out is not None and log:
                out.write(log.encode())
            return FakeProc()
        with patch.object(u.subprocess, 'Popen', side_effect=factory) as p:
            u.worker('sid1', 'tok')
            args = p.call_args[0][0]
        with u.transaction() as s:
            return s['tasks']['sid1'], args

    def test_resume_uses_print_and_resume_flags(self):
        _, args = self.run_worker(0)
        self.assertEqual(args[1:4], ['-p', '--resume', 'sid1'])

    def test_limit_log_without_reset_is_rate_limited(self):
        t, _ = self.run_worker(1, 'You have reached your limit. Try again later.')
        self.assertEqual(t['state'], 'Rate limited')
        self.assertIsNone(t['reset'])

    def test_limit_log_with_reset_waits(self):
        t, _ = self.run_worker(1, 'Rate limit exceeded, retry after 3600 seconds')
        self.assertEqual(t['state'], 'Waiting')
        self.assertGreater(t['reset'], time.time())

    def test_exit_0_reinspects_the_session(self):
        t, _ = self.run_worker(0)
        self.assertEqual(t['state'], 'Completed')

    def test_other_failure_needs_input(self):
        t, _ = self.run_worker(1, 'boom')
        self.assertEqual(t['state'], 'Needs Input')
        self.assertIn('boom', t['note'])


if __name__ == '__main__':
    unittest.main()
