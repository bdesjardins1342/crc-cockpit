# CRC COCKPIT — Document de référence
Version 1.6 — Mars 2026 | Benoit Desjardins | CRC | Montréal

---

## 0. Démarrage rapide
```powershell
cd C:\Users\BenoitDesjardins\Documents\Claude\crc-cockpit
python -m uvicorn serveur_cockpit:app --reload --port 8000
# Ouvrir http://localhost:8000
```

**En début de session Claude** : coller https://raw.githubusercontent.com/bdesjardins1342/crc-cockpit/main/reference.md

---

## 1. Vision & Architecture

**Concept** : Système d'automatisation pour EG — analyse soumissions, suivi projets, intelligence marché. Interface principale : Cowork (commandes à distance) + cockpit web local sur `http://localhost:8000`.
```
[Benoit à distance] → [Cowork] → [Ordi dédié local]
  ├── Cockpit HTML (http://localhost:8000)
  ├── Scripts Python (analyser_soumission.py, seao_scraper.py)
  ├── Serveur FastAPI (serveur_cockpit.py)
  ├── Base de données SQLite (seao.db)
  └── Cache fichiers SEAO (data/)
```

**Infrastructure :**
- Chrome + Claude in Chrome ✅
- Git + repo GitHub public : https://github.com/bdesjardins1342/crc-cockpit.git ✅
- Node.js v24.14.0 ✅
- Claude Code : `npm install -g @anthropic-ai/claude-code` ✅
- ANTHROPIC_API_KEY : variable d'environnement permanente (User) ✅
- FastAPI + uvicorn : `pip install fastapi uvicorn` ✅

**Racine projets** : `C:\Users\BenoitDesjardins\Documents\Claude\Projet 2026`
**Racine cockpit** : `C:\Users\BenoitDesjardins\Documents\Claude\crc-cockpit`

---

## 2. Fichiers du repo crc-cockpit

| Fichier | Rôle |
|---------|------|
| `cockpit v1.1.html` | Dashboard principal servi par FastAPI |
| `serveur_cockpit.py` | Serveur FastAPI — routes analyse + SEAO + Budget |
| `analyser_soumission.py` | Script d'analyse de dossiers AO |
| `seao_scraper.py` | Scraper données ouvertes SEAO |
| `budget_manager.py` | Helper SQLite pour budget.db |
| `seao.db` | Base SQLite — AOs, soumissions, mes données |
| `budget.db` | Base SQLite — projets budget, postes, dépenses |
| `data/` | Cache local des fichiers JSON SEAO hebdo/mensuel |
| `sync_seao.bat` | Script batch pour sync hebdo |
| `setup_tache_planifiee.bat` | Crée la tâche Windows Task Scheduler (1x admin) |
| `logs/sync_seao.log` | Log des syncs automatiques |
| `reference.md` | Ce fichier |

---

## 3. Cockpit HTML — Pages

**Navigation sidebar :**
- **Analyser AO** — lancer analyse + voir projets récents
- **Projets** — liste complète avec statuts
- **Livrables** — consulter les .md générés
- **Marché** — intelligence SEAO
- **Budget** — suivi budgétaire par projet

**Page Marché :**
- Filtre par année dynamique (boutons générés depuis DB) + bouton [Tout] + dates custom
- Boutons années multi-sélection (toggle), défaut = année courante
- Bouton "Mes AOs" — filtre sur CRC seulement
- KPIs : AO en DB, mes soumissions, profit estimé, position moyenne
- Pastilles : 🟢 rang 1 | 🟡 rang 2-3 | 🔴 rang 4+
- Sidebar compétiteurs : ⭐ CRC épinglé en haut (amber) + recherche par nom + tri colonnes
- Modal détail AO : tous soumissionnaires + montants + saisie marge
- Page paramètres ⚙ : mon NEQ, seuils, bouton sync manuel
- Bouton "+ AO privé" : saisie manuelle hors SEAO

**Page Budget :**
- Sélecteur projet (lié à S-26-XXX ou autre identifiant)
- KPIs : Budget total, Engagé, Payé, Écart
- Postes budgétaires collapsibles (code MasterFormat + nom + budget prévu)
- Dépenses par poste avec types : P=Payé · E=Engagé · C=Contrat · X=Extra
- Bouton déplacer dépense vers autre poste
- Filtre par type P/E/C/X
- Import PDF (détection automatique des montants via pdfminer)
- Export CSV (UTF-8 BOM, compatible Excel)

---

## 4. Module SEAO

### seao_scraper.py

