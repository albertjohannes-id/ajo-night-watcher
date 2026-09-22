import importlib.util, json, os, pathlib, sqlite3, tempfile, time, unittest
from unittest.mock import patch
spec = importlib.util.spec_from_file_location('opencode_backend', pathlib.Path(__file__).parents[1] / 'backend/opencode.py')
o = importlib.util.module_from_spec(spec); spec.loader.exec_module(o)


def make_db(path, sessions):
    c = sqlite3.connect(path)
    c.execute('create table project (id text, name text)')
    c.execute('create table session (id text, project_id text, directory text, title text, model text, agent text, time_created integer, time_updated integer, time_archived integer)')
    c.execute('create table message (id text, session_id text, time_created integer, time_updated integer, data text)')
    for s in sessions:
        c.execute('insert into session values (?,?,?,?,?,?,?,?,?)',
                  (s['id'], s.get('project_id'), s.get('directory', '/tmp'), s.get('title', 'T'),
                   json.dumps(s.get('model', {'id': 'm', 'providerID': 'opencode'})),
                   s.get('agent', 'build'), s.get('created', 1700000000000), s.get('updated', 1700000000000), None))
        for i, m in enumerate(s.get('messages', [])):
            c.execute('insert into message values (?,?,?,?,?)',
                      ('msg%d' % i, s['id'], 1700000000000 + i, 1700000000000 + i, json.dumps(m)))
    c.commit(); c.close()


def assistant(error=None):
    m = {'role': 'assistant', 'agent': 'build', 'model': {'providerID': 'opencode', 'modelID': 'm'},
         'time': {'created': 1700000000000, 'completed': 1700000001000}}
    if error is not None:
        m['error'] = error
    return m


class ParserTests(unittest.TestCase):
    def test_rate_limit_429_needs_manual_without_reset(self):
        err = {'name': 'APIError', 'data': {'message': 'Too many requests: rate limit exceeded', 'statusCode': 429}}
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            make_db(root / 'opencode.db', [{'id': 'ses_1', 'messages': [assistant(err)]}])
            with patch.object(o, 'OPENCODE_HOME', root):
                self.assertEqual(o.inspect_session('ses_1')[0], 'Needs Input')

    def test_rate_limit_with_iso_reset_waits(self):
        err = {'name': 'APIError', 'data': {'message': '429 rate limit, retry after 2030-01-01T00:00:00Z', 'statusCode': 429}}
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            make_db(root / 'opencode.db', [{'id': 'ses_1', 'messages': [assistant(err)]}])
            with patch.object(o, 'OPENCODE_HOME', root):
                status, reset, _ = o.inspect_session('ses_1')
                self.assertEqual(status, 'Waiting')
                self.assertEqual(reset, 1893456000)

    def test_non_rate_error_needs_input(self):
        err = {'name': 'APIError', 'data': {'message': "model 'x' has reached end of life", 'statusCode': 410}}
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            make_db(root / 'opencode.db', [{'id': 'ses_1', 'messages': [assistant(err)]}])
            with patch.object(o, 'OPENCODE_HOME', root):
                self.assertEqual(o.inspect_session('ses_1')[0], 'Needs Input')

    def test_clean_assistant_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            make_db(root / 'opencode.db', [{'id': 'ses_1', 'messages': [assistant()]}])
            with patch.object(o, 'OPENCODE_HOME', root):
                self.assertEqual(o.inspect_session('ses_1')[0], 'Completed')

    def test_user_without_reply_needs_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            make_db(root / 'opencode.db', [{'id': 'ses_1', 'messages': [{'role': 'user'}]}])
            with patch.object(o, 'OPENCODE_HOME', root):
                self.assertEqual(o.inspect_session('ses_1')[0], 'Needs Input')

    def test_never_reads_credentials(self):
        source = pathlib.Path(o.__file__).read_text()
        # No code path may open credential stores; docstring mentions are fine.
        for forbidden in ('/ \'auth.json\'', '/ "auth.json"', "/ 'account.json'", "'credential'\"",
                          'from credential', 'FROM credential'):
            self.assertNotIn(forbidden, source)
        self.assertIn('?mode=ro', source)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.addCleanup(patch.stopall)
        patch.object(o, 'ROOT', self.root / 'data').start()
        patch.object(o, 'OPENCODE_HOME', self.root).start()
        make_db(self.root / 'opencode.db', [{'id': 'ses_1', 'directory': str(self.root),
                                             'model': {'id': 'muse-spark', 'providerID': 'opencode'},
                                             'messages': [assistant()]}])

    def test_discovery_lists_all_models(self):
        s = o.command({'op': 'snapshot'})
        self.assertEqual(len(s['tasks']), 1)
        self.assertEqual(s['tasks'][0]['provider'], 'opencode')

    def test_discovery_does_not_arm_or_spawn(self):
        with patch.object(o.subprocess, 'Popen') as p:
            s = o.command({'op': 'tick'})
            self.assertFalse(s['tasks'][0]['armed']); p.assert_not_called()

    def test_manual_schedule_and_due_dispatch_once(self):
        o.command({'op': 'snapshot'})
        o.command({'op': 'settings', 'cli': '/usr/bin/true', 'prompt': 'Continue'})
        o.command({'op': 'schedule', 'id': 'ses_1', 'reset': time.time() + 60})
        with o.transaction() as s:
            s['tasks']['ses_1']['reset'] = time.time() - 30
        with patch.object(o.subprocess, 'Popen') as p:
            o.command({'op': 'tick'}); o.command({'op': 'tick'})
            self.assertEqual(p.call_count, 1)
        # detached worker spawner: [python, backend/opencode.py, 'worker', id, token], never a shell
        args = p.call_args[0][0]
        self.assertEqual(args[2:4], ['worker', 'ses_1'])

    def test_resume_requires_executable_cli(self):
        o.command({'op': 'snapshot'})
        o.command({'op': 'settings', 'cli': '/usr/bin/true', 'prompt': 'Continue'})
        t = o.command({'op': 'resume', 'id': 'ses_1'})['tasks'][0]
        self.assertEqual(t['state'], 'Running')


if __name__ == '__main__':
    unittest.main()
