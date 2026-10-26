# KYC Recertification — AI Document Control (Domino Data Lab)

Streamlit application that runs the validated notebook
`kyc_qwen38_production_kyc_pipeline_name_similarity_v2.ipynb` on an uploaded
`kyc_documents_test.zip` and delivers `/mnt/output_qwen38/reports/kyc_recertification_control.xlsx`.

The notebook remains the **source of truth**. Its code is extracted, unchanged, into an importable
module; the application only adds upload handling, orchestration, progress, locking and download.

---

## 1. Architecture

```
Browser ──► Streamlit app.py (UI only, French interface)
              │  st.cache_resource → one JobManager + one ModelManager per process
              ▼
         job_manager.py ── background thread (survives refresh / tab close)
              │  + in-process mutex + file lock /mnt/output_qwen38/.kyc_processing.lock
              ▼
         pipeline.py  run_kyc_pipeline(zip) → PipelineResult
              │  preflight (CRC, reference, model dir, /mnt) → reset per-run state
              ▼
         engine/notebook_core.py   ← GENERATED from the notebook (47 code cells, in order)
              _nb_stage_inventory()            = notebook cell 11 (execution part)
              _nb_stage_pass_a()               = notebook cell 22 (RAW OCR + benchmark)
              _nb_stage_pass_b()               = notebook cell 37 (structured extraction)
              _nb_stage_matching_and_reports() = notebook cell 43 (CSV, matching, Excel, audit)
              │  engine/instrumentation.py wraps 15 high-level notebook functions to emit
              │  ProgressEvents (no function body changed)
              ▼
         /mnt/output_qwen38/...  (notebook checkpoints, JSON, CSV, Excel — unchanged layout)
```

**Why Streamlit.** The workload is upload → long job → progress → metrics → download. Streamlit covers
this natively (`st.file_uploader`, `st.progress`, `st.status`, `st.fragment` auto-refresh,
`st.download_button`), Domino documents it as a supported App framework, and nothing in the notebook
conflicts with it. Two Streamlit behaviours are handled explicitly:

* **Reruns.** Streamlit re-executes `app.py` on every interaction and *interrupts* the session's script.
  The pipeline therefore never runs in the session script: it runs in one background thread owned by a
  process-level `JobManager`. Refreshing or closing the browser does not stop processing.
* **Model reloads.** The model lives in the engine module globals (`model`, `processor`, `MODEL_READY`),
  imported once per process (`sys.modules`). `--server.fileWatcherType=none` prevents module reloads.
  One Streamlit process only — no multi-worker server — so exactly one model copy is on the GPU.

**Why a generated engine module instead of hand-split modules.** The notebook's ~5,400 lines share one
global namespace (e.g. `CFG`, `DIRS`, `BUDGET`, and later cells extend `TYPE_PHRASES`, `STRUCTURED_PROMPTS`
before `EXTRACTION_FINGERPRINT` is computed). Splitting cells across Python modules would change name
resolution and cache fingerprints — a real regression risk. `engine/build_engine.py` copies every cell
byte-for-byte into one module (like one Jupyter kernel), verifies it with the AST, and fails if the
notebook's cell layout ever changes. Application concerns live in separate, small modules.

---

## 2. Project files

