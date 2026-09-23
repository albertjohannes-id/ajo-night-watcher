#!/usr/bin/env python3
"""Local Command Code session monitor. No third-party packages, credential reads or shell commands.

Reads only ~/.commandcode/projects/*/[session-uuid].jsonl transcripts (bounded
tails) plus their [session-uuid].meta.json titles over plain file reads. Never
touches auth.json, settings.json or any credential store.

Command Code keeps no plan, quota or reset information on disk, and the product
guidance is explicit that the plan is not readable headlessly, so this adapter
reads no usage at all. Limits are detected from the watcher's own resume run log
through the documented exit codes (5 rate limited, 10 insufficient credits);
manual scheduling is the dependable fallback.
"""
import contextlib, datetime, fcntl, json, os, pathlib, re, shutil, subprocess, sys, time, uuid

HOME = pathlib.Path.home()
ROOT = pathlib.Path(os.environ.get('AJO_NIGHT_WATCHER_DATA_HOME', HOME / 'Library/Application Support/Ajo Night Watcher'))
COMMANDCODE_HOME = pathlib.Path(os.environ.get('COMMANDCODE_HOME', HOME / '.commandcode'))
DEFAULT_PROMPT = 'Continue from where you stopped. Review the existing changes first and continue the original task.'
DEFAULT_CLI = 'cmd'

REGISTRY = 'commandcode_registry.json'
STATE_LOCK = 'commandcode_state.lock'
WORKER_ERROR_LOG = 'commandcode-worker-errors.log'

# Sidecars that sit next to a transcript and share its .jsonl suffix.
SIDECARS = ('.checkpoints.jsonl', '.prompts.jsonl')

EXIT_RATE_LIMITED = 5
EXIT_INSUFFICIENT_CREDITS = 10


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
    return ('429' in lowered or 'rate_limit' in lowered or 'rate limit' in lowered
            or 'usage limit' in lowered or 'usage_limit' in lowered
            or 'insufficient credit' in lowered or 'no remaining credit' in lowered
            or 'quota' in lowered or 'too many requests' in lowered)


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


def cli_executable(cli):
    if '/' in cli:
        return os.access(cli, os.X_OK)
    return shutil.which(cli) is not None


def entry_message(entry):
    message = entry.get('message')
    return message if isinstance(message, dict) else {}


def entry_source(entry):
    meta = entry_message(entry).get('meta')
    return (meta or {}).get('source') if isinstance(meta, dict) else None


def saved_title(path):
    try:
        meta = json.loads(path.parent.joinpath(path.stem + '.meta.json').read_text())
    except (OSError, ValueError):
        return None
    title = meta.get('title') if isinstance(meta, dict) else None
    return title.strip() if isinstance(title, str) and title.strip() else None


def session_meta(path):
    """Header cwd, saved title and stat pair; the session id is the file stem."""
    try:
        preview = head(path)
        st = os.stat(path)
    except OSError as e:
        raise RuntimeError('Session history unavailable: ' + str(e))
    cwd, first_text = '', None
    for line in preview.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get('type') == 'session' and isinstance(entry.get('cwd'), str) and not cwd:
            cwd = entry['cwd']
        # Tool results are carried as user entries too, so only source "user" is a prompt.
        if first_text is None and entry.get('type') == 'message' and entry_source(entry) == 'user':
            for item in entry_message(entry).get('content') or []:
                if isinstance(item, dict) and item.get('type') == 'text' and str(item.get('text', '')).strip():
                    first_text = str(item['text']).strip().splitlines()[0][:160]
                    break
        if cwd and first_text:
            break
    title = saved_title(path) or first_text
    if not title:
        title = (pathlib.Path(cwd).name + ' session') if cwd else 'Untitled Command Code task'
    return {'id': path.stem, 'title': title[:160], 'cwd': cwd, 'updated_at': st.st_mtime,
            'source': [st.st_mtime_ns, st.st_size], 'path': str(path)}


