"""Configuration pytest : chemins temporaires AVANT tout import de config (aucune écriture sous /mnt)."""
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_ROOT = Path(tempfile.mkdtemp(prefix="kyc_app_tests_"))
os.environ["KYC_OUTPUT_ROOT"] = str(TEST_ROOT / "output")
os.environ["KYC_APP_ROOT"] = str(TEST_ROOT / "app")
os.environ["KYC_REFERENCE_CSV"] = str(TEST_ROOT / "reference.csv")
os.environ["KYC_MODEL_PATH"] = str(TEST_ROOT / "model")
os.environ["KYC_RUN_MODE"] = "FULL_PIPELINE"
os.environ["KYC_LOCK_HEARTBEAT_S"] = "1"
os.environ["KYC_LOCK_STALE_AFTER_S"] = "60"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