```
<project root>/                      (/mnt for DFS projects, /mnt/code for Git-based projects)
├── kyc_qwen38_production_kyc_pipeline_name_similarity_v2.ipynb   reference notebook (source of truth)
├── app.sh                           Domino App entry point → scripts/run_app.sh
├── app.py                           Streamlit UI (presentation only)
├── config.py                        paths, env overrides, versions (INPUT_ZIP_PATH, OUTPUT_ROOT, REPORT_PATH)
├── exceptions.py                    InvalidZipError, MissingReferenceFileError, ModelLoadError, CudaUnavailableError,
│                                    PipelineExecutionError, ReportGenerationError, ConcurrentRunError, PipelineCancelled
├── logging_config.py                app log /mnt/output_qwen38/logs/kyc_app.log (+ stdout)
├── progress.py                      ProgressEvent, 9 stages, ProgressTracker (thread-safe, status.json), CancelToken
├── file_manager.py                  ZIP pre-checks, atomic upload write, readiness checks, run metadata
├── run_lock.py                      file lock with PID/host/run_id/heartbeat + stale detection (CLI: python -m run_lock)
├── model_manager.py                 façade over the engine's single model (state, GPU info, explicit load)
├── pipeline_context.py              PipelineContext, PipelineResult dataclasses
├── pipeline.py                      run_kyc_pipeline(), error classification, CLI (python pipeline.py --zip …)
├── job_manager.py                   background run thread, lock heartbeat, last-run persistence
├── reporting.py                     report validation (exists, size, mtime ≥ run start, sheets, SHA-256), KPIs
├── engine/
│   ├── __init__.py                  load_engine() once, integrity checks, reset_run_state()
│   ├── build_engine.py              notebook → notebook_core.py generator (+ --check)
│   ├── instrumentation.py           progress/cancellation wrappers around notebook functions
│   ├── notebook_core.py             GENERATED — do not edit
│   └── engine_manifest.json         GENERATED — notebook SHA-256, cell map, versions, per-run state names
├── scripts/
│   ├── run_app.sh                   launch Streamlit (0.0.0.0, port 8888 default, single process)
│   ├── validate_environment.sh      environment checks without loading the 27B model
│   ├── cleanup_runtime.sh           safe temp cleanup (dry run by default), lock status / stale-lock break
│   ├── build_engine.sh              regenerate / check the engine
│   └── run_tests.sh                 pytest suite
├── tools/
│   ├── validate_environment.py      used by validate_environment.sh
│   ├── gpu_smoke_test.py            real model load + one real page inference, logs "GPU SMOKE"
│   ├── regression_compare.py        notebook outputs vs application outputs
│   └── cleanup_runtime.py           used by cleanup_runtime.sh
├── tests/                           unit tests, synthetic data, simulated Qwen, dry-run & equivalence harnesses
├── .streamlit/config.toml           headless, no telemetry, XSRF/CORS off for the Domino proxy
├── requirements.txt                 light app dependencies only
├── requirements-dev.txt             + pytest
└── environment-prerequisites.txt    GPU stack provided by the Domino environment (not for pip)
```

---

## 3. Notebook → module mapping

