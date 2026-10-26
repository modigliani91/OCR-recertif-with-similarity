"""Points d'observation de progression autour des fonctions de haut niveau du notebook.

Aucune fonction du notebook n'est modifiée : chaque fonction listée ci-dessous est remplacée,
dans l'espace de noms du moteur, par une enveloppe qui (1) émet un ``ProgressEvent``,
(2) consulte la demande d'arrêt AVANT un nouveau document/client, puis (3) appelle la fonction
d'origine avec les mêmes arguments et renvoie son résultat inchangé. Les boucles du notebook
appelant ces fonctions par leur nom global, les enveloppes sont utilisées sans autre changement.

Un callback défaillant n'interrompt jamais le pipeline (erreur journalisée seulement).
"""
from __future__ import annotations

import functools
import logging
import threading
from collections import Counter

from progress import ProgressEvent

LOGGER = logging.getLogger("kyc_app.engine")
_ORIGINALS: dict[str, object] = {}
_LOCK = threading.Lock()


class _RunState:
    def __init__(self, sink, cancel_token):
        self.sink = sink
        self.cancel_token = cancel_token
        self.stage: str | None = None
        self.in_final_audit = False
        self.model_loaded_during_run = False
        self.counters: Counter = Counter()
        self.customer_positions: dict[str, int] = {}
        self.customer_total = 0
        self.docs_seen_by_customer: Counter = Counter()
        self.docs_expected_by_customer: Counter = Counter()


_ACTIVE: dict[str, _RunState | None] = {"run": None}


def activate(sink, cancel_token=None) -> None:
    _ACTIVE["run"] = _RunState(sink, cancel_token)


def deactivate() -> None:
    _ACTIVE["run"] = None


def current() -> _RunState | None:
    return _ACTIVE["run"]


def original(name: str):
    """Fonction d'origine du notebook, sans observation (ex. validation préalable de la référence)."""
    return _ORIGINALS[name]


def _emit(stage: str, message: str, current_value=None, total=None, customer_id=None, **counters) -> None:
    run = _ACTIVE["run"]
    if run is None:
        return
    run.stage = stage
    try:
        run.sink(ProgressEvent(stage=stage, message=message, current=current_value, total=total,
                               customer_id=customer_id, counters=counters))
    except Exception:  # noqa: BLE001 — l'observation ne doit jamais casser le traitement
        LOGGER.exception("Callback de progression en échec (ignoré)")


def _check_cancel() -> None:
    run = _ACTIVE["run"]
    if run is not None and run.cancel_token is not None:
        run.cancel_token.raise_if_requested()


def _customer_position(core, customer_id: str) -> tuple[int, int]:
    run = _ACTIVE["run"]
    if run is None:
        return 0, 0
    if not run.customer_positions:
        selected = list(getattr(core, "CUSTOMERS_SELECTED", []) or [])
        run.customer_positions = {cid: i + 1 for i, cid in enumerate(selected)}
        run.customer_total = len(selected)
    return run.customer_positions.get(customer_id, 0), run.customer_total


