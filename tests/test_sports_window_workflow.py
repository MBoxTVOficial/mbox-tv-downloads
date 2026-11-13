"""Execute only the publication shell with a local git mock; never contacts GitHub."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]


class WindowWorkflowTests(unittest.TestCase):
    def run_publication(self, bundle, changed=True):
        bash = Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Git/bin/bash.exe'
        executable = str(bash) if bash.is_file() else shutil.which('bash')
        if executable is None:
            self.skipTest('Local bash required for the offline publication-shell test')
        workflow = (ROOT / '.github/workflows/update-sports-today.yml').read_text(encoding='utf8')
        body = workflow.split('- name: Commit only sports agenda artifacts', 1)[1].split('run: |', 1)[1]
        body = textwrap.dedent(body).strip()
        mock = '''git() {
  printf '%s\\t' "$@" >> "$TRACE"
  printf '\\n' >> "$TRACE"
  if [ "$1" = diff ]; then return "$DIFF_RESULT"; fi
  return 0
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('sports_today.json', 'sports_broadcasts_today.json', 'sports_fixture_identity.json'):
                (root / name).write_text('{}', encoding='utf8')
            if bundle:
                (root / 'sports_agenda_window.json').write_text('{}', encoding='utf8')
            trace = root / 'trace.txt'
            result = subprocess.run([executable, '-e', '-c', mock + body], cwd=root,
                env={**os.environ, 'TRACE': trace.as_posix(), 'DIFF_RESULT': '1' if changed else '0'},
                capture_output=True, text=True, timeout=15)
            self.assertEqual(0, result.returncode, result.stderr)
            return [line.rstrip('\t').split('\t') for line in trace.read_text(encoding='utf8').splitlines()]

    def test_first_deployment_missing_bundle_has_no_invalid_add_or_commit_path(self):
        commands = self.run_publication(bundle=False)
        self.assertFalse(any('sports_agenda_window.json' in command for command in commands))
        self.assertEqual(1, sum(command[0] == 'commit' for command in commands))
        self.assertEqual(1, sum(command[0] == 'push' for command in commands))

    def test_generated_window_is_staged_and_published_with_the_legacy_artifacts(self):
        commands = self.run_publication(bundle=True)
        staged = [command for command in commands if command[0] == 'add']
        self.assertTrue(any('sports_agenda_window.json' in command for command in staged))
        commit = next(command for command in commands if command[0] == 'commit')
        self.assertIn('sports_agenda_window.json', commit)
        self.assertIn('sports_today.json', commit)
        self.assertIn('--only', commit)

    def test_unchanged_generation_does_not_commit_or_push(self):
        commands = self.run_publication(bundle=True, changed=False)
        self.assertFalse(any(command[0] in {'commit', 'push'} for command in commands))


if __name__ == '__main__':
    unittest.main()