| Notebook component (cell) | Python module | Function / class |
|---|---|---|
| §0 Configuration `Config`, `StructuredConfig`, `CFG` (1) | `engine/notebook_core.py` (verbatim) + `config.py` | `StructuredConfig`; `AppConfig.engine_overrides()` supplies paths/mode only |
| §1–2 Package checks, imports (2–3) | `engine/notebook_core.py` | verbatim (IPython `display` fallback) |
| §3 Business rules, anomaly catalog (4) | `engine/notebook_core.py` | `BUSINESS_RULES`, `ANOMALY_CATALOG` |
| §4 Directories, session state (5) | `engine/notebook_core.py`, `engine/__init__.py` | `DIRS`; `reset_run_state()` per run |
| §9 Safe ZIP extraction (10) | `engine/notebook_core.py`; pre-check in `file_manager.py` | `safe_extract_zip()`; `inspect_zip()`, `full_integrity_check()` |
| §10 Families & same-size duplicates (11) | `engine/notebook_core.py` | `build_inventory()`, `resolve_documents()` |
| §11 Customer discovery (12) | `engine/notebook_core.py` | `prepare_inventory()`; execution → `_nb_stage_inventory()` |
| §12–14 Rendering, RAW prompt, template / `enable_thinking=False` (13–15) | `engine/notebook_core.py` | `render_page()`, `prepare_image()`, `RAW_OCR_PROMPT`, `apply_template()`, `inspect_template()` |
| §15–16 Qwen3.8 loading, FP8→BF16, GPU placement (16–17) | `engine/notebook_core.py` (sole owner); façade `model_manager.py` | `ensure_model_loaded()`, `model_diagnostics()`; `ModelManager.ensure_loaded()/runtime_info()/get_model()` |
| §17–18 Degeneration guard, RAW inference (18–19) | `engine/notebook_core.py` | `detect_degenerate_output()`, `infer_image()`, `run_attempt()` |
| §19–20 RAW checkpoints, RAW OCR all customers (20–21) | `engine/notebook_core.py` | `raw_for_page()`, `run_raw_ocr_document()`, `process_raw_dataset()` |
| §21 Cold/warm benchmark (22) | `engine/notebook_core.py` | `run_raw_preflight()` |
| §22 Pass A execution (23) | `engine/notebook_core.py` → `pipeline.py` | `_nb_stage_pass_a()` |
| §23–31 Structured extraction (24–32) | `engine/notebook_core.py` | `deterministic_page_extraction()`, `structured_for_page()`, `infer_text()`, `structured_attempt()` |
| §32–35 Name checks, validation, aggregation, document JSON (33–36) | `engine/notebook_core.py` | `validate_identity()`, `aggregate_document()`, `build_document_result()` |
| §36 Structured extraction all customers (37) | `engine/notebook_core.py` | `process_structured_dataset()` |
| §37 Pass B execution (38) | `engine/notebook_core.py` → `pipeline.py` | `_nb_stage_pass_b()` |
| §38 Reference CSV (39) | `engine/notebook_core.py`; pre-validation in `pipeline.py` | `load_reference_csv()` |
| §38 bis FATCA / signature name similarity ≥ 75 % (40) | `engine/notebook_core.py` | `compute_proper_name_similarity()`, `match_handwritten_name_to_reference()` |
| §39 Conformity, carton rule name OR account (41) | `engine/notebook_core.py` | `match_document()`, `reduce_database_comparisons()`, `aggregate_family()` |
| §39 bis Similarity self-tests (42) | `engine/notebook_core.py` | `run_name_similarity_tests()` — executed at engine import (fail fast) |
| §40 Matching all customers (43) | `engine/notebook_core.py` | `process_customer()`, `process_all_customers()` |
| §41–42 Excel, reporting, final audit (44–46) | `engine/notebook_core.py`; checks in `reporting.py` | `export_reports()`, `final_audit()`; `validate_report()`, `build_summary()` |
| §43 Matching & reports execution (47) | `engine/notebook_core.py` → `pipeline.py` | `_nb_stage_matching_and_reports()` |
| Full pipeline | `pipeline.py` | `run_kyc_pipeline()` |
| Progress callbacks | `engine/instrumentation.py`, `progress.py` | wrappers → `ProgressEvent` → `ProgressTracker` |

### Migration notes (regression-risk audit)

* **Retained unchanged** — all 47 code cells, in notebook order, byte-for-byte (41 cells untouched; checked
  by `build_engine.py --check` and `tests/test_engine_build.py`): prompts, Qwen calls, FP8 config,
  `enable_thinking=False`, rendering, preprocessing, duplicate rule, checkpoints, extraction, FATCA and
  carton logic, 75 % threshold, exact account matching, manual-review rules, conformity, Excel layout.
* **Moved into functions** — the 4 execution cells (end of cell 11, cells 22, 37, 43) became
  `_nb_stage_*()` with their exact statements and a `global` declaration for the names they assign.
* **Adapted for application configuration** — one line: `CFG = StructuredConfig(**_APP_ENGINE_OVERRIDES)`
  (OUTPUT_DIR, ZIP_PATH, EXTRACT_DIR, REFERENCE_CSV, RUN_MODE=FULL_PIPELINE, INPUT_SOURCE=ZIP;
  MODEL_PATH only if `KYC_MODEL_PATH` is set). `ZIP_PATH` is set per run to the uploaded file (it does
  not enter any fingerprint). `IPython.display` gets a print fallback.
* **Adapted for progress** — nothing inside the notebook functions. 15 high-level functions are wrapped
  in the engine namespace (event before/after, cancellation check before a new document/customer).
* **Per-run state** — `reset_run_state()` re-creates exactly what cell 5 creates in a fresh kernel
  (SESSION_ID, ERRORS, ATTEMPTS, budget, summaries…) and deletes previous stage outputs, while keeping
  the loaded model. Cache fingerprints (`RAW_FINGERPRINT`, `EXTRACTION_FINGERPRINT`, matching) are
  identical to the notebook's, so notebook checkpoints are reusable by the app and vice versa.

---

## 4. Requirements