**Source** : Données Québec — fichiers JSON hebdo (~15 Mo) et mensuel (~63 Mo)
**Standard** : OCDS (Open Contracting Data Standard), depuis mars 2021
**API catalogue** : `https://www.donneesquebec.ca/recherche/api/3/action/package_show?id=systeme-electronique-dappel-doffres-seao`

**Filtres :**
- Région : 04-Mauricie + 17-Centre-du-Québec (via FSA postal)
- Catégorie : `mainProcurementCategory = "works"`

**Format OCDS — parsing bids (format réel) :**
```json
"bids": [
  {"id": "FO-1180040314", "value": 434131.33, "valueUnit": "1"},
  {"id": "FO-1173521015", "value": 399970, "valueUnit": "1"}
]
```
- NEQ extrait via `bid["id"].replace("FO-", "")`
- Montant via `bid["value"]` (float direct)
- Rang = tri croissant des montants
- Noms via index `neq_to_nom` construit depuis `parties` + `tenderers`
- `ON CONFLICT DO UPDATE` — préserve les `montant_manuel` saisis

**CLI :**
```powershell
python seao_scraper.py --sync           # télécharge nouveaux fichiers
python seao_scraper.py --sync --max 5   # test sur 5 fichiers
python seao_scraper.py --resync         # re-parse tout l'historique (cache local)
python seao_scraper.py --resync --max 20  # re-parse les 20 plus récents
python seao_scraper.py --stats          # stats de la DB
python seao_scraper.py --reset          # recrée la DB
```

**Cache local** : `data/` — les fichiers JSON sont gardés localement, `--resync` est instantané si déjà en cache.

**Base de données seao.db :**
```
TABLE appels_offres
  no_avis, ocid, titre, organisme, region,
  date_publication, montant_estime, statut,
  nb_soumissions, url_seao, source (seao|prive)

TABLE soumissions
  no_avis, rang, soumissionnaire, neq,
  montant, montant_manuel, gagnant

TABLE mes_projets
  no_avis, ma_marge_pct, mon_montant, notes

TABLE parametres
  mon_neq = '1180040314'
  mon_nom = 'CONSTRUCTION RICHARD CHAMPAGNE INC.'
  seuil_ecart_eleve = 5 (%)
  marge_min_viable = 8 (%)
```

**Stats actuelles (sync complet + resync) :**
- 8,139 AO en DB | 925 actifs
- 228 soumissions CRC | 64 gagnés — 28.1%
- Position moyenne : #2.5
- ~100% des montants disponibles après resync
- Historique depuis 2021

**Sync automatique :**
- Tâche Windows : chaque lundi 6h00
- Créer : exécuter `setup_tache_planifiee.bat` en admin (1 seule fois)
- Log : `logs/sync_seao.log`
- Sync manuel depuis cockpit : bouton ↻ dans page Paramètres

**AOs privés (hors SEAO) :**
- Organismes non assujettis (CNA, OBNL, coopératives) ne publient pas sur SEAO
- Saisie manuelle via bouton "+ AO privé" dans le cockpit
- Marqués `source='prive'` dans appels_offres
- Inclus dans tous les calculs KPI

### Routes serveur SEAO
```
GET  /seao/dashboard          → KPIs + derniers AO
GET  /seao/appels             → liste paginée (filtres: annee, date, mes_ao)
GET  /seao/appel/{no_avis}    → détail + soumissions + mes données
GET  /seao/competiteur/{neq}  → stats compétiteur (filtré région 04/17)
GET  /seao/annees_disponibles → années distinctes en DB (pour boutons dynamiques)
GET  /seao/dashboard          → KPIs filtrés (params: annee, date_debut, date_fin)
GET  /seao/competiteurs       → top compétiteurs région 04/17 (param ?q=)
GET  /seao/parametres         → lire paramètres
POST /seao/parametres         → sauvegarder paramètres
POST /seao/marge              → saisir marge + montant sur un AO
POST /seao/ao_prive           → créer AO hors SEAO manuellement
POST /seao/sync               → lancer sync en arrière-plan
```

### Routes serveur Budget
```
GET  /budget/projets              → liste projets avec KPIs agrégés
GET  /budget/projet/{id}          → postes + dépenses du projet
POST /budget/projet               → créer/modifier projet
POST /budget/poste                → créer/modifier poste
POST /budget/depense              → ajouter/modifier dépense
DELETE /budget/depense/{id}       → supprimer dépense
POST /budget/depense/{id}/deplacer → déplacer vers autre poste
POST /budget/import_pdf           → analyser PDF et extraire montants
GET  /budget/export/{id}          → export CSV
```

