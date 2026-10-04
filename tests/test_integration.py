"""Both repository collectors share one temporary archive; no real history writes."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

BIN = Path(__file__).resolve().parents[1] / 'bin'

class IntegrationTests(unittest.TestCase):
    def test_parallel_both_collectors_and_search(self):
        with tempfile.TemporaryDirectory(prefix='history-integrated-') as temp:
            root = Path(temp) / 'archive with spaces'
            sessions = Path(temp) / 'sessions'
            sessions.mkdir()
            env = dict(os.environ, AI_HISTORY_DIR=str(root),
                       AI_HISTORY_CODEX_SESSIONS=str(sessions), CLAUDE_CODE_ENTRYPOINT='cli')
            def run(tool, event):
                result = subprocess.run([sys.executable, str(BIN / ('ai-history-' + tool))],
                    input=json.dumps(event), text=True, capture_output=True, env=env)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
            jobs = []
            for i in range(16):
                tool = 'codex' if i % 2 == 0 else 'claude'
                sid = f'session-{i}'
                event = dict(session_id=sid, turn_id=f'turn-{i}', cwd=str(Path.home()),
                             hook_event_name='UserPromptSubmit', prompt=f'PROMPT {i}\n')
                if tool == 'codex':
                    path = sessions / (sid + '.jsonl')
                    path.write_text(json.dumps({'type': 'session_meta', 'payload': {
                        'id': sid, 'originator': 'codex-tui', 'source': 'cli', 'cwd': str(Path.home())}}) + '\n')
                    event['transcript_path'] = str(path)
                run(tool, event)
                event.update(hook_event_name='Stop', last_assistant_message=f'ANSWER {i}\n')
                jobs.append((tool, event))
            with concurrent.futures.ThreadPoolExecutor(max_workers=16) as workers:
                list(workers.map(lambda pair: run(*pair), jobs))
            data = ''.join(path.read_text() for path in sorted(root.glob('*/*.md')))
            self.assertEqual(data.count('===== '), 16)
            for i in range(16):
                self.assertIn(f'> PROMPT {i}\n> \nANSWER {i}\n\n\n', data)
            self.assertFalse((root / '.lock').exists())
            for tool in ('claude', 'codex'):
                self.assertEqual(list((root / '.pending' / (tool + '-events')).glob('*.json')), [])
                self.assertFalse((root / ('.' + tool + '-state') / 'transaction.json').exists())
                query = subprocess.run([sys.executable, str(BIN / 'aih'), '-t', tool, '-l'],
                                       env=env, text=True, capture_output=True)
                self.assertEqual(query.returncode, 0)
                self.assertEqual(query.stdout.count('===== '), 8)
            for path in root.glob('*/*.md'):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

if __name__ == '__main__':
    unittest.main()
