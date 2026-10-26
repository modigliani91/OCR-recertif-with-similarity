"""Jeu de données synthétique (fictif) pour les tests : PDF texte, archive ZIP, référence, faux dépôt modèle.

Aucune donnée réelle. Les PDF contiennent une couche texte que le Qwen simulé « transcrit ».
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

ARCHIVE_NAME = "kyc_documents_test.zip"
ROOT = "kyc_documents_test"

IDENTITY_DZ = [
    "REPUBLIQUE ALGERIENNE DEMOCRATIQUE ET POPULAIRE DZA",
    "CARTE NATIONALE D IDENTITE",
    "Nom : BENALI",
    "Prénom : MOHAMED",
    "Date de naissance : 15/02/1980",
    "Date d'expiration : 15/02/2030",
    "Nationalité : ALGERIENNE",
    "IDDZA1234567895<<<<<<<<<<<<<<<",
    "8002155M3002151DZA<<<<<<<<<<<6",
    "BENALI<<MOHAMED<<<<<<<<<<<<<<<",
]
IDENTITY_FR = [
    "REPUBLIQUE FRANCAISE",
    "PASSEPORT",
    "Nom : DUPONT",
    "Prénom : JEAN",
    "Date de naissance : 01/07/1975",
    "Date d'expiration : 31/01/2031",
    "Nationalité : FRANCAISE",
]


def _domicile(name: str, kind: str = "CERTIFICAT DE RESIDENCE") -> list[str]:
    return [kind, "Commune de Ain Benian", "Il est certifie que la personne ci-dessous reside a l adresse indiquee",
            "", "", name]


def _convention(name: str) -> list[str]:
    return ["CONVENTION DE COMPTE PARTICULIER", f"Nom & Prénom : {name}", "Conditions generales en annexe."]


def _fatca(surname: str, given: str) -> list[str]:
    return ["FORMULAIRE D'IDENTIFICATION US-PERSON AU REGARD DE LA LOI FATCA", "SOUSCRIPTEUR/ASSURE",
            f"Nom : {surname}", f"Prénom : {given}", "Etes-vous une US Person ? NON"]


def _signature(name: str, account: str) -> list[str]:
    return ["SPECIMEN DE SIGNATURE", f"Nom et Prénom : {name}", f"Numéro de compte : {account}", "Signature :"]


CUSTOMERS = {
    "1000123": {
        "JUSTIFICATIF IDENTITE.PDF": [IDENTITY_DZ],
        "JUSTIFICATIF DOMICILE.PDF": [_domicile("BENALI MOHAMED")],
        "CONVENTION COMPTE.PDF": [_convention("BENALI MOHAMED"), ["Page 2 : conditions tarifaires."]],
        "FATCA.PDF": [_fatca("BENALI", "MOHAMED")],
        "FATCA (1).PDF": [_fatca("BENALI", "MOHAMED")],  # doublon identique (même taille) : ignoré, tracé
        "CARTON SIGNATURE.PDF": [_signature("BENALI MOHAMEO", "001234")],  # alias + faute de lecture manuscrite
    },
    "1000456": {
        "JUSTIFICATIF IDENTITE.PDF": [IDENTITY_FR],
        "JUSTIFICATIF DOMICILE.PDF": [_domicile("DUPONT JEAN", "TITRE DE SEJOUR")],
        "CONVENTION COMPTE.PDF": [_convention("DUPONT JEAN")],
        "FATCA.PDF": [_fatca("DUPONT", "JEAN")],
        "CARTON SIGNATUTE.PDF": [_signature("DUPONT JEAN", "009999")],  # compte différent de la référence
    },
    "1000789": {  # carton absent, client absent de la référence
        "JUSTIFICATIF IDENTITE.PDF": [IDENTITY_DZ[:7]],  # sans MRZ : règle métier NON_BIOMETRIQUE
        "JUSTIFICATIF DOMICILE.PDF": [_domicile("KARIM AHMED")],
        "CONVENTION COMPTE.PDF": [_convention("KARIM AHMED")],
        "FATCA.PDF": [_fatca("KARIM", "AHMED")],
    },
}
REFERENCE_ROWS = [
    ["Id tiers", "Date de naissance", "Nom abrege tiers", "Date expiration document", "Numero de compte"],
    ["1000123", "1980-02-15", "BENALI MOHAMED", "2030-02-15", "001234"],
    ["1000456", "1975-07-01", "DUPONT JEAN", "2031-01-31", "009876"],
]


def make_pdf(path: Path, pages: list[list[str]]) -> None:
    import fitz

    path.parent.mkdir(parents=True, exist_ok=True)
    document = fitz.open()
    for lines in pages:
        page = document.new_page(width=595, height=842)
        y = 72
        for line in lines:
            page.insert_text((60, y), line, fontsize=11, fontname="helv")
            y += 22
    document.save(path, deflate=False, garbage=0, no_new_id=True)
    document.close()


def build_dataset(workdir: Path) -> dict:
    """Crée ZIP, référence et faux dépôt modèle ; renvoie leurs chemins."""
    workdir.mkdir(parents=True, exist_ok=True)
    source = workdir / "source"
    for customer, documents in CUSTOMERS.items():
        for filename, pages in documents.items():
            make_pdf(source / ROOT / customer / filename, pages)
    archive = workdir / ARCHIVE_NAME
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
        for path in sorted(source.rglob("*")):
            handle.write(path, path.relative_to(source).as_posix())
    reference = workdir / "recertication_lissage_updated.csv"
    reference.write_text("\n".join(";".join(row) for row in REFERENCE_ROWS) + "\n", encoding="utf-8-sig")
    model = workdir / "fake_model"
    model.mkdir(exist_ok=True)
    (model / "config.json").write_text(json.dumps({
        "model_type": "dry_run_vl", "architectures": ["DryRunForConditionalGeneration"],
        "vision_config": {"depth": 1}, "quantization_config": {"quant_method": "fp8"}}), encoding="utf-8")
    return {"zip": archive, "reference": reference, "model": model}


def make_zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as handle:
        for name, data in entries.items():
            handle.writestr(name, data)
    return path