def _wrappers(core) -> dict:
    """Fabriques d'enveloppes, indexées par le nom exact de la fonction du notebook."""

    def safe_extract_zip(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            _emit("EXTRACT", "Extraction sécurisée de l'archive (chemins, liens, tailles et ratios contrôlés)")
            manifest = fn(*args, **kwargs)
            files = len(manifest.get("files", []))
            _emit("EXTRACT", f"{len(manifest.get('customers', []))} dossiers clients et {files} PDF cibles extraits",
                  files, files)
            return manifest
        return wrapper

    def build_inventory(fn):
        @functools.wraps(fn)
        def wrapper(manifest, *args, **kwargs):
            total = len(manifest.get("customers", []))
            _emit("CUSTOMERS", f"Inventaire de {total} dossiers clients", 0, total)
            rows = fn(manifest, *args, **kwargs)
            _emit("CUSTOMERS", f"{total} dossiers clients inventoriés", total, total, customers_total=total)
            return rows
        return wrapper

    def resolve_documents(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            _emit("DOCUMENTS", "Sélection d'une instance par famille et taille (doublons tracés)")
            selections = fn(*args, **kwargs)
            found = sum(bool(s.get("document_found")) for s in selections)
            duplicates = sum(int(s.get("duplicate_count") or 0) for s in selections)
            missing = sum(not s.get("document_found") for s in selections)
            _emit("DOCUMENTS", f"{found} PDF sélectionnés · {duplicates} doublon(s) écarté(s) · {missing} document(s) manquant(s)",
                  found, found, documents_selected=found, documents_missing=missing, duplicates_skipped=duplicates)
            return selections
        return wrapper

    def run_raw_preflight(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            _emit("RAW_OCR", "Contrôle préalable GPU (benchmark froid/chaud, réutilisé s'il est déjà validé)")
            return fn(*args, **kwargs)
        return wrapper

    def ensure_model_loaded(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            was_ready = bool(getattr(core, "MODEL_READY", False))
            run = _ACTIVE["run"]
            if not was_ready and run is not None:
                _emit(run.stage or "RAW_OCR", "Chargement du modèle Qwen3.8 sur GPU (une seule fois par processus)…")
            result = fn(*args, **kwargs)
            if not was_ready and getattr(core, "MODEL_READY", False):
                if run is not None:
                    run.model_loaded_during_run = True
                    _emit(run.stage or "RAW_OCR", "Modèle chargé")
                diagnostics = getattr(core, "MODEL_DIAGNOSTICS", {}) or {}
                environment = getattr(core, "ENVIRONMENT", {}) or {}
                LOGGER.info("GPU SMOKE | GPUs=%s | model_class=%s | load_time_s=%.1f | parameters_by_device=%s | dtypes=%s",
                            [g.get("name") for g in environment.get("gpus", [])], diagnostics.get("model_class"),
                            float(getattr(core, "MODEL_LOAD_TIME_S", 0.0) or 0.0),
                            diagnostics.get("parameters_by_device"), list((diagnostics.get("parameters_by_dtype") or {}).keys()))
            return result
        return wrapper

    def process_raw_dataset(fn):
        @functools.wraps(fn)
        def wrapper(customer_ids, *args, **kwargs):
            run = _ACTIVE["run"]
            ids = set(customer_ids)
            selections = [s for s in core.SELECTED_DOCUMENTS if s["customer_id"] in ids]
            docs = sum(bool(s.get("document_found")) for s in selections)
            pages = sum(int(s.get("page_count") or 0) for s in selections if s.get("document_found"))
            if run is not None:
                run.docs_expected_by_customer = Counter(s["customer_id"] for s in selections)
                run.counters.update(raw_docs_total=docs, raw_pages_total=pages)
            _emit("RAW_OCR", f"RAW OCR : {len(ids)} clients · {docs} PDF · {pages} pages", 0, docs,
                  customers_total=len(ids), documents_ocr_total=docs, documents_ocr_done=0,
                  pages_ocr_total=pages, pages_ocr_done=0, customers_ocr_done=0)
            return fn(customer_ids, *args, **kwargs)
        return wrapper

    def run_raw_ocr_document(fn):
        @functools.wraps(fn)
        def wrapper(selection, *args, **kwargs):
            _check_cancel()  # frontière sûre : aucun document en cours
            run = _ACTIVE["run"]
            cid = selection["customer_id"]
            position, total = _customer_position(core, cid)
            result = fn(selection, *args, **kwargs)
            if run is not None:
                run.docs_seen_by_customer[cid] += 1
                if selection.get("document_found"):
                    run.counters["raw_docs_done"] += 1
                if result.get("reused"):  # document entier repris d'un checkpoint : pages non revisitées
                    reused_pages = len(result.get("pages") or [])
                    run.counters["raw_pages_done"] += reused_pages
                    run.counters["raw_pages_reused"] += reused_pages
                if run.docs_seen_by_customer[cid] == run.docs_expected_by_customer.get(cid, 0):
                    run.counters["raw_customers_done"] += 1
                _emit("RAW_OCR", f"Client {position}/{total} — documents OCR {run.counters['raw_docs_done']}/{run.counters['raw_docs_total']}",
                      run.counters["raw_docs_done"], run.counters["raw_docs_total"], cid,
                      documents_ocr_done=run.counters["raw_docs_done"], customers_ocr_done=run.counters["raw_customers_done"],
                      pages_ocr_done=run.counters["raw_pages_done"], pages_ocr_reused=run.counters["raw_pages_reused"])
            return result
        return wrapper

    def raw_for_page(fn):
        @functools.wraps(fn)
        def wrapper(page, *args, **kwargs):
            result = fn(page, *args, **kwargs)
            run = _ACTIVE["run"]
            if run is not None:
                run.counters["raw_pages_done"] += 1
                run.counters["raw_pages_reused"] += int(bool(result.get("reused")))
                position, total = _customer_position(core, page["customer_id"])
                suffix = " (réutilisée depuis checkpoint)" if result.get("reused") else ""
                _emit("RAW_OCR", f"Client {position}/{total} — page {page['page_number']}/{page['page_count']}{suffix}",
                      run.counters["raw_docs_done"], run.counters["raw_docs_total"], page["customer_id"],
                      pages_ocr_done=run.counters["raw_pages_done"], pages_ocr_reused=run.counters["raw_pages_reused"])
            return result
        return wrapper

    def process_structured_dataset(fn):
        @functools.wraps(fn)
        def wrapper(customer_ids, *args, **kwargs):
            _emit("STRUCTURE", f"Extraction structurée depuis le RAW sauvegardé : {len(customer_ids)} clients",
                  0, len(customer_ids), structured_customers_done=0, customers_total=len(customer_ids))
            return fn(customer_ids, *args, **kwargs)
        return wrapper

    def process_structured_customer(fn):
        @functools.wraps(fn)
        def wrapper(customer_id, *args, **kwargs):
            _check_cancel()
            run = _ACTIVE["run"]
            position, total = _customer_position(core, customer_id)
            result = fn(customer_id, *args, **kwargs)
            if run is not None:
                run.counters["structured_done"] += 1
                _emit("STRUCTURE", f"Clients traités : {run.counters['structured_done']} / {total}",
                      run.counters["structured_done"], total, customer_id,
                      structured_customers_done=run.counters["structured_done"])
            return result
        return wrapper

    def load_reference_csv(fn):
        @functools.wraps(fn)
        def wrapper(path, *args, **kwargs):
            _emit("MATCHING", "Chargement et indexation de la base de recertification")
            reference = fn(path, *args, **kwargs)
            _emit("MATCHING", f"Référence indexée : {reference.get('row_count', 0)} lignes", 1, 1,
                  reference_rows=reference.get("row_count", 0), reference_duplicate_ids=len(reference.get("duplicates", [])))
            return reference
        return wrapper

    def process_all_customers(fn):
        @functools.wraps(fn)
        def wrapper(customer_ids, *args, **kwargs):
            _emit("CONFORMITY", f"Rapprochement et conformité : {len(customer_ids)} clients", 0, len(customer_ids),
                  conformity_customers_done=0, customers_total=len(customer_ids))
            return fn(customer_ids, *args, **kwargs)
        return wrapper

    def process_customer(fn):
        @functools.wraps(fn)
        def wrapper(customer_id, *args, **kwargs):
            _check_cancel()
            run = _ACTIVE["run"]
            _, total = _customer_position(core, customer_id)
            result = fn(customer_id, *args, **kwargs)
            if run is not None:
                run.counters["conformity_done"] += 1
                _emit("CONFORMITY", f"Clients traités : {run.counters['conformity_done']} / {total}",
                      run.counters["conformity_done"], total, customer_id,
                      conformity_customers_done=run.counters["conformity_done"])
            return result
        return wrapper

    def export_reports(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            _emit("REPORT", "Génération des CSV et du classeur Excel", 0, 2)
            result = fn(*args, **kwargs)
            _emit("REPORT", "Classeur Excel écrit ; audit final en cours", 1, 2)
            return result
        return wrapper

    def final_audit(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            run = _ACTIVE["run"]
            if run is not None:
                run.in_final_audit = True
            _emit("REPORT", "Audit final de réconciliation de la population")
            result = fn(*args, **kwargs)
            if run is not None:
                run.in_final_audit = False
            return result
        return wrapper

    factories = locals()
    return {name: factories[name] for name in INSTRUMENTED_FUNCTIONS}


def install(core) -> None:
    """Installe les enveloppes une seule fois par processus (idempotent)."""
    with _LOCK:
        if getattr(core, "_APP_INSTRUMENTED", False):
            return
        for name, factory in _wrappers(core).items():
            function = getattr(core, name)
            _ORIGINALS[name] = function
            setattr(core, name, factory(function))
        core._APP_INSTRUMENTED = True


INSTRUMENTED_FUNCTIONS = (
    "safe_extract_zip", "build_inventory", "resolve_documents", "run_raw_preflight", "ensure_model_loaded",
    "process_raw_dataset", "run_raw_ocr_document", "raw_for_page", "process_structured_dataset",
    "process_structured_customer", "load_reference_csv", "process_all_customers", "process_customer",
    "export_reports", "final_audit",
)
