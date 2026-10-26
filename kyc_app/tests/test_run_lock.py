"""Verrou : aucun traitement actif, traitement actif, verrou orphelin (pid mort, battement ancien, fichier corrompu)."""
import json
import os
import socket
import subprocess
import sys
import time

import pytest

from exceptions import ConcurrentRunError
from run_lock import RunLock


def write_lock(path, **overrides):
    payload = {"run_id": "other", "pid": os.getppid(), "host": socket.gethostname(),
               "started_at": "x", "heartbeat_at": "x", "heartbeat_epoch": time.time(), **overrides}
    path.write_text(json.dumps(payload))


def test_acquire_release(tmp_path):
    lock = RunLock(tmp_path / ".lock", 60)
    lock.acquire("r1")
    info = json.loads((tmp_path / ".lock").read_text())
    assert info["run_id"] == "r1" and info["pid"] == os.getpid()
    lock.heartbeat()
    lock.release()
    assert not (tmp_path / ".lock").exists()


def test_active_lock_blocks(tmp_path):
    path = tmp_path / ".lock"
    write_lock(path)  # processus parent vivant, même hôte, battement récent
    with pytest.raises(ConcurrentRunError):
        RunLock(path, 60).acquire("r2")


def test_dead_owner_is_stale(tmp_path):
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    path = tmp_path / ".lock"
    write_lock(path, pid=process.pid)
    lock = RunLock(path, 60)
    assert lock.status().reason == "OWNER_PROCESS_DEAD"
    lock.acquire("r3")
    assert json.loads(path.read_text())["run_id"] == "r3"
    assert list(tmp_path.glob(".lock.stale-*"))


def test_old_heartbeat_other_host_is_stale(tmp_path):
    path = tmp_path / ".lock"
    write_lock(path, host="previous-domino-container", pid=1, heartbeat_epoch=time.time() - 3600)
    RunLock(path, 60).acquire("r4")


def test_recent_heartbeat_other_host_blocks(tmp_path):
    path = tmp_path / ".lock"
    write_lock(path, host="another-container", pid=1)
    with pytest.raises(ConcurrentRunError):
        RunLock(path, 60).acquire("r5")


def test_own_pid_without_active_job_is_stale(tmp_path):
    path = tmp_path / ".lock"
    write_lock(path, pid=os.getpid())
    with pytest.raises(ConcurrentRunError):
        RunLock(path, 60).acquire("r6", local_job_active=True)
    RunLock(path, 60).acquire("r6", local_job_active=False)


def test_corrupt_lock(tmp_path):
    path = tmp_path / ".lock"
    path.write_text("{not json")
    with pytest.raises(ConcurrentRunError):
        RunLock(path, 60).acquire("r7")
    old = time.time() - 3600
    os.utime(path, (old, old))
    RunLock(path, 60).acquire("r7")
