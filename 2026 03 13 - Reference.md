# CRC COCKPIT — Document de référence
Version 1.0 — Mars 2026 | Benoit Desjardins | CRC | Montréal

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
- MAX_CONTEXTE_CHARS : 250,000
- 2 appels API Claude par analyse (sections 01-08 + section 09)
- Registre MD5 (registre.json) — évite re-analyse si fichiers inchangés
- Parsing JSON : 4 fallbacks, compatible Windows \r\n
- Récupération partielle si JSON tronqué (≥3 clés)

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

Format sortie : `=== INFORMATIONS CONTRACTUELLES EXTRAITES ===`
Champs absents : `ABSENT — vérifier manuellement`

**Résultats S-26-010** : Contrat 238K → 7,780 chars (-97%) ✅ | Régie 87K → 1,950 chars (-98%) ✅

### Stratégie 2 — DEVIS_ARCH : Filtrage en 3 couches

**Couche 1 — Règle MasterFormat (sections 02 00 00+) :**
- PARTIE 1 / GÉNÉRALITÉS → SUPPRIMER
- PARTIE 2 / PRODUITS → GARDER intégralement
- PARTIE 3 / EXÉCUTION → SUPPRIMER
- Détection section : `r'\b(0[2-9]|[1-9]\d)\s+\d{2}\s+\d{2}\b'`

**Couche 2 — Signal/Bruit (sections 01 et préambule) :**
- Seuil >= 2 pour garder. Helper partagé : `_scorer_paragraphe(para)`
- SIGNAL +3 : chiffres+$/%/jour, mots contractuels (pénalité, retenue, BSDQ, amiante, PCI, milieu occupé), MAJUSCULES≥5, dates
- SIGNAL +2 : obligatoire/interdit/requis, coordination spécifique, approbation requise
- BRUIT -2 : "règles de l'art", codes/normes seuls, nettoyage générique, garantie standard
- BRUIT -3 : titres seuls, listes fabricants, "se conformer à" seul
- Liste noire (score -5) : "selon les règles de l'art", "tel qu'indiqué aux plans", "coordonner avec les" (sans nom), etc.

**Couche 3 — Exception universelle (override) :**
- Garder TOUJOURS si contient : pénalité, $/jour, point d'arrêt, BSDQ, amiante, PCI, infection nosocomiale, milieu occupé, date spécifique
- Tag `[CRITIQUE]` ajouté au paragraphe gardé

Affichage : `ARCH [fichier] : P1 supprimées: Xkb | P2 gardées: Xkb | P3 supprimées: Xkb | Exceptions: N | Score/Bruit: N supprimés`

### Stratégie 3 — DEVIS_ADMIN : Signal/Bruit seul (seuil >= 1)

### PLANS — Nouveau comportement (PENDING)

Fonction `detect_pages_devis(pdf_path)` :
- Score +5 : pattern section MasterFormat avec tiret (ex: `26 05 00 —`)
- Score +3 : titres de devis reconnus (16 titres dans _TITRES_DEVIS_PLANS)
- Score +2 : densité texte > 2000 chars/page
- Score -3 : page graphique (grille plan + texte < 1000 chars)
- Si score >= 5 → extraire et traiter comme DEVIS_ADMIN
- Si score < 5 → ignorer

Affichage : `PLANS [fichier] : [N] pages analysées → [M] pages de devis extraites ([K] chars)`
Cas d'usage : plans Pluritec E11/E12/E13 contiennent des devis intégrés.

### Résultats de filtrage — S-26-010

| Document | Brut | Condensé | Réduction | Statut |
|----------|------|----------|-----------|--------|
| Contrat | 238,906 | 7,780 | -97% | ✅ |
| Régie | 87,088 | 1,950 | -98% | ✅ |
| Devis ARCH | 462,340 | 340,439 | -26% | ⚠️ PENDING |
| Devis Admin | 51,668 | 51,667 | 0% | ⚠️ PENDING |
| PLANS | 166,572+ | exclu | -100% | ⚠️ PENDING |
| Soumissions reçues | ~7,000 | ~1,000 | -86% à -89% | ✅ |

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

### Coûts API

- Tarif Sonnet : $3 USD/M tokens input | $15 USD/M tokens output
- Par analyse (~250K chars) : ~$0.41 USD
- 50 analyses/an : ~$20 USD

---

## 5. Architecture 3 phases

| Phase | Nom | Script | Statut |
|-------|-----|--------|--------|
| 1 | Analyse dossier AO | analyser_soumission.py | ✅ En cours |
| 1b | Alertes addendas | analyser_soumission.py --addenda | ⬜ PENDING |
| 2 | Suivi soumissions reçues | suivre_soumissions.py | ⬜ PENDING |
| 3 | Post-mortem projet | postmortem.py (SQLite) | ⬜ PENDING |

---

## 6. Statut

### ✅ Complété
- Cockpit HTML créé et affiché dans Chrome via Cowork
- Git configuré + repo GitHub
- Node.js + Claude Code installés
- ANTHROPIC_API_KEY configurée
- Script analyser_soumission.py fonctionnel (livrables .md générés)
- Optimisation : 9 appels → 2 appels API
- Fix parsing JSON Windows (\r\n)
- Suppression complète Ollama — Python pur uniquement
- Stratégie extraction ciblée CONTRAT/RÉGIE (-97%/-98%)
- Architecture 3 couches filtrage DEVIS_ARCH (code écrit)
- MAX_CONTEXTE_CHARS monté à 250,000
- Scan récursif sous-dossiers Plan & Devis
- Classification documents par type
- Registre MD5 pour éviter re-analyse

### ⬜ Pending
- Implémenter couche MasterFormat P1/P2/P3 dans filtrer_devis_arch() → objectif 462K → <80K
- Implémenter liste noire phrases exactes (score -5) dans filtrer_signal_bruit()
- Implémenter detect_pages_devis() pour plans Pluritec E11-E13
- Relancer S-26-010 avec --forcer après ces changements
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
- Section MasterFormat : `r'\b(0[2-9]|[1-9]\d)\s+\d{2}\s+\d{2}\b'`
- PARTIE 1 : `r'PARTIE\s*1|1\.\s*GÉNÉRALIT|SECTION\s+1\b'`
- PARTIE 2 : `r'PARTIE\s*2|2\.\s*PRODUIT|SECTION\s+2\b'`
- PARTIE 3 : `r'PARTIE\s*3|3\.\s*EXÉCUTION|SECTION\s+3\b'`
- Termes critiques : `pénalité | $/jour | point d'arrêt | BSDQ | amiante | PCI`

**Usage en début de session Claude :**
> "Voici mon doc de référence : https://raw.githubusercontent.com/bdesjardins1342/crc-cockpit/main/REFERENCE.md"
