"""Journalisation de l'application (fichier sous /mnt/output_qwen38/logs + sortie standard Domino).

Le moteur du notebook conserve son propre journal ``logs/kyc_structured.log`` (logger
``kyc_structured``, inchangé). Ce module ne journalise jamais de contenu OCR ni de nom client :
uniquement des événements opérationnels, des identifiants de run et des compteurs.
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
import threading

from config import AppConfig

APP_LOGGER_NAME = "kyc_app"
_CONFIGURED = threading.Lock()
_MARKER = "_kyc_app_handler"


def configure_logging(cfg: AppConfig, level: int = logging.INFO) -> logging.Logger:
    """Idempotent : Streamlit réexécute app.py à chaque interaction, les handlers ne sont ajoutés qu'une fois."""
    logger = logging.getLogger(APP_LOGGER_NAME)
    with _CONFIGURED:
        if any(getattr(h, _MARKER, False) for h in logger.handlers):
            return logger
        logger.setLevel(level)
        logger.propagate = False
        formatter = logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s")
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        setattr(console, _MARKER, True)
        logger.addHandler(console)
        try:
            cfg.logs_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                cfg.app_log_path, maxBytes=20 * 1024 * 1024, backupCount=10, encoding="utf-8")
            file_handler.setFormatter(formatter)
            setattr(file_handler, _MARKER, True)
            logger.addHandler(file_handler)
        except OSError as exc:  # /mnt non inscriptible : l'indicateur de disponibilité le signalera.
            logger.warning("Journal fichier indisponible (%s) : sortie standard uniquement.", exc)
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(APP_LOGGER_NAME if not name else f"{APP_LOGGER_NAME}.{name}")
