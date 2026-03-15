# CRC COCKPIT — Document de référence
Version 1.2 — Mars 2026 | Benoit Desjardins | CRC | Montréal

---

## 1. Vision & Architecture

**Concept** : Système d'automatisation pour EG — analyse soumissions, suivi projets, intelligence marché. Interface principale : Cowork (commandes à distance) + cockpit web local sur `http://localhost:8000`.
```
[Benoit à distance] → [Cowork] → [Ordi dédié local]
  ├── Cockpit HTML (http://localhost:8000)
  ├── Scripts Python (analyser_soumission.py, seao_scraper.py)
  ├── Serveur FastAPI (serveur_cockpit.py)
  ├── Base de données SQLite (seao.db)
  └── Intégrations (SEAO, fichiers projets)
```

**Infrastructure installée :**
- Chrome + Claude in Chrome ✅
- Git + repo GitHub public : https://github.com/bdesjardins1342/crc-cockpit.git ✅
- Node.js v24.14.0 ✅
- Claude Code : `npm install -g @anthropic-ai/claude-code` ✅
- ANTHROPIC_API_KEY : variable d'environnement permanente (User) ✅
- FastAPI + uvicorn : `pip install fastapi uvicorn` ✅
- Ollama : installé mais RETIRÉ — Python pur utilisé ⚠️

**Racine projets** : `C:\Users\BenoitDesjardins\Documents\Claude\Projet 2026`
**Racine cockpit** : `C:\Users\BenoitDesjardins\Documents\Claude\crc-cockpit`

**Lancer le serveur :**
```powershell
cd C:\Users\BenoitDesjardins\Documents\Claude\crc-cockpit
python -m uvicorn serveur_cockpit:app --reload --port 8000
```

---

## 2. Fichiers du repo crc-cockpit

| Fichier | Rôle |
|---------|------|
| `cockpit v1.1.html` | Dashboard principal servi par FastAPI |
| `serveur_cockpit.py` | Serveur FastAPI — routes analyse + SEAO |
| `analyser_soumission.py` | Script d'analyse de dossiers AO |
| `seao_scraper.py` | Scraper données ouvertes SEAO |
| `seao.db` | Base SQLite — AOs, soumissions, mes données |
| `sync_seao.bat` | Script batch pour sync hebdo |
| `setup_tache_planifiee.bat` | Crée la tâche Windows Task Scheduler (1x admin) |
| `logs/sync_seao.log` | Log des syncs automatiques |
| `reference.md` | Ce fichier |

---

## 3. Cockpit HTML — Pages

**Navigation sidebar :**
- **Analyser AO** — lancer analyse + voir projets récents
- **Projets** — liste complète avec statuts
- **Livrables** — consulter les .md générés avec rendu markdown
- **Marché** — intelligence SEAO (nouveau)

**Page Marché :**
- Filtre par année (2023/24/25/26) ou date custom
- Bouton "Mes AOs" — filtre sur CRC seulement
- KPIs : AO en DB, mes soumissions, profit estimé, position moyenne
- Tableau AO avec pastilles colorées (🟢 rang 1, 🟡 rang 2-3, 🔴 rang 4+)
- Sidebar compétiteurs avec stats
- Modal détail AO : tous les soumissionnaires, saisie montants manuels, saisie marge
- Page paramètres (⚙) : mon NEQ, seuils, bouton sync manuel

**Boutons à venir (FastAPI branché) :**
| Bouton | Script |
|--------|--------|
| 🚨 VÉRIFIER ADDENDAS | analyser_soumission.py --addenda |
| 📊 SUIVI SOUMISSIONS | suivre_soumissions.py |
| 🏆 POST-MORTEM | postmortem.py |

---

## 4. Module SEAO

### seao_scraper.py

**Source données :** Données Québec — fichiers JSON hebdo (~15 Mo) et mensuel (~63 Mo)
**Standard :** Open Contracting Data Standard (OCDS), depuis mars 2021
**API catalogue :** `https://www.donneesquebec.ca/recherche/api/3/action/package_show?id=systeme-electronique-dappel-doffres-seao`

**Filtres appliqués :**
- Région : 04-Mauricie + 17-Centre-du-Québec (via FSA postal)
- Catégorie : `mainProcurementCategory = "works"` (construction)

**CLI :**
```powershell
python seao_scraper.py --sync          # télécharge nouveaux fichiers
python seao_scraper.py --sync --max 5  # test sur 5 fichiers
python seao_scraper.py --stats         # stats de la DB
python seao_scraper.py --reset         # recrée la DB
```

**Base de données seao.db :**
```
TABLE appels_offres : no_avis, titre, organisme, region,
  date_ouverture, montant_estime, categorie, nb_soumissions

TABLE soumissions : no_avis, rang, soumissionnaire, neq,
  montant, montant_manuel, gagnant

TABLE mes_projets : no_avis, ma_marge_pct, mon_montant, notes

TABLE parametres : cle, valeur
  mon_neq = '1180040314'
  mon_nom = 'CONSTRUCTION RICHARD CHAMPAGNE INC.'
  seuil_ecart_eleve = 5 (%)
  marge_min_viable = 8 (%)
```

**Stats actuelles (sync complet) :**
- 8,139 AO en DB | 925 actifs
- 228 soumissions CRC | 64 gagnés — 28.1%
- Position moyenne : #2.5
- Historique depuis 2021