| Provided by the Domino GPU compute environment (do **not** pip-install from the app) | Light app dependencies (`requirements.txt`) |
|---|---|
| Python ≥ 3.10, PyTorch with CUDA (BF16), Transformers ≥ 4.57 (`AutoModelForImageTextToText`, `FineGrainedFP8Config`), Accelerate ≥ 0.30, NVIDIA drivers, local Qwen3.8 repository (EDV) | `streamlit>=1.37,<2`, plus the notebook's existing minimums: numpy, pandas, Pillow, PyMuPDF, openpyxl, packaging, psutil |

GPU: one H100 (80 GB). The checkpoint is FP8 but is **dequantized to BF16 at load** (≈54 GB of weights
plus vision tower, KV cache and buffers). No pip install ever runs inside the application.

## 5. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `KYC_OUTPUT_ROOT` | `/mnt/output_qwen38` | notebook outputs; report at `reports/kyc_recertification_control.xlsx` |
| `KYC_PERSISTENT_OUTPUT_DIR` | unset | Dataset/EDV path; `run_app.sh` links `KYC_OUTPUT_ROOT` to it (see §7 step 7) |
| `KYC_APP_ROOT` | `/mnt/kyc_app` | `input/` (uploaded ZIP), `extracted/` (safe extraction) |
| `KYC_REFERENCE_CSV` | first existing of `/mnt/recertication_lissage_updated.csv`, `/mnt/recertification_lissage_updated.csv`, `<project>/recertication_lissage_updated.csv` | reference base |
| `KYC_MODEL_PATH` | notebook value `/domino/edv/modelhub/ModelHub-model-huggingface-Qwen/Qwen3.8-27B-FP8/main` | local model |
| `KYC_RUN_MODE` | `FULL_PIPELINE` | `DIAGNOSTIC` = notebook diagnostic mode (1 customer) for smoke tests |
| `PORT` / `KYC_APP_PORT` | `8888` | Domino Apps serve on 8888 |
| `KYC_MAX_UPLOAD_MB` | `4096` | max ZIP size (the upload is held in memory by Streamlit before being written) |
| `KYC_LOCK_STALE_AFTER_S` / `KYC_LOCK_HEARTBEAT_S` | `900` / `30` | stale-lock detection |
| `KYC_STREAMLIT_BASE_URL_PATH` | unset | only if your Domino proxy does not strip the App route prefix |

Set them in **Project settings → Environment variables** (or export them before `app.sh`).

---

## 6. Application workflow

1. Open the App. The banner shows **Application prête** or **Configuration incomplète** (output/input
   writable, reference present, model `config.json` present, engine matches notebook; GPU via `nvidia-smi`
   as a warning). The model is **not** loaded at startup.
2. **1. Chargement du dossier KYC** — upload `kyc_documents_test.zip`. Shown: file name, size, status
   (`✓ Archive valide`, customers, target PDFs) or the reason for rejection (not a ZIP, empty, `../` or
   absolute paths, links, no customer folder, no target document, encrypted PDFs).
3. **2. Lancer le traitement** — explicit click. The ZIP is written atomically (temp file → fsync →
   rename) to `/mnt/kyc_app/input/<original name>` under the processing lock; the original name is kept
   because the notebook detects the customer root folder from it.
4. **3. Progression** — progress bar, `Étape n/9`, stage checklist, `Clients traités : x / y`,
   `Documents OCR : a / b · pages`. Refreshes every 2 s; closing the tab does not stop the run.
   Optional **Arrêter après le document en cours** (see §9).
5. **4. Résultats** — `Traitement terminé` / `Traitement terminé avec anomalies`, KPIs (clients, documents,
   conformes, non conformes, à recontrôler, références non trouvées, revue manuelle) read from the
   pipeline's own results, then **Télécharger kyc_recertification_control.xlsx** — the exact file written by
   the notebook, verified by SHA-256 and mtime against the end-of-run validation.
6. **Détails techniques** — APP_VERSION, PIPELINE_VERSION and other versions, model/GPU state, run id,
   Qwen calls, pages, errors, paths, log locations.

Stages: 1 Validation de l'archive · 2 Extraction ZIP · 3 Inventaire clients · 4 Inventaire documents ·
5 RAW OCR · 6 Extraction structurée · 7 Matching base clients (reference load/index) ·
8 Contrôles de conformité (per-customer matching and conformity) · 9 Génération du rapport Excel (+ audit).

