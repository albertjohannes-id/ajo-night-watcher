#!/usr/bin/env python3
"""Local Cursor session monitor (desktop + CLI). No third-party packages, credential reads or shell commands.

Reads only two documented local stores, both over read-only/bounded access:
- ~/.cursor/chats/<workspace>/<session-uuid>/meta.json for titles, working
  directories and timestamps, plus a bounded tail of store.db message blobs
  (assistant/user roles only) for turn state. CLI agent sessions land here too.
- ~/.cursor/projects/<slug>/agent-transcripts/<uuid>/<uuid>.jsonl desktop
  agent transcripts (bounded tails); the slug encodes the workspace path and
  is only accepted when it decodes to a real directory.

Never touches account, auth, configuration or analytics material; only the
two session stores above are ever opened. Cursor keeps no quota or reset information on disk, so limits are recognized
from assistant-role text markers with explicit reset timestamps honored when
present, from the watcher's own resume run log, or through manual reset entry.
Headless continuation runs `cursor-agent -p --resume SESSION_ID PROMPT` in the
recorded directory; implemented but not yet smoke-tested against a disposable
session, so auto-resume should not be relied on until that probe passes.
"""
import contextlib, datetime, fcntl, json, os, pathlib, re, shutil, sqlite3, subprocess, sys, time, uuid

HOME = pathlib.Path.home()
ROOT = pathlib.Path(os.environ.get('AJO_NIGHT_WATCHER_DATA_HOME', HOME / 'Library/Application Support/Ajo Night Watcher'))
CURSOR_HOME = pathlib.Path(os.environ.get('CURSOR_HOME', HOME / '.cursor'))
DEFAULT_PROMPT = 'Continue from where you stopped. Review the existing changes first and continue the original task.'
DEFAULT_CLI = 'cursor-agent'

REGISTRY = 'cursor_registry.json'
STATE_LOCK = 'cursor_state.lock'
WORKER_ERROR_LOG = 'cursor-worker-errors.log'

# A session counts as live when its store moved this recently.
HOT_SECONDS = 90
# Maximum sessions kept from discovery, newest first.
DISCOVERY_LIMIT = 300

# Extra absolute locations checked when `cursor-agent` is not on PATH
# (e.g. a fresh install that only dropped the binary in ~/.local/bin).
CLI_CANDIDATES = (
    HOME / '.local/bin/cursor-agent',
    pathlib.Path('/opt/homebrew/bin/cursor-agent'),
    pathlib.Path('/usr/local/bin/cursor-agent'),
)


def prepare():
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(ROOT, 0o700)


@contextlib.contextmanager
def transaction():
    prepare()
    with (ROOT / STATE_LOCK).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = ROOT / REGISTRY
        state = json.loads(path.read_text()) if path.exists() else {'tasks': {}, 'prompt': DEFAULT_PROMPT, 'cli': DEFAULT_CLI, 'events': []}
        yield state
        tmp = ROOT / (REGISTRY + '.tmp')
        tmp.write_text(json.dumps(state, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)


def event(state, title, body):
    state['events'] = (state.get('events', []) + [{'id': str(uuid.uuid4()), 'title': title, 'body': body}])[-40:]


def epoch(value):
    if isinstance(value, (int, float)) and value > 1_000_000_000:
        return value / 1000 if value > 10_000_000_000 else value
    if isinstance(value, str):
        try:
            return epoch(float(value))
        except ValueError:
            try:
                return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
            except ValueError:
                pass
    return None


def reset_from_text(message):
    """Only accept explicit reset timestamps; never invent a quota window."""
    m = re.search(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})', message)
    if m:
        value = epoch(m.group())
        if value:
            return value
    m = re.search(r'(?i)(?:resets?\s+(?:at|in)|retry[^\d]{0,20})(\d+)\s*(seconds?|secs?|s\b|minutes?|mins?|m\b|hours?|h\b)?', message)
    if m:
        try:
            amount = int(m.group(1))
        except ValueError:
            amount = 0
        unit = (m.group(2) or 's').lower()
        seconds = amount * (3600 if unit.startswith('h') else 60 if unit.startswith('m') else 1)
        if 0 < seconds <= 7 * 24 * 3600:
            return time.time() + seconds
    return None


