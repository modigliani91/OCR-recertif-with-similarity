"""Interface Streamlit — KYC Recertification (couche de présentation uniquement).

Aucune logique métier ici : l'interface téléverse l'archive, démarre un traitement via le
JobManager, affiche la progression publiée par le pipeline et propose le téléchargement du
rapport Excel exact écrit par le notebook. Le modèle n'est jamais chargé par ce fichier.
"""
from __future__ import annotations

import streamlit as st

st.set_page_config(page_title="KYC Recertification — AI Document Control", page_icon="🗂️", layout="centered")

try:
    from config import APP_TITLE, load_config, pipeline_versions
    from exceptions import ConcurrentRunError, KycAppError
    from file_manager import human_size, inspect_zip, readiness
    from job_manager import get_job_manager
    from logging_config import configure_logging, get_logger
    from model_manager import ModelManager
    from progress import STAGES, TERMINAL_STATES
    from reporting import XLSX_MIME, read_report_for_download

    CFG = load_config()
except Exception as startup_error:  # manifeste absent, variable d'environnement invalide…
    st.title("KYC Recertification — AI Document Control")
    st.error("Configuration incomplète : l'application ne peut pas démarrer.")
    st.caption(str(startup_error))
    st.stop()

LOGGER = configure_logging(CFG)

st.markdown(
    """
    <style>
      .block-container {max-width: 860px; padding-top: 2.2rem;}
      div[data-testid="stMetricValue"] {font-size: 1.7rem;}
      .kyc-muted {color: #6b7280; font-size: 0.9rem;}
      .kyc-step {font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.92rem; line-height: 1.7;}
    </style>
    """,
    unsafe_allow_html=True,
)


def full_width(widget) -> dict:
    """Largeur pleine compatible avec Streamlit 1.37 (use_container_width) et récent (width="stretch")."""
    import inspect

    parameters = inspect.signature(widget).parameters
    return {"width": "stretch"} if "width" in parameters else {"use_container_width": True}


@st.cache_resource(show_spinner=False)
def jobs():
    LOGGER.info("Démarrage de l'application (processus unique) | %s", CFG.describe())
    return get_job_manager(CFG)


@st.cache_resource(show_spinner=False)
def models():
    return ModelManager(CFG)


@st.cache_data(ttl=60, show_spinner=False)
def cached_readiness():
    return [check.__dict__ for check in readiness(CFG)]


def render_stage_list(snapshot: dict) -> str:
    lines = []
    done = set(snapshot.get("completed_stages") or [])
    for index, (key, label, _) in enumerate(STAGES, 1):
        if key in done:
            mark = "✓"
        elif key == snapshot.get("stage") and snapshot.get("state") == "RUNNING":
            mark = "▶"
        elif key == snapshot.get("stage"):
            mark = "✕" if snapshot.get("state") in {"FAILED", "REPORT_FAILED"} else "■"
        else:
            mark = "○"
        lines.append(f"{mark}&nbsp; {index} — {label}")
    return "<div class='kyc-step'>" + "<br>".join(lines) + "</div>"


def render_progress(snapshot: dict) -> None:
    fraction = float(snapshot.get("fraction") or 0.0)
    st.progress(fraction, text=f"{fraction * 100:.0f} %")
    index, label = snapshot.get("stage_index") or 0, snapshot.get("stage_label") or "Préparation"
    with st.status(f"Étape {index}/9 — {label}", state="running", expanded=True):
        st.markdown(render_stage_list(snapshot), unsafe_allow_html=True)
        counters = snapshot.get("counters") or {}
        total = counters.get("customers_total")
        if total:
            done = {"RAW_OCR": counters.get("customers_ocr_done"),
                    "STRUCTURE": counters.get("structured_customers_done"),
                    "CONFORMITY": counters.get("conformity_customers_done")}.get(snapshot.get("stage"))
            if done is not None:
                st.write(f"**Clients traités : {done} / {total}**")
        if counters.get("documents_ocr_total"):
            st.write(f"Documents OCR : {counters.get('documents_ocr_done', 0)} / {counters['documents_ocr_total']}"
                     f" · pages : {counters.get('pages_ocr_done', 0)} / {counters.get('pages_ocr_total', 0)}")
        if snapshot.get("stage_message"):
            st.caption(snapshot["stage_message"])
    minutes, seconds = divmod(int(snapshot.get("elapsed_s") or 0), 60)
    hours, minutes = divmod(minutes, 60)
    st.caption(f"Durée écoulée : {hours:d} h {minutes:02d} min {seconds:02d} s · "
               "fermer ou rafraîchir le navigateur n'interrompt pas le traitement.")
    if snapshot.get("cancel_requested"):
        st.info("Arrêt demandé : il sera appliqué à la fin du document en cours.")


