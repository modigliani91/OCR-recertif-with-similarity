"""Qwen SIMULÉ pour les tests d'intégration sans GPU.

Remplace uniquement les points d'appel GPU du notebook (chargement, ``infer_image``,
``infer_text``, ``infer_structured_image``, inspection AutoConfig du dépôt). Toute la logique
du notebook autour (rendu des pages, validation des transcriptions, checkpoints, règles,
extraction ancrée, validation, similarité, matching, rapports) s'exécute réellement.

La « transcription » renvoyée est la couche texte du PDF synthétique ; les réponses
structurées simulées ne contiennent aucun champ (les valeurs viennent des règles du notebook).
Les résultats métier ne sont donc PAS représentatifs de Qwen : seul le câblage est testé.
"""
from __future__ import annotations

import time
from collections import Counter

CALLS: Counter = Counter()


def install_fake_qwen(core, page_delay_s: float = 0.0) -> None:
    import fitz

    def fake_ensure_model_loaded():
        if core.RUNTIME_STATE["fatal_cuda"]:
            raise core.FatalCudaError("Contexte CUDA inutilisable (simulation)")
        if core.BUDGET.used >= core.BUDGET.limit:
            raise core.BudgetExceeded("Budget épuisé (simulation)")
        CALLS["ensure_model_loaded"] += 1

    def fake_inspect_local_repository():
        info = core.repository_manifest(core.CFG.MODEL_PATH)
        info["resolved_model_class"] = "DRY_RUN_SIMULATED"
        return info

    def _finish(result: dict, text: str, tokens_in: int = 512) -> None:
        result["call_number"] = core.BUDGET.consume()
        result["qwen_called"] = True
        result.update(tokens_in=tokens_in, tokens_out=max(1, len(text.split())), stop_reason="eos",
                      reached_max_new_tokens=False, generation_time_s=0.01, tokens_per_second=100.0,
                      first_step_logits={"nan": False}, thinking_tokens_emitted=False,
                      raw_generated_text_with_special_tokens=text, total_time_s=0.01,
                      gpu_memory_allocated_before_mb=0.0, gpu_memory_allocated_after_mb=0.0, gpu_memory_reserved_mb=0.0)

    def fake_infer_image(image, result):
        with fitz.open(result["full_path"]) as pdf:
            text = pdf[result["page_index"]].get_text().strip()
        if page_delay_s:
            time.sleep(page_delay_s)
        result["raw_ocr"] = text
        _finish(result, text)
        CALLS["infer_image"] += 1

    def fake_infer_text(prompt, result):
        text = '{"document_type": "UNKNOWN", "type_evidence": null, "fields": {}}'
        result.update(raw_model_response=text, generated_token_ids=[], token_logprobs=[],
                      score_collection_status="UNAVAILABLE")
        _finish(result, text)
        CALLS["infer_text"] += 1

    def fake_infer_structured_image(image, result, prompt, max_new_tokens, structured=True):
        text = '{"document_type": "UNKNOWN"}'
        result.update(raw_ocr=text, generated_token_ids=[], token_logprobs=[], score_collection_status="UNAVAILABLE")
        _finish(result, text)
        CALLS["infer_structured_image"] += 1

    core.ensure_model_loaded = fake_ensure_model_loaded
    core.inspect_local_repository = fake_inspect_local_repository
    core.infer_image = fake_infer_image
    core.infer_text = fake_infer_text
    core.infer_structured_image = fake_infer_structured_image
