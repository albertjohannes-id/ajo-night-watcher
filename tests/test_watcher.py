import importlib.util, json, os, pathlib, sqlite3, tempfile, time, unittest
from unittest.mock import patch
spec = importlib.util.spec_from_file_location('watcher', pathlib.Path(__file__).parents[1] / 'backend/watcher.py')
w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)

def events(*items): return '\n'.join(json.dumps(i) for i in items)
def event(t, **kw): return {'type': 'event_msg', 'payload': dict(type=t, **kw)}

class ParserTests(unittest.TestCase):
    def test_actual_quota_requires_error(self):
        q = event('token_count', rate_limits={'primary': {'used_percent': 100, 'resets_at': 1900000000}, 'secondary': {'used_percent': 30, 'resets_at': 2000000000}})
        self.assertEqual(w.inspect_log(events(q))[0], 'Idle')
        s,r,_ = w.inspect_log(events(q,event('error',message='You have reached your usage limit')))
        self.assertEqual((s,r),('Waiting',1900000000))
    def test_both_exhausted_uses_later_reset(self):
        self.assertEqual(w.quota_reset({'primary':{'used_percent':100,'resets_at':1900000000},'secondary':{'used_percent':100,'resets_at':2000000000}}),2000000000)
    def test_error_without_reset_needs_input(self):
        self.assertEqual(w.inspect_log(events({'type':'turn.failed','error':{'message':'usage limit reached'}}))[0], 'Needs Input')
    def test_no_parsing_user_text_or_tool_output(self):
        text = events({'type':'response_item','payload':{'type':'message','content':'usage limit resets_at 1900000000'}},{'type':'item.completed','item':{'type':'command_execution','aggregated_output':'usage limit'}})
        self.assertEqual(w.inspect_log(text)[0], 'Idle')
    def test_new_turn_clears_old_rate_limit(self):
        text = events(event('error',message='usage limit',resets_at=1900000000),event('task_started'),event('task_complete'))
        self.assertEqual(w.inspect_log(text)[0:2], ('Completed',None))
    def test_real_exec_terminal_events(self):
        self.assertEqual(w.inspect_log(events({'type':'turn.started'},{'type':'turn.completed'}))[0],'Completed')
        self.assertEqual(w.inspect_log(events({'type':'error','message':'Authentication failed'}))[0],'Needs Input')
    def test_partial_json_ignored(self):
        self.assertEqual(w.inspect_log('{"type":')[0],'Idle')
    def test_iso_timestamp(self):
        self.assertEqual(w.inspect_log(events(event('error',message='usage limit until 2030-01-01T00:00:00Z')))[1],1893456000)

class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.addCleanup(patch.stopall)
        patch.object(w,'ROOT',self.root/'data').start()
        patch.object(w,'CODEX_HOME',self.root).start()
        self.log = self.root/'rollout.jsonl'; self.log.write_text(events(event('task_complete')))
        c=sqlite3.connect(self.root/'state_5.sqlite')
        c.execute('create table threads (id,title,cwd,rollout_path,updated_at,archived,agent_path)')
        c.execute('insert into threads values (?,?,?,?,?,0,null)',('session','Example',str(self.root),str(self.log),time.time())); c.commit();c.close()
    def test_discovery_does_not_arm_or_spawn(self):
        with patch.object(w.subprocess,'Popen') as p:
            s=w.command({'op':'tick'})
            self.assertFalse(s['tasks'][0]['armed']); p.assert_not_called()
    def test_manual_schedule_persists_and_disarm_prevents_launch(self):
        w.command({'op':'snapshot'})
        w.command({'op':'schedule','id':'session','reset':time.time()+60})
        with w.transaction() as s:s['tasks']['session']['reset']=time.time()-30
        w.command({'op':'arm','id':'session','armed':False})
        with patch.object(w.subprocess,'Popen') as p:
            s=w.command({'op':'tick'});p.assert_not_called()
        self.assertEqual(s['tasks'][0]['state'],'Waiting')
    def test_due_job_dispatches_once_across_ticks(self):
        w.command({'op':'snapshot'});w.command({'op':'schedule','id':'session','reset':time.time()+60})
        with w.transaction() as s:s['tasks']['session']['reset']=time.time()-30
        with patch.object(w.subprocess,'Popen') as p:
            w.command({'op':'tick'});w.command({'op':'tick'});self.assertEqual(p.call_count,1)
    def test_running_task_cannot_resume_or_schedule(self):
        self.log.write_text(events(event('task_started')))
        for op in ('resume','schedule'):
            with self.assertRaises(ValueError):w.command({'op':op,'id':'session','reset':time.time()+60})
    def test_restart_recovers_interrupted_worker(self):
        w.command({'op':'snapshot'})
        with w.transaction() as s:s['tasks']['session'].update(state='Running',run={'started':time.time()-60,'token':'x'})
        self.assertEqual(w.command({'op':'snapshot'})['tasks'][0]['state'],'Needs Input')
    def test_new_external_turn_cancels_schedule(self):
        w.command({'op':'snapshot'});w.command({'op':'schedule','id':'session','reset':time.time()+60})
        self.log.write_text(events(event('task_started')))
        t=w.command({'op':'tick'})['tasks'][0]
        self.assertEqual(t['state'],'Running');self.assertIsNone(t['reset'])
    def test_corrupt_state_fails_closed(self):
        w.prepare();(w.ROOT/'registry.json').write_text('bad')
        with self.assertRaises(ValueError):w.command({'op':'tick'})
    def test_prompt_shell_characters_are_data(self):
        w.command({'op':'snapshot'})
        prompt='Do `nothing`; $(touch NEVER)'
        s=w.command({'op':'settings','cli':'/usr/bin/true','prompt':prompt})
        self.assertEqual(s['prompt'],prompt)

if __name__=='__main__':unittest.main()