**Input format.** `kyc_documents_test.zip` → `kyc_documents_test/<Id tiers>/<DOCUMENT>.PDF` (or customer
folders at the ZIP root). Documents: `JUSTIFICATIF IDENTITE.PDF`, `JUSTIFICATIF DOMICILE.PDF`,
`CONVENTION COMPTE.PDF`, `FATCA.PDF`, `CARTON SIGNATUTE.PDF` (alias `CARTON SIGNATURE.PDF`), duplicates `(1)`.

**Output.** Unchanged notebook layout under `/mnt/output_qwen38/` (`inventory/`, `raw_ocr/`, `checkpoints/`,
`customers/`, `global/*.csv`, `reports/kyc_recertification_control.xlsx`, `logs/`) plus
`app_runs/<run_id>/run.json`, `status.json` and `app_runs/last_run.json` (run id, start/end, input ZIP,
report path, SHA-256, KPIs).

---

## 7. Domino deployment — step by step

**Step 1 — Create/import the project files.** In Domino, open (or create) the project. DFS project: the
project root is `/mnt`; Git-based project: code is at `/mnt/code`. Upload the delivered archive through
*Files → Upload*, or in a workspace terminal:
```bash
unzip -o kyc_app_domino.zip -d /tmp/kyc_delivery
cp -a /tmp/kyc_delivery/kyc_app/. /mnt/          # /mnt/code/ for a Git-based project
cd /mnt && ls app.sh app.py engine/notebook_core.py
```
Domino looks for `app.sh` at the **project root**: keep `app.sh`, `app.py` and the other files together
at that level (if you keep a `kyc_app/` sub-folder, create a root `app.sh` containing
`exec bash kyc_app/scripts/run_app.sh`).

**Step 2 — Notebook and reference.** The notebook must stay next to `app.py` (integrity check). Put the
reference at `/mnt/recertication_lissage_updated.csv` or set `KYC_REFERENCE_CSV` (mandatory for Git-based
projects, e.g. `/mnt/data/<dataset>/recertication_lissage_updated.csv`).

**Step 3 — Python files.** Already in the archive. Confirm the generated engine matches the notebook:
```bash
bash scripts/build_engine.sh --check
```

**Step 4 — Shell scripts.**
```bash
chmod +x app.sh scripts/*.sh
```

**Step 5 — Environment.** Use the same GPU compute environment as the notebook and add Streamlit to it
(*Environments → Edit Dockerfile*): `RUN pip install "streamlit>=1.37,<2"`, then rebuild. Do not
reinstall torch/CUDA. In a workspace on that environment:
```bash
pip install -r requirements.txt          # only if Streamlit is not in the image yet (workspace test)
bash scripts/validate_environment.sh     # Python, packages, torch+CUDA+H100+BF16, Transformers classes,
                                         # model config/AutoConfig (no weights), /mnt, reference, engine
```

**Step 6 — Model path.**
```bash
ls -l /domino/edv/modelhub/ModelHub-model-huggingface-Qwen/Qwen3.8-27B-FP8/main/config.json
python -c "import json;c=json.load(open('/domino/edv/modelhub/ModelHub-model-huggingface-Qwen/Qwen3.8-27B-FP8/main/config.json'));print(c.get('model_type'),c.get('quantization_config'))"
```
The EDV must be mounted for the App as well as the workspace (project *External Data Volumes*).

**Step 7 — `/mnt` folders and permissions.**
```bash
mkdir -p /mnt/output_qwen38 /mnt/kyc_app/input /mnt/kyc_app/extracted
touch /mnt/output_qwen38/.w && rm /mnt/output_qwen38/.w && df -h /mnt
```
Persistence: Domino Apps do not write their local file changes back to the project and re-hydrate files
on restart. Reports are downloaded from the UI, but to keep **checkpoints** across App restarts, set
`KYC_PERSISTENT_OUTPUT_DIR` to a Dataset/EDV path (e.g. `/mnt/data/kyc_outputs/output_qwen38`): `run_app.sh`
then makes `/mnt/output_qwen38` a link to it, so the business path stays exactly
`/mnt/output_qwen38/reports/kyc_recertification_control.xlsx`. (Do not commit an `output_qwen38/` folder to
the project files, otherwise the link cannot be created; the script warns.)

