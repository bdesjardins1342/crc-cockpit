# CRC COCKPIT — Document de référence
Version 1.1 — Mars 2026 | Benoit Desjardins | CRC | Montréal

---

## 1. Vision & Architecture

**Concept** : Système d'automatisation pour EG — analyse soumissions, suivi projets, gestion documentaire. Interface principale : Cowork (commandes à distance) + cockpit web local.
```
[Benoit à distance] → [Cowork] → [Ordi dédié local]
  ├── Cockpit HTML (localhost)
  ├── Scripts Python (skills)
  ├── Base de données SQLite
  └── Intégrations (email, SEAO, fichiers)
```

**Infrastructure installée :**
- Chrome + Claude in Chrome ✅
- Git + repo GitHub : https://github.com/bdesjardins1342/crc-cockpit.git ✅
- Node.js v24.14.0 ✅
- Claude Code : `npm install -g @anthropic-ai/claude-code` ✅
- ANTHROPIC_API_KEY : variable d'environnement permanente (User) ✅
- Ollama : installé mais RETIRÉ — Python pur utilisé ⚠️

**Racine projets** : `C:\Users\BenoitDesjardins\Documents\Claude\Projet 2026`

---

## 2. Cockpit HTML

**Fichier** : `C:\Users\BenoitDesjardins\Documents\Claude\Projet 2026\cockpit.html`

**Design** : thème sombre industriel, header horloge live + statut Cowork, sidebar navigation, 4 KPIs, tableau AO, grille 6 skills, feed activité.

**Boutons à brancher (FastAPI local) :**
| Bouton | Script |
|--------|--------|
| 📋 ANALYSER DOSSIER AO | analyser_soumission.py |
| 🚨 VÉRIFIER ADDENDAS | analyser_soumission.py --addenda |
| 📊 SUIVI SOUMISSIONS | suivre_soumissions.py |
| 🏆 POST-MORTEM | postmortem.py |

---

## 3. Structure des projets

**Numérotation** : Soumissions `S-26-001`, Projets obtenus `P26001`
**13 projets** : S-26-001 à S-26-023 + dossier "Non obtenu"

**Structure interne (ex: S-26-010) :**
```
S-26-010 - Réaménagement Radio-oncologie CHAUR\
  Addenda\ Administration\ Bon de commande\ CNESST\ Contrat\
  Courriel & Correspondance\ DIRECTIVE ET ORDRE DE CHANGEMENT\
  Échéancier\ Fiche technique\ Fin des travaux - Documents\
  Liste intervenants\ Photos\ Plan & Devis\
    01 - Devis Soumission\ 02 - Plans pour soumission\
  QRT\ Régies Contrôlées\ Réquisition\ Soumission\
  Soumissions reçues\ Visite etou réunion\
  CRC-FORM-04 - Contrôle Budgétaire_V1.xlsm
```

---

## 4. Script analyser_soumission.py

**Localisation** : `C:\Users\BenoitDesjardins\Documents\Claude\Projet 2026\analyser_soumission.py`

**CLI :**
```powershell
python analyser_soumission.py --projet "S-26-010 - Nom"   # un projet
python analyser_soumission.py --tous                       # tous les projets
python analyser_soumission.py --lister                     # liste sans analyser
python analyser_soumission.py --forcer                     # régénère même si registre OK
python analyser_soumission.py --sans-ollama                # no-op (compat.)
```

**Constantes :**
- MAX_CONTEXTE_CHARS : 800,000
- MODELE_CLAUDE : claude-sonnet-4-6
- MAX_TOKENS_01_08 : 60,000 | MAX_TOKENS_09 : 16,000
- Split corpus : bin-packing intelligent, max 200,000 chars/chunk, jamais couper un document
- Appels API : N chunks (01-08) + 1 appel séparé (09 rapport estimateur)
- Streaming activé (évite timeout >10 min)
- Registre MD5 (registre.json) — évite re-analyse si fichiers inchangés
- Parsing JSON : 4 fallbacks, compatible Windows \r\n
- Récupération partielle si JSON tronqué (≥3 clés)
- Warnings ToUnicode PDF supprimés (logging.ERROR sur pdfminer)

### Stratégie 1 — CONTRAT, RÉGIE : Extraction ciblée par regex

Fonction `extraire_champs_contrat(texte)` — fenêtre 300 chars autour de chaque match.

