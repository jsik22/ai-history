#!/usr/bin/env python3
"""Import Claude transcripts. Legacy positional arguments retained; --apply writes."""
import argparse
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from ai_history_common import Archive, atomic, key, local_timestamp

SKIP_PREFIX = ('<local-command-', '<bash-', '<task-notification>', '<system-reminder>',
               '[Request interrupted by user', 'Caveat:')


def prompt_text(entry):
    content = entry.get('message', {}).get('content')
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in content):
            return None
        text = '\n'.join(b.get('text', '') for b in content
                         if isinstance(b, dict) and b.get('type') == 'text')
    else:
        return None
    if not text.strip() or text.lstrip().startswith(SKIP_PREFIX):
        return None
    return text


def rows(projects, cutoff):
    # Merge duplicate user UUIDs before choosing the answer, including copied files.
    turns = {}
    for path in sorted(projects.glob('*/*.jsonl')):
        try:
            entries = []
            with path.open(encoding='utf-8') as source:
                for line in source:
                    try:
                        entry = json.loads(line)
                        if isinstance(entry, dict):
                            entries.append(entry)
                    except ValueError:
                        if not line.endswith('\n'):
                            break  # A still-being-written final record.
                        raise ValueError(f'Malformed complete transcript record: {path}')
        except OSError as exc:
            print(f'Skipped unreadable transcript: {path}: {exc}', file=sys.stderr)
            continue
        users = [e for e in entries if e.get('type') == 'user']
        if not users or str(users[0].get('entrypoint', '')).startswith('sdk'):
            continue
        current = None
        for entry in entries:
            if entry.get('isSidechain') or entry.get('isMeta') or entry.get('isCompactSummary'):
                continue
            if entry.get('type') == 'user':
                prompt = prompt_text(entry)
                if prompt is None:
                    continue
                uid = entry.get('uuid')
                sid = entry.get('sessionId') or path.stem
                if not uid or not entry.get('timestamp'):
                    continue
                if current is not None and current['identity'] != (sid, uid):
                    current['followed_by_user'] = True
                current = turns.setdefault((sid, uid), dict(entry=entry, prompt=prompt,
                                                          identity=(sid, uid), messages={},
                                                          last=None, seen=set(), followed_by_user=False))
            elif entry.get('type') == 'assistant' and current is not None:
                eid = entry.get('uuid')
                if eid and eid in current['seen']:
                    continue
                if eid:
                    current['seen'].add(eid)
                message = entry.get('message', {})
                content = message.get('content', [])
                if not isinstance(content, list):
                    continue
                texts = [b.get('text', '') for b in content
                         if isinstance(b, dict) and b.get('type') == 'text']
                mid = message.get('id') or eid or str(len(current['messages']))
                current['messages'].setdefault(mid, []).extend(texts)
                if message.get('stop_reason') in ('end_turn', 'stop_sequence', 'refusal'):
                    current['last'] = mid
    for (sid, uid), turn in turns.items():
        entry = turn['entry']
        try:
            ts = local_timestamp(entry['timestamp'])
        except (ValueError, TypeError):
            continue
        if ts >= cutoff:
            continue
        if turn['last'] is None and not turn['followed_by_user']:
            continue  # Latest unfinished turn may still be running; do not mark done.
        if turn['last'] is None and turn['prompt'].startswith('/'):
            continue
        answer = ('\n'.join(turn['messages'][turn['last']]) if turn['last'] is not None
                  else '(이 턴의 답변 기록 없음)')
        yield dict(sid=sid, tid=uid, ts=ts, cwd=entry.get('cwd', ''),
                   prompt=turn['prompt'], answer=answer or '(텍스트 답변 없음)')


def legacy_row(row):
    """Reproduce the previous importer's transformations only for adoption."""
    old = dict(row)
    prompt = old['prompt'].strip()
    match = re.search(r'<command-name>(.*?)</command-name>', prompt, re.S)
    if match:
        args = re.search(r'<command-args>(.*?)</command-args>', prompt, re.S)
        prompt = (match.group(1).strip() + ' ' + (args.group(1).strip() if args else '')).strip()
    old['prompt'] = prompt
    return old


def import_row(archive, row, data=b''):
    if (archive.state / 'done' / key(row['sid'], row['tid'])).exists():
        return False
    if data:
        # Historical identities were absent; recognize exact old serialization.
        if archive.block(legacy_row(row)) in data or archive.block(row) in data:
            atomic(archive.state / 'done' / key(row['sid'], row['tid']),
                   {'sid': row['sid'], 'tid': row['tid'], 'adopted': True})
            return False
        # Do not append a changed representation of an already recorded turn.
        header = archive.block(row).split(b'\n', 1)[0] + b'\n'
        if header in data and archive.block(row) not in data:
            raise ValueError('ambiguous existing Claude turn; inspect legacy record before import')
    return archive.commit(row)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('out_root', nargs='?', default=os.environ.get('AI_HISTORY_DIR', str(Path.home() / '.ai_history')))
    parser.add_argument('cutoff', nargs='?', default=local_timestamp())
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    projects = Path(os.environ.get('AI_HISTORY_CLAUDE_PROJECTS', str(Path.home() / '.claude' / 'projects')))
    collected = sorted(rows(projects, args.cutoff), key=lambda row: row['ts'])
    if not args.apply:
        print(f'Dry run: {len(collected)} candidate turns. Use --apply to import.')
        return
    os.umask(0o077)
    archive = Archive(Path(args.out_root), 'claude')
    archive.setup()
    added = 0
    existing = {}
    with archive.locked():
        archive.recover_all()
        renders = {}
        for row in collected:
            target = archive.root / row['ts'][:7] / (row['ts'][:10] + '.md')
            if target not in existing:
                existing[target] = target.read_bytes() if target.exists() else b''
            rendered = archive.block(legacy_row(row))
            identity = (row['sid'], row['tid'])
            if rendered in renders and renders[rendered] != identity:
                previous = renders[rendered]
                known = all((archive.state / 'done' / key(*ident)).exists() for ident in (previous, identity))
                if not known and rendered in existing[target]:
                    raise ValueError('ambiguous identical historical turns; UUID adoption requires inspection')
            renders[rendered] = identity
            header = archive.block(row).split(b'\n', 1)[0] + b'\n'
            if (not (archive.state / 'done' / key(*identity)).exists()
                    and header in existing[target] and rendered not in existing[target]
                    and archive.block(row) not in existing[target]):
                raise ValueError('ambiguous existing Claude turn; inspect legacy record before import')
    for row in collected:
        with archive.locked():
            target = archive.root / row['ts'][:7] / (row['ts'][:10] + '.md')
            added += bool(import_row(archive, row, existing[target]))
    print(f'Imported {added} new turns; examined {len(collected)} candidates.')


if __name__ == '__main__':
    main()
