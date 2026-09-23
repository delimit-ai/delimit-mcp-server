"""Execution-slot evidence for the existing contained-worker lifecycle.

Linux /proc bindings are required by that launcher; unsupported/unreadable
process state fails closed. This does not discover or govern external CLIs.
"""
import os
import signal
import time
from pathlib import Path

DEFAULT_CONCURRENT_SESSIONS = 5


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def process_identity(pid):
    """Return kernel identity, distinguishing vanished processes from errors."""
    try:
        raw = Path(f'/proc/{int(pid)}/stat').read_text()
    except FileNotFoundError:
        return None
    fields = raw[raw.rfind(')') + 2:].split()
    return {'pid': int(pid), 'state': fields[0], 'pgrp': int(fields[2]),
            'start_ticks': int(fields[19])}


def process_snapshot():
    """A permission error is not evidence of absence."""
    found = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        identity = process_identity(int(entry.name))
        if identity is None or identity['state'] == 'Z':
            continue
        try:
            identity['argv'] = (entry / 'cmdline').read_bytes().split(b'\0')
        except FileNotFoundError:
            continue
        found.append(identity)
    return found


def exited(prefix):
    try:
        text = Path(str(prefix) + '.rc').read_text().strip()
        return text.startswith('rc=') and text[3:].lstrip('-').isdigit()
    except OSError:
        return False


def occupies_slot(task):
    slot = task.get('execution_slot')
    if slot:
        return slot.get('state') != 'released' and not exited(slot['output_prefix'])
    # Pre-upgrade bindings count conservatively until their wrapper exits.
    return bool(task.get('runtime') and task.get('output_prefix')
                and not exited(task['output_prefix']))


def admission(tasks):
    active = [key for key, task in tasks.items() if occupies_slot(task)]
    if len(active) < DEFAULT_CONCURRENT_SESSIONS:
        return None
    return {'status': 'concurrency_limited', 'active_sessions': len(active),
            'limit': DEFAULT_CONCURRENT_SESSIONS, 'active_task_ids': active,
            'reason': 'Concurrent tracked session limit reached. Poll finished workers or explicitly cancel a bound worker; audit-only dispatch remains available.'}


def reserve(prefix, session_id):
    owner = process_identity(os.getpid())
    if owner is None:
        raise OSError('cannot establish launch-owner identity')
    return {'state': 'reserved', 'output_prefix': str(prefix),
            'session_id': session_id, 'boot_id': boot_id(),
            'launch_owner': owner, 'reserved_at_epoch': time.time()}


def reclaim(slot):
    """Explicit cancel only: prove exit or terminate the exact worker group.

    Called while the caller holds the task-store transaction lock. Never
    releases on age, heartbeat or a sent signal alone. Returns audit evidence.
    """
    prefix = slot['output_prefix']
    if exited(prefix):
        return {'released': True, 'proof': 'wrapper_exit', 'output_prefix': prefix}
    if slot['boot_id'] != boot_id():
        return {'released': True, 'proof': 'prior_boot', 'boot_id': slot['boot_id']}
    sid = slot['session_id'].encode()
    group_file = Path(prefix + '.process-group')
    try:
        group, start = map(int, group_file.read_text().split())
    except FileNotFoundError:
        owner = slot['launch_owner']
        current = process_identity(owner['pid'])
        if current and current['start_ticks'] == owner['start_ticks'] and not slot.get('launch_returned'):
            return {'released': False, 'reason': 'launch owner still exists; missing process-group binding'}
        if any(sid in p['argv'] for p in process_snapshot()):
            return {'released': False, 'reason': 'launch/session process still exists without group binding'}
        return {'released': True, 'proof': 'pre_spawn_owner_gone_and_no_session',
                'launch_owner': owner, 'session_id': slot['session_id']}
    except (ValueError, OSError):
        return {'released': False, 'reason': 'process-group binding unreadable'}
    if group <= 1 or group == os.getpgrp():
        return {'released': False, 'reason': 'unsafe process-group identity'}
    leader = process_identity(group)
    members = [p for p in process_snapshot() if p['pgrp'] == group]
    if not members:
        return {'released': True, 'proof': 'group_absent', 'pgrp': group, 'start_ticks': start}
    # A reused group leader is never signalled. If the original leader exited,
    # the still-existing group retains its identity until its last member dies.
    if leader and leader['start_ticks'] != start:
        return {'released': False, 'reason': 'process-group leader identity changed'}
    if leader is None and not any(sid in p['argv'] for p in members):
        return {'released': False, 'reason': 'orphan group lacks a matching session identity'}
    os.killpg(group, signal.SIGTERM)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not any(p['pgrp'] == group for p in process_snapshot()):
            return {'released': True, 'proof': 'group_terminated', 'pgrp': group, 'start_ticks': start}
        time.sleep(.05)
    # Revalidate before escalation; do not kill a reused group.
    leader = process_identity(group)
    if leader and leader['start_ticks'] != start:
        return {'released': False, 'reason': 'process-group identity changed during cancellation'}
    try:
        os.killpg(group, signal.SIGKILL)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not any(p['pgrp'] == group for p in process_snapshot()):
            return {'released': True, 'proof': 'group_killed', 'pgrp': group, 'start_ticks': start}
        time.sleep(.05)
    return {'released': False, 'reason': 'termination not confirmed'}
