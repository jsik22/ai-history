import datetime
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
COLLECTOR = REPO / 'bin/ai-history-claude'
BACKFILL = REPO / 'scripts/backfill_claude.py'
sys.path.insert(0, str(COLLECTOR.parent))
from ai_history_common import Archive, atomic, key


class ClaudeReview(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ai-history-claude-test-')
        self.root = Path(self.temp.name) / 'history'
        self.projects = Path(self.temp.name) / 'projects'
        self.env = dict(os.environ, AI_HISTORY_DIR=str(self.root), CLAUDE_CODE_ENTRYPOINT='cli',
                        AI_HISTORY_CLAUDE_PROJECTS=str(self.projects))

    def tearDown(self):
        self.temp.cleanup()

    def event(self, name, **fields):
        return dict(hook_event_name=name, session_id='session123', cwd='/tmp/project', **fields)

    def hook(self, name, **fields):
        result = subprocess.run([sys.executable, str(COLLECTOR)], env=self.env,
                                input=json.dumps(self.event(name, **fields)), text=True, capture_output=True)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        return result

    def text(self):
        return ''.join(p.read_text() for p in sorted(self.root.glob('*/*.md')))

    def fixture(self, prompt='  raw prompt\n\n', duplicate=False):
        path = self.projects / 'project' / 'session.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        user = dict(type='user', uuid='user1', sessionId='session123', entrypoint='cli',
                    timestamp='2026-09-01T01:02:03Z', cwd='/tmp/project', message={'content':prompt})
        answer = dict(type='assistant', uuid='answer1', message={'id':'message1', 'stop_reason':'end_turn', 'content':[{'type':'text','text':'final\n\n'}]})
        path.write_text(''.join(json.dumps(e)+'\n' for e in ([user, user, answer] if duplicate else [user, answer])))
        return path

    def backfill(self, apply=False):
        args = [sys.executable, str(BACKFILL), str(self.root), '2027-01-01 00:00:00']
        if apply:
            args.append('--apply')
        return subprocess.run(args, env=self.env, text=True, capture_output=True)

    def test_raw_preservation(self):
        self.hook('UserPromptSubmit', prompt='  prompt\n\n')
        self.hook('Stop', last_assistant_message='answer\n\n')
        self.assertIn('>   prompt\n> \n> \nanswer\n\n\n\n', self.text())

    def test_failed_stop_is_retried(self):
        self.hook('UserPromptSubmit', prompt='first')
        lock = self.root / '.lock'; lock.mkdir()
        self.hook('Stop', last_assistant_message='durable answer')
        self.assertTrue(list((self.root/'.pending/claude-events').glob('*.json')))
        lock.rmdir()
        self.hook('UserPromptSubmit', prompt='next')
        self.assertIn('durable answer', self.text())
        self.hook('Stop', last_assistant_message='next answer')
        self.assertEqual(self.text().count('===== 20'), 2)

    def test_concurrent_duplicate_stop(self):
        self.hook('UserPromptSubmit', prompt='first')
        lock=self.root/'.lock';lock.mkdir()
        processes=[]
        for _ in range(2):
            p=subprocess.Popen([sys.executable,str(COLLECTOR)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=self.env,text=True)
            p.stdin.write(json.dumps(self.event('Stop',last_assistant_message='answer')));p.stdin.close()
            processes.append(p)
        time.sleep(.2);lock.rmdir()
        for p in processes:
            self.assertEqual(p.wait(),0)
            p.stdout.close();p.stderr.close()
        self.assertEqual(self.text().count('===== 20'),1)

    def test_repeated_legitimate_prompt(self):
        for _ in range(2):
            self.hook('UserPromptSubmit',prompt='same')
            self.hook('Stop',last_assistant_message='same answer')
        self.assertEqual(self.text().count('===== 20'),2)

    def test_orphan_stop_dedup(self):
        for _ in range(2):self.hook('Stop',last_assistant_message='answer')
        self.assertEqual(self.text().count('===== 20'),1)
        self.assertIn('> (이어서)\n',self.text())

    def test_legacy_pending(self):
        pending=self.root/'.pending/claude-session123';pending.mkdir(parents=True)
        for name,text in [('ts','2026-09-01 23:59:00\n'),('cwd','/tmp/project\n'),('prompt','old prompt\n')]:
            (pending/name).write_text(text)
        self.hook('Stop',last_assistant_message='final')
        self.assertIn('> old prompt\nfinal',self.text())
        self.assertFalse(pending.exists())

    def test_filter_sdk(self):
        self.env['CLAUDE_CODE_ENTRYPOINT']='sdk-cli'
        self.hook('UserPromptSubmit',prompt='excluded')
        self.assertFalse(self.root.exists())

    def test_backfill_dry_run(self):
        self.fixture(); result=self.backfill()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse(self.root.exists())

    def test_backfill_preservation_dedup_uuid(self):
        self.fixture(duplicate=True)
        for _ in range(2):
            result=self.backfill(True);self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.text().count('===== 20'),1)
        self.assertIn('>   raw prompt\n> \n> \nfinal\n\n\n\n',self.text())

    def test_backfill_legacy_adoption(self):
        self.fixture()
        archive=Archive(self.root,'claude');archive.setup()
        ts=datetime.datetime.fromisoformat('2026-09-01T01:02:03+00:00').astimezone().strftime('%Y-%m-%d %H:%M:%S')
        row=dict(sid='session123',tid='old',ts=ts,cwd='/tmp/project',prompt='raw prompt',answer='final\n\n')
        path=self.root/ts[:7]/(ts[:10]+'.md');path.parent.mkdir();path.write_bytes(archive.block(row))
        before=path.read_bytes();result=self.backfill(True)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(path.read_bytes(),before)

    def test_signal_releases_lock(self):
        # Termination while waiting retains a queued event and does not remove another owner's lock.
        archive=Archive(self.root,'claude');archive.setup();lock=self.root/'.lock';lock.mkdir()
        p=subprocess.Popen([sys.executable,str(COLLECTOR)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=self.env,text=True)
        p.stdin.write(json.dumps(self.event('UserPromptSubmit',prompt='survive')));p.stdin.close()
        deadline=time.monotonic()+2
        while not list(archive.queue.glob('*.json')) and time.monotonic()<deadline:time.sleep(.01)
        p.terminate();self.assertEqual(p.wait(),0);p.stdout.close();p.stderr.close()
        self.assertTrue(lock.exists());self.assertTrue(list(archive.queue.glob('*.json')))

    def test_backfill_malformed_record_refuses_write(self):
        path=self.fixture()
        lines=path.read_text().splitlines(True)
        path.write_text(lines[0]+'{invalid}\n'+lines[1])
        result=self.backfill(True)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.text(),'')

    def test_backfill_distinct_identical_turns(self):
        path=self.fixture(prompt='same')
        entries=[json.loads(line) for line in path.read_text().splitlines()]
        second=json.loads(json.dumps(entries))
        second[0]['uuid']='user2';second[1]['uuid']='answer2'
        path.write_text(''.join(json.dumps(e)+'\n' for e in entries+second))
        for _ in range(2):
            result=self.backfill(True);self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.text().count('===== 20'),2)

    def test_transcript_uuid_connects_live_and_backfill(self):
        path=self.fixture(prompt='linked')
        entries=[json.loads(line) for line in path.read_text().splitlines()]
        entries[0]['timestamp']=datetime.datetime.now(datetime.timezone.utc).isoformat()
        path.write_text(''.join(json.dumps(e)+'\n' for e in entries))
        self.hook('UserPromptSubmit',prompt='linked',transcript_path=str(path))
        self.hook('Stop',last_assistant_message='final\n\n',transcript_path=str(path))
        result=self.backfill(True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.text().count('===== 20'),1)
        self.assertTrue((self.root/'.claude-state/done'/key('session123','user1')).exists())

    def test_post_commit_state_failure_replays_without_duplicate(self):
        loader=importlib.machinery.SourceFileLoader('claude_review_module',str(COLLECTOR))
        spec=importlib.util.spec_from_loader(loader.name,loader)
        module=importlib.util.module_from_spec(spec);loader.exec_module(module)
        archive=Archive(self.root,'claude');archive.setup()
        submit=dict(self.event('UserPromptSubmit',prompt='durable'),_id='submit-id',_ts='2026-09-01 01:02:03')
        stop=dict(self.event('Stop',last_assistant_message='final'),_id='stop-id',_ts='2026-09-01 01:02:04')
        with archive.locked():module.process(archive,submit)
        real_atomic=module.atomic
        def fail_state(path,value):
            if path.name.startswith('session-'):raise OSError('simulated metadata write failure')
            return real_atomic(path,value)
        with archive.locked(), mock.patch.object(module,'atomic',side_effect=fail_state):
            with self.assertRaises(OSError):module.process(archive,stop)
        with archive.locked():module.process(archive,stop)
        self.assertEqual(self.text().count('===== 20'),1)

    def test_missing_and_empty_answer(self):
        self.hook('UserPromptSubmit',prompt='first');self.hook('Stop')
        self.hook('UserPromptSubmit',prompt='second');self.hook('Stop',last_assistant_message='')
        self.assertIn('(답변 필드 없음 - 훅 형식 변경 확인 필요)',self.text())
        self.assertIn('(텍스트 답변 없음)',self.text())

    def test_backfill_active_tool_narration_deferred(self):
        path=self.fixture()
        entries=[json.loads(line) for line in path.read_text().splitlines()]
        entries[1]['message']['stop_reason']='tool_use'
        path.write_text(''.join(json.dumps(e)+'\n' for e in entries))
        result=self.backfill(True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.text(),'')
        self.assertFalse((self.root/'.claude-state/done'/key('session123','user1')).exists())

    def test_backfill_empty_terminal_not_commentary(self):
        path=self.fixture()
        entries=[json.loads(line) for line in path.read_text().splitlines()]
        entries[1]['message']['stop_reason']='tool_use'
        entries.append(dict(type='assistant',uuid='answer2',message={
            'id':'terminal','stop_reason':'end_turn','content':[]}))
        path.write_text(''.join(json.dumps(e)+'\n' for e in entries))
        result=self.backfill(True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('(텍스트 답변 없음)',self.text())
        self.assertNotIn('final',self.text())

    def test_backfill_terminal_chunks_selected(self):
        path=self.fixture()
        entries=[json.loads(line) for line in path.read_text().splitlines()]
        entries[1]['message']['stop_reason']='tool_use'
        entries[1]['message']['content'][0]['text']='commentary'
        entries.extend([
            dict(type='assistant',uuid='answer2',message={'id':'terminal','stop_reason':None,
                 'content':[{'type':'text','text':'part one'}]}),
            dict(type='assistant',uuid='answer3',message={'id':'terminal','stop_reason':'end_turn',
                 'content':[{'type':'text','text':'part two'}]})])
        path.write_text(''.join(json.dumps(e)+'\n' for e in entries))
        result=self.backfill(True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('part one\npart two',self.text())
        self.assertNotIn('commentary',self.text())

    def test_backfill_unanswered_prior_only_after_next_user(self):
        path=self.fixture()
        entries=[json.loads(line) for line in path.read_text().splitlines()]
        entries[1]['message']['stop_reason']='tool_use'
        second=json.loads(json.dumps(entries[0]));second['uuid']='user2'
        second['message']['content']='still running'
        entries.append(second)
        path.write_text(''.join(json.dumps(e)+'\n' for e in entries))
        result=self.backfill(True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.text().count('===== 20'),1)
        self.assertIn('(이 턴의 답변 기록 없음)',self.text())
        self.assertNotIn('still running',self.text())


if __name__ == '__main__':
    unittest.main()
