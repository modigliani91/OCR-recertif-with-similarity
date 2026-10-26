"""Exceptions de l'application, chacune associée à un message compréhensible par l'utilisateur.

Le détail technique (trace complète) est écrit dans les journaux ; seule ``user_message``
est affichée dans l'interface.
"""
from __future__ import annotations

from config import ConfigurationError as _BaseConfigurationError


class KycAppError(Exception):
    """Base des erreurs applicatives présentables à l'utilisateur."""

    kind = "APPLICATION_ERROR"
    default_message = "Une erreur inattendue est survenue. Les détails sont dans le journal de l'application."

    def __init__(self, detail: str = "", user_message: str | None = None):
        super().__init__(detail or user_message or self.default_message)
        self.detail = detail
        self.user_message = user_message or self.default_message


class InvalidZipError(KycAppError):
    kind = "INVALID_ZIP"
    default_message = "L'archive ZIP n'est pas valide."


class MissingReferenceFileError(KycAppError):
    kind = "MISSING_REFERENCE"
    default_message = "Le fichier de référence recertication_lissage_updated.csv est introuvable."


class InvalidReferenceFileError(KycAppError):
    kind = "INVALID_REFERENCE"
    default_message = ("Le fichier de référence recertication_lissage_updated.csv est illisible "
                       "ou ne contient pas les colonnes attendues.")


class ModelLoadError(KycAppError):
    kind = "MODEL_LOAD"
    default_message = "Le modèle Qwen3.8 local n'a pas pu être chargé. Vérifier le chemin du modèle et l'environnement."


class CudaUnavailableError(KycAppError):
    kind = "CUDA_UNAVAILABLE"
    default_message = "Le GPU requis pour l'analyse n'est pas disponible."


class PipelineExecutionError(KycAppError):
    kind = "PIPELINE_EXECUTION"
    default_message = "Le traitement a été interrompu. Les résultats déjà sauvegardés pourront être réutilisés."


class ReportGenerationError(KycAppError):
    kind = "REPORT_GENERATION"
    default_message = "Le traitement des documents est terminé mais la génération du rapport a échoué."


class ConcurrentRunError(KycAppError):
    kind = "CONCURRENT_RUN"
    default_message = "Un traitement est déjà en cours."


class AppConfigurationError(KycAppError):
    kind = "CONFIGURATION"
    default_message = "La configuration de l'application est incomplète. Consulter « Détails techniques »."


class PipelineCancelled(KeyboardInterrupt):
    """Arrêt demandé par l'utilisateur, levé UNIQUEMENT entre deux documents/clients.

    Hérite de KeyboardInterrupt volontairement : le notebook traite déjà l'interruption
    Jupyter (KeyboardInterrupt) comme un arrêt propre — statut INTERRUPTED, manifestes et
    exports RAW écrits dans ses blocs finally, checkpoints réutilisables. L'application
    réutilise exactement ce chemin validé, sans ajouter de logique d'arrêt au moteur.
    """

    user_message = ("Traitement arrêté à votre demande entre deux documents. "
                    "Les résultats déjà sauvegardés seront réutilisés au prochain lancement.")


ConfigurationError = _BaseConfigurationError
