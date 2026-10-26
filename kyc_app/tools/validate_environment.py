"""Validation de l'environnement Domino SANS charger le modèle 27B.

Contrôle : Python, paquets (versions minimales du notebook), Streamlit, PyTorch + CUDA + GPU
(H100, BF16), Transformers (classes utilisées par le notebook, FineGrainedFP8Config),
dépôt modèle local (config.json, vision, quantification FP8, AutoConfig local), /mnt et droits
d'écriture, fichier de référence (5 colonnes exactes), conformité du moteur généré.

    python tools/validate_environment.py            # complet
    python tools/validate_environment.py --no-gpu   # sans contrôles GPU/PyTorch (poste CPU)
Code retour 0 si tous les contrôles bloquants passent.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

RESULTS: list[tuple[str, str, str]] = []  # (statut, contrôle, détail)
NOTEBOOK_MINIMUMS = {"numpy": "1.23", "pandas": "1.5", "Pillow": "9.0", "PyMuPDF": "1.23", "openpyxl": "3.1",
                     "packaging": "21.0", "psutil": "5.9", "streamlit": "1.37"}
GPU_MINIMUMS = {"torch": "2.0", "transformers": "4.57", "accelerate": "0.30"}
REFERENCE_COLUMNS = ["Id tiers", "Date de naissance", "Nom abrege tiers", "Date expiration document", "Numero de compte"]


def record(ok: bool | None, name: str, detail: str = "") -> None:
    status = "OK" if ok else ("WARN" if ok is None else "FAIL")
    RESULTS.append((status, name, detail))
    print(f"[{status:4}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def check_packages(minimums: dict) -> None:
    from packaging.version import Version

    for package, minimum in minimums.items():
        try:
            version = metadata.version(package)
        except metadata.PackageNotFoundError:
            record(False, f"paquet {package}", "absent")
            continue
        record(True if Version(version) >= Version(minimum) else None, f"paquet {package}", f"{version} (min. {minimum})")


def check_gpu() -> None:
    try:
        import torch
    except Exception as exc:  # noqa: BLE001
        record(False, "import torch", str(exc))
        return
    record(True, "import torch", f"{torch.__version__} · CUDA runtime {torch.version.cuda}")
    if not torch.cuda.is_available():
        record(False, "CUDA visible", "torch.cuda.is_available() == False (tier matériel GPU ?)")
        return
    names = []
    for index in range(torch.cuda.device_count()):
        props = torch.cuda.get_device_properties(index)
        with torch.cuda.device(index):
            bf16 = torch.cuda.is_bf16_supported()
        names.append(props.name)
        record(bf16, f"GPU {index}", f"{props.name} · {props.total_memory / 1024**3:.0f} GiB · BF16={bf16}")
    record(True if any("H100" in n for n in names) else None, "H100 détecté", ", ".join(names))
    try:
        free, total = torch.cuda.mem_get_info(0)
        record(True if free > 60 * 1024**3 else None, "Mémoire GPU libre", f"{free / 1024**3:.0f} / {total / 1024**3:.0f} GiB "
               "(≈54 Go de poids BF16 + vision + cache requis)")
    except Exception as exc:  # noqa: BLE001
        record(None, "Mémoire GPU libre", str(exc))
    try:
        from transformers import AutoConfig, AutoModelForImageTextToText, AutoProcessor  # noqa: F401
        try:
            from transformers.integrations.finegrained_fp8 import FineGrainedFP8Config  # noqa: F401
        except ImportError:
            from transformers import FineGrainedFP8Config  # noqa: F401
        record(True, "classes Transformers du notebook", "AutoProcessor, AutoModelForImageTextToText, FineGrainedFP8Config")
    except Exception as exc:  # noqa: BLE001
        record(False, "classes Transformers du notebook", str(exc))
    smi = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader"],
                         capture_output=True, text=True, check=False) if _which("nvidia-smi") else None
    record(True if smi and smi.returncode == 0 else None, "nvidia-smi", (smi.stdout.strip() if smi else "absent"))


def _which(executable: str) -> bool:
    from shutil import which

    return which(executable) is not None


def check_model(cfg, with_transformers: bool) -> None:
    config_path = cfg.model_path / "config.json"
    if not config_path.is_file():
        record(False, "dépôt modèle Qwen3.8", f"{config_path} absent")
        return
    config = json.loads(config_path.read_text(encoding="utf-8"))
    quant = str((config.get("quantization_config") or {}).get("quant_method", "")).lower()
    shards = list(cfg.model_path.glob("*.safetensors"))
    record(True, "dépôt modèle Qwen3.8", f"{cfg.model_path} · model_type={config.get('model_type')} · {len(shards)} shard(s)")
    record("vision_config" in json.dumps(config), "configuration vision déclarée")
    record(quant in {"fp8", "finegrained_fp8", "fine_grained_fp8"}, "quantification FP8 déclarée", quant or "absente")
    if with_transformers:
        try:
            from transformers import AutoConfig

            AutoConfig.from_pretrained(str(cfg.model_path), trust_remote_code=True, local_files_only=True)
            record(True, "AutoConfig local (sans poids)")
        except Exception as exc:  # noqa: BLE001
            record(False, "AutoConfig local (sans poids)", str(exc)[:300])


def check_storage(cfg) -> None:
    from file_manager import check_writable

    record(Path("/mnt").is_dir() or None, "/mnt disponible")
    for directory in (cfg.output_root, cfg.input_dir, cfg.extract_dir, cfg.logs_dir, cfg.runs_dir):
        ok, detail = check_writable(directory)
        record(ok, f"écriture {directory}", "" if ok else detail)
    try:
        stats = os.statvfs(cfg.output_root)
        free_gib = stats.f_bavail * stats.f_frsize / 1024**3
        record(True if free_gib > 20 else None, "espace libre sorties", f"{free_gib:.0f} GiB")
    except OSError as exc:
        record(None, "espace libre sorties", str(exc))


def check_reference(cfg) -> None:
    path = cfg.reference_csv
    if not path.is_file():
        record(False, "fichier de référence", f"{path} absent (candidats : {', '.join(map(str, cfg.reference_candidates))})")
        return
    head = path.read_bytes()[:65536].decode("utf-8-sig", errors="strict")
    header = None
    for separator in (";", ",", "\t", "|"):
        row = next(csv.reader(io.StringIO(head), delimiter=separator))
        if set(REFERENCE_COLUMNS).issubset({c.strip() for c in row}):
            header = (separator, row)
            break
    record(header is not None, "fichier de référence (5 colonnes, UTF-8)",
           f"{path} · séparateur {header[0]!r}" if header else f"{path} : colonnes attendues {REFERENCE_COLUMNS}")


def check_engine(cfg) -> None:
    code = subprocess.run([sys.executable, str(PROJECT_ROOT / "engine" / "build_engine.py"), "--check"],
                          capture_output=True, text=True, check=False)
    record(code.returncode == 0, "moteur conforme au notebook", (code.stdout or code.stderr).strip())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-gpu", action="store_true", help="ignorer PyTorch/CUDA (poste sans GPU)")
    args = parser.parse_args()
    record(sys.version_info >= (3, 10), "Python", sys.version.split()[0])
    check_packages(NOTEBOOK_MINIMUMS)
    try:
        from config import load_config

        cfg = load_config()
        record(True, "configuration application", json.dumps(cfg.describe(), ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        record(False, "configuration application", str(exc))
        return summary()
    if not args.no_gpu:
        check_packages(GPU_MINIMUMS)
        check_gpu()
    check_model(cfg, with_transformers=not args.no_gpu)
    check_storage(cfg)
    check_reference(cfg)
    check_engine(cfg)
    return summary()


def summary() -> int:
    failed = [r for r in RESULTS if r[0] == "FAIL"]
    warned = [r for r in RESULTS if r[0] == "WARN"]
    print(f"\nRÉSUMÉ : {len(RESULTS) - len(failed) - len(warned)} OK · {len(warned)} avertissement(s) · {len(failed)} échec(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