def discovery(limit=300):
    projects = COMMANDCODE_HOME / 'projects'
    try:
        files = sorted(projects.glob('*/*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError as e:
        raise RuntimeError('Cannot read local Command Code sessions: ' + str(e))
    sessions = []
    for path in files:
        if path.name.startswith('.') or path.name.endswith(SIDECARS):
            continue
        if len(sessions) >= limit:
            break
        try:
            sessions.append(session_meta(path))
        except RuntimeError:
            continue
    return sessions


def inspect_session(path):
    """Derive Idle/Waiting/Needs Input/Completed from the bounded tail only.

    Only the entry structure is read, never message text: an ordinary conversation
    about rate limits would otherwise match and invent a limit that never happened.
    """
    try:
        text = tail(path)
    except OSError as e:
        return 'Needs Input', None, 'Session history unavailable: ' + str(e)
    last = None
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get('type') == 'message':
            last = entry
    if last is None:
        return 'Idle', None, ''
    source = entry_source(last)
    content = entry_message(last).get('content')
    content = content if isinstance(content, list) else []
    if source == 'model':
        if content and isinstance(content[-1], dict) and content[-1].get('type') == 'tool_use':
            return 'Needs Input', None, 'Turn stopped mid-tool with no recorded result. Review the session before resuming.'
        return 'Completed', None, 'Latest Command Code turn finished; this does not certify the whole goal is complete.'
    if source == 'user':
        return 'Needs Input', None, 'Latest message has no recorded reply. Review the session before resuming.'
    return 'Needs Input', None, 'Turn stopped before the model replied. Review the session before resuming.'


def worker_alive(task):
    run = task.get('run')
    if not run:
        return False
    with (ROOT / (task['id'] + '.commandcode.run.lock')).open('a') as f:
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
            event(state, 'Command Code task needs attention', task['title'] + ': interrupted run')
        if not task.get('available'):
            continue
        if task.get('manual') and task.get('state') not in ('Running', 'Completed'):
            continue
        status, reset, note = inspect_session(task['path'])
        if task.get('manual') and status not in ('Running', 'Completed'):
            continue
        # The fingerprint covers the transcript stat and the derived state, so a
        # changed classification re-applies even when the transcript has not moved.
        fingerprint = list(task.get('source') or []) + [status]
        if fingerprint != task.get('fingerprint'):
            task['fingerprint'] = fingerprint
            task.update(state=status, reset=reset, note=note, manual=False)


def launch(state, task):
    if task.get('run') or task['state'] == 'Running':
        raise ValueError('Task is already running. Finish or stop it in Command Code first.')
    if not task.get('available'):
        raise ValueError('Task is archived or not available in the local registry.')
    if any(t['id'] != task['id'] and t['state'] == 'Running'
           and t.get('cwd') and task.get('cwd')
           and os.path.realpath(t['cwd']) == os.path.realpath(task['cwd'])
           for t in state['tasks'].values()):
        raise ValueError('Another Command Code task is running in this working directory.')
    if not task.get('cwd') or not pathlib.Path(task['cwd']).is_dir():
        raise ValueError('Working directory no longer exists.')
    if not cli_executable(state['cli']):
        raise ValueError('Command Code executable was not found. Update its path in Settings.')
    if any(t.get('run') for t in state['tasks'].values()):
        raise ValueError('Another watcher continuation is running.')
    token = str(uuid.uuid4())
    task.update(state='Running', reset=None, manual=False,
                run={'token': token, 'started': time.time()}, note='Starting Command Code continuation…')
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
    with (ROOT / (task_id + '.commandcode.run.lock')).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with transaction() as state:
            task = state['tasks'][task_id]
            if (task.get('run') or {}).get('token') != token:
                return
            cli, prompt, cwd = state['cli'], state['prompt'], task['cwd']
            event(state, 'Command Code task resumed', task['title'])
        path = ROOT / (task_id + '.commandcode.log')
        status, reset, note = 'Needs Input', None, 'Command Code did not finish successfully.'
        try:
            # Direct argv: session IDs and prompts never pass through a shell.
            # `cmd -p --resume <id> <prompt>` continues the session headless.
            args = [cli, '-p', '--resume', task_id, prompt]
            with path.open('wb') as output:
                p = subprocess.Popen(args, cwd=cwd, stdin=subprocess.DEVNULL,
                                     stdout=output, stderr=subprocess.STDOUT)
                p.communicate(timeout=6 * 3600)
            try:
                logged = tail(path)
            except OSError:
                logged = ''
            # Credits exhaustion is checked first: its text also matches the
            # rate-limit pattern, but it has no reset time to wait for.
            if p.returncode == EXIT_INSUFFICIENT_CREDITS:
                status, note = 'Needs Input', 'Command Code reports no remaining credits. Top up the account before resuming.'
            elif p.returncode == EXIT_RATE_LIMITED or (p.returncode != 0 and rate_error_text(logged)):
                reset = reset_from_text(logged)
                if reset and reset > time.time():
                    status, note = 'Waiting', logged[-1000:]
                else:
                    status, note = 'Rate limited', (logged[-1000:] or 'Command Code is rate limited.') + ' Reset time unavailable; set it manually.'
            elif p.returncode != 0:
                status, note = 'Needs Input', (logged[-1000:] or 'Command Code exited with code %s. Open the run log.' % p.returncode)
            else:
                status, reset, note = inspect_session(task['path'])
                if status in ('Idle', 'Running'):
                    status, reset, note = 'Needs Input', None, 'No terminal Command Code event was received. Open the run log.'
        except Exception as e:
            note = str(e)
        with transaction() as state:
            task = state['tasks'][task_id]
            if (task.get('run') or {}).get('token') != token:
                return
            if reset and reset <= time.time():
                status, reset, note = 'Needs Input', None, 'Command Code reported a reset time that has already passed. Set a new time or Resume Now.'
            task.update(state=status, reset=reset, run=None, note=note)
            event(state, 'Command Code task ' + ('finished' if status == 'Completed' else 'needs attention'),
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
                    raise ValueError('Task is running. Stop it in Command Code before scheduling.')
                reset = float(request['reset'])
                if reset <= time.time():
                    raise ValueError('Choose a future reset time.')
                task.update(state='Waiting', armed=True, reset=reset, manual=True, note='Reset time entered manually.')
            elif op == 'idle':
                if task.get('run'):
                    raise ValueError('The watcher still has an active worker.')
                task.update(state='Idle', reset=None, manual=False,
                            note='Marked idle by you. Ensure Command Code is no longer running this task.')
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
                raise ValueError('Choose an executable Command Code CLI path.')
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
                    event(state, 'Command Code resume failed', task['title'] + ': ' + str(e))
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