**Step 8 — Interactive tests (workspace, GPU tier).**
```bash
bash scripts/run_tests.sh               # 43 tests incl. end-to-end dry-runs with simulated Qwen (no model load)
python tools/gpu_smoke_test.py          # real Qwen3.8 load + one real page inference; prints RÉUSSI/ÉCHEC
KYC_RUN_MODE=DIAGNOSTIC KYC_OUTPUT_ROOT=/mnt/output_qwen38_diag \
  python pipeline.py --zip /mnt/kyc_app/input/kyc_documents_test.zip   # notebook DIAGNOSTIC mode, 1 customer
```

**Step 9 — Manual Streamlit launch.**
```bash
PORT=8501 bash scripts/run_app.sh &
curl -s localhost:8501/_stcore/health     # → ok
```
If your workspace exposes ports through Jupyter server proxy, open `…/proxy/8501/`; stop with `kill %1`.
Stop it before publishing (the GPU must not hold two model copies).

**Step 10 — App launch command.** Domino runs `app.sh` from the project root:
```bash
bash app.sh        # = bash scripts/run_app.sh  (0.0.0.0:8888, single Streamlit process)
```
In *Deployments → Apps → Publish/Create App*, select the project, the GPU environment from Step 5, and
`app.sh` as the launch file if your Domino version asks for one.

**Step 11 — Hardware tier.** Select the H100 GPU tier (≥ 80 GB GPU memory, enough host RAM for the
low-CPU-memory load). Keep one App instance.

**Step 12 — Deploy.** Add the environment variables (Step 7, `KYC_REFERENCE_CSV` if needed), restrict access
to authorized users (KYC data — not "Anyone can access"), click *Publish*, wait for *Running*, *View App*.

**Step 13 — Smoke test.**
1. Banner shows *Application prête*.
2. Upload `kyc_documents_test.zip` → `✓ Archive valide` with the expected customer count.
3. Click *Lancer le traitement*; the uploader and button disable; a second tab shows *Un traitement est déjà en cours.*
4. App logs show the model loading once: `grep "GPU SMOKE" /mnt/output_qwen38/logs/kyc_app.log`.
5. Progress moves through the 9 stages with client/document counters.
6. `ls -l /mnt/output_qwen38/reports/kyc_recertification_control.xlsx` (timestamp = this run).
7. Download from the browser; open it in Excel.

**Step 14 — Real ZIP.** Run the production archive. Check *Détails techniques* (Qwen calls, pages,
errors) and `app_runs/last_run.json`. A second run of the same ZIP reuses RAW checkpoints (few or no Qwen calls).

**Step 15 — Verify Excel generation/download.**
```bash
sha256sum /mnt/output_qwen38/reports/kyc_recertification_control.xlsx
grep "RAPPORT validé" /mnt/output_qwen38/logs/kyc_app.log | tail -1     # same SHA-256 as the download
python -c "import json;print(json.load(open('/mnt/output_qwen38/app_runs/last_run.json'))['report_sha256'])"
```

---

## 8. Testing

* `bash scripts/run_tests.sh` — upload (valid/invalid/empty/traversal/absolute/no-folder/root PDF/names),
  atomic write, writable paths, lock (free/active/dead owner/old heartbeat/corrupt), report
  (missing/empty/stale/missing sheets/valid/download checksum), progress, cancellation, engine fidelity,
  error messages, and three integration tests (skipped without torch/transformers):
  `tests/dryrun_harness.py --scenario full|cancel` and `tests/notebook_equivalence_harness.py`.
  They run the real notebook engine end to end with a **simulated Qwen** (text layer of synthetic PDFs);
  they test plumbing, not OCR quality.
* **GPU smoke test** — `python tools/gpu_smoke_test.py`; evidence in logs:
  `GPU SMOKE | H100 détecté=True`, `modèle chargé en …s`, `device map validée (paramètres par device)={'cuda:0': …}`,
  `inférence CUDA OK`. During normal App runs the first model load logs one `GPU SMOKE | GPUs=… | parameters_by_device=…` line.

