import json, pathlib, sys, tempfile, unittest
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / 'backend'))
import codex_usage as u

class UsageTests(unittest.TestCase):
    def test_remaining_not_used_and_clamped(self):
        v=u.normalize({'rateLimits':{'primary':{'usedPercent':80,'windowDurationMins':300,'resetsAt':1900000000},'secondary':{'usedPercent':101}}})
        self.assertEqual([w['remaining'] for w in v[0]['windows']], [20,0])
        self.assertEqual(v[0]['windows'][0]['minutes'],300)
    def test_missing_is_unavailable(self):
        v=u.normalize({'rateLimits':{'primary':{'usedPercent':None}}})
        self.assertIsNone(v[0]['windows'][0]['remaining'])
        self.assertEqual(u.normalize({'rateLimits':None}),[])
    def test_bucket_map_takes_precedence(self):
        v=u.normalize({'rateLimits':{'primary':{'usedPercent':99}},'rateLimitsByLimitId':{'codex':{'primary':{'usedPercent':12}},'other':{'limitName':'Other model','secondary':{'usedPercent':50}}}})
        self.assertEqual(len(v),2);self.assertEqual(v[0]['windows'][0]['remaining'],88)
    def test_non_finite_is_unavailable(self):
        v=u.normalize({'rateLimits':{'primary':{'usedPercent':float('nan'),'resetsAt':float('inf')}}})
        self.assertIsNone(v[0]['windows'][0]['remaining']);self.assertIsNone(v[0]['windows'][0]['reset'])
    def test_failure_preserves_labeled_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=pathlib.Path(tmp)/'usage.json'
            with patch.object(u,'read_limits',return_value={'rateLimits':{'primary':{'usedPercent':30}}}): first=u.snapshot('/cli',p)
            with patch.object(u,'read_limits',side_effect=TimeoutError('Timed out')):
                second=u.snapshot('/cli',p)
                other=u.snapshot('/different-cli',p)
            self.assertTrue(second['stale']);self.assertEqual(second['updated'],first['updated'])
            self.assertEqual(other['buckets'],[])
    def test_rpc_read_only_and_batched_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=pathlib.Path(tmp)/'cli'; log=pathlib.Path(tmp)/'requests'
            p.write_text('''#!/usr/bin/python3
import sys,json
for line in sys.stdin:
 r=json.loads(line)
 with open(%r,'a') as f:f.write(r['method']+'\\n')
 if r.get('id')==1:
  print(json.dumps({'method':'notification'})+'\\n'+json.dumps({'id':1,'result':{}}),flush=True)
 if r.get('id')==2:
  print(json.dumps({'id':2,'result':{'rateLimits':{'primary':{'usedPercent':25}}}}),flush=True)
''' % str(log));p.chmod(0o700)
            result=u.read_limits(str(p),timeout=3)
            self.assertEqual(u.normalize(result)[0]['windows'][0]['remaining'],75)
            self.assertEqual(log.read_text().splitlines(),['initialize','initialized','account/rateLimits/read'])
    def test_timeout_cleans_up_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=pathlib.Path(tmp)/'cli';p.write_text('#!/usr/bin/python3\nimport time\ntime.sleep(20)\n');p.chmod(0o700)
            with self.assertRaises(TimeoutError):u.read_limits(str(p),timeout=0.1)