def rate_error_text(text):
    lowered = text.lower()
    return ('rate limit' in lowered or 'rate_limit' in lowered or 'usage limit' in lowered
            or 'usage_limit' in lowered or 'limit reached' in lowered or 'reached your limit' in lowered
            or 'too many requests' in lowered or 'try again in' in lowered or 'try again later' in lowered
            or 'resets at' in lowered or 'resets in' in lowered or 'retry after' in lowered
            or 'insufficient credit' in lowered or 'quota' in lowered or ' 429' in lowered or '(429' in lowered)


def tail(path, limit=2 * 1024 * 1024):
    with open(path, 'rb') as f:
        size = f.seek(0, 2)
        f.seek(max(0, size - limit))
        if size > limit:
            f.readline()
        return f.read().decode('utf-8', errors='replace')


def head(path, limit=64 * 1024):
    with open(path, 'rb') as f:
        return f.read(limit).decode('utf-8', errors='replace')


def resolve_cli(configured):
    """Prefer the stored path, then PATH, then known install locations."""
    if configured and cli_executable(configured):
        return configured
    found = shutil.which('cursor-agent')
    if found:
        return found
    for candidate in CLI_CANDIDATES:
        if os.access(str(candidate), os.X_OK):
            return str(candidate)
    return configured or DEFAULT_CLI


def cli_executable(cli):
    if not cli:
        return False
    if '/' in cli:
        return os.access(cli, os.X_OK)
    return shutil.which(cli) is not None


def text_of(content, chars=4000):
    """Flatten a message content (plain string or part list) to plain text."""
    if isinstance(content, str):
        return content[:chars]
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get('type') == 'text' and isinstance(item.get('text'), str):
                parts.append(item['text'])
        return '\n'.join(parts)[:chars]
    return ''


def decode_slug(slug):
    """Turn a Cursor workspace slug back into a path; only real directories count."""
    parts = slug.split('-')
    if len(parts) > 2:
        candidate = '/' + '/'.join(parts)
        if pathlib.Path(candidate).is_dir():
            return candidate
        if parts[-1] == 'workspace':
            candidate = '/' + '/'.join(parts[:-1])
            if pathlib.Path(candidate).is_dir():
                return candidate
    return ''


def chat_meta(session_dir):
    """meta.json title/cwd/timestamps; the session id is the directory name."""
    try:
        data = json.loads((session_dir / 'meta.json').read_text())
    except (OSError, ValueError) as e:
        raise RuntimeError('Session history unavailable: ' + str(e))
    if not isinstance(data, dict):
        raise RuntimeError('Session history unavailable: unreadable chat metadata.')
    title = data.get('title')
    title = title.strip()[:160] if isinstance(title, str) and title.strip() else 'Untitled Cursor task'
    cwd = data.get('cwd') if isinstance(data.get('cwd'), str) else ''
    updated = epoch(data.get('updatedAtMs')) or 0
    db = session_dir / 'store.db'
    wal = session_dir / 'store.db-wal'
    try:
        st = os.stat(str(wal if wal.exists() else db))
        source = [st.st_mtime_ns, st.st_size]
        if st.st_mtime > updated:
            updated = st.st_mtime
    except OSError:
        source = []
    return {'id': session_dir.name, 'title': title, 'cwd': cwd, 'updated_at': updated,
            'source': source, 'kind': 'chat', 'path': str(session_dir)}


def chat_messages(session_dir, count=60):
    """Most recent message blobs (role/content only), oldest first, bounded."""
    db = session_dir / 'store.db'
    try:
        with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=2) as c:
            rows = c.execute('select data from blobs order by rowid desc limit ?', (count,)).fetchall()
    except (sqlite3.Error, OSError) as e:
        raise RuntimeError('Session history unavailable: ' + str(e))
    messages = []
    for (data,) in rows:
        try:
            obj = json.loads(data.decode('utf-8', errors='replace') if isinstance(data, bytes) else data)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get('role') in ('user', 'assistant'):
            messages.append(obj)
    messages.reverse()
    return messages