## 9. Restart, resume and cancellation

* **Checkpoints are the durable state** (notebook design, unchanged): RAW per page, structured per page,
  matching per customer, keyed by PDF SHA-256, model signature and versions. Re-running the same ZIP reuses
  them; only missing work calls Qwen. The benchmark is cached once validated.
* **Browser refresh / tab close** — no effect on the run; reopening shows live progress or the last result
  (`app_runs/last_run.json`).
* **App restart during a run** — the run stops; the lock is detected stale (dead owner / old heartbeat) and
  quarantined automatically on the next start; *Le traitement précédent a été interrompu* is shown;
  relaunch the same ZIP to resume from checkpoints (persisted only if `KYC_PERSISTENT_OUTPUT_DIR` is set).
* **Cancellation** — *Arrêter après le document en cours* sets a flag checked **only before a new
  document (RAW) or customer (structure, matching)**. The GPU generation in progress is never interrupted.
  It raises an interruption handled by the notebook's own `KeyboardInterrupt` path (status `INTERRUPTED`,
  manifests and exports written in its `finally` blocks); the lock is released and the next run resumes.
* **Fatal CUDA error** — like the notebook, the process must be restarted (App *Restart*); checkpoints remain.

## 10. Logs

* `/mnt/output_qwen38/logs/kyc_app.log` — app events (start, upload, validation, stage changes, completion,
  failures with full tracebacks, report SHA-256). No OCR text or customer names.
* `/mnt/output_qwen38/logs/kyc_structured.log`, `errors_<session>.json`, `environment_*.json`,
  `model_diagnostics_*.json`, `template_*.json` — notebook logs, unchanged.
* Domino App logs (stdout) — notebook progress prints and app log lines.

## 11. Regression validation (notebook vs application)

Business results must be identical on the same `kyc_documents_test.zip`.

**A. Logic equivalence (recommended first; isolates the refactoring from GPU nondeterminism).**
1. In the notebook (Jupyter, H100 workspace), set in the config cell: `RUN_MODE="FULL_PIPELINE"`,
   `OUTPUT_DIR=Path("/mnt/output_qwen38_notebook")`, `ZIP_PATH=Path("/mnt/kyc_regression/kyc_documents_test.zip")`,
   `EXTRACT_DIR=Path("/mnt/kyc_regression/extracted_nb")`, same `REFERENCE_CSV`; run all cells.
2. Copy its checkpoints and run the application pipeline on the copy (it reuses identical RAW/structured caches):
   ```bash
   cp -a /mnt/output_qwen38_notebook /mnt/output_qwen38_app_regression
   KYC_OUTPUT_ROOT=/mnt/output_qwen38_app_regression KYC_APP_ROOT=/mnt/kyc_regression/app \
     python pipeline.py --zip /mnt/kyc_regression/kyc_documents_test.zip
   python tools/regression_compare.py --reference /mnt/output_qwen38_notebook \
     --candidate /mnt/output_qwen38_app_regression --json /mnt/kyc_regression/comparison.json
   ```
**B. Independent end-to-end.** Same as A without the copy (fresh `KYC_OUTPUT_ROOT`): the app redoes all
inference. Greedy decoding is deterministic in principle, but GPU kernels can introduce rare token-level
differences; investigate any reported difference by comparing the RAW texts of the pages concerned.

The comparator checks population (customers, selected/missing/duplicate documents), customer statuses
(families, matches, manual review, consistency), document statuses (OCR, type, biometric, expiry, extracted
values), FATCA and CARTON SIGNATURE similarity scores (unrounded), decisions and reasons, the similarity
summary, Excel sheets and row counts of business sheets. Exit code 0 = **ÉQUIVALENT**.
`tests/notebook_equivalence_harness.py` runs the same comparison automatically (original `.ipynb` cells vs
app, simulated Qwen): 348 checks, 0 differences at delivery.

Business rules preserved and verifiable in the comparison: FATCA and CARTON SIGNATURE proper-name
similarity ≥ 75 % (inclusive, unrounded, `MATCHING_VERSION KYC_MATCHING_HANDWRITTEN_NAME_SIM_75_V1`),
exact account match, carton conformant if reliable name ≥ 75 % **or** reliable exact account.

