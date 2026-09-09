import unittest
import test_watcher
w = test_watcher.w

class DisplayMetadataTests(unittest.TestCase):
    def row(self, **changes):
        return dict(dict(id='chat',title='## Files mentioned\nPrompt',name=None,cwd='/repo/app',project_id=None),**changes)
    def test_saved_chat_name_over_prompt(self):
        self.assertEqual(w.display_metadata(self.row(name='My Chat'),{}, {})['title'],'My Chat')
    def test_empty_title_fallback(self):
        self.assertEqual(w.display_metadata(self.row(title=''),{}, {})['title'],'Untitled task')
    def test_database_project_priority(self):
        d={'projectless-thread-ids':['chat']}
        self.assertEqual(w.display_metadata(self.row(project_id='p'),d,{'p':'Database Project'})['project_name'],'Database Project')
    def test_explicit_assignment_handles_worktree(self):
        d={'local-projects':{'p':{'name':'My Project','rootPaths':['/repo/app']}},'thread-project-assignments':{'chat':{'projectId':'p'}}}
        self.assertEqual(w.display_metadata(self.row(cwd='/worktree/branch'),d,{})['project_name'],'My Project')
    def test_projectless_never_inherits_folder(self):
        d={'projectless-thread-ids':['chat'],'local-projects':{'p':{'name':'My Project','rootPaths':['/repo/app']}}}
        self.assertIsNone(w.display_metadata(self.row(),d,{})['project_name'])
    def test_exact_legacy_project_root(self):
        d={'local-projects':{'p':{'name':'My Project','rootPaths':['/repo/app']}}}
        self.assertEqual(w.display_metadata(self.row(),d,{})['project_name'],'My Project')
        self.assertIsNone(w.display_metadata(self.row(cwd='/repo/app/other'),d,{})['project_name'])
    def test_missing_project_is_not_guessed(self):
        self.assertIsNone(w.display_metadata(self.row(project_id='missing'),{}, {})['project_name'])

class MetadataStorageTests(unittest.TestCase):
    setUp = test_watcher.RegistryTests.setUp
    def test_saved_name_updates_without_rollout_change(self):
        import sqlite3
        w.command({'op':'snapshot'})
        with sqlite3.connect(self.root/'state_5.sqlite') as c:
            c.execute('alter table threads add column name text')
            c.execute("update threads set name='Renamed Chat'")
        self.assertEqual(w.command({'op':'snapshot'})['tasks'][0]['title'],'Renamed Chat')
    def test_corrupt_optional_desktop_file_falls_back(self):
        (self.root/'.codex-global-state.json').write_text('{')
        self.assertEqual(w.command({'op':'snapshot'})['tasks'][0]['title'],'Example')