def transcript_meta(path, slug):
    """Title from the first user query, cwd decoded from the workspace slug."""
    try:
        preview = head(path)
        st = os.stat(path)
    except OSError as e:
        raise RuntimeError('Session history unavailable: ' + str(e))
    first_text = None
    seen_users = 0
    for line in preview.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get('role') != 'user':
            continue
        seen_users += 1
        if seen_users > 5:
            break
        text = text_of((entry.get('message') or {}).get('content')).strip()
        # Strip the <user_query> wrapper Cursor records around prompts.
        text = re.sub(r'^<user_query>\s*', '', text)
        text = re.sub(r'\s*</user_query>$', '', text).strip()
        # Skip timestamp headers, bare image references and pasted dumps:
        # they carry no title information.
        if not text or re.match(r'^(<timestamp>|\[Image\]|\[Pasted text|\[Attachment)', text):
            continue
        first_text = text.splitlines()[0][:160]
        break
    cwd = decode_slug(slug)
    title = first_text or (pathlib.Path(cwd).name + ' session' if cwd
                           else slug.replace('-', ' ')[:160] or 'Untitled Cursor task')
    return {'id': path.stem, 'title': title[:160], 'cwd': cwd, 'updated_at': st.st_mtime,
            'source': [st.st_mtime_ns, st.st_size], 'kind': 'transcript', 'path': str(path)}


