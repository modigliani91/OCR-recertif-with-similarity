"""Façade du modèle Qwen3.8 : état, informations GPU et chargement explicite optionnel.

Le modèle n'est JAMAIS chargé ici en double : l'unique propriétaire est le moteur du notebook
(``ensure_model_loaded`` de la cellule 16, avec ses contrôles FP8→BF16, placement GPU,
template sans thinking). Le pipeline le charge paresseusement, au premier besoin réel
d'inférence ; si tous les RAW/structures sont déjà en checkpoint, il n'est pas chargé.

L'application obtient une seule instance de cette façade via ``st.cache_resource``.
"""
from __future__ import annotations

import sys
import threading

from config import AppConfig
from file_manager import gpu_inventory


class ModelManager:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self._lock = threading.Lock()

    @staticmethod
    def _core():
        module = sys.modules.get("engine.notebook_core")
        return module if module is not None and getattr(module, "_APP_INSTRUMENTED", False) else None

    def engine_loaded(self) -> bool:
        return self._core() is not None

    def is_model_loaded(self) -> bool:
        core = self._core()
        return bool(core is not None and getattr(core, "MODEL_READY", False))

    def ensure_loaded(self) -> None:
        """Chargement explicite (script de smoke test GPU). Délègue au notebook, sans copie du modèle."""
        import engine

        with self._lock:
            core = engine.load_engine(self.cfg)
            if not getattr(core, "MODEL_READY", False):
                if not hasattr(core, "BUDGET"):
                    raise RuntimeError("Budget d'appels absent : moteur non initialisé")
                core.ensure_model_loaded()

    def get_model(self):
        core = self._core()
        if core is None or not getattr(core, "MODEL_READY", False):
            return None, None
        return core.model, core.processor

    def runtime_info(self) -> dict:
        """Informations affichées dans « Détails techniques » (lecture seule, aucun import lourd)."""
        core = self._core()
        info = {"model_path": str(self.cfg.model_path), "engine_loaded": core is not None,
                "model_loaded": self.is_model_loaded(), "gpus_nvidia_smi": gpu_inventory()["gpus"]}
        if core is not None:
            environment = getattr(core, "ENVIRONMENT", None) or {}
            diagnostics = getattr(core, "MODEL_DIAGNOSTICS", None) or {}
            info.update({
                "model_class": diagnostics.get("model_class"),
                "processor_class": diagnostics.get("processor_class"),
                "model_dtype": diagnostics.get("model_dtype"),
                "model_load_time_s": getattr(core, "MODEL_LOAD_TIME_S", None),
                "parameters_by_device": diagnostics.get("parameters_by_device"),
                "gpus_torch": [{"name": g.get("name"), "total_memory_gib": round(g.get("total_memory_gib", 0), 1),
                                "bf16": g.get("bf16_supported")} for g in environment.get("gpus", [])],
                "cuda_runtime": environment.get("cuda_runtime"),
                "fatal_cuda": bool((getattr(core, "RUNTIME_STATE", {}) or {}).get("fatal_cuda")),
                "thinking_disabled_verified": (getattr(core, "TEMPLATE_STATE", {}) or {}).get("thinking_disable_verified"),
            })
        return info
