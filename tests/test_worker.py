"""Exercise a real detached worker with a fake CLI; no account usage."""
import json, os, pathlib, subprocess, tempfile, time, unittest
import test_watcher
w = test_watcher.w

class WorkerTests(unittest.TestCase):
    setUp = test_watcher.RegistryTests.setUp
    def run_worker(self, output, code=0):
        w.command({'op':'snapshot'})
        fake=self.root/'fake-codex'
        fake.write_text('#!/usr/bin/python3\nimport json,sys\nfrom pathlib import Path\nPath('+repr(str(self.root/'input.json'))+').write_text(json.dumps([sys.argv,sys.stdin.read()]))\nprint('+repr(output)+')\nsys.exit('+str(code)+')\n')
        fake.chmod(0o700)
        w.command({'op':'settings','cli':str(fake),'prompt':'Continue $(no shell); `no shell`'})
        # Directly seed a claimed job, then use the same entrypoint as launch().
        with w.transaction() as s:
            s['tasks']['session'].update(state='Running',run={'token':'test','started':time.time()})
        env=dict(os.environ,NIGHT_WATCHER_DATA_HOME=str(w.ROOT),CODEX_HOME=str(w.CODEX_HOME))
        subprocess.run(['/usr/bin/python3',str(pathlib.Path(w.__file__).resolve()),'worker','session','test'],env=env,check=True,timeout=10)
        return w.command({'op':'snapshot'})['tasks'][0]
    def test_worker_success_and_exact_argv(self):
        t=self.run_worker('{"type":"turn.completed"}')
        self.assertEqual(t['state'],'Completed')
        args,prompt=json.loads((self.root/'input.json').read_text())
        self.assertEqual(args[-2:],['session','-'])
        self.assertEqual(prompt,'Continue $(no shell); `no shell`')
        self.assertIn('workspace-write',args)
    def test_worker_rate_limit_keeps_future_reset(self):
        reset=time.time()+500
        t=self.run_worker(json.dumps({'type':'turn.failed','error':{'message':'usage limit','resets_at':reset}}),1)
        self.assertEqual(t['state'],'Waiting');self.assertEqual(t['reset'],reset)
    def test_worker_expired_reset_does_not_loop(self):
        t=self.run_worker(json.dumps({'type':'turn.failed','error':{'message':'usage limit','resets_at':time.time()-100}}),1)
        self.assertEqual(t['state'],'Needs Input');self.assertIsNone(t['reset'])
    def test_worker_nonzero_exit_requires_attention(self):
        self.assertEqual(self.run_worker('connection failed',1)['state'],'Needs Input')