if hasattr(st, "fragment"):
    @st.fragment(run_every=2.0)
    def live_progress() -> None:
        snapshot = jobs().snapshot()
        if snapshot is None or snapshot.get("state") != "RUNNING":
            st.rerun()  # traitement terminé : réaffichage complet (résultats et téléchargement)
        render_progress(snapshot)
else:  # Streamlit < 1.37 : rafraîchissement manuel
    def live_progress() -> None:
        snapshot = jobs().snapshot()
        if snapshot:
            render_progress(snapshot)
        st.button("Actualiser la progression")


def render_results(metadata: dict) -> None:
    state = metadata.get("state")
    message = metadata.get("user_message") or ""
    if state == "SUCCESS":
        st.success("Traitement terminé")
    elif state == "SUCCESS_WITH_ANOMALIES":
        st.warning("Traitement terminé avec anomalies")
    elif state == "CANCELLED":
        st.info(message)
    elif state == "INTERRUPTED":
        st.warning(message)
        return
    else:
        st.error(message or "Le traitement a échoué.")
    if metadata.get("success"):
        col1, col2, col3 = st.columns(3)
        col1.metric("Clients analysés", metadata.get("customers_processed", 0))
        col2.metric("Documents analysés", metadata.get("documents_processed", 0))
        col3.metric("Revue manuelle requise", metadata.get("manual_review_count", 0))
        col4, col5, col6, col7 = st.columns(4)
        col4.metric("Dossiers conformes", metadata.get("conform_count", 0))
        col5.metric("Non conformes", metadata.get("non_conform_count", 0))
        col6.metric("À recontrôler", metadata.get("recheck_count", 0))
        col7.metric("Références non trouvées", metadata.get("reference_not_found_count", 0))
        if metadata.get("processing_error_count"):
            st.caption(f"{metadata['processing_error_count']} dossier(s) en erreur de traitement — voir le rapport.")
        for anomaly in metadata.get("anomalies") or []:
            st.caption("• " + anomaly)
        data = read_report_for_download(CFG.report_path, metadata.get("report_sha256"), metadata.get("report_mtime_ns"))
        if data is not None:
            st.markdown("Rapport généré avec succès.")
            st.download_button("Télécharger kyc_recertification_control.xlsx", data=data,
                               file_name="kyc_recertification_control.xlsx", mime=XLSX_MIME, type="primary",
                               **full_width(st.download_button))
        else:
            st.warning("Le rapport sur disque ne correspond plus à ce traitement (il a été régénéré ou déplacé). "
                       "Relancer le traitement pour obtenir un rapport validé.")


def render_technical(metadata: dict | None) -> None:
    with st.expander("Détails techniques"):
        versions = pipeline_versions()
        st.write({**versions, "NOTEBOOK_SHA256": versions["NOTEBOOK_SHA256"][:16] + "…"})
        st.write({"configuration": CFG.describe(), "modèle": models().runtime_info()})
        if metadata:
            st.write({key: metadata.get(key) for key in (
                "run_id", "state", "status", "error_kind", "run_mode", "input_filename", "started_at", "finished_at",
                "elapsed_seconds", "qwen_calls", "model_loaded_this_run", "raw_pages_processed", "documents_missing",
                "duplicates_skipped", "engine_session_id", "report_sha256", "report_size_bytes")})
            technical = metadata.get("technical") or {}
            if technical:
                st.write({"pipeline": technical})
        st.caption(f"Journal application : {CFG.app_log_path} · journal moteur : {CFG.logs_dir / 'kyc_structured.log'}")


# ======================================================================================
st.title(APP_TITLE)
st.markdown("Analyse automatisée des documents KYC et rapprochement avec la base de recertification.")

checks = cached_readiness()
blocking = [c for c in checks if c["blocking"] and not c["ok"]]
warnings = [c for c in checks if not c["blocking"] and not c["ok"]]
if blocking:
    st.error("Configuration incomplète")
    for check in blocking:
        st.caption(f"✕ {check['name']} — {check['detail']}")
