"""Synthetic Codex regressions; no live archive or model invocation."""
import contextlib
import io
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'bin/ai-history-codex'


class CodexReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='codex-review-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'history'
        self.sessions = Path(self.tmp.name) / 'sessions'
        self.sessions.mkdir()
        loader = importlib.machinery.SourceFileLoader('archive', str(SCRIPT))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.m = importlib.util.module_from_spec(spec)
        loader.exec_module(self.m)
        self.m.ROOT = self.root
        self.m.SESSIONS = self.sessions
        self.m.STATE = self.root / '.codex-state'
        self.m.PENDING = self.root / '.pending/codex'
        self.m.QUEUE = self.root / '.pending/codex-events'
        self.m.setup()
        self.meta = {'id': '12345678-session', 'originator': 'codex-tui', 'source': 'cli', 'cwd': str(Path.home())}
        self.path = self.sessions / 'rollout-12345678-session.jsonl'
        self.write_transcript([])

    def write_transcript(self, events):
        rows = [{'type': 'session_meta', 'payload': self.meta}] + events
        self.path.write_text(''.join(json.dumps(x) + '\n' for x in rows))

    def event(self, name, **extra):
        payload = dict(hook_event_name=name, session_id=self.meta['id'], turn_id='turn-1',
                       transcript_path=str(self.path), cwd=str(Path.home()), **extra)
        return {'observed_at': '2026-10-03T23:58:00+09:00', 'input': payload}

    def process(self, name, **extra):
        with self.m.locked():
            self.m.recover()
            self.m.process(self.event(name, **extra))

    def text(self):
        return ''.join(p.read_text() for p in self.root.glob('*/*.md'))

    def test_normal_and_duplicate(self):
        self.process('UserPromptSubmit', prompt='a\nb\n')
        self.process('Stop', last_assistant_message='answer\nraw')
        ts = self.m.local_timestamp('2026-10-03T23:58:00+09:00')
        expected = f'===== {ts} | codex | ~ | s:12345678 =====\n> a\n> b\n> \nanswer\nraw\n\n'
        self.assertEqual(self.text(), expected)
        self.process('Stop', last_assistant_message='duplicate')
        self.assertEqual(self.text(), expected)
        self.assertEqual(next(self.root.glob('*/*.md')).stat().st_mode & 0o777, 0o600)

    def test_interrupt_and_session_end(self):
        self.process('UserPromptSubmit', prompt='prompt')
        self.process('Interrupt')
        self.process('SessionEnd')
        self.assertEqual(self.text().count('(중단됨)'), 1)

    def test_unanswered(self):
        self.process('UserPromptSubmit', prompt='prompt')
        self.process('SessionEnd')
        self.assertIn(self.m.UNANSWERED, self.text())

    def test_missing_field(self):
        self.process('Stop')
        self.assertIn('> (이어서)', self.text())
        self.assertIn(self.m.MISSING, self.text())
        self.assertIn('last_assistant_message', (self.root / '.errors').read_text())

    def test_empty_field(self):
        self.process('Stop', last_assistant_message=None)
        self.assertIn(self.m.EMPTY, self.text())

    def test_exclusions(self):
        for origin, source in [('codex_exec', 'exec'), ('codex-tui', {'subagent': {'thread_spawn': {}}}), ('unknown', 'cli')]:
            self.meta.update(originator=origin, source=source)
            self.write_transcript([])
            self.process('UserPromptSubmit', prompt='excluded')
            self.process('Stop', last_assistant_message='excluded')
        self.assertEqual(self.text(), '')
        self.assertEqual(list(self.m.PENDING.iterdir()), [])

    def test_transcript_steering_and_retry(self):
        first = self.message('turn-1', 'first')
        second = self.message('turn-1', 'steer')
        second['payload']['item']['id'] = 'second'
        self.write_transcript([first, first, second])
        self.process('UserPromptSubmit', prompt='first')
        self.process('Stop', last_assistant_message='final')
        self.assertIn('> first\n> steer\nfinal', self.text())
        self.assertEqual(self.text().count('> first'), 1)

    def test_journal_recovery(self):
        import ai_history_common as storage
        row = dict(sid=self.meta['id'], tid='turn-1', ts='2026-10-03 12:00:00', cwd='~', prompt='p', answer='a')
        with patch.object(storage.Archive, 'recover', lambda self: None):
            self.m.commit(row)
        with self.m.locked():
            self.m.commit(row)
        self.assertEqual(self.text().count('===== 2026'), 1)
        ident = self.m.key(row['sid'], row['tid'])
        (self.m.STATE / 'done' / ident).unlink()
        path = next(self.root.glob('*/*.md'))
        self.m.atomic(self.m.STATE / 'transaction.json', dict(key=ident, sid=row['sid'], tid=row['tid'], relative_path=str(path.relative_to(self.root)), offset=0, block=self.m.block(row).decode()))
        with self.m.locked():
            self.m.recover()
        self.assertEqual(self.text().count('===== 2026'), 1)

    def test_lock_timeout_queues_silently(self):
        lock = self.root / '.lock'
        lock.mkdir()
        env = dict(os.environ, AI_HISTORY_DIR=str(self.root), AI_HISTORY_CODEX_SESSIONS=str(self.sessions))
        result = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(self.event('Stop', last_assistant_message='a')['input']), env=env, text=True, capture_output=True)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        self.assertTrue(lock.exists())
        self.assertEqual(len(list(self.m.QUEUE.glob('*.json'))), 1)
        self.assertEqual(self.text(), '')
        lock.rmdir()
        self.m.drain()
        self.assertEqual(len(list(self.m.QUEUE.glob('*.json'))), 0)
        self.assertIn('\na\n', self.text())
    def queue(self, number, name, **extra):
        envelope = self.event(name, **extra)
        path = self.m.QUEUE / f'{number:03}.json'
        self.m.atomic(path, envelope)
        return path

    def message(self, tid, text):
        return {'type': 'event_msg', 'timestamp': '2026-10-03T14:58:00Z',
                'payload': {'type': 'item_completed', 'turn_id': tid,
                            'item': {'type': 'UserMessage', 'id': tid,
                                     'content': [{'text': text}]}}}

    def test_failed_prompt_blocks_later_same_session(self):
        first = self.queue(1, 'UserPromptSubmit', prompt='preserve me')
        second = self.queue(2, 'Stop', last_assistant_message='final')
        original = self.m.process
        def fail_prompt(envelope):
            if envelope['input']['hook_event_name'] == 'UserPromptSubmit':
                raise OSError('temporary unavailable transcript')
            original(envelope)
        with patch.object(self.m, 'process', fail_prompt):
            self.m.drain()
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(self.text(), '')
        self.m.drain()
        self.assertIn('> preserve me\nfinal', self.text())

    def test_exhausted_budget_retains_queue_and_releases_lock(self):
        event = self.queue(1, 'Stop', last_assistant_message='final')
        with self.assertRaises(TimeoutError):
            self.m.drain(budget=0)
        self.assertTrue(event.exists())
        self.assertFalse((self.root / '.lock').exists())
        self.assertIsNone(self.m.DEADLINE)
        self.m.drain()
        self.assertFalse(event.exists())

    def test_failed_session_does_not_block_other_session(self):
        failed = self.queue(1, 'UserPromptSubmit', prompt='retry later')
        other_meta = dict(self.meta, id='87654321-other')
        other_path = self.sessions / 'rollout-87654321-other.jsonl'
        other_path.write_text(json.dumps({'type': 'session_meta', 'payload': other_meta}) + '\n')
        other = self.event('Stop', last_assistant_message='independent answer')
        other['input'].update(session_id=other_meta['id'], transcript_path=str(other_path))
        self.m.atomic(self.m.QUEUE / '002.json', other)
        original = self.m.process
        def fail_first(envelope):
            if envelope['input']['session_id'] == self.meta['id']:
                raise OSError('retry this session later')
            original(envelope)
        with patch.object(self.m, 'process', fail_first):
            self.m.drain()
        self.assertTrue(failed.exists())
        self.assertIn('independent answer', self.text())

    def test_partial_json_tail_preserves_complete_prompt(self):
        self.write_transcript([self.message('turn-1', 'complete prompt')])
        with self.path.open('a') as stream:
            stream.write('{"type":"event_msg",')
        self.process('Stop', last_assistant_message='final')
        self.assertIn('> complete prompt\nfinal', self.text())

    def test_malformed_complete_line_retains_event(self):
        with self.path.open('a') as stream:
            stream.write('{broken}\n')
        event = self.queue(1, 'Stop', last_assistant_message='final')
        self.m.drain()
        self.assertTrue(event.exists())
        self.assertEqual(self.text(), '')

    def test_backfill_skips_nonlast_unfinished_turn(self):
        self.write_transcript([
            self.message('turn-1', 'still active'),
            self.message('turn-2', 'finished'),
            {'type': 'event_msg', 'timestamp': '2026-10-03T14:59:00Z',
             'payload': {'type': 'task_complete', 'turn_id': 'turn-2',
                         'last_agent_message': 'second answer'}},
        ])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.m.backfill(True)
        self.assertEqual(json.loads(output.getvalue())['open_turns'], 1)
        self.assertNotIn('still active', self.text())
        self.process('Stop', last_assistant_message='real first answer')
        self.assertIn('> still active\nreal first answer', self.text())

    def test_backfill_missing_answer_field_logs_error(self):
        self.write_transcript([
            self.message('turn-1', 'question'),
            {'type': 'event_msg', 'timestamp': '2026-10-03T14:59:00Z',
             'payload': {'type': 'task_complete', 'turn_id': 'turn-1'}},
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            self.m.backfill(False)
        self.assertFalse((self.root / '.errors').exists())
        with contextlib.redirect_stdout(io.StringIO()):
            self.m.backfill(True)
        self.assertIn(self.m.MISSING, self.text())
        self.assertIn('missing final answer field', (self.root / '.errors').read_text())

    def test_unresolved_transaction_stops_drain(self):
        first = self.queue(1, 'Stop', last_assistant_message='first')
        second = self.queue(2, 'Stop', last_assistant_message='second')
        called = []
        def fail_commit(envelope):
            called.append(envelope)
            self.m.atomic(self.m.STATE / 'transaction.json', {'sentinel': True})
            raise OSError('injected transaction failure')
        with patch.object(self.m, 'process', fail_commit):
            self.m.drain()
        self.assertEqual(len(called), 1)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(self.text(), '')

    def test_done_marker_failure_retries_without_duplicate(self):
        import ai_history_common as storage
        self.queue(1, 'Stop', last_assistant_message='first answer')
        envelope = self.event('Stop', last_assistant_message='second answer')
        envelope['input']['turn_id'] = 'turn-2'
        self.m.atomic(self.m.QUEUE / '002.json', envelope)
        original = storage.atomic
        failed = False
        def fail_once(path, value):
            nonlocal failed
            if path.parent == self.m.STATE / 'done' and not failed:
                failed = True
                raise OSError('injected done-marker write failure')
            return original(path, value)
        with patch.object(storage, 'atomic', fail_once):
            self.m.drain()
        self.assertTrue((self.m.STATE / 'transaction.json').exists())
        self.assertEqual(len(list(self.m.QUEUE.glob('*.json'))), 2)
        self.m.drain()
        self.assertEqual(self.text().count('first answer'), 1)
        self.assertEqual(self.text().count('second answer'), 1)
        self.assertEqual(list(self.m.QUEUE.glob('*.json')), [])

    def test_claude_writer_recovers_pending_codex_append_first(self):
        import ai_history_common as storage
        row = dict(sid=self.meta['id'], tid='turn-1', ts='2026-10-03 12:00:00',
                   cwd='~', prompt='Codex prompt', answer='Codex answer')
        with patch.object(storage.Archive, 'recover', lambda self: None):
            self.m.commit(row)
        self.assertEqual(self.text(), '')
        claude = storage.Archive(self.root, 'claude')
        claude.setup()
        with claude.locked():
            claude.commit(dict(row, sid='claude-session', prompt='Claude prompt', answer='Claude answer'))
        self.assertEqual(self.text().count('Codex answer'), 1)
        self.assertEqual(self.text().count('Claude answer'), 1)
        self.assertLess(self.text().index('Codex answer'), self.text().index('Claude answer'))
        self.assertFalse((self.m.STATE / 'transaction.json').exists())

    def test_maintenance_reports_retained_event_failure(self):
        envelope = self.event('Stop', last_assistant_message='private answer')
        envelope['input']['transcript_path'] = str(self.sessions / 'missing.jsonl')
        self.m.atomic(self.m.QUEUE / '001.json', envelope)
        env = dict(os.environ, AI_HISTORY_DIR=str(self.root), AI_HISTORY_CODEX_SESSIONS=str(self.sessions))
        result = subprocess.run([sys.executable, str(SCRIPT), '--drain'], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout), {'remaining_events': 1})
        self.assertNotIn('private answer', result.stdout + result.stderr)

    def test_maintenance_reports_success(self):
        self.queue(1, 'Stop', last_assistant_message='final')
        env = dict(os.environ, AI_HISTORY_DIR=str(self.root), AI_HISTORY_CODEX_SESSIONS=str(self.sessions))
        result = subprocess.run([sys.executable, str(SCRIPT), '--drain'], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {'remaining_events': 0})

    def test_backfill_dry_run_does_not_create_archive(self):
        missing_root = Path(self.tmp.name) / 'absent-history'
        self.write_transcript([
            self.message('turn-1', 'question'),
            {'type': 'event_msg', 'timestamp': '2026-10-03T14:59:00Z',
             'payload': {'type': 'task_complete', 'turn_id': 'turn-1',
                         'last_agent_message': 'answer'}},
        ])
        env = dict(os.environ, AI_HISTORY_DIR=str(missing_root), AI_HISTORY_CODEX_SESSIONS=str(self.sessions))
        result = subprocess.run([sys.executable, str(SCRIPT), '--backfill'], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['new_turns'], 1)
        self.assertFalse(missing_root.exists())


if __name__ == '__main__':
    unittest.main()
