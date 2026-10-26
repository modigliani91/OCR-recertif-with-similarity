"""Intégration de bout en bout avec Qwen simulé (aucun GPU). Ignoré si PyTorch/Transformers sont absents.

Sur Domino (environnement GPU), ces tests importent le vrai PyTorch mais ne chargent pas le modèle.
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("torch") is None or importlib.util.find_spec("transformers") is None,
    reason="PyTorch/Transformers absents : intégration à exécuter dans l'environnement Domino GPU")


def run(script: str, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("KYC_")}
    return subprocess.run([sys.executable, str(HERE / script), *args], capture_output=True, text=True, env=env, timeout=1800)


def test_full_run_and_checkpoint_reuse(tmp_path):
    result = run("dryrun_harness.py", "--workdir", str(tmp_path), "--scenario", "full")
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
    assert "DRYRUN_SUMMARY" in result.stdout


def test_cancel_then_resume(tmp_path):
    result = run("dryrun_harness.py", "--workdir", str(tmp_path), "--scenario", "cancel")
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]


def test_notebook_and_app_outputs_are_equivalent(tmp_path):
    result = run("notebook_equivalence_harness.py", "--workdir", str(tmp_path))
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
    assert "RÉSULTAT : ÉQUIVALENT" in result.stdout