else:
    st.success("Application prête")
for check in warnings:
    st.caption(f"⚠ {check['name']} : non confirmé ({check['detail']}). Le traitement échouera si une inférence GPU est nécessaire.")

manager = jobs()
snapshot = manager.snapshot()
running = bool(snapshot and snapshot.get("state") == "RUNNING")
foreign = manager.foreign_lock()
busy = running or bool(foreign)

# --- 1. Chargement -------------------------------------------------------------------------
st.subheader("1. Chargement du dossier KYC")
uploaded = st.file_uploader("Archive ZIP des dossiers clients (un sous-dossier par Id tiers)", type=["zip"],
                            disabled=busy, key="zip_uploader")
inspection = None
if uploaded is not None:
    signature = (uploaded.name, uploaded.size, getattr(uploaded, "file_id", None))
    if st.session_state.get("upload_signature") != signature:
        inspection = inspect_zip(uploaded, uploaded.name, uploaded.size)
        st.session_state["upload_signature"] = signature
        st.session_state["upload_inspection"] = inspection.to_dict()
        LOGGER.info("Upload reçu : %s (%d octets) — %s", uploaded.name, uploaded.size,
                    "valide" if inspection.valid else "refusé : " + "; ".join(inspection.errors))
    inspection = st.session_state["upload_inspection"]
    col1, col2, col3 = st.columns([3, 1.3, 2])
    col1.markdown(f"**Nom fichier**  \n{uploaded.name}")
    col2.markdown(f"**Taille**  \n{human_size(uploaded.size)}")
    if inspection["valid"]:
        col3.markdown(f"**Statut ZIP**  \n✓ Archive valide  \n<span class='kyc-muted'>{inspection['customer_count']} "
                      f"dossiers clients · {inspection['target_pdf_count']} PDF cibles</span>", unsafe_allow_html=True)
        for warning in inspection["warnings"]:
            st.caption("ⓘ " + warning)
    else:
        col3.markdown("**Statut ZIP**  \n✕ Archive refusée")
        st.error("L'archive ZIP n'est pas valide. " + " ".join(inspection["errors"]))

# --- 2. Lancement --------------------------------------------------------------------------
st.subheader("2. Lancer le traitement")
can_start = bool(uploaded is not None and inspection and inspection["valid"] and not blocking and not busy)
if busy:
    st.info("Un traitement est déjà en cours.")
launch = st.button("Lancer le traitement", type="primary", disabled=not can_start, **full_width(st.button))
if launch and can_start:
    try:
        run_id = manager.start(uploaded, uploaded.name)
        st.session_state["active_run_id"] = run_id
        LOGGER.info("run=%s lancé depuis l'interface", run_id)
        st.rerun()
    except ConcurrentRunError:
        st.warning("Un traitement est déjà en cours.")
    except KycAppError as error:
        LOGGER.warning("Lancement refusé : %s", error.detail or error)
        st.error(error.user_message)
if running:
    if st.button("Arrêter après le document en cours", help="Arrêt propre entre deux documents : les checkpoints "
                 "sont conservés et réutilisés au prochain lancement. L'inférence GPU en cours n'est pas coupée."):
        manager.request_cancel()
        st.rerun()
elif foreign:
    st.caption(f"Traitement détenu par un autre processus (run {foreign.get('run_id')}, démarré {foreign.get('started_at')}).")

# --- 3. Progression --------------------------------------------------------------------------
st.subheader("3. Progression")
if running:
    live_progress()
elif snapshot is not None:
    st.progress(float(snapshot.get("fraction") or 0.0))
    st.markdown(render_stage_list(snapshot), unsafe_allow_html=True)
else:
    st.caption("Aucun traitement en cours.")

# --- 4. Résultats ----------------------------------------------------------------------------
st.subheader("4. Résultats")
last = manager.last_run()
if running:
    st.caption("Les résultats s'afficheront à la fin du traitement.")
elif last and last.get("state") in TERMINAL_STATES | {"INTERRUPTED"}:
    st.caption(f"Traitement {last.get('run_id')} · {last.get('input_filename') or ''} · terminé {last.get('finished_at') or ''}")
    render_results(last)
else:
    st.caption("Aucun résultat disponible.")

render_technical(last)