Champs extraits :
- Cautionnement (soumission, exécution, main-d'œuvre)
- Assurances (RC générale, chantier, tous risques, wrap-up) + montants
- Dates (début, fin, durée en jours)
- Pénalités (montant/jour, plafond)
- Retenues (%, conditions de libération)
- Travail de soir/nuit (restrictions, majorations)
- Résiliation (motifs, contexte)
- **ANNEXES** : détection pattern `ANNEXE \w+` + 2000 chars suivants ← PENDING

Format sortie : `=== INFORMATIONS CONTRACTUELLES EXTRAITES ===`
Champs absents : `ABSENT — vérifier manuellement`

**Résultats S-26-010** : Contrat 238K → 7,780 chars (-97%) ✅ | Régie 87K → 1,950 chars (-98%) ✅

### Stratégie 2 — DEVIS_ARCH : Filtrage en 3 couches

**Couche 1 — Règle MasterFormat (sections 02 00 00+) :**
- Séparation TOC/corps : tout avant le premier "Partie 1" = table des matières → Signal/Bruit seulement
- Corps du texte : split par `(?=\bPartie\s*[123]\b)`
- PARTIE 1 / GÉNÉRALITÉS → SUPPRIMER
- PARTIE 2 / PRODUITS → GARDER intégralement
- PARTIE 3 / EXÉCUTION → SUPPRIMER
- Format section détecté par pré-analyse Claude API (échantillon 1/10 pages, confiance retournée)
- Résultat mis en cache dans registre.json par MD5

**Couche 2 — Signal/Bruit (sections 01 et TOC) :**
- Seuil >= 2 pour garder. Helper partagé : `_scorer_paragraphe(para)`
- SIGNAL +3 : chiffres+$/%/jour, mots contractuels (pénalité, retenue, BSDQ, amiante, PCI, milieu occupé), MAJUSCULES≥5, dates
- SIGNAL +2 : obligatoire/interdit/requis, coordination spécifique, approbation requise
- BRUIT -2 : "règles de l'art", codes/normes seuls, nettoyage générique, garantie standard
- BRUIT -3 : titres seuls, listes fabricants, "se conformer à" seul
- Liste noire (score -5) : "selon les règles de l'art", "tel qu'indiqué aux plans", "coordonner avec les" (sans nom)...

**Couche 3 — Exception universelle (override) :**
- Garder TOUJOURS si contient : pénalité, $/jour, point d'arrêt, BSDQ, amiante, PCI, infection nosocomiale, milieu occupé, date spécifique
- Tag `[CRITIQUE]` ajouté

Affichage : `ARCH [fichier] : P1 supprimées: Xkb | P2 gardées: Xkb | P3 supprimées: Xkb | Exceptions: N | Score/Bruit: N supprimés`

**Résultats S-26-010** : 462K → 140K (-70%) ✅

### Stratégie 3 — DEVIS_ADMIN : Signal/Bruit seul (seuil >= 1)
⚠️ PENDING : 0% de réduction actuellement

### PLANS — Détection devis intégrés
Fonction `detect_pages_devis(pdf_path)` :
- Score +5 : pattern section MasterFormat avec tiret (ex: `26 05 00 —`)
- Score +3 : titres de devis reconnus (16 titres dans _TITRES_DEVIS_PLANS)
- Score +2 : densité texte > 2000 chars/page
- Score -3 : page graphique (grille plan + texte < 1000 chars)
- Si score >= 5 → extraire et traiter comme DEVIS_ADMIN | Si score < 5 → ignorer

**Résultats S-26-010 PLANS** :
- 2506-155A D_plans ARCH : 16 pages → 6 pages devis (81,374 chars)
- Pluritec MB : 9 pages → 6 pages (106,432 chars)
- Pluritec S : 4 pages → 2 pages (12,751 chars)
- Pluritec E : 17 pages → 9 pages (103,762 chars)

### Résultats de filtrage — S-26-010

| Document | Brut | Condensé | Réduction | Statut |
|----------|------|----------|-----------|--------|
| Contrat | 238,906 | 7,780 | -97% | ✅ |
| Régie | 87,088 | 1,950 | -98% | ✅ |
| Devis ARCH | 462,340 | 140,596 | -70% | ✅ |
| Devis Admin | 51,668 | 51,667 | 0% | ⚠️ PENDING |
| PLANS (4) | 305,277 | 304,819 | ~0% | ⚠️ PENDING |
| Soumissions reçues | ~7,000 | ~1,000 | -86% à -89% | ✅ |
| **Corpus final** | **1,198K** | **554K** | **-53.7%** | ✅ sous 800K |

**Split chunks S-26-010** : 4 chunks [196kb, 134kb, 103kb, 119kb]

### Livrables générés dans `Analyse\`
```
00_table_matières_technique.md    ← PENDING
01_table_documentaire.md
02_délais_échéancier.md
03_pénalités_retenues.md
04_assurances_garanties.md
05_responsabilités_EG.md
06_risques_techniques.md
07_BSDQ_sous-traitance.md
08_soumissions_reçues.md
09_rapport_estimateur.md
log_analyse.txt
registre.json
reponse_brute_01_08.txt
reponse_brute_09.txt
```

**Qualité des livrables** : détaillée et utilisable — jalons, préavis, horaires, phasage bien capturés.
**Problème identifié** : ANNEXES du contrat (ex: ANNEXE 0.01.13 - ÉCHÉANCIER à page 75) non capturées → PENDING

### Coûts API
- Tarif Sonnet : $3 USD/M tokens input | $15 USD/M tokens output
- Long context >200K tokens : $6 USD/M input
- Par analyse S-26-010 (4 chunks) : ~$1-2 USD estimé
- 50 analyses/an : ~$50-100 USD — acceptable

---

## 5. Architecture 3 phases

| Phase | Nom | Script | Statut |
|-------|-----|--------|--------|
| 1 | Analyse dossier AO | analyser_soumission.py | ✅ Fonctionnel |
| 1b | Alertes addendas | analyser_soumission.py --addenda | ⬜ PENDING |
| 2 | Suivi soumissions reçues | suivre_soumissions.py | ⬜ PENDING |
| 3 | Post-mortem projet | postmortem.py (SQLite) | ⬜ PENDING |

---

## 6. Statut

### ✅ Complété
- Cockpit HTML créé et affiché dans Chrome via Cowork
- Git configuré + repo GitHub public
- Node.js + Claude Code installés
- ANTHROPIC_API_KEY configurée
- Script analyser_soumission.py fonctionnel — 9 livrables générés
- Suppression doublons fichiers (set() sur chemins abspath)
- Optimisation : 9 appels → N chunks + 1 appel API
- Streaming activé (évite timeout >10 min)
- Fix parsing JSON Windows (\r\n)
- Suppression complète Ollama — Python pur
- Stratégie extraction ciblée CONTRAT/RÉGIE (-97%/-98%)
- Filtrage MasterFormat P1/P2/P3 DEVIS_ARCH (-70%)
- Pré-analyse structure devis via Claude API (confiance retournée, cache MD5)
- Split corpus bin-packing intelligent (max 200K/chunk, jamais couper un doc)
- MAX_CONTEXTE_CHARS monté à 800,000
- Scan récursif sous-dossiers Plan & Devis
- Détection pages de devis intégrées aux plans (Pluritec E11-E13)
- Registre MD5 pour éviter re-analyse
- Warnings ToUnicode PDF supprimés

### ⬜ Pending
- Capturer ANNEXES du contrat dans extraire_champs_contrat() (ex: ANNEXE 0.01.13 - ÉCHÉANCIER)
- DEVIS_ADMIN : améliorer filtrage (0% réduction actuellement)
- Implémenter 00_table_matières_technique.md
- Implémenter mode --addenda
- Créer suivre_soumissions.py
- Brancher boutons cockpit via FastAPI local
- Créer postmortem.py avec SQLite

---

## 7. Notes techniques

**Commande de test standard :**
```powershell
python analyser_soumission.py --projet "S-26-010 - Réaménagement Radio-oncologie CHAUR" --forcer
```

**Patterns regex clés :**
- Section MasterFormat : `r'(?:^|\n)((?:0[2-9]|[1-9]\d)\s+\d{2}\s+\d{2})\s*[-–—]?\s*([A-Z][^\n]{3,60})'`
- Split Parties : `r'(?=\bPartie\s*[123]\b)'`
- Termes critiques : `pénalité | $/jour | point d'arrêt | BSDQ | amiante | PCI`
- Annexes contrat : `r'(?i)ANNEXE\s+\w+[\d\.]*\s*[-–—]?\s*[^\n]{5,60}'` ← PENDING

**Usage en début de session Claude :**
> Coller l'URL raw : https://raw.githubusercontent.com/bdesjardins1342/crc-cockpit/main/reference.md
