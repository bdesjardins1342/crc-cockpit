#!/usr/bin/env python3
"""
analyser_soumission.py
Analyseur de soumissions pour entrepreneur général au Québec (marchés publics).
Génère 9 livrables Markdown par projet en utilisant l'API Claude.
"""

import os
import sys
import json
import hashlib
import logging
import argparse
import time
import tempfile
import re
import difflib
from collections import defaultdict
from pathlib import Path
from datetime import datetime
from typing import Optional


# ---------------------------------------------------------------------------
# Vérification et installation des dépendances
# ---------------------------------------------------------------------------

DEPENDANCES = {
    "anthropic": "anthropic",
    "pdfplumber": "pdfplumber",
    "extract_msg": "extract-msg",
    "rich": "rich",
}


def verifier_dependances() -> None:
    manquantes = []
    for module, paquet in DEPENDANCES.items():
        try:
            __import__(module)
        except ImportError:
            manquantes.append(paquet)

    if manquantes:
        print(f"[INFO] Installation des dépendances manquantes : {', '.join(manquantes)}")
        import subprocess
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet"] + manquantes
        )
        print("[INFO] Dépendances installées. Relancez le script.")
        sys.exit(0)


verifier_dependances()

import anthropic  # noqa: E402
import pdfplumber  # noqa: E402
import extract_msg  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.table import Table  # noqa: E402

import warnings  # noqa: E402
warnings.filterwarnings("ignore")
logging.getLogger("pdfminer").setLevel(logging.ERROR)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

RACINE_PROJETS = Path(r"C:\Users\BenoitDesjardins\Documents\Claude\Projet 2026")
DOSSIERS_SOURCE = ["Addenda", "Plan & Devis", "Soumissions reçues"]
DOSSIER_ANALYSE = "Analyse"
REGISTRE_FICHIER = "registre.json"
LOG_FICHIER = "log_analyse.txt"

MODELE_CLAUDE = "claude-sonnet-4-6"
MAX_TOKENS = 4000
MAX_TOKENS_01_08 = 60000   # Appel 1 — sections analytiques 01 à 08
MAX_TOKENS_09 = 16000      # Appel 2 — rapport estimateur 09
MAX_RETRY = 3
DELAI_RETRY_BASE = 5       # secondes — multiplié par le numéro de tentative
DELAI_RATE_LIMIT = 30      # secondes entre tentatives rate-limit
MAX_CONTEXTE_CHARS = 800_000  # Limite corpus final après condensation (chars)

# Mots-clés pour le filtre AUTRE (> 5 000 cars)
MOTS_CLES_CONTRAT = [
    "délai", "pénalité", "penalite", "retenue", "responsabilité", "responsabilite",
    "garantie", "cautionnement", "assurance", "bsdq", "résiliation", "resiliation",
    "amiante", "coordination", "obligatoire", "interdit", "requis",
]

# Ordre d'assemblage du corpus (valeur = priorité, 1 = tête)
TYPE_ORDRE: dict[str, int] = {
    "ADDENDA":          2,
    "CONTRAT":          3,
    "RÉGIE":            4,
    "FORMULAIRE":       5,
    "DEVIS_ARCH":       6,
    "DEVIS_ADMIN":      7,
    "SOUMISSION_REÇUE": 8,
    "AUTRE":            9,
    "PLANS":           10,  # exclu du corpus Claude
}

# Patterns DEVIS_ARCH — filtrage 3 couches
_RE_SECTION_MF = re.compile(
    r"(?:^|\n)((?:0[2-9]|[1-9]\d)\s+\d{2}\s+\d{2})\s*[-–—]?\s*([A-ZÀÂÉÈÊËÎÏÔÙÛÜ][^\n]{3,60})",
    re.MULTILINE,
)
_RE_DEVIS_PARTIE1 = re.compile(
    r"^\s*(?:PARTIE\s*1\b|1[\s.]+G[EÉ]N[EÉ]RALIT|GÉNÉRALITÉS?\s*$|GENERALITES?\s*$)",
    re.IGNORECASE,
)
_RE_DEVIS_PARTIE2 = re.compile(
    r"^\s*(?:PARTIE\s*2\b|2[\s.]+PRODUITS?|PRODUITS?\s*$)",
    re.IGNORECASE,
)
_RE_DEVIS_PARTIE3 = re.compile(
    r"^\s*(?:PARTIE\s*3\b|3[\s.]+EX[EÉ]CUTION|EX[EÉ]CUTION\s*$)",
    re.IGNORECASE,
)
_RE_DEVIS_CRITIQUE = re.compile(
    r"p[eé]nalit[eé]|\$/jour|%/jour|point.{0,4}arr[eê]t|BSDQ"
    r"|amiante|PCI\b|infection\s+nosocomial|milieu\s+occup[eé]"
    r"|\b\d{1,2}\s+(?:janvier|f[eé]vrier|mars|avril|mai|juin|juillet"
    r"|ao[uû]t|septembre|octobre|novembre|d[eé]cembre)\s+\d{4}\b",
    re.IGNORECASE,
)


def _construire_regexes_devis(structure: dict) -> tuple:
    """Construit les patterns de détection depuis la structure renvoyée par detecter_structure_devis."""
    TITRE = r'([A-ZÀÂÉÈÊËÎÏÔÙÛÜ][^\n]{3,60})'
    fmt = structure.get("section_format", "").strip()
    if re.match(r'^\d{6}$', fmt):          # "093000"
        re_section = re.compile(
            r"(?:^|\n)((?:0[2-9]|[1-9]\d)\d{4})\s*[-–—]?\s*" + TITRE,
            re.MULTILINE,
        )
    elif re.match(r'^\d+\.\d+', fmt):      # "9.3" ou "09.30"
        re_section = re.compile(
            r"(?:^|\n)((?:0?[2-9]|[1-9]\d)\.\d+(?:\.\d+)?)\s*[-–—]?\s*" + TITRE,
            re.MULTILINE,
        )
    else:                                   # "09 30 00" ou inconnu -> défaut
        re_section = _RE_SECTION_MF

    def _re_partie(marker: str, fallback: re.Pattern) -> re.Pattern:
        if marker and marker.strip():
            return re.compile(r"^\s*" + re.escape(marker.strip()), re.IGNORECASE)
        return fallback

    re_p1 = _re_partie(structure.get("p1_marker", ""), _RE_DEVIS_PARTIE1)
    re_p2 = _re_partie(structure.get("p2_marker", ""), _RE_DEVIS_PARTIE2)
    re_p3 = _re_partie(structure.get("p3_marker", ""), _RE_DEVIS_PARTIE3)
    return re_section, re_p1, re_p2, re_p3


PROMPT_SYSTEME = (
    "Tu es un auditeur contractuel expert pour entrepreneur général au Québec (marchés publics). "
    "Analyse en français. Pour chaque section, cite le document source + section/page. "
    "Marque AMBIGU/ABSENT si une info manque. Analyse exhaustive, sceptique et exploitable."
)

# (nom_fichier, titre_affichage)
SECTIONS = [
    ("01_table_documentaire.md",    "Table documentaire"),
    ("02_délais_échéancier.md",     "Délais et échéancier"),
    ("03_pénalités_retenues.md",    "Pénalités et retenues"),
    ("04_assurances_garanties.md",  "Assurances et garanties"),
    ("05_responsabilités_EG.md",    "Responsabilités de l'EG"),
    ("06_risques_techniques.md",    "Risques techniques"),
    ("07_BSDQ_sous-traitance.md",   "BSDQ et sous-traitance"),
    ("08_soumissions_reçues.md",    "Soumissions reçues"),
    ("09_rapport_estimateur.md",    "Rapport de l'estimateur"),
]

