"""Regression checks for repository aih; fixtures never use live history."""
import datetime
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

AIH = Path(__file__).resolve().parents[1] / 'bin/aih'


def block(stamp, tool, prompt, answer, cwd='~/project with space'):
    return f'===== {stamp} | {tool} | {cwd} | s:12345678 =====\n> {prompt}\n{answer}\n\n'


class AihTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aih regression ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.first = block('2001-01-02 22:00:00', 'codex', 'FIRST', 'alpha something beta\nliteral\\new [x].*\n===== ordinary separator')
        self.second = block('2001-01-02 01:00:00', 'claude', 'second', 'alpha beta\n\n')
        self.third = block('2001-01-03 01:00:00', 'codex', 'third', 'final')
        self.write('2001-01/2001-01-02.md', self.first + self.second)
        self.write('2001-01/2001-01-03.md', self.third)
        self.write('.pending/2001-01-04.md', self.third)
        self.write('.2001-01-05.md', self.third)

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def query(self, *args):
        return subprocess.run([str(AIH), *args], env={**os.environ, 'AI_HISTORY_DIR': str(self.root)}, capture_output=True, text=True)

    def good(self, args, expected):
        result = self.query(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        self.assertEqual(result.stdout, expected)

    def test_spaces_exact_output_hidden_files(self):
        self.good(['-d', '2001'], self.first + self.second + self.third)

    def test_phrase_preserves_argument_boundary(self):
        self.good(['alpha beta'], self.second)

    def test_and_case_insensitive_and_literal(self):
        self.good(['ALPHA', 'FIRST', '[x].*', 'literal\\new'], self.first)

    def test_tool_project_filters(self):
        self.good(['-t', 'claude', '-p', 'project with space'], self.second)
        self.good(['-p', 'absent'], '')

    def test_last_n_preserves_file_order(self):
        self.good(['-d', '2001-01-02', '-n', '1'], self.second)
        self.good(['-d', '2001', '-n', '2'], self.second + self.third)
        self.good(['-d', '2001', '-n', '0'], self.first + self.second + self.third)

    def test_list(self):
        self.good(['-t', 'claude', '-l'], '\n'.join(self.second.split('\n')[:2]) + '\n')

    def test_bad_arguments(self):
        for args in [('-n', '1.5'), ('-n', 'abc'), ('-n', '-1'), ('-n',), ('-z',), ('-p',), ('-t', 'unknown')]:
            with self.subTest(args=args):
                result = self.query(*args)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertIn('error:', result.stderr)

    def test_help(self):
        result = self.query('-h')
        self.assertEqual(result.returncode, 0)
        self.assertIn('파일 순서상', result.stdout)

    def test_no_args_today(self):
        day = datetime.date.today().isoformat()
        today = block(day + ' 01:00:00', 'codex', 'today', 'response')
        self.write(day[:7] + '/' + day + '.md', today)
        self.good([], today)

    def test_date_prefix_is_literal(self):
        self.good(['-d', '2001*'], '')


if __name__ == '__main__':
    unittest.main()