## 12. Troubleshooting

| Symptom | Checks / actions |
|---|---|
| Streamlit does not start | App logs: `[run_app] ERREUR …`. `bash scripts/validate_environment.sh`; `python -c "import streamlit"`; `bash scripts/build_engine.sh --check` |
| Wrong port / App never "Running" | Domino expects 0.0.0.0:8888: do not set `PORT` unless required; `grep "Adresse/port" ` in logs; `curl localhost:8888/_stcore/health` from a workspace test |
| Upload fails with 403 | XSRF must be off behind the Domino proxy: `--server.enableXsrfProtection=false` is in `run_app.sh`; do not override it |
| Model path missing | `ls $KYC_MODEL_PATH/config.json` (default notebook path); EDV attached to the project and mounted in the App |
| CUDA not visible | `nvidia-smi`; `python -c "import torch;print(torch.cuda.is_available())"`; App hardware tier must be GPU |
| H100 unavailable | `nvidia-smi --query-gpu=name,memory.total --format=csv`; choose the H100 tier; the notebook only warns for other GPUs but needs ≥ ~60 GB free |
| Reference CSV missing | banner shows the path tried; set `KYC_REFERENCE_CSV`; check `ls -l` and the 5 exact columns (`validate_environment.sh`) |
| ZIP invalid | the UI states the reason; expected `<zip name>/<Id tiers>/<DOCUMENT>.PDF`; `unzip -l kyc_documents_test.zip | head` |
| Permissions under `/mnt` | `touch /mnt/output_qwen38/.w`; `ls -ld /mnt /mnt/output_qwen38 /mnt/kyc_app`; set `KYC_OUTPUT_ROOT`/`KYC_APP_ROOT` to a writable Dataset if needed |
| Out of memory | another process holds the GPU (`nvidia-smi`): stop workspaces/notebook kernels with Qwen loaded; one App instance only; never run Streamlit manually while the App runs |
| Old lock file / *Un traitement est déjà en cours* with nothing running | `bash scripts/cleanup_runtime.sh --lock-status`; stale locks are quarantined automatically; `bash scripts/cleanup_runtime.sh --break-stale-lock`; last resort `python -m run_lock --break --force` after checking no run is active |
| Report missing / *génération du rapport a échoué* | `grep -n "ÉCHEC\|RAPPORT" /mnt/output_qwen38/logs/kyc_app.log`; disk space `df -h`; the report must be newer than the run start; relaunch (checkpoints reused) |
| *Le contexte GPU est devenu inutilisable* | restart the App; checkpoints are reused |
| *Le moteur d'analyse ne correspond pas au notebook* | notebook changed: `bash scripts/build_engine.sh`, review `engine/engine_manifest.json`, re-run tests, restart |

Temporary cleanup (dry run by default; never touches checkpoints, outputs, reference or model):
`bash scripts/cleanup_runtime.sh` then `--apply`.

## 13. Security considerations

* Local inference only: `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `local_files_only=True` (notebook);
  no external AI API; no internet dependency.
* No telemetry: `browser.gatherUsageStats=false`; `browser.serverAddress=localhost` prevents Streamlit's
  startup external-IP lookup (an outbound call to checkip.amazonaws.com otherwise).
* ZIP handling: pre-checks reject traversal/absolute/drive paths, links, special entries, encrypted PDFs;
  extraction is still done only by the notebook's `safe_extract_zip` (sizes, ratios, staging, manifest).
* The UI displays only aggregate results and stage counters (`Client 37/200`), never OCR text or names;
  error details and tracebacks go to logs only (`client.showErrorDetails=false`).
* Restrict App access to authorized users in Domino; KYC outputs stay in Domino storage.
* XSRF protection is disabled because Streamlit uploads fail behind the Domino proxy otherwise; access
  control relies on Domino App permissions.

## 14. Updating the notebook

1. Validate the change in the notebook. 2. `bash scripts/build_engine.sh` (fails if cells were added/removed/
reordered: update `CELL_PLAN` in `engine/build_engine.py` after review). 3. `bash scripts/run_tests.sh`
and the regression procedure. 4. Restart the App. Never edit `engine/notebook_core.py` by hand.