**Sync automatique :**
- Tâche Windows : chaque lundi 6h00
- Créer : exécuter `setup_tache_planifiee.bat` en admin (1 seule fois)
- Log : `logs/sync_seao.log`
- Sync manuel depuis cockpit : bouton ↻ dans page Paramètres

### Routes serveur SEAO
```
GET  /seao/dashboard         → KPIs + derniers AO
GET  /seao/appels            → liste paginée (filtres: annee, date, mes_ao)
GET  /seao/appel/{no_avis}   → détail + soumissions + mes données
GET  /seao/competiteur/{neq} → stats compétiteur
GET  /seao/competiteurs      → top compétiteurs région 04/17
GET  /seao/parametres        → lire paramètres
POST /seao/parametres        → sauvegarder paramètres
POST /seao/marge             → saisir marge + montant sur un AO
POST /seao/sync              → lancer sync en arrière-plan
```

---

## 5. Script analyser_soumission.py

**Localisation** : `C:\Users\BenoitDesjardins\Documents\Claude\Projet 2026\analyser_soumission.py`

**CLI :**
```powershell
python analyser_soumission.py --projet "S-26-010 - Nom"
python analyser_soumission.py --tous
python analyser_soumission.py --lister
python analyser_soumission.py --forcer
```

**Constantes :**
- MAX_CONTEXTE_CHARS : 800,000
- MODELE_CLAUDE : claude-sonnet-4-6
- MAX_TOKENS_01_08 : 60,000 | MAX_TOKENS_09 : 16,000
- Split corpus : bin-packing, max 200,000 chars/chunk
- Streaming activé | Registre MD5 | Warnings PDF supprimés

### Stratégies de filtrage

| Type doc | Stratégie | Résultat S-26-010 |
|---|---|---|
| CONTRAT | Extraction ciblée regex | 238K → 7,780 (-97%) ✅ |
| RÉGIE | Extraction ciblée regex | 87K → 1,950 (-98%) ✅ |
| AVIS_AO | Extraction ciblée (SQI/municipal) | Nouveau ✅ |
| DEVIS_ARCH | MasterFormat P1/P2/P3 + Signal/Bruit | 462K → 140K (-70%) ✅ |
| DEVIS_ADMIN | Signal/Bruit (seuil >= 1) | 0% ⚠️ PENDING |
| PLANS | detect_pages_devis() | Devis intégrés extraits ✅ |

**AVIS_AO — détection :**
Patterns : "AVIS D'APPEL D'OFFRES", "AAO-", "SEAO", "SQI", "Numéro de contrat"
Champs : durée, date début, garantie soumission, pénalité, visite

**Corpus final S-26-010** : 1,198K brut → 554K (-53.7%), 4 chunks

### Livrables générés dans `Analyse\`
```
00_table_matières_technique.md
01_table_documentaire.md
02_délais_échéancier.md
03_pénalités_retenues.md
04_assurances_garanties.md
05_responsabilités_EG.md
06_risques_techniques.md
07_BSDQ_sous-traitance.md
08_soumissions_reçues.md
09_rapport_estimateur.md
log_analyse.txt | registre.json
```

---

## 6. Statut

### ✅ Complété
- Cockpit HTML servi par FastAPI sur localhost:8000
- Git + repo GitHub public
- Script analyser_soumission.py — 9 livrables générés
- Filtrage MasterFormat DEVIS_ARCH (-70%)
- Extraction ciblée CONTRAT/RÉGIE (-97%/-98%)
- Type AVIS_AO pour documents SQI/municipaux
- Split corpus bin-packing (max 200K/chunk)
- Streaming API activé
- Détection pages devis intégrées aux plans
- Module SEAO complet :
  - Scraper données ouvertes Données Québec
  - Base SQLite 8,139 AO, 228 soumissions CRC
  - Dashboard avec KPIs, filtres, pastilles rang
  - Filtre "Mes AOs"
  - Modal détail avec saisie montants manuels
  - Saisie marge + profit estimé
  - Liste compétiteurs avec stats
  - Sync automatique Windows Task Scheduler
  - Route POST /seao/sync depuis cockpit

### ⬜ Pending
- DEVIS_ADMIN : améliorer filtrage (0% réduction)
- ANNEXES contrat : capturer annexes (ex: échéancier p.75)
- 00_table_matières_technique.md
- Mode --addenda
- suivre_soumissions.py
- postmortem.py avec SQLite
- Page compétiteur détaillée dans cockpit
- Page paramètres SEAO complète

---

## 7. Notes techniques

**Commandes de test :**
```powershell
# Analyse
python analyser_soumission.py --projet "S-26-010 - Réaménagement Radio-oncologie CHAUR" --forcer

# SEAO
python seao_scraper.py --sync --max 5
python seao_scraper.py --stats
```

**Mon NEQ CRC :** `1180040314`
**Nom exact DB :** `CONSTRUCTION RICHARD CHAMPAGNE INC.`

**Patterns regex clés :**
- Section MasterFormat : `r'(?:^|\n)((?:0[2-9]|[1-9]\d)\s+\d{2}\s+\d{2})\s*[-–—]?\s*([A-Z][^\n]{3,60})'`
- Split Parties : `r'(?=\bPartie\s*[123]\b)'`
- AVIS_AO : `r'AVIS\s+D.APPEL\s+D.OFFRES|AAO-|Numéro de contrat'`

**Usage en début de session Claude :**
> Coller : https://raw.githubusercontent.com/bdesjardins1342/crc-cockpit/main/reference.md