def discovery(limit=DISCOVERY_LIMIT):
    try:
        chats = sorted((p for p in (CURSOR_HOME / 'chats').glob('*/*/meta.json') if p.is_file()),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError as e:
        raise RuntimeError('Cannot read local Cursor sessions: ' + str(e))
    try:
        transcripts = sorted((CURSOR_HOME / 'projects').glob('*/agent-transcripts/*/*.jsonl'),
                             key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError as e:
        raise RuntimeError('Cannot read local Cursor sessions: ' + str(e))
    sessions = []
    for meta in chats:
        if len(sessions) >= limit:
            break
        try:
            sessions.append(chat_meta(meta.parent))
        except RuntimeError:
            continue
    for path in transcripts:
        if len(sessions) >= limit:
            break
        if path.name.startswith('.'):
            continue
        try:
            sessions.append(transcript_meta(path, path.parents[2].name))
        except RuntimeError:
            continue
    sessions.sort(key=lambda s: s['updated_at'], reverse=True)
    return sessions[:limit]


def hot(task):
    """A session whose store moved within HOT_SECONDS is treated as running."""
    try:
        if task.get('kind') == 'chat':
            session_dir = pathlib.Path(task['path'])
            wal = session_dir / 'store.db-wal'
            mtime = os.stat(str(wal if wal.exists() else session_dir / 'store.db')).st_mtime
        else:
            mtime = os.stat(task['path']).st_mtime
    except OSError:
        return False
    return time.time() - mtime < HOT_SECONDS


def classify(assistant_texts, last_role):
    """Waiting/Rate limited from assistant-role limit markers with explicit
    resets honored; Completed for a replied turn; Needs Input otherwise.

    Only assistant-role text is matched: user prompts and tool output often
    discuss rate limits without one ever happening.
    """
    for text in assistant_texts:
        if rate_error_text(text):
            reset = reset_from_text(text)
            if reset and reset > time.time():
                return 'Waiting', reset, text[-1000:]
            return 'Rate limited', None, (text[-1000:] or 'Cursor reports a limit.') + ' Reset time unavailable; set it manually.'
    if last_role == 'assistant':
        return 'Completed', None, 'Latest Cursor turn finished; this does not certify the whole goal is complete.'
    if last_role == 'user':
        return 'Needs Input', None, 'Latest message has no recorded reply. Review the session before resuming.'
    return 'Idle', None, ''


def inspect_session(task):
    if hot(task):
        return 'Running', None, 'Cursor session is active.'
    try:
        if task.get('kind') == 'chat':
            messages = chat_messages(pathlib.Path(task['path']))
            last_role = messages[-1]['role'] if messages else None
            assistant_texts = [text_of(m.get('content')) for m in messages if m.get('role') == 'assistant']
            assistant_texts = [t for t in assistant_texts if t][-3:]
            return classify(assistant_texts, last_role)
        entries = []
        for line in tail(task['path'], limit=512 * 1024).splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get('role') in ('user', 'assistant'):
                entries.append(entry)
        entries = entries[-60:]
        last_role = entries[-1]['role'] if entries else None
        assistant_texts = [text_of((e.get('message') or {}).get('content')) for e in entries if e.get('role') == 'assistant']
        assistant_texts = [t for t in assistant_texts if t][-3:]
        return classify(assistant_texts, last_role)
    except (OSError, RuntimeError) as e:
        return 'Needs Input', None, 'Session history unavailable: ' + str(e)


def worker_alive(task):
    run = task.get('run')
    if not run:
        return False
    with (ROOT / (task['id'] + '.cursor.run.lock')).open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return time.time() - run['started'] < 10  # process startup grace


def refresh(state):
    rows = discovery()
    known = {r['id'] for r in rows}
    for row in rows:
        task = state['tasks'].get(row['id'])
        if task is None:
            task = dict(row, state='Idle', armed=False, reset=None, note='', fingerprint=None)
            state['tasks'][row['id']] = task
        task.update(row)
        task['available'] = True
    for task in state['tasks'].values():
        if task['id'] not in known:
            task['available'] = False
        if task.get('run'):
            if worker_alive(task):
                continue
            task.update(state='Needs Input', reset=None, run=None,
                        note='Watcher run was interrupted. Review the task before resuming.')
            event(state, 'Cursor task needs attention', task['title'] + ': interrupted run')
        if not task.get('available'):
            continue
        if task.get('manual') and task.get('state') not in ('Running', 'Completed'):
            continue
        status, reset, note = inspect_session(task)
        if task.get('manual') and status not in ('Running', 'Completed'):
            continue
        # The fingerprint covers the source stat and the derived state, so a
        # changed classification re-applies even when the session has not moved.
        fingerprint = list(task.get('source') or []) + [status]
        if fingerprint != task.get('fingerprint'):
            task['fingerprint'] = fingerprint
            task.update(state=status, reset=reset, note=note, manual=False)


def launch(state, task):
    if task.get('run') or task['state'] == 'Running':
        raise ValueError('Task is already running. Finish or stop it in Cursor first.')
    if not task.get('available'):
        raise ValueError('Task is archived or not available in the local registry.')
    if any(t['id'] != task['id'] and t['state'] == 'Running'
           and t.get('cwd') and task.get('cwd')
           and os.path.realpath(t['cwd']) == os.path.realpath(task['cwd'])
           for t in state['tasks'].values()):
        raise ValueError('Another Cursor task is running in this working directory.')
    if not task.get('cwd') or not pathlib.Path(task['cwd']).is_dir():
        raise ValueError('Working directory no longer exists.')
    cli = resolve_cli(state['cli'])
    if cli != state['cli']:
        state['cli'] = cli
        event(state, 'Cursor CLI path updated', 'The stored executable was not found; using ' + cli + '.')
    if not cli_executable(cli):
        raise ValueError('Cursor executable was not found. Update its path in Settings.')
    if any(t.get('run') for t in state['tasks'].values()):
        raise ValueError('Another watcher continuation is running.')
    token = str(uuid.uuid4())
    task.update(state='Running', reset=None, manual=False,
                run={'token': token, 'started': time.time()}, note='Starting Cursor continuation…')
    try:
        with (ROOT / WORKER_ERROR_LOG).open('ab') as log:
            subprocess.Popen([sys.executable, str(pathlib.Path(__file__).resolve()),
                              'worker', task['id'], token],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    except Exception:
        task.update(state='Needs Input', run=None, note='Could not launch the worker.')
        raise


def worker(task_id, token):
    prepare()
    with (ROOT / (task_id + '.cursor.run.lock')).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with transaction() as state:
            task = state['tasks'][task_id]
            if (task.get('run') or {}).get('token') != token:
                return
            cli, prompt, cwd = resolve_cli(state['cli']), state['prompt'], task['cwd']
            event(state, 'Cursor task resumed', task['title'])
        path = ROOT / (task_id + '.cursor.log')
        status, reset, note = 'Needs Input', None, 'Cursor did not finish successfully.'
        try:
            # Direct argv: session IDs and prompts never pass through a shell.
            # `cursor-agent -p --resume <id> <prompt>` continues headless.
            args = [cli, '-p', '--resume', task_id, prompt]
            with path.open('wb') as output:
                p = subprocess.Popen(args, cwd=cwd, stdin=subprocess.DEVNULL,
                                     stdout=output, stderr=subprocess.STDOUT)
                p.communicate(timeout=6 * 3600)
            try:
                logged = tail(path)
            except OSError:
                logged = ''
            if p.returncode != 0 and rate_error_text(logged):
                reset = reset_from_text(logged)
                if reset and reset > time.time():
                    status, note = 'Waiting', logged[-1000:]
                else:
                    status, note = 'Rate limited', (logged[-1000:] or 'Cursor is rate limited.') + ' Reset time unavailable; set it manually.'
            elif p.returncode != 0:
                status, note = 'Needs Input', (logged[-1000:] or 'Cursor exited with code %s. Open the run log.' % p.returncode)
            else:
                status, reset, note = inspect_session(task)
                if status in ('Idle', 'Running'):
                    status, reset, note = 'Needs Input', None, 'No terminal Cursor event was received. Open the run log.'
        except Exception as e:
            note = str(e)
        with transaction() as state:
            task = state['tasks'][task_id]
            if (task.get('run') or {}).get('token') != token:
                return
            if reset and reset <= time.time():
                status, reset, note = 'Needs Input', None, 'Cursor reported a reset time that has already passed. Set a new time or Resume Now.'
            task.update(state=status, reset=reset, run=None, note=note)
            event(state, 'Cursor task ' + ('finished' if status == 'Completed' else 'needs attention'),
                  task['title'] + ': ' + note[:200])


def command(request):
    with transaction() as state:
        op = request.get('op', 'snapshot')
        error = None
        try:
            refresh(state)
        except (OSError, RuntimeError) as e:
            error = str(e)
        if op in ('arm', 'schedule', 'resume', 'idle'):
            task = state['tasks'][request['id']]
            if op == 'arm':
                task['armed'] = bool(request['armed'])
            elif op == 'schedule':
                if task['state'] == 'Running':
                    raise ValueError('Task is running. Stop it in Cursor before scheduling.')
                reset = float(request['reset'])
                if reset <= time.time():
                    raise ValueError('Choose a future reset time.')
                task.update(state='Waiting', armed=True, reset=reset, manual=True, note='Reset time entered manually.')
            elif op == 'idle':
                if task.get('run'):
                    raise ValueError('The watcher still has an active worker.')
                task.update(state='Idle', reset=None, manual=False,
                            note='Marked idle by you. Ensure Cursor is no longer running this task.')
            else:
                if error:
                    raise ValueError(error)
                launch(state, task)
        elif op == 'settings':
            prompt = request['prompt'].strip()
            if not prompt:
                raise ValueError('Continuation prompt cannot be empty.')
            cli = os.path.expanduser(request['cli'])
            if not cli_executable(cli):
                raise ValueError('Choose an executable Cursor CLI path.')
            state.update(prompt=prompt, cli=cli)
        if op == 'tick' and not error and not any(t.get('run') for t in state['tasks'].values()):
            due = [t for t in state['tasks'].values()
                   if t['armed'] and t['state'] == 'Waiting' and t.get('reset') and t['reset'] + 15 <= time.time()]
            if due:
                task = min(due, key=lambda t: t['reset'])
                try:
                    launch(state, task)
                except ValueError as e:
                    task.update(state='Needs Input', reset=None, note=str(e))
                    event(state, 'Cursor resume failed', task['title'] + ': ' + str(e))
        return dict(state, tasks=sorted(state['tasks'].values(), key=lambda t: (not t['armed'], -(t.get('updated_at') or 0))), error=error)


if __name__ == '__main__':
    os.umask(0o077)
    if len(sys.argv) > 1 and sys.argv[1] == 'worker':
        worker(sys.argv[2], sys.argv[3])
    else:
        try:
            print(json.dumps(command(json.load(sys.stdin))))
        except Exception as e:
            print(json.dumps({'error': str(e)}))
            sys.exit(1)