PROMPTS_SECTIONS: dict[str, str] = {
    "01_table_documentaire.md": """\
Produis une table documentaire complète de tous les documents analysés.

Format : tableau Markdown avec colonnes :
| Nom du fichier | Type | Source (dossier) | Description sommaire | Pages/Taille |

- Si un dossier est VIDE ou ABSENT, indique-le explicitement sous le tableau.
- Liste chaque PDF et email séparément, ainsi que leurs pièces jointes PDF.
""",

    "02_délais_échéancier.md": """\
Analyse tous les délais et l'échéancier du projet.

Identifie et documente :
- Date limite de dépôt de soumission
- Date de début des travaux
- Durée / date de fin contractuelle
- Jalons intermédiaires
- Périodes de mobilisation / démobilisation
- Délais de livraison de matériaux à long délai d'approvisionnement
- Délais d'approbation des dessins d'atelier
- Contraintes saisonnières

Pour chaque élément, cite le document source + section/page.
Marque AMBIGU si des délais sont contradictoires entre documents.
Marque ABSENT si une information est introuvable.
""",

    "03_pénalités_retenues.md": """\
Analyse toutes les clauses de pénalités et retenues.

Identifie et documente :
- Dommages liquidés (montant $/jour)
- Pénalités pour retard de toute nature
- Retenues de garantie (% et durée)
- Conditions de libération des retenues
- Bonus d'avance (le cas échéant)
- Pénalités pour non-conformité ou défaut d'exécution
- Mécanismes de réclamation et délais de préavis

Pour chaque clause, cite le document + section/page.
Marque AMBIGU si les conditions sont floues. Marque ABSENT si manquant.
""",

    "04_assurances_garanties.md": """\
Analyse toutes les exigences d'assurances et de garanties.

Identifie et documente :
- Assurance responsabilité civile générale (montant, conditions, endossements)
- Assurance chantier tous risques (valeur, franchise)
- Cautionnement d'exécution (% du contrat)
- Cautionnement pour gages, matériaux et services
- Garantie de soumission (montant/forme)
- Durée des garanties sur les travaux (par corps de métier)
- Exigences d'assurance imposées aux sous-traitants
- Assurance professionnelle requise (si applicable)

Pour chaque exigence, cite le document + section/page.
Marque AMBIGU si les conditions sont floues. Marque ABSENT si manquant.
""",

    "05_responsabilités_EG.md": """\
Analyse les responsabilités spécifiques de l'entrepreneur général.

Identifie et documente :
- Coordination et surveillance des sous-traitants
- Responsabilité sur les dessins d'atelier et fiches techniques
- Gestion du chantier (clôtures, accès, sécurité, stationnement)
- Obligations environnementales (gestion des déchets, bruit, poussière)
- Rapports et documentation exigés (journaux, photos, rapports mensuels)
- Interface avec le donneur d'ouvrage / surveillance professionnelle
- Obligations de nettoyage, déblaiement et mise en service
- Obligations légales propres au Québec (CCQ, RBQ, CNESST, LEED, etc.)
- Responsabilités vis-à-vis des occupants si milieu occupé

Pour chaque responsabilité, cite le document + section/page.
Marque AMBIGU si la portée est floue. Marque ABSENT si manquant.
""",

    "06_risques_techniques.md": """\
Analyse les risques techniques du projet.

Identifie et documente chaque risque avec :
| Risque | Description | Probabilité (F/M/É) | Impact (F/M/É) | Mitigation possible | Source |

Catégories à couvrir :
- Conditions de sol / fondations / eau souterraine
- Travaux en milieu occupé ou actif
- Interférences avec structures ou équipements existants
- Contraintes d'accès ou de livraison au chantier
- Matériaux à long délai d'approvisionnement
- Technologies spécialisées ou nouvelles
- Travaux de décontamination ou de démolition à risques
- Coordination avec services publics (Hydro, Bell, Gaz Métro)
- Risques météorologiques / saisonniers
- Plans incomplets, contradictions entre documents ou addenda tardifs

Cite le document + section/page. Marque AMBIGU si le risque est mal défini.
""",

    "07_BSDQ_sous-traitance.md": """\
Analyse les aspects BSDQ et sous-traitance du projet.

Identifie et documente :
- Spécialités assujetties au BSDQ (liste complète)
- Date limite de dépôt BSDQ (heure et fuseau horaire)
- Exigences de qualification / certification des sous-traitants
- Restrictions sur le choix des sous-traitants (liste approuvée, exclusions)
- Clauses de remplacement de sous-traitants en cours de contrat
- Modalités de paiement des sous-traitants (délais, retenues)
- Exigences CCQ (convention collective applicable)
- Obligations de divulgation des sous-traitants au dépôt

Pour chaque élément, cite le document + section/page.
Marque AMBIGU si les spécialités ne sont pas clairement définies. Marque ABSENT si manquant.
""",

    "08_soumissions_reçues.md": """\
Analyse comparative des soumissions reçues (dossier « Soumissions reçues »).

Si des soumissions sont disponibles, produis :
1. Tableau comparatif : | Soumissionnaire | Prix total | Prix alt. | Délai proposé | Réserves/Conditions |
2. Analyse des écarts significatifs entre soumissionnaires (>10 % de l'écart moyen)
3. Vérification de conformité : documents requis, cautionnements, formules signées
4. Résumé des réserves et conditions émises par chaque soumissionnaire
5. Recommandation d'adjudication avec justification contractuelle

Si aucune soumission n'est disponible :
Indique clairement [SOUMISSIONS NON REÇUES — APPEL EN COURS] et décris ce qui était attendu.

Cite les documents sources. Marque AMBIGU si des éléments sont incomparables.
""",

    "09_rapport_estimateur.md": """\
Produis le rapport complet de l'estimateur dans l'ordre exact suivant :

## 1. RÉSUMÉ EXÉCUTIF — TOP 10 RISQUES PAR SÉVÉRITÉ
Tableau : | Rang | Risque | Sévérité (1-10) | Impact $ estimé | Impact délai | Action requise |

## 2. REGISTRE DES RISQUES COMPLET
Pour chaque risque identifié dans l'ensemble des documents :
| Risque | Description détaillée | Impact $ | Impact délai | Probabilité | Mitigation recommandée | Source |

## 3. QUESTIONS À ENVOYER AVANT LE DÉPÔT
Liste numérotée, format : [PRIORITÉ HAUTE/MOYENNE/BASSE] — Question complète et précise à envoyer au client/architecte/ingénieur.

## 4. HYPOTHÈSES DE SOUMISSION RECOMMANDÉES
Texte prêt à copier-coller dans la lettre de soumission.
Rédige en style professionnel québécois, en français, couvrant toutes les zones d'ambiguïté identifiées.

## 5. CHECKLIST DE CONFORMITÉ
Tableau : | Élément | Requis (O/N) | Statut | Document de référence |
Couvre : Assurances | Cautionnements | BSDQ | Formulaires obligatoires | Licences/Permis | CCQ
""",
}

# Prompts en deux appels : sections 01-08 puis rapport estimateur 09
_SECTIONS_01_08 = [nom for nom in PROMPTS_SECTIONS if not nom.startswith("09")]
_SECTIONS_09    = [nom for nom in PROMPTS_SECTIONS if nom.startswith("09")]

_DETAILS_01_08 = "\n\n".join(
    f"### {nom.replace('.md', '')}\n{PROMPTS_SECTIONS[nom].strip()}"
    for nom in _SECTIONS_01_08
)
_DETAILS_09 = "\n\n".join(
    f"### {nom.replace('.md', '')}\n{PROMPTS_SECTIONS[nom].strip()}"
    for nom in _SECTIONS_09
)

_ENTETE_JSON = (
    "IMPORTANT: Réponds UNIQUEMENT avec le JSON brut. "
    "Pas de ```json, pas de markdown, pas d'explication. "
    "Commence directement par { et termine par }.\n\n"
    "La valeur de chaque clé doit être une STRING markdown simple. "
    "Pas d'objets imbriqués, pas de tableaux JSON.\n"
    'Exemple correct :\n'
    '{\n'
    '  "01_table_documentaire": "# Titre\\n\\nContenu markdown ici...",\n'
    '  "02_délais_échéancier": "# Titre\\n\\nContenu..."\n'
    '}\n'
    "Chaque section doit faire maximum 1500 mots.\n\n"
)

PROMPT_JSON_01_08 = (
    _ENTETE_JSON
    + "Analyse les documents du projet et retourne un objet JSON avec exactement 8 clés.\n\n"
    + "Clés attendues (valeur = contenu Markdown complet, citations de sources incluses) :\n"
    + '"01_table_documentaire", "02_délais_échéancier", "03_pénalités_retenues",\n'
    + '"04_assurances_garanties", "05_responsabilités_EG", "06_risques_techniques",\n'
    + '"07_BSDQ_sous-traitance", "08_soumissions_reçues"\n\n'
    + "Instructions par section :\n\n"
    + _DETAILS_01_08
)

PROMPT_JSON_09 = (
    _ENTETE_JSON
    + "Analyse les documents du projet et retourne un objet JSON avec exactement 1 clé.\n\n"
    + 'Clé attendue : "09_rapport_estimateur"\n\n'
    + "Instructions :\n\n"
    + _DETAILS_09
)

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
console = Console(legacy_windows=False)


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

def configurer_logging(dossier_analyse: Path) -> logging.Logger:
    """Configure un logger avec handler fichier horodaté."""
    logger = logging.getLogger("analyser_soumission")
    logger.setLevel(logging.DEBUG)

    if logger.handlers:
        logger.handlers.clear()

    log_path = dossier_analyse / LOG_FICHIER
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(fh)
    return logger