**budget.db :**
```
TABLE projets_budget
  projet_id (TEXT PK), budget_total, date_creation

TABLE postes
  id, projet_id, code (MasterFormat), nom, budget_prevu
  UNIQUE(projet_id, code)

TABLE depenses
  id, poste_id, projet_id, type (P/E/C/X),
  reference, fournisseur, detail, montant, date_depense
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
| CONTRAT | Extraction ciblée regex + ANNEXES | 238K → 7,780 (-97%) ✅ |
| RÉGIE | Extraction ciblée regex | 87K → 1,950 (-98%) ✅ |
| AVIS_AO | Extraction ciblée (SQI/municipal) | ✅ |
| DEVIS_ARCH | MasterFormat P1/P2/P3 + Signal/Bruit | 462K → 140K (-70%) ✅ |
| DEVIS_ADMIN | Signal/Bruit (seuil >= 1) | ⚠️ 0% PENDING |
| PLANS | detect_pages_devis() | ✅ |

**AVIS_AO** — détecté via : "AVIS D'APPEL D'OFFRES", "AAO-", "SEAO", "SQI", "Numéro de contrat"
Champs extraits : durée, date début, garantie soumission, pénalité, visite

**Corpus final S-26-010** : 1,198K → 554K (-53.7%), 4 chunks [196kb, 134kb, 103kb, 119kb]

### Livrables dans `Analyse\`
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
- Script analyser_soumission.py — 9 livrables générés
- Filtrage MasterFormat DEVIS_ARCH (-70%)
- Extraction ciblée CONTRAT/RÉGIE (-97%/-98%)
- Type AVIS_AO pour documents SQI/municipaux
- Split corpus bin-packing (max 200K/chunk)
- Module SEAO complet :
  - Scraper données ouvertes + cache local data/
  - Parsing bids OCDS correct (NEQ via FO-, montant direct)
  - ON CONFLICT DO UPDATE (préserve montant_manuel)
  - --resync pour re-parser l'historique sans re-télécharger
  - Base SQLite 8,139 AO, 228 soumissions CRC
  - Dashboard KPIs, filtres, pastilles rang
  - Filtre "Mes AOs"
  - Modal détail avec montants réels
  - Stats compétiteurs filtrées région 04/17
  - Saisie marge + profit estimé
  - AOs privés (hors SEAO)
  - Sync automatique Windows Task Scheduler
  - Multi-sélection années (boutons toggle, filtre IN)
  - Boutons années dynamiques depuis DB + bouton [Tout]
  - KPI dashboard respecte le filtre période (annee/dates)
  - Tri colonnes tableau Marché (Date/Rang/Montant/Écart ↑↓)
  - Recherche compétiteurs par nom (barre de recherche)
  - ⭐ CRC épinglé en haut de la sidebar compétiteurs
  - Tri colonnes dans modal compétiteur (date/rang/montant)
  - Import PDF Budget : gestion PDF scanné sans couche texte
- Module Budget complet :
  - budget_manager.py + budget.db (3 tables)
  - CRUD projets / postes / dépenses
  - Types P/E/C/X avec couleurs
  - Postes collapsibles + KPIs (engagé/payé/écart)
  - Déplacement dépense entre postes
  - Import PDF (pdfminer, détection montants)
  - Export CSV compatible Excel

### ⬜ Pending
- DEVIS_ADMIN : améliorer filtrage (0% réduction)
- ANNEXES contrat (ex: échéancier p.75)
- 00_table_matières_technique.md
- Mode --addenda
- suivre_soumissions.py
- postmortem.py avec SQLite
- Page compétiteur détaillée dans cockpit
- Page paramètres SEAO complète
- Accès distant via Tailscale

---

## 7. Notes techniques

**NEQ CRC principal :** `1148164123` (169 soumissions)
**NEQ CRC secondaire :** `1180040314` (8 soumissions récentes)
**mon_neq en DB :** `1148164123` | **mon_nom_like :** `%RICHARD CHAMPAGNE%`
**Nom exact DB :** `CONSTRUCTION RICHARD CHAMPAGNE INC.`
**Adresse :** 253 route 153, Saint-Tite QC G0X 3H0

**Commandes de test :**
```powershell
python analyser_soumission.py --projet "S-26-010 - Réaménagement Radio-oncologie CHAUR" --forcer
python seao_scraper.py --sync --max 5
python seao_scraper.py --resync --max 10
python seao_scraper.py --stats
```

**Patterns regex clés :**
- Section MasterFormat : `r'(?:^|\n)((?:0[2-9]|[1-9]\d)\s+\d{2}\s+\d{2})\s*[-–—]?\s*([A-Z][^\n]{3,60})'`
- Split Parties : `r'(?=\bPartie\s*[123]\b)'`
- AVIS_AO : `r'AVIS\s+D.APPEL\s+D.OFFRES|AAO-|Numéro de contrat'`