def calculer_md5(chemin: Path) -> str:
    md5 = hashlib.md5()
    with open(chemin, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            md5.update(chunk)
    return md5.hexdigest()


def charger_registre(dossier_analyse: Path) -> dict:
    chemin = dossier_analyse / REGISTRE_FICHIER
    if chemin.exists():
        try:
            with open(chemin, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}
    return {}


def sauvegarder_registre(dossier_analyse: Path, registre: dict) -> None:
    chemin = dossier_analyse / REGISTRE_FICHIER
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(registre, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Extraction de texte
# ---------------------------------------------------------------------------

def extraire_texte_pdf(chemin: Path, logger: logging.Logger) -> str:
    """Extrait le texte de chaque page d'un PDF avec pdfplumber."""
    try:
        pages = []
        with pdfplumber.open(chemin) as pdf:
            nb_pages = len(pdf.pages)
            for i, page in enumerate(pdf.pages, 1):
                texte = page.extract_text()
                if texte and texte.strip():
                    pages.append(f"[Page {i}/{nb_pages}]\n{texte.strip()}")
                else:
                    pages.append(f"[Page {i}/{nb_pages} — sans texte extractible]")
        resultat = "\n\n".join(pages)
        logger.info(f"PDF OK : {chemin.name} ({nb_pages} pages, {len(resultat):,} car.)")
        return resultat if resultat.strip() else f"[PDF SANS TEXTE EXTRACTIBLE : {chemin.name}]"
    except Exception as exc:
        logger.error(f"Erreur extraction PDF {chemin.name} : {exc}")
        return f"[ERREUR EXTRACTION PDF : {chemin.name} — {exc}]"


def extraire_texte_msg(chemin: Path, logger: logging.Logger) -> tuple[str, list[tuple[str, Path]]]:
    """
    Extrait le corps d'un email .msg et retourne la liste (nom_pj, chemin_tmp)
    pour les pièces jointes PDF (fichiers temporaires à supprimer après usage).
    """
    try:
        msg = extract_msg.Message(str(chemin))

        corps = (
            f"De      : {msg.sender}\n"
            f"À       : {msg.to}\n"
            f"Date    : {msg.date}\n"
            f"Sujet   : {msg.subject}\n\n"
            f"--- CORPS ---\n"
            f"{msg.body or '[Corps vide]'}"
        )

        pj_pdfs: list[tuple[str, Path]] = []
        if msg.attachments:
            for att in msg.attachments:
                nom = getattr(att, "longFilename", None) or getattr(att, "shortFilename", None) or ""
                if nom.lower().endswith(".pdf"):
                    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
                    try:
                        tmp.write(att.data)
                        tmp.flush()
                    finally:
                        tmp.close()
                    pj_pdfs.append((nom, Path(tmp.name)))
                    logger.info(f"  -> Pièce jointe PDF : {nom}")

        logger.info(f"Email OK : {chemin.name} ({len(pj_pdfs)} PDF joint(s))")
        return corps, pj_pdfs

    except Exception as exc:
        logger.error(f"Erreur extraction MSG {chemin.name} : {exc}")
        return f"[ERREUR EXTRACTION EMAIL : {chemin.name} — {exc}]", []


# ---------------------------------------------------------------------------
# Détection des pages de devis dans les fichiers PLANS
# ---------------------------------------------------------------------------

_RE_PLAN_SECTION_NORM = re.compile(
    r"\b\d{2}\s+\d{2}\s+\d{2}[A-Z]?\s*[—–\-]", re.IGNORECASE
)
_TITRES_DEVIS_PLANS = [
    "PORTÉE D'APPROVISIONNEMENT", "SOMMAIRE DES TRAVAUX",
    "RESTRICTIONS VISANT LES TRAVAUX", "DÉFINITIONS",
    "DÉCOUPAGE DES RESPONSABILITÉS", "SANTÉ ET SÉCURITÉ",
    "EXIGENCES RÉGLEMENTAIRES", "MISE EN SERVICE",
    "DESSINS D'ATELIER", "GARANTIE", "NETTOYAGE",
    "IDENTIFICATION", "FILS ET CÂBLES", "CONDUITS",
    "PANNEAUX DE DISTRIBUTION", "MISE À LA TERRE",
]
_RE_PLAN_GRILLE = re.compile(
    r"ÉCHELLE\s*:\s*1\s*:|PLAN\s+CL[EÉ]|NIVEAU\s+[012]", re.IGNORECASE
)


def detect_pages_devis(
    pdf_path: Path,
    logger: logging.Logger,
) -> tuple[str, int, int]:
    """
    Analyse un PDF de plans page par page.
    Retourne (texte_devis, nb_pages_devis, nb_pages_total).

    Score par page :
      +5 : section normalisée MasterFormat avec tiret (ex: "26 05 00 —")
      +3 : titre de devis reconnu dans la liste _TITRES_DEVIS_PLANS
      +2 : densité de texte élevée (> 2 000 chars)
      -3 : grille de plan graphique (marqueurs ÉCHELLE/PLAN CLÉ/NIVEAU + texte < 1 000 chars)
    Page gardée si score >= 5.
    """
    pages_devis: list[str] = []
    nb_pages_total = 0

    try:
        with pdfplumber.open(pdf_path) as pdf:
            nb_pages_total = len(pdf.pages)
            for i, page in enumerate(pdf.pages, 1):
                texte = page.extract_text() or ""
                score = 0

                # +5 : section normalisée MasterFormat avec tiret
                if _RE_PLAN_SECTION_NORM.search(texte):
                    score += 5

                # +3 : titre de devis reconnu (premier match suffit)
                texte_maj = texte.upper()
                for titre in _TITRES_DEVIS_PLANS:
                    if titre in texte_maj:
                        score += 3
                        break

                # +2 : densité de texte élevée
                if len(texte) > 2000:
                    score += 2

                # -3 : grille de plan graphique (peu de texte + marqueur de grille)
                if len(texte) < 1000 and _RE_PLAN_GRILLE.search(texte):
                    score -= 3

                if score >= 5:
                    pages_devis.append(f"[Page {i}/{nb_pages_total}]\n{texte.strip()}")

    except Exception as exc:
        logger.error(f"Erreur detect_pages_devis {pdf_path.name} : {exc}")
        return "", 0, 0

    texte_devis = "\n\n".join(pages_devis)
    logger.info(
        f"detect_pages_devis {pdf_path.name} : "
        f"{len(pages_devis)}/{nb_pages_total} pages de devis, {len(texte_devis):,} chars"
    )
    return texte_devis, len(pages_devis), nb_pages_total


# ---------------------------------------------------------------------------
# Collecte des fichiers
# ---------------------------------------------------------------------------

def collecter_fichiers(dossier_projet: Path, logger: logging.Logger) -> dict[str, dict]:
    """
    Retourne un dict {nom_dossier: {"statut": ..., "fichiers": [Path, ...]}}
    Statuts possibles : "OK", "VIDE", "ABSENT"
    """
    result: dict[str, dict] = {}
    for nom in DOSSIERS_SOURCE:
        chemin = dossier_projet / nom
        if not chemin.exists():
            logger.warning(f"Dossier absent : {chemin}")
            result[nom] = {"statut": "ABSENT", "fichiers": [], "racine": chemin}
            continue

        vus: set[str] = set()
        fichiers: list[Path] = []
        for pattern in ("*.pdf", "*.PDF", "*.msg", "*.MSG"):
            for f in chemin.rglob(pattern):
                cle = os.path.abspath(f)
                if cle not in vus:
                    vus.add(cle)
                    fichiers.append(f)

        if not fichiers:
            result[nom] = {"statut": "VIDE", "fichiers": [], "racine": chemin}
        else:
            result[nom] = {"statut": "OK", "fichiers": sorted(fichiers), "racine": chemin}

    return result


# ---------------------------------------------------------------------------
# Appel API Claude
# ---------------------------------------------------------------------------

def appeler_claude(
    client: anthropic.Anthropic,
    contexte: str,
    prompt_section: str,
    nom_section: str,
    logger: logging.Logger,
    max_tokens: int = MAX_TOKENS,
) -> str:
    """Appelle l'API Claude avec backoff exponentiel (max MAX_RETRY tentatives)."""
    message_user = (
        f"Voici les documents du projet à analyser :\n\n"
        f"{contexte}\n\n"
        f"---\n\n"
        f"{prompt_section}"
    )

    for tentative in range(1, MAX_RETRY + 1):
        try:
            logger.info(f"Appel API — {nom_section} (tentative {tentative}/{MAX_RETRY})")
            with client.messages.stream(
                model=MODELE_CLAUDE,
                max_tokens=max_tokens,
                system=PROMPT_SYSTEME,
                messages=[{"role": "user", "content": message_user}],
            ) as stream:
                texte = stream.get_final_text()
            logger.info(f"Réponse reçue — {nom_section} ({len(texte):,} car.)")
            return texte

        except anthropic.APITimeoutError as exc:
            logger.warning(f"Timeout API (tentative {tentative}) : {exc}")
            if tentative < MAX_RETRY:
                time.sleep(DELAI_RETRY_BASE * tentative)
            else:
                return f"[ERREUR API : Timeout après {MAX_RETRY} tentatives — {exc}]"

        except anthropic.RateLimitError as exc:
            logger.warning(f"Rate limit (tentative {tentative}) : {exc}")
            if tentative < MAX_RETRY:
                time.sleep(DELAI_RATE_LIMIT)
            else:
                return f"[ERREUR API : Rate limit — {exc}]"

        except anthropic.APIError as exc:
            logger.error(f"Erreur API (tentative {tentative}) : {exc}")
            if tentative < MAX_RETRY:
                time.sleep(DELAI_RETRY_BASE)
            else:
                return f"[ERREUR API : {exc}]"

        except Exception as exc:
            logger.error(f"Erreur inattendue (tentative {tentative}) : {exc}")
            if tentative < MAX_RETRY:
                time.sleep(DELAI_RETRY_BASE)
            else:
                return f"[ERREUR INATTENDUE : {exc}]"

    return "[ERREUR API : Échec après toutes les tentatives]"


# ---------------------------------------------------------------------------
# Classification et condensation par type de document
# ---------------------------------------------------------------------------

def classifier_document(nom: str, dossier: str) -> str:
    """Retourne le type du document selon son nom et son dossier source."""
    if dossier == "Soumissions reçues":
        return "SOUMISSION_REÇUE"
    if re.search(r"contrat", nom, re.IGNORECASE):
        return "CONTRAT"
    if re.search(r"r[eé]gie", nom, re.IGNORECASE):
        return "RÉGIE"
    if re.search(r"formulaire", nom, re.IGNORECASE):
        return "FORMULAIRE"
    if re.search(r"addenda", nom, re.IGNORECASE):
        return "ADDENDA"
    if re.search(r"devis", nom, re.IGNORECASE) and re.search(r"arch", nom, re.IGNORECASE):
        return "DEVIS_ARCH"
    if re.search(r"devis", nom, re.IGNORECASE) and re.search(r"admin", nom, re.IGNORECASE):
        return "DEVIS_ADMIN"
    if re.search(r"plans?", nom, re.IGNORECASE):
        return "PLANS"
    return "AUTRE"


def filtrer_devis_arch(texte: str, nom_fichier: str = "", structure: dict | None = None) -> tuple[str, int]:
    """
    Filtrage 3 couches séquentielles pour DEVIS_ARCH.

    Couche 1 (sections 02+ MasterFormat) : P1 -> supprimé, P2 -> gardé intégralement, P3 -> supprimé
    Couche 2 (section 01 / préambule)    : signal/bruit avec seuil >= 2
    Couche 3 (universelle, override)     : terme critique -> gardé avec tag [CRITIQUE]

    structure : résultat de detecter_structure_devis() — ajuste les patterns dynamiquement.
    Retourne (texte_filtré, nb_blocs_P3_traités).
    """
    # ── Confiance trop basse -> fallback Signal/Bruit seul ─────────────────────
    if structure and float(structure.get("confiance", 1.0)) < 0.50:
        return filtrer_signal_bruit(texte, nom_fichier), 0

    # ── Patterns : dynamiques si structure fournie, sinon défauts globaux ──────
    if structure and float(structure.get("confiance", 0.0)) >= 0.50:
        re_section, re_p1, re_p2, re_p3 = _construire_regexes_devis(structure)
    else:
        re_section, re_p1, re_p2, re_p3 = _RE_SECTION_MF, _RE_DEVIS_PARTIE1, _RE_DEVIS_PARTIE2, _RE_DEVIS_PARTIE3
    # ── Séparer table des matières / corps du devis ────────────────────────────
    # Chercher "Partie 1" dans le texte brut (avant tout découpage) pour éviter
    # que la TOC ne déclenche in_02plus prématurément.
    match_corps = re.search(r'(?i)\bPartie\s*1\b', texte)
    idx_corps   = match_corps.start() if match_corps else 0
    texte_toc   = texte[:idx_corps]
    texte_corps = texte[idx_corps:]
    paras_toc = [p.strip() for p in re.split(r'\n{2,}', texte_toc) if p.strip()]

    c_p1_supp  = 0
    c_p2_gard  = 0
    c_p3_supp  = 0
    n_except   = 0
    n_sb_supp  = 0
    n_p3_blocs = 0

    gardes: list[str] = []

    # ── Phase TOC : signal/bruit seul (Couches 2 et 3) ────────────────────────
    for para in paras_toc:
        est_critique = bool(_RE_DEVIS_CRITIQUE.search(para))
        score = _scorer_paragraphe(para)
        if score >= 2:
            gardes.append(para)
        elif est_critique:
            gardes.append(f"[CRITIQUE] {para}")
            n_except += 1
        else:
            n_sb_supp += 1

    # ── Phase corps : split sur marqueurs Partie 1/2/3 ────────────────────────
    blocs = re.split(r'(?i)(?=\bPartie\s*[123]\b)', texte_corps)

    _RE_PARTIE_NUM = re.compile(r'(?i)\bPartie\s*([123])\b')

    for bloc in blocs:
        if not bloc.strip():
            continue
        m = _RE_PARTIE_NUM.match(bloc.lstrip())
        num_partie = int(m.group(1)) if m else 1  # aucun marqueur -> traité comme P1

        if num_partie == 2:
            # Couche 1 — Partie 2 : garder intégralement
            gardes.append(bloc.strip())
            c_p2_gard += len(bloc)
        else:
            # Couche 1 — Partie 1 ou 3 : supprimer sauf critiques (Couche 3)
            if num_partie == 3:
                n_p3_blocs += 1
            for para in (p.strip() for p in re.split(r'\n{2,}', bloc) if p.strip()):
                if _RE_DEVIS_CRITIQUE.search(para):
                    gardes.append(f"[CRITIQUE] {para}")
                    n_except += 1
                elif num_partie == 1:
                    c_p1_supp += len(para)
                else:
                    c_p3_supp += len(para)

    # ── Affichage stats terminal ───────────────────────────────────────────────
    if nom_fichier:
        console.print(
            f"  [dim]ARCH[/dim] {nom_fichier[:35]} : "
            f"P1 supprimées: {c_p1_supp // 1024}kb | "
            f"P2 gardées: {c_p2_gard // 1024}kb | "
            f"P3 supprimées: {c_p3_supp // 1024}kb | "
            f"Exceptions: {n_except} | "
            f"Score/Bruit: {n_sb_supp} supprimés"
        )

    return "\n\n".join(gardes), n_p3_blocs


def resumer_soumission(texte: str) -> str:
    """Extrait les informations clés d'une soumission (max 500 cars)."""
    MOTS = ["total", "prix", "valide", "validité", "exclusion",
            "entreprise", "soumissionnaire", "montant"]
    lignes_cles = [
        ligne.strip()
        for ligne in texte.split("\n")
        if ligne.strip() and any(m in ligne.lower() for m in MOTS)
    ]
    resume = "\n".join(lignes_cles)
    if len(resume) > 500:
        resume = resume[:497] + "..."
    return resume if resume.strip() else texte[:500]


def detecter_structure_devis(
    texte: str,
    nom_fichier: str,
    client: anthropic.Anthropic,
    registre: dict,
    dossier_analyse: Path,
    logger: logging.Logger,
) -> dict:
    """
    Appelle Claude pour détecter le format de sections et de parties d'un DEVIS_ARCH.
    Résultat mis en cache dans registre["struct_devis"][md5_texte].

    confiance >= 0.80 -> [AUTO]   filtrage MasterFormat avec patterns détectés
    confiance 0.50–0.79 -> [INCERTAIN]  idem + warning
    confiance < 0.50  -> [SKIP_MF]  fallback Signal/Bruit seul
    """
    FALLBACK: dict = {
        "section_format": "", "p1_marker": "", "p2_marker": "",
        "p3_marker": "", "confiance": 0.0, "note": "non détecté",
    }

    cle_md5 = hashlib.md5(texte.encode("utf-8", errors="replace")).hexdigest()
    cache = registre.setdefault("struct_devis", {})

    if cle_md5 in cache:
        struct = cache[cle_md5]
        logger.info(
            f"Structure devis (cache) : {nom_fichier} "
            f"— confiance={float(struct.get('confiance', 0)):.2f}"
        )
        return struct

    # Échantillonnage : 1 page sur 10, max 20 000 chars
    morceaux = re.split(r'\[Page \d+/\d+\]\n?', texte)
    pages_sample = [p.strip() for i, p in enumerate(morceaux) if i % 10 == 0 and p.strip()]
    echantillon = "\n\n[...]\n\n".join(pages_sample)[:20_000]

    prompt_user = (
        f"Voici un échantillon du devis {nom_fichier} :\n\n"
        f"{echantillon}\n\n"
        "Retourne ce JSON exact, sans markdown :\n"
        "{\n"
        '  "section_format": "ex: 09 30 00 ou 093000 ou 9.3",\n'
        '  "p1_marker": "texte exact qui introduit la Partie 1",\n'
        '  "p2_marker": "texte exact qui introduit la Partie 2",\n'
        '  "p3_marker": "texte exact qui introduit la Partie 3",\n'
        '  "confiance": 0.85,\n'
        '  "note": "explication courte si confiance < 0.80"\n'
        "}"
    )

    try:
        logger.info(f"detecter_structure_devis : appel API pour {nom_fichier}")
        response = client.messages.create(
            model=MODELE_CLAUDE,
            max_tokens=300,
            system=(
                "Tu analyses la structure d'un devis de construction québécois. "
                "Identifie les patterns de formatage et retourne UNIQUEMENT un JSON."
            ),
            messages=[{"role": "user", "content": prompt_user}],
        )
        texte_rep = response.content[0].text.strip()
        texte_rep = re.sub(r'^```(?:json)?\s*|\s*```$', '', texte_rep, flags=re.MULTILINE).strip()
        struct = json.loads(texte_rep)
    except Exception as exc:
        logger.warning(f"detecter_structure_devis — erreur : {exc}")
        return FALLBACK

    for champ in ("section_format", "p1_marker", "p2_marker", "p3_marker", "confiance"):
        if champ not in struct:
            logger.warning(f"detecter_structure_devis — champ manquant : {champ!r}")
            return FALLBACK

    struct["confiance"] = float(struct["confiance"])
    struct.setdefault("note", "")

    confiance = struct["confiance"]
    nom_court = nom_fichier[:35]
    if confiance >= 0.80:
        logger.info(
            f"[AUTO] Structure devis : {nom_fichier} — {struct['section_format']} "
            f"— confiance={confiance:.2f}"
        )
        console.print(
            f"  [green][AUTO][/green] {nom_court} : "
            f"format=[bold]{struct['section_format']}[/bold] (confiance {confiance:.0%})"
        )
    elif confiance >= 0.50:
        logger.warning(
            f"[INCERTAIN] Structure devis : {nom_fichier} — {struct['section_format']} "
            f"— confiance={confiance:.2f} — {struct['note']}"
        )
        console.print(
            f"  [yellow][INCERTAIN][/yellow] {nom_court} : "
            f"format=[bold]{struct['section_format']}[/bold] "
            f"(confiance {confiance:.0%}) — {struct['note']}"
        )
    else:
        logger.warning(
            f"[SKIP_MF] Filtrage MasterFormat désactivé : {nom_fichier} "
            f"— confiance={confiance:.2f} — {struct['note']}"
        )
        console.print(
            f"  [red][SKIP_MF][/red] {nom_court} : filtrage MasterFormat désactivé "
            f"(confiance {confiance:.0%}) — {struct['note']}"
        )

    cache[cle_md5] = struct
    sauvegarder_registre(dossier_analyse, registre)
    return struct


def traiter_document(
    texte: str,
    type_doc: str,
    nom_fichier: str = "",
    structure: dict | None = None,
) -> tuple[str, int]:
    """
    Applique la stratégie de condensation selon le type de document.
    Retourne (texte_traité, nb_sections_exec_sautées).

    CONTRAT, RÉGIE  -> extraction regex ciblée (extraire_champs_contrat)
    DEVIS_ARCH      -> filtrage 3 couches MasterFormat + signal/bruit
    DEVIS_ADMIN     -> filtrage signal/bruit
    FORMULAIRE, ADDENDA -> texte complet (courts et 100 % pertinents)
    PLANS           -> filtrage signal/bruit des pages de devis extraites (exclu si aucune)
    SOUMISSION_REÇUE -> résumé 500 chars
    AUTRE           -> filtre par mots-clés si > 5 000 chars
    """
    if type_doc in ("CONTRAT", "RÉGIE"):
        return extraire_champs_contrat(texte), 0
    if type_doc in ("FORMULAIRE", "ADDENDA"):
        return texte, 0
    if type_doc == "PLANS":
        if not texte.strip():
            return "", 0
        return filtrer_signal_bruit(texte, nom_fichier), 0
    if type_doc == "DEVIS_ARCH":
        return filtrer_devis_arch(texte, nom_fichier, structure)
    if type_doc == "DEVIS_ADMIN":
        return filtrer_signal_bruit(texte, nom_fichier), 0
    if type_doc == "SOUMISSION_REÇUE":
        return resumer_soumission(texte), 0
    # AUTRE
    if len(texte) <= 5000:
        return texte, 0
    lignes_gardees = [
        l for l in texte.split("\n")
        if not l.strip() or any(m in l.lower() for m in MOTS_CLES_CONTRAT)
    ]
    return "\n".join(lignes_gardees), 0


def extraire_json_reponse(texte: str, logger: logging.Logger) -> dict:
    """
    Extrait et parse le JSON de la réponse Claude.
    Stratégie en 3 étapes — compatible Windows \\r\\n.
    """
    # Étape 1 : strip robuste des balises code fence (compatible \r\n Windows)
    nettoye = texte.strip()
    for fence in ["```json", "```"]:
        if nettoye.startswith(fence):
            nettoye = nettoye[len(fence):]
            break
    if nettoye.rstrip().endswith("```"):
        nettoye = nettoye.rstrip()[:-3]
    nettoye = nettoye.strip()

    try:
        donnees = json.loads(nettoye)
        logger.info("JSON parsé avec succès (méthode 1 : strip code fence).")
        return donnees
    except json.JSONDecodeError:
        pass

    # Étape 2 : extraire entre le premier { et le dernier }
    start = nettoye.find("{")
    end = nettoye.rfind("}")
    if start != -1 and end > start:
        try:
            donnees = json.loads(nettoye[start:end + 1])
            logger.info("JSON parsé avec succès (méthode 2 : extraction {…} sur texte nettoyé).")
            return donnees
        except json.JSONDecodeError:
            pass

    # Étape 3 : même extraction sur le texte brut original
    start = texte.find("{")
    end = texte.rfind("}")
    if start != -1 and end > start:
        try:
            donnees = json.loads(texte[start:end + 1])
            logger.info("JSON parsé avec succès (méthode 3 : extraction {…} sur texte brut).")
            return donnees
        except json.JSONDecodeError:
            pass

    # Étape 4 : récupération partielle par regex — JSON tronqué
    _RE_PAIRE = re.compile(r'"(\d{2}_[^"]+)"\s*:\s*"((?:[^"\\]|\\.)*)"', re.DOTALL)
    paires = _RE_PAIRE.findall(texte)
    if paires:
        recupere = {cle: valeur.replace("\\n", "\n").replace('\\"', '"') for cle, valeur in paires}
        logger.warning(
            f"JSON tronqué — récupération partielle : {len(recupere)} clé(s) extraite(s) "
            f"par regex : {list(recupere.keys())}"
        )
        sections_attendues = [
            "01_table_documentaire", "02_délais_échéancier", "03_pénalités_retenues",
            "04_assurances_garanties", "05_responsabilités_EG", "06_risques_techniques",
            "07_BSDQ_sous-traitance", "08_soumissions_reçues", "09_rapport_estimateur",
        ]
        manquantes = [s for s in sections_attendues if s not in recupere]
        if manquantes:
            logger.warning(f"Sections manquantes : {manquantes}")
        if len(recupere) >= 3:
            for cle in recupere:
                recupere[cle] += (
                    "\n\n---\n> **[SECTION PARTIELLEMENT RÉCUPÉRÉE]** "
                    "Le JSON de la réponse API était tronqué. "
                    "Relancez avec `--forcer` pour régénérer cette section complètement."
                )
            logger.info(f"Récupération partielle acceptée ({len(recupere)} sections ≥ 3).")
            return recupere

    logger.error("Impossible de parser le JSON après 4 tentatives (récupération partielle insuffisante).")
    logger.debug(f"Début de la réponse reçue : {texte[:500]!r}")
    return {}


# ---------------------------------------------------------------------------
# Extraction contractuelle — CONTRAT, RÉGIE
# ---------------------------------------------------------------------------

def extraire_champs_contrat(texte: str) -> str:
    """
    Extrait les champs contractuels clés par regex + fenêtre de contexte (300 chars).
    Retourne un texte structuré ~5 000 chars.
    """
    FENETRE = 300

    def contextes(pattern: str) -> list[str]:
        """Fenêtres de contexte autour de chaque match (max 3)."""
        resultats: list[str] = []
        for m in re.finditer(pattern, texte, re.IGNORECASE):
            debut = max(0, m.start() - FENETRE // 2)
            fin   = min(len(texte), m.end() + FENETRE // 2)
            ctx   = texte[debut:fin].replace("\n", " ").strip()
            if ctx and ctx not in resultats:
                resultats.append(ctx)
        return resultats[:3]

    lignes: list[str] = ["=== INFORMATIONS CONTRACTUELLES EXTRAITES ===\n"]

    def ajouter(titre: str, valeurs: list[str]) -> None:
        if valeurs:
            for v in valeurs:
                lignes.append(f"[{titre}] {v}")
        else:
            lignes.append(f"[{titre}] ABSENT — vérifier manuellement")

    # Cautionnements
    ajouter("CAUTIONNEMENT SOUMISSION",
            contextes(r"cautionnement\s+de\s+soumission"))
    ajouter("CAUTIONNEMENT EXÉCUTION",
            contextes(r"cautionnement\s+d.{0,3}ex[eé]cution"))
    ajouter("CAUTIONNEMENT MAIN-D'ŒUVRE",
            contextes(r"cautionnement.{0,40}main.{0,5}uvre"))

    # Assurances
    ajouter("ASSURANCE RC GÉNÉRALE",
            contextes(r"responsabilit[eé]\s+civile|RC\s+g[eé]n[eé]rale"))
    ajouter("ASSURANCE CHANTIER / TOUS RISQUES",
            contextes(r"assurance\s+chantier|tous\s+risques|wrap.up"))

    # Dates et délais
    ajouter("DATE DÉBUT TRAVAUX",
            contextes(r"d[eé]but\s+des\s+travaux|date\s+de\s+commencement|avis\s+de\s+commencer"))
    ajouter("DATE FIN / ACHÈVEMENT",
            contextes(r"fin\s+des\s+travaux|date\s+d.{0,3}ach[eè]vement|d[eé]lai\s+d.{0,3}ex[eé]cution"))
    ajouter("DURÉE EN JOURS",
            contextes(r"\d+\s+jours?\s+(?:civils?|ouvrables?|calendriers?)"))

    # Pénalités
    ajouter("PÉNALITÉS / DOMMAGES LIQUIDÉS",
            contextes(r"p[eé]nalit[eé]|dommages?\s+liquid[eé]s?|indemni[tté][eé]\s+de\s+retard"))

    # Retenues
    ajouter("RETENUES / RÉTENTION",
            contextes(r"\bretenue\b|\br[eé]tention\b"))

    # Travail hors heures
    ajouter("TRAVAIL HORS HEURES / ACCÈS RESTREINT",
            contextes(
                r"travail\s+de\s+nuit|travail\s+de\s+soir"
                r"|heures?\s+suppl[eé]mentaires?"
                r"|restriction\s+d.{0,3}acc[eè]s|heures?\s+autoris[eé]es?"
            ))

    # Résiliation
    ajouter("RÉSILIATION",
            contextes(r"r[eé]siliation|r[eé]solution\s+du\s+contrat"))

    # Annexes — titre + 2 000 chars de contenu
    _RE_ANNEXE = re.compile(
        r'(?i)ANNEXE\s+\w+[\d\.]*\s*[-–—]?\s*[^\n]{5,60}',
        re.MULTILINE,
    )
    blocs_annexes: list[str] = []
    vus: set[str] = set()
    for m in _RE_ANNEXE.finditer(texte):
        titre = m.group(0).strip()
        if titre in vus:
            continue
        vus.add(titre)
        contenu = texte[m.end():m.end() + 2000].strip()
        blocs_annexes.append(f"--- {titre} ---\n{contenu}")
    if blocs_annexes:
        lignes.append("\n=== ANNEXES ===")
        lignes.extend(blocs_annexes)

    lignes.append("\n=== FIN EXTRACTION ===")
    return "\n".join(lignes)


# ---------------------------------------------------------------------------
# Filtrage signal/bruit — DEVIS_ARCH, DEVIS_ADMIN
# ---------------------------------------------------------------------------

_MOIS_FR = (
    r"janvier|f[eé]vrier|mars|avril|mai|juin"
    r"|juillet|ao[uû]t|septembre|octobre|novembre|d[eé]cembre"
)

_RE_SB_SIGNAL3: list[re.Pattern] = [
    re.compile(r"\d+[\d\s,.]*(?:\$|%|/jour|/semaine|/mois|/heure|M\$)", re.IGNORECASE),
    re.compile(
        r"p[eé]nalit[eé]|retenue|cautionnement|assurance|r[eé]siliation|r[eé]clamation"
        r"|point.{0,4}arr[eê]t|d[eé]lai\s+de\s+rigueur|BSDQ"
        r"|Hydro.Qu[eé]bec|[EÉ]nergir|amiante|PCI\b|infection"
        r"|milieu\s+occup[eé]|zone\s+sensible|phasage\s+critique",
        re.IGNORECASE,
    ),
    re.compile(r"(?<![a-z])[A-ZÉÀÂÊÎÔÛÙÇŒ]{5,}(?![a-z])"),
    re.compile(rf"(?:{_MOIS_FR})\s+\d{{4}}", re.IGNORECASE),
]

_RE_SB_SIGNAL2 = re.compile(
    r"obligatoire|interdit\b|requis\b|exig[eé]\b"
    r"|ne\s+pas\b|sans\s+quoi|[àa]\s+d[eé]faut|sous\s+peine"
    r"|approbation\s+requise|inspection\s+obligatoire"
    r"|avant\s+de\s+proc[eé]der|soumettre\s+pour\s+approbation"
    r"|coordination\s+avec\b",
    re.IGNORECASE,
)

_RE_SB_BRUIT2: list[re.Pattern] = [
    re.compile(
        r"r[eè]gles?\s+de\s+l.art|bonnes?\s+pratiques?"
        r"|codes?\s+et\s+normes?|CNB\b|CNPI\b|CSA\b",
        re.IGNORECASE,
    ),
    re.compile(r"nettoy(?:er|age)\b", re.IGNORECASE),
    re.compile(r"prot[eé]ger\s+les\s+ouvrages?\s+existants?", re.IGNORECASE),
    re.compile(r"garantie.{0,20}un\s+an.{0,20}main.d", re.IGNORECASE),
]

_RE_SB_BRUIT3_TITRE = re.compile(
    r"^\s*(?:PARTIE\s+\d|[12]\s+G[EÉ]N[EÉ]RALIT[EÉ]S|[12]\s+PRODUITS"
    r"|[13]\s+EX[EÉ]CUTION|\d{1,2}\.\d{0,3}\s+[A-ZÉÀÂÊÎÔÛÇŒ])",
    re.IGNORECASE,
)
_RE_SB_CONFORMER = re.compile(r"se\s+conformer\s+[àa]\b", re.IGNORECASE)


def _scorer_paragraphe(para: str) -> int:
    """Calcule le score signal/bruit d'un paragraphe (partagé entre filtrer_devis_arch et filtrer_signal_bruit)."""
    nb_mots = len(para.split())
    score   = 0

    if nb_mots < 12:
        score -= 2
    if nb_mots < 10 and _RE_SB_BRUIT3_TITRE.match(para):
        score -= 3
    if para.count(",") >= 3 and nb_mots < 20:
        score -= 3
    if _RE_SB_CONFORMER.search(para) and nb_mots < 15:
        score -= 3
    for pat in _RE_SB_BRUIT2:
        if pat.search(para):
            score -= 2
    for pat in _RE_SB_SIGNAL3:
        if pat.search(para):
            score += 3
            break
    if _RE_SB_SIGNAL2.search(para):
        score += 2

    return score


def filtrer_signal_bruit(texte: str, nom_fichier: str = "") -> str:
    """
    Filtre un texte de devis en gardant uniquement les paragraphes
    avec un score signal/bruit >= 1 (utilisé pour DEVIS_ADMIN).
    Affiche les stats si nom_fichier est fourni.
    """
    paras  = [p.strip() for p in re.split(r"\n{2,}", texte) if p.strip()]
    gardes = [p for p in paras if _scorer_paragraphe(p) >= 1]

    if nom_fichier:
        nb_total = max(len(paras), 1)
        pct = len(gardes) / nb_total * 100
        console.print(
            f"  [dim]Signal/Bruit[/dim] {nom_fichier[:40]} : "
            f"{len(paras)} paragraphes -> {len(gardes)} gardés "
            f"([green]{pct:.0f}%[/green])"
        )

    return "\n\n".join(gardes)


def dedupliquer_corpus(blocs: list[str]) -> tuple[str, int]:
    """
    Identifie les paragraphes présents dans 3+ documents avec similarité ≥ 85 %
    et les regroupe dans un bloc CONDITIONS GÉNÉRALES en tête du corpus.
    Les occurrences dans les blocs originaux sont remplacées par [voir CONDITIONS GÉNÉRALES].
    Retourne (corpus_final, nb_paragraphes_dédupliqués).
    """
    MIN_PARA = 60           # longueur minimale (chars) pour être candidat
    MAX_CANDIDATS_SM = 300  # limite pour la passe SequenceMatcher (performances)

    if len(blocs) < 3:
        return "\n\n".join(blocs), 0

    paras_par_bloc: list[list[str]] = [
        [p.strip() for p in re.split(r"\n{2,}", b) if p.strip()]
        for b in blocs
    ]

    def normaliser(s: str) -> str:
        return re.sub(r"\s+", " ", s.lower()).strip()

    # Phase 1 : déduplication exacte normalisée — O(n)
    occurrence: dict[str, set[int]] = defaultdict(set)
    norm_to_canon: dict[str, str] = {}
    for i, paras in enumerate(paras_par_bloc):
        for para in paras:
            if len(para) < MIN_PARA:
                continue
            n = normaliser(para)
            occurrence[n].add(i)
            norm_to_canon.setdefault(n, para)

    redondants_norms: set[str] = {n for n, idx in occurrence.items() if len(idx) >= 3}
    redondants_canon: list[str] = [norm_to_canon[n] for n in redondants_norms]

    # Phase 2 : SequenceMatcher pour les quasi-doublons non capturés en phase 1
    candidats: list[tuple[int, str, str]] = [
        (i, para, normaliser(para))
        for i, paras in enumerate(paras_par_bloc)
        for para in paras
        if len(para) >= MIN_PARA and normaliser(para) not in redondants_norms
    ]

    if len(candidats) <= MAX_CANDIDATS_SM:
        vus: set[int] = set()
        for idx_a, (bloc_a, para_a, norm_a) in enumerate(candidats):
            if idx_a in vus:
                continue
            blocs_sim: set[int] = {bloc_a}
            for idx_b, (bloc_b, para_b, norm_b) in enumerate(candidats):
                if idx_b <= idx_a or bloc_b == bloc_a or idx_b in vus:
                    continue
                # Filtre rapide : longueur similaire (±30 %) avant SequenceMatcher
                if abs(len(norm_a) - len(norm_b)) / max(len(norm_a), 1) > 0.30:
                    continue
                if difflib.SequenceMatcher(None, norm_a, norm_b, autojunk=False).ratio() >= 0.85:
                    blocs_sim.add(bloc_b)
                    vus.add(idx_b)
            if len(blocs_sim) >= 3:
                redondants_canon.append(para_a)
                redondants_norms.add(norm_a)
                vus.add(idx_a)

    if not redondants_canon:
        return "\n\n".join(blocs), 0

    # Remplacer les paragraphes redondants dans les blocs originaux
    corps_modifies: list[str] = []
    for bloc in blocs:
        paras = re.split(r"\n{2,}", bloc)
        nouvelles: list[str] = []
        for para in paras:
            n = normaliser(para.strip())
            if len(para.strip()) >= MIN_PARA and n in redondants_norms:
                nouvelles.append("[voir CONDITIONS GÉNÉRALES]")
                continue
            remplace = False
            if len(para.strip()) >= MIN_PARA:
                for ref_n in redondants_norms:
                    if abs(len(n) - len(ref_n)) / max(len(n), 1) <= 0.30:
                        if difflib.SequenceMatcher(None, n, ref_n, autojunk=False).ratio() >= 0.85:
                            nouvelles.append("[voir CONDITIONS GÉNÉRALES]")
                            remplace = True
                            break
            if not remplace:
                nouvelles.append(para)
        corps_modifies.append("\n\n".join(nouvelles))

    bloc_cg = (
        "=== CONDITIONS GÉNÉRALES APPLICABLES À TOUTES LES SECTIONS ===\n\n"
        + "\n\n".join(redondants_canon)
        + "\n\n==="
    )
    return bloc_cg + "\n\n" + "\n\n".join(corps_modifies), len(redondants_canon)


# ---------------------------------------------------------------------------
# Table des matières technique — livrable 00 (indépendant de l'API)
# ---------------------------------------------------------------------------

# Pattern MasterFormat : "03 30 00  Béton coulé en place  15"
_RE_MASTERFORMAT = re.compile(
    r"^\s*(\d{2}\s+\d{2}\s+\d{2})\s+(.+?)(?:[\s.]+\d+)?\s*$"
)


def generer_table_matieres_technique(
    dossier_projet: Path,
    dossier_analyse: Path,
    nom_projet: str,
    logger: logging.Logger,
) -> None:
    """
    Génère 00_table_matières_technique.md depuis le devis d'architecture.
    Aucun appel API — extraction regex pure sur le PDF complet.
    """
    LIVRABLE = "00_table_matières_technique.md"
    chemin_sortie = dossier_analyse / LIVRABLE

    # --- Recherche du fichier devis ARCH dans Plan & Devis (récursif) ---
    chemin_plan_devis = dossier_projet / "Plan & Devis"
    devis_arch: Optional[Path] = None

    if chemin_plan_devis.exists():
        for pdf in sorted(chemin_plan_devis.rglob("*.pdf")):
            if re.search(r"devis", pdf.name, re.IGNORECASE) and re.search(r"arch", pdf.name, re.IGNORECASE):
                devis_arch = pdf
                break
        if devis_arch is None:
            for pdf in sorted(chemin_plan_devis.rglob("*.PDF")):
                if re.search(r"devis", pdf.name, re.IGNORECASE) and re.search(r"arch", pdf.name, re.IGNORECASE):
                    devis_arch = pdf
                    break

    if devis_arch is None:
        logger.warning("Table des matières technique : aucun devis ARCH trouvé dans Plan & Devis.")
        console.print("[yellow]Table des matières : aucun fichier « Devis + ARCH » détecté.[/yellow]")
        contenu = (
            f"# Table des matières — Sections techniques\n\n"
            f"**Projet :** {nom_projet}  \n"
            f"**Devis source :** ABSENT  \n"
            f"**Extrait le :** {datetime.now().strftime('%Y-%m-%d %H:%M')}  \n\n"
            f"> Aucun fichier dont le nom contient « Devis » et « arch » n'a été trouvé "
            f"dans le dossier `Plan & Devis`.\n"
        )
        chemin_sortie.write_text(contenu, encoding="utf-8")
        return

    logger.info(f"Table des matières : devis ARCH -> {devis_arch.relative_to(dossier_projet)}")
    console.print(f"[cyan]Table des matières : scan de {devis_arch.name}...[/cyan]")

    # --- Extraction intégrale du PDF (aucune limite de pages) ---
    sections: list[tuple[str, str]] = []   # (numéro normalisé, titre)
    sections_vues: set[str] = set()        # déduplication par numéro

    try:
        with pdfplumber.open(devis_arch) as pdf:
            for page in pdf.pages:
                texte = page.extract_text()
                if not texte:
                    continue
                for ligne in texte.splitlines():
                    m = _RE_MASTERFORMAT.match(ligne)
                    if m:
                        numero = re.sub(r"\s+", " ", m.group(1).strip())
                        titre = m.group(2).strip()
                        if numero not in sections_vues:
                            sections.append((numero, titre))
                            sections_vues.add(numero)
    except Exception as exc:
        logger.error(f"Erreur lecture devis ARCH {devis_arch.name} : {exc}")
        console.print(f"[red]Erreur lecture {devis_arch.name} : {exc}[/red]")
        return

    logger.info(f"Table des matières : {len(sections)} sections MasterFormat détectées")

    # --- Génération du livrable ---
    lignes_tableau = ["| Section | Titre |", "|---------|-------|"]
    for numero, titre in sorted(sections):
        lignes_tableau.append(f"| {numero} | {titre} |")

    if not sections:
        lignes_tableau.append("| — | Aucune section MasterFormat détectée dans ce fichier |")

    contenu = (
        f"# Table des matières — Sections techniques\n\n"
        f"**Projet :** {nom_projet}  \n"
        f"**Devis source :** {devis_arch.name}  \n"
        f"**Extrait le :** {datetime.now().strftime('%Y-%m-%d %H:%M')}  \n"
        f"**Total sections :** {len(sections)}  \n\n"
        + "\n".join(lignes_tableau)
        + "\n\n---\n"
        "*Utiliser cette liste pour l'invitation des sous-traitants à soumissionner.*\n"
    )

    try:
        chemin_sortie.write_text(contenu, encoding="utf-8")
        logger.info(f"Livrable écrit : {LIVRABLE} ({len(sections)} sections)")
        console.print(
            f"[green]Table des matières : {len(sections)} section(s) -> {LIVRABLE}[/green]"
        )
    except Exception as exc:
        logger.error(f"Impossible d'écrire {LIVRABLE} : {exc}")
        console.print(f"[red]Erreur écriture {LIVRABLE} : {exc}[/red]")


# ---------------------------------------------------------------------------
def split_corpus_en_chunks(blocs: list[str], max_chars: int = 200_000) -> list[list[str]]:
    """
    Découpe une liste de blocs de texte en chunks de max_chars.
    Un bloc plus grand que max_chars forme un chunk à lui seul (pas de coupure interne).
    Retourne une liste de chunks, chaque chunk étant une liste de blocs.
    """
    chunks: list[list[str]] = []
    chunk_courant: list[str] = []
    taille_courante = 0

    for bloc in blocs:
        taille_bloc = len(bloc)
        if taille_courante + taille_bloc > max_chars and chunk_courant:
            chunks.append(chunk_courant)
            chunk_courant = [bloc]
            taille_courante = taille_bloc
        else:
            chunk_courant.append(bloc)
            taille_courante += taille_bloc

    if chunk_courant:
        chunks.append(chunk_courant)

    return chunks


# ---------------------------------------------------------------------------
# Analyse d'un projet
# ---------------------------------------------------------------------------

def analyser_projet(nom_projet: str, client: anthropic.Anthropic, forcer: bool = False, sans_ollama: bool = False) -> bool:
    """Orchestre l'analyse complète d'un projet. Retourne True si succès."""

    dossier_projet = RACINE_PROJETS / nom_projet
    if not dossier_projet.exists():
        console.print(f"[red]Erreur : projet introuvable -> {dossier_projet}[/red]")
        return False

    # Créer Analyse\ si nécessaire
    dossier_analyse = dossier_projet / DOSSIER_ANALYSE
    dossier_analyse.mkdir(exist_ok=True)

    logger = configurer_logging(dossier_analyse)
    logger.info(f"{'=' * 60}")
    logger.info(f"DÉBUT ANALYSE : {nom_projet}")
    logger.info(f"{'=' * 60}")

    console.print(
        Panel(
            f"[bold cyan]{nom_projet}[/bold cyan]",
            title="[bold]Analyse du projet[/bold]",
            subtitle=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )
    )

    # --- Registre ---
    registre = charger_registre(dossier_analyse)
    logger.info(f"Registre chargé : {len(registre)} fichier(s) déjà connu(s)")

    # --- Collecte ---
    fichiers_par_dossier = collecter_fichiers(dossier_projet, logger)

    table_src = Table(title="Sources", show_header=True, header_style="bold")
    table_src.add_column("Dossier", style="cyan")
    table_src.add_column("Statut")
    table_src.add_column("Fichiers", justify="right")
    for nom_d, info in fichiers_par_dossier.items():
        couleur = {"OK": "green", "VIDE": "yellow", "ABSENT": "red"}[info["statut"]]
        table_src.add_row(
            nom_d,
            f"[{couleur}]{info['statut']}[/{couleur}]",
            str(len(info["fichiers"])) if info["statut"] == "OK" else "—",
        )
    console.print(table_src)

    # --- Détection des nouveaux fichiers ---
    tous_fichiers: list[tuple[str, Path]] = []
    for nom_d, info in fichiers_par_dossier.items():
        for f in info["fichiers"]:
            tous_fichiers.append((nom_d, f))

    hashes_actuels: dict[str, tuple[str, Path]] = {}  # hash -> (dossier, chemin)
    for nom_d, chemin in tous_fichiers:
        try:
            h = calculer_md5(chemin)
            hashes_actuels[h] = (nom_d, chemin)
        except Exception as exc:
            logger.error(f"Impossible de lire {chemin.name} : {exc}")

    nouveaux_hashes = {h for h in hashes_actuels if h not in registre}

    if not nouveaux_hashes and not forcer:
        console.print("\n[green]Aucun nouveau fichier détecté. Les livrables sont à jour.[/green]")
        logger.info("Aucun nouveau fichier. Analyse ignorée.")
        return True

    if forcer and not nouveaux_hashes:
        console.print("\n[yellow]--forcer activé : régénération de tous les livrables.[/yellow]")
        logger.info("Mode --forcer : régénération forcée, registre ignoré.")

    noms_nouveaux = [hashes_actuels[h][1].name for h in nouveaux_hashes]
    if noms_nouveaux:
        console.print(
            f"\n[yellow]{len(nouveaux_hashes)} nouveau(x) fichier(s) détecté(s) :[/yellow] "
            + ", ".join(noms_nouveaux)
        )
    logger.info(f"Nouveaux fichiers : {', '.join(noms_nouveaux)}")

    # --- Extraction de TOUS les fichiers ---
    docs_extraits: list[dict] = []    # {nom, chemin_relatif, dossier, texte_brut}
    tmp_a_supprimer: list[Path] = []
    plans_info: list[tuple[str, int, int, int]] = []   # (nom, nb_devis, nb_total, chars)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        tache = progress.add_task("[cyan]Extraction des documents...", total=len(tous_fichiers))

        for nom_d, chemin in tous_fichiers:
            racine_source = fichiers_par_dossier[nom_d]["racine"]
            chemin_relatif = chemin.relative_to(racine_source)
            en_tete_base = (
                f"\n{'=' * 60}\n"
                f"FICHIER  : {chemin_relatif}\n"
                f"SOURCE   : {nom_d}\n"
                f"{'=' * 60}\n"
            )

            progress.update(tache, description=f"[cyan]Lecture : {str(chemin_relatif)[:50]}...")

            if chemin.suffix.lower() == ".pdf":
                if classifier_document(chemin.name, nom_d) == "PLANS":
                    texte, nb_dv, nb_tot = detect_pages_devis(chemin, logger)
                    plans_info.append((chemin.name, nb_dv, nb_tot, len(texte)))
                else:
                    texte = extraire_texte_pdf(chemin, logger)
                docs_extraits.append({
                    "nom": chemin.name,
                    "chemin_relatif": str(chemin_relatif),
                    "dossier": nom_d,
                    "texte_brut": en_tete_base + texte,
                })

            elif chemin.suffix.lower() == ".msg":
                corps, pj_pdfs = extraire_texte_msg(chemin, logger)
                docs_extraits.append({
                    "nom": chemin.name,
                    "chemin_relatif": str(chemin_relatif),
                    "dossier": nom_d,
                    "texte_brut": en_tete_base + corps,
                })
                for nom_pj, chemin_tmp in pj_pdfs:
                    if classifier_document(nom_pj, nom_d) == "PLANS":
                        texte_pj, nb_dv_pj, nb_tot_pj = detect_pages_devis(chemin_tmp, logger)
                        plans_info.append((nom_pj, nb_dv_pj, nb_tot_pj, len(texte_pj)))
                    else:
                        texte_pj = extraire_texte_pdf(chemin_tmp, logger)
                    docs_extraits.append({
                        "nom": nom_pj,
                        "chemin_relatif": f"{chemin_relatif} -> {nom_pj}",
                        "dossier": nom_d,
                        "texte_brut": f"\n--- Pièce jointe PDF : {nom_pj} (de : {chemin.name}) ---\n" + texte_pj,
                    })
                    tmp_a_supprimer.append(chemin_tmp)

            progress.advance(tache)

    # Supprimer les fichiers temporaires
    for tmp in tmp_a_supprimer:
        try:
            tmp.unlink()
        except Exception:
            pass

    # Affichage stats detect_pages_devis
    for nom_plan, nb_dv, nb_tot, chars in plans_info:
        console.print(
            f"  [dim]PLANS[/dim] {nom_plan[:40]} : "
            f"{nb_tot} pages analysées -> {nb_dv} pages de devis extraites "
            f"([{'green' if nb_dv > 0 else 'yellow'}]{chars:,} chars[/{'green' if nb_dv > 0 else 'yellow'}])"
        )

    # --- Classification et condensation par type ---
    console.print("\n[bold]Classification et condensation du corpus...[/bold]")

    taille_originale = sum(len(d["texte_brut"]) for d in docs_extraits)
    nb_exec_total = 0
    plans_exclus: list[str] = []

    for doc in docs_extraits:
        type_doc = classifier_document(doc["nom"], doc["dossier"])
        doc["type"] = type_doc
        doc["taille_brute"] = len(doc["texte_brut"])
        structure = None
        if type_doc == "DEVIS_ARCH":
            structure = detecter_structure_devis(
                doc["texte_brut"], doc["nom"], client, registre, dossier_analyse, logger
            )
        condense, nb_exec = traiter_document(doc["texte_brut"], type_doc, doc["nom"], structure=structure)
        doc["texte_condense"] = condense
        doc["taille_condensee"] = len(condense)
        nb_exec_total += nb_exec
        if type_doc == "PLANS" and not condense:
            plans_exclus.append(doc["nom"])

    # Déduplication des DEVIS_ARCH entre eux
    blocs_arch = [
        (i, d["texte_condense"])
        for i, d in enumerate(docs_extraits)
        if d["type"] == "DEVIS_ARCH" and d["texte_condense"]
    ]
    cg_block = ""
    nb_dedup = 0
    devis_arch_condense: list[str] = [t for _, t in blocs_arch]

    if len(blocs_arch) >= 3:
        corpus_arch, nb_dedup = dedupliquer_corpus([t for _, t in blocs_arch])
        if nb_dedup > 0:
            cg_marker = "=== CONDITIONS GÉNÉRALES APPLICABLES À TOUTES LES SECTIONS ==="
            end_marker = "\n\n==="
            if corpus_arch.startswith(cg_marker):
                sep = corpus_arch.find(end_marker)
                if sep != -1:
                    cg_block = corpus_arch[:sep + len(end_marker)]
                    devis_arch_condense = [corpus_arch[sep + len(end_marker):].lstrip("\n")]

    # Tableau de stats par document
    table_docs = Table(title="Classification et condensation", header_style="bold magenta")
    table_docs.add_column("Fichier", style="cyan", max_width=38)
    table_docs.add_column("Type", style="yellow")
    table_docs.add_column("Brut", justify="right")
    table_docs.add_column("Condensé", justify="right")
    table_docs.add_column("Réduction", justify="right", style="green")

    taille_condensee_total = 0
    for doc in docs_extraits:
        brut = doc["taille_brute"]
        cond = doc["taille_condensee"]
        taille_condensee_total += cond
        pct = (1 - cond / max(brut, 1)) * 100 if brut > 0 else 0
        table_docs.add_row(
            doc["nom"][:36],
            doc["type"],
            f"{brut:,}",
            "[dim]exclu[/dim]" if (doc["type"] == "PLANS" and not doc["texte_condense"]) else f"{cond:,}",
            f"−{pct:.0f} %" if pct > 1 else "—",
        )
    console.print(table_docs)

    if plans_exclus:
        console.print(
            f"[dim]Plans exclus ({len(plans_exclus)}) : " + ", ".join(plans_exclus) + "[/dim]"
        )

    # --- Assemblage du corpus dans l'ordre défini ---
    blocs_assembles: list[str] = []
    if cg_block:
        blocs_assembles.append(cg_block)

    for type_key in sorted(TYPE_ORDRE, key=lambda t: TYPE_ORDRE[t]):
        if type_key == "PLANS":
            # Inclure uniquement les plans contenant des pages de devis
            groupe_plans = sorted(
                [d for d in docs_extraits if d["type"] == type_key and d["texte_condense"]],
                key=lambda d: d["nom"],
            )
            blocs_assembles.extend(d["texte_condense"] for d in groupe_plans)
            continue
        if type_key == "DEVIS_ARCH":
            blocs_assembles.extend(devis_arch_condense)
            continue
        groupe = sorted(
            [d for d in docs_extraits if d["type"] == type_key and d["texte_condense"]],
            key=lambda d: d["nom"],
            reverse=(type_key == "ADDENDA"),   # addendas : plus récent en premier
        )
        blocs_assembles.extend(d["texte_condense"] for d in groupe)

    # Notices dossiers absents/vides + plans exclus
    notices: list[str] = []
    for nom_d in DOSSIERS_SOURCE:
        info = fichiers_par_dossier.get(nom_d, {"statut": "ABSENT"})
        if info["statut"] == "ABSENT":
            notices.append(f"[DOSSIER {nom_d} : ABSENT DU PROJET]")
        elif info["statut"] == "VIDE":
            notices.append(f"[DOSSIER {nom_d} : VIDE — AUCUN DOCUMENT]")
    if plans_exclus:
        notices.append(
            "[Plans exclus du corpus Claude — analyse visuelle requise : "
            + ", ".join(plans_exclus) + "]"
        )

    header_projet = (
        f"# PROJET : {nom_projet}\n"
        f"# DATE D'ANALYSE : {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"# NOUVEAUX FICHIERS : {', '.join(noms_nouveaux) or '(régénération forcée)'}\n"
        + ("\n".join(notices) + "\n" if notices else "")
        + "\n"
    )

    contexte_global = header_projet + "\n\n".join(blocs_assembles)
    pct_total = (1 - len(contexte_global) / max(taille_originale, 1)) * 100
    console.print(
        f"\nCorpus final : [bold]{len(contexte_global):,}[/bold] car. "
        f"/ brut : {taille_originale:,} car. "
        f"([green]−{pct_total:.1f} %[/green]) | "
        f"{nb_exec_total} sections EXEC sautées | {nb_dedup} paras dédupliqués"
    )
    logger.info(
        f"Corpus : {taille_originale:,} -> {len(contexte_global):,} car. "
        f"(−{pct_total:.1f} %) | {nb_exec_total} EXEC | {nb_dedup} dédupliqués"
    )

    # --- Split corpus en chunks de 200K ---
    CHUNK_MAX = 200_000
    chunks = split_corpus_en_chunks(blocs_assembles, max_chars=CHUNK_MAX)
    tailles = [sum(len(b) for b in c) for c in chunks]
    console.print(
        f"Corpus splitté en [bold]{len(chunks)}[/bold] chunk(s) : "
        f"[{', '.join(f'{t // 1000}kb' for t in tailles)}]"
    )
    logger.info(f"Split corpus : {len(chunks)} chunk(s) — tailles : {tailles}")

    # --- Table des matières technique (livrable 00 — sans API) ---
    generer_table_matieres_technique(dossier_projet, dossier_analyse, nom_projet, logger)

    # --- Appels 01-08 : un par chunk, fusion JSON ---
    donnees_01_08: dict = {}
    for i, chunk_blocs in enumerate(chunks, 1):
        contexte_chunk = header_projet + "\n\n".join(chunk_blocs)
        console.print(
            f"\n[bold]Appel 01-08 chunk {i}/{len(chunks)} "
            f"({len(contexte_chunk) // 1000}kb)...[/bold]"
        )
        logger.info(f"Appel 01-08 chunk {i}/{len(chunks)} ({len(contexte_chunk):,} car.)")
        reponse = appeler_claude(
            client, contexte_chunk, PROMPT_JSON_01_08,
            f"Sections 01-08 chunk {i}/{len(chunks)}", logger, max_tokens=MAX_TOKENS_01_08,
        )
        donnees_chunk = extraire_json_reponse(reponse, logger)
        if not donnees_chunk:
            fname = f"reponse_brute_01_08_chunk{i}.txt"
            console.print(f"[red]Erreur : JSON invalide (chunk {i}).[/red]")
            console.print(f"[yellow]Réponse brute sauvegardée -> {fname}[/yellow]")
            (dossier_analyse / fname).write_text(reponse, encoding="utf-8")
            logger.error(f"Réponse JSON invalide chunk {i} — sauvegardée dans {fname}")
        else:
            for cle, valeur in donnees_chunk.items():
                if cle in donnees_01_08:
                    donnees_01_08[cle] += f"\n\n---\n\n{valeur}"
                else:
                    donnees_01_08[cle] = valeur

    # --- Appel 09 : corpus complet tronqué à CHUNK_MAX si nécessaire ---
    corpus_09 = header_projet + "\n\n".join(blocs_assembles)
    if len(corpus_09) > CHUNK_MAX:
        corpus_09 = corpus_09[:CHUNK_MAX] + "\n\n[... CONTEXTE TRONQUÉ ...]"
        logger.warning(f"Corpus 09 tronqué à {CHUNK_MAX:,} car.")
    console.print(
        f"\n[bold]Appel 09 — Rapport estimateur ({len(corpus_09) // 1000}kb)...[/bold]"
    )
    logger.info(f"Appel 09 ({len(corpus_09):,} car.)")
    reponse_09 = appeler_claude(
        client, corpus_09, PROMPT_JSON_09,
        "Section 09", logger, max_tokens=MAX_TOKENS_09,
    )
    donnees_09 = extraire_json_reponse(reponse_09, logger)
    if not donnees_09:
        console.print("[red]Erreur : impossible de parser la réponse JSON (appel 09).[/red]")
        console.print("[yellow]Réponse brute sauvegardée -> reponse_brute_09.txt[/yellow]")
        (dossier_analyse / "reponse_brute_09.txt").write_text(reponse_09, encoding="utf-8")
        logger.error("Réponse JSON invalide (appel 09) — sauvegardée dans reponse_brute_09.txt")

    donnees = {**donnees_01_08, **donnees_09}

    if not donnees:
        console.print("[red]Aucune section générée — vérifiez les fichiers reponse_brute_*.txt[/red]")
    else:
        date_analyse = datetime.now().strftime("%Y-%m-%d %H:%M")
        fichiers_analyses = ", ".join(noms_nouveaux) or "(régénération forcée)"
        for nom_fichier, titre in SECTIONS:
            cle = nom_fichier.replace(".md", "")
            contenu = donnees.get(cle, f"[SECTION ABSENTE DE LA RÉPONSE JSON : {cle}]")
            en_tete_md = (
                f"# {titre}\n\n"
                f"**Projet :** {nom_projet}  \n"
                f"**Date d'analyse :** {date_analyse}  \n"
                f"**Nouveaux fichiers analysés :** {fichiers_analyses}  \n\n"
                f"---\n\n"
            )
            chemin_sortie = dossier_analyse / nom_fichier
            try:
                chemin_sortie.write_text(en_tete_md + contenu, encoding="utf-8")
                logger.info(f"Livrable écrit : {nom_fichier} ({len(contenu):,} car.)")
            except Exception as exc:
                logger.error(f"Impossible d'écrire {nom_fichier} : {exc}")
                console.print(f"[red]Erreur écriture {nom_fichier} : {exc}[/red]")
        console.print(f"[green]{len(donnees)} section(s) générée(s) depuis la réponse JSON.[/green]")

    # --- Mise à jour du registre ---
    for h, (nom_d, chemin) in hashes_actuels.items():
        registre[h] = {
            "nom": chemin.name,
            "dossier": nom_d,
            "date_analyse": datetime.now().isoformat(),
            "taille_octets": chemin.stat().st_size,
        }
    sauvegarder_registre(dossier_analyse, registre)
    logger.info(f"Registre sauvegardé : {len(registre)} fichier(s) au total")

    # --- Résumé final ---
    console.print(
        Panel(
            f"[green]Analyse terminée avec succès.[/green]\n\n"
            f"Nouveaux fichiers traités : [bold]{len(nouveaux_hashes)}[/bold]{'  [yellow](forcé)[/yellow]' if forcer else ''}\n"
            f"Livrables dans           : [cyan]{dossier_analyse}[/cyan]\n"
            f"Log                      : [cyan]{dossier_analyse / LOG_FICHIER}[/cyan]",
            title="[bold green]Résumé[/bold green]",
            border_style="green",
        )
    )

    logger.info(f"FIN ANALYSE : {nom_projet}")
    logger.info(f"{'=' * 60}")
    return True


# ---------------------------------------------------------------------------
# Utilitaire : liste des projets
# ---------------------------------------------------------------------------

def lister_projets() -> list[str]:
    if not RACINE_PROJETS.exists():
        return []
    return sorted(
        item.name
        for item in RACINE_PROJETS.iterdir()
        if item.is_dir() and not item.name.startswith(".")
    )


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyseur de soumissions pour entrepreneur général au Québec",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples :
  python analyser_soumission.py --projet "S-26-001 - École Primaire Laval"
  python analyser_soumission.py --tous
  python analyser_soumission.py --lister
""",
    )

    groupe = parser.add_mutually_exclusive_group(required=True)
    groupe.add_argument(
        "--projet",
        metavar="NOM_DOSSIER",
        help="Nom exact du sous-dossier projet à analyser",
    )
    groupe.add_argument(
        "--tous",
        action="store_true",
        help="Analyser tous les projets dans le dossier racine",
    )
    parser.add_argument(
        "--forcer",
        action="store_true",
        help="Ignorer le registre MD5 et régénérer tous les livrables",
    )
    parser.add_argument(
        "--sans-ollama",
        dest="sans_ollama",
        action="store_true",
        help="(Conservé pour compatibilité — sans effet, la condensation est assurée par des filtres Python natifs)",
    )
    groupe.add_argument(
        "--lister",
        action="store_true",
        help="Lister les projets disponibles sans analyser",
    )

    args = parser.parse_args()

    # --lister ne nécessite pas de clé API
    if args.lister:
        projets = lister_projets()
        table = Table(title=f"Projets disponibles ({len(projets)})", header_style="bold")
        table.add_column("Projet", style="cyan")
        table.add_column("Analyse existante", justify="center")
        for p in projets:
            a_analyse = (RACINE_PROJETS / p / DOSSIER_ANALYSE).exists()
            table.add_row(p, "[green]Oui[/green]" if a_analyse else "[dim]Non[/dim]")
        console.print(table)
        return

    # Vérifier clé API
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        console.print(
            "[red]Erreur : variable d'environnement ANTHROPIC_API_KEY non définie.[/red]\n"
            "Sous Windows, exécutez :\n"
            "  [bold]set ANTHROPIC_API_KEY=sk-ant-...[/bold]  (session courante)\n"
            "ou ajoutez-la dans les variables d'environnement système."
        )
        sys.exit(1)

    if not RACINE_PROJETS.exists():
        console.print(f"[red]Erreur : dossier racine introuvable -> {RACINE_PROJETS}[/red]")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    if args.tous:
        projets = lister_projets()
        if not projets:
            console.print("[yellow]Aucun projet trouvé dans le dossier racine.[/yellow]")
            return
        console.print(f"[bold]{len(projets)} projet(s) à analyser.[/bold]")
        for i, nom in enumerate(projets, 1):
            console.rule(f"Projet {i}/{len(projets)}")
            analyser_projet(nom, client, forcer=args.forcer, sans_ollama=args.sans_ollama)
    else:
        analyser_projet(args.projet, client, forcer=args.forcer, sans_ollama=args.sans_ollama)


if __name__ == "__main__":
    main()
