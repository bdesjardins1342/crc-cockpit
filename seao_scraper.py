"""
seao_scraper.py — Synchronisation SEAO vers SQLite
Données ouvertes Québec (format OCDS)

Usage :
  python seao_scraper.py --sync          # télécharge les fichiers manquants
  python seao_scraper.py --resync        # re-parse tous les fichiers en cache
  python seao_scraper.py --resync --max 20  # re-parse les 20 plus récents
  python seao_scraper.py --stats         # affiche les stats de la DB
  python seao_scraper.py --reset         # recrée la DB (efface tout)
  python seao_scraper.py --show N        # affiche N records bruts (debug)
"""

import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CKAN_API = (
    "https://www.donneesquebec.ca/recherche/api/3/action/"
    "package_show?id=systeme-electronique-dappel-doffres-seao"
)
DB_PATH  = Path(__file__).parent / "seao.db"
DATA_DIR = Path(__file__).parent / "data"   # cache des fichiers JSON SEAO
HEADERS  = {"User-Agent": "CRC-Cockpit/1.0 (contact: crc@crc.qc.ca)"}

# Catégories de construction retenues (mainProcurementCategory OCDS)
CATS_CONSTRUCTION = {"works"}

# Villes et codes postaux Mauricie (04) et Centre-du-Québec (17)
VILLES_MAURICIE = {
    "trois-rivières", "trois-rivieres", "shawinigan", "la tuque", "latuque",
    "louiseville", "yamachiche", "maskinongé", "maskinonge", "saint-tite",
    "grand-mère", "grand-mere", "hérouxville", "herouxville", "saint-séverin",
    "cap-de-la-madeleine", "bécancour",  # Bécancour est parfois en 04 admin
}
VILLES_CDQ = {
    "drummondville", "victoriaville", "bécancour", "becancour", "nicolet",
    "plessisville", "warwick", "daveluyville", "princeville", "acton vale",
    "acton-vale", "saint-hyacinthe",  # parfois classé 17
    "sainte-croix", "gentilly", "fortierville", "manseau",
}
# Préfixes FSA (3 premiers caractères du code postal)
FSA_MAURICIE = {
    "G4N","G4P","G4R","G4S","G4T","G4V","G4W","G4X","G4Y",   # Shawinigan / La Tuque
    "G8B","G8C","G8E","G8H","G8J","G8K","G8L","G8M","G8N",   # TR sud
    "G8P","G8R","G8S","G8T","G8V","G8W","G8X","G8Y","G8Z",   # TR centre/nord
    "G9A","G9B","G9C","G9H","G9N","G9R","G9T","G9X",          # TR est, rural N
    "G0T","G0V","G0W","G0X",                                   # rural Mauricie
}
FSA_CDQ = {
    "G6P","G6S","G6T",                                         # Victoriaville/Arthabaska
    "J2A","J2B","J2C","J2E","J2N",                             # Drummondville
    "J0A","J0B","J0C",                                         # rural CDQ
    "G0Z","G0S",                                               # rural/Nicolet
}


def est_region_cible(parties: list, buyer_id: str) -> bool:
    """Retourne True si l'acheteur est en Mauricie (04) ou Centre-du-Québec (17)."""
    for p in parties:
        if p.get("id") != buyer_id:
            continue
        addr  = p.get("address", {})
        pc    = addr.get("postalCode", "")[:3].upper()
        ville = addr.get("locality", "").lower().strip()
        if pc in FSA_MAURICIE or pc in FSA_CDQ:
            return True
        if ville in VILLES_MAURICIE or ville in VILLES_CDQ:
            return True
    return False


def region_label(parties: list, buyer_id: str) -> str:
    for p in parties:
        if p.get("id") != buyer_id:
            continue
        addr  = p.get("address", {})
        pc    = addr.get("postalCode", "")[:3].upper()
        ville = addr.get("locality", "").lower().strip()
        if pc in FSA_MAURICIE or ville in VILLES_MAURICIE:
            return "04-Mauricie"
        if pc in FSA_CDQ or ville in VILLES_CDQ:
            return "17-Centre-du-Québec"
    return "Inconnue"


# ---------------------------------------------------------------------------
# Base de données
# ---------------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS appels_offres (
    ocid              TEXT PRIMARY KEY,
    no_avis           TEXT,
    titre             TEXT,
    organisme         TEXT,
    region            TEXT,
    date_publication  TEXT,
    date_ouverture    TEXT,
    montant_estime    REAL,
    categorie         TEXT,
    sous_categorie    TEXT,
    nb_soumissions    INTEGER,
    statut            TEXT,
    url_seao          TEXT,
    fichier_source    TEXT
);

CREATE TABLE IF NOT EXISTS soumissions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ocid          TEXT NOT NULL,
    rang          INTEGER,
    soumissionnaire TEXT,
    neq           TEXT,
    montant       REAL,
    gagnant       INTEGER DEFAULT 0,
    UNIQUE(ocid, soumissionnaire)
);

CREATE TABLE IF NOT EXISTS mes_projets (
    ocid          TEXT PRIMARY KEY,
    ma_marge_pct  REAL,
    mon_montant   REAL,
    notes         TEXT,
    date_maj      TEXT
);

CREATE TABLE IF NOT EXISTS parametres (
    cle   TEXT PRIMARY KEY,
    valeur TEXT
);

CREATE TABLE IF NOT EXISTS fichiers_importes (
    nom       TEXT PRIMARY KEY,
    date_import TEXT,
    nb_records  INTEGER,
    nb_filtres  INTEGER
);
"""

PARAMS_DEFAUT = {
    "ecart_marge_eleve": "5",
    "marge_min_viable":  "8",
    "marge_cible":       "12",
    "alerte_concurrent": "3",
}


def get_db(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    conn.commit()
    # Insérer les paramètres par défaut s'ils n'existent pas
    for cle, val in PARAMS_DEFAUT.items():
        conn.execute(
            "INSERT OR IGNORE INTO parametres(cle, valeur) VALUES (?,?)", (cle, val)
        )
    conn.commit()
    return conn


def reset_db(path: Path = DB_PATH) -> None:
    if path.exists():
        path.unlink()
        print(f"DB supprimée : {path}")
    conn = get_db(path)
    conn.close()
    print(f"DB recréée : {path}")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def fetch_json(url: str, retries: int = 3, delay: float = 4.0) -> dict | list:
    for attempt in range(retries):
        try:
            req  = urllib.request.Request(url, headers=HEADERS)
            resp = urllib.request.urlopen(req, timeout=30)
            return json.loads(resp.read())
        except Exception as e:
            if attempt < retries - 1:
                print(f"  Retry {attempt+1}/{retries} — {e}")
                time.sleep(delay)
            else:
                raise


# ---------------------------------------------------------------------------
# Cache local
# ---------------------------------------------------------------------------

def _charger_fichier(nom: str, url: str) -> dict:
    """Retourne le JSON depuis le cache local; télécharge et met en cache sinon."""
    DATA_DIR.mkdir(exist_ok=True)
    chemin = DATA_DIR / nom
    if chemin.exists():
        return json.loads(chemin.read_text(encoding="utf-8"))
    time.sleep(2)
    data = fetch_json(url)
    chemin.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


# ---------------------------------------------------------------------------
# Parsing OCDS
# ---------------------------------------------------------------------------

def parse_release(r: dict, fichier_source: str) -> tuple[dict | None, list[dict]]:
    """
    Parse un release OCDS.
    Retourne (ao_dict, [soumissions]) ou (None, []) si filtré.
    """
    tender    = r.get("tender", {})
    buyer     = r.get("buyer", {})
    parties   = r.get("parties", [])
    buyer_id  = buyer.get("id", "")

    # Filtre catégorie construction
    if tender.get("mainProcurementCategory") not in CATS_CONSTRUCTION:
        return None, []

    # Filtre région
    if not est_region_cible(parties, buyer_id):
        return None, []

    # Champs principaux
    ocid    = r.get("ocid", "")
    no_avis = tender.get("id", "")
    titre   = tender.get("title", "")
    organisme = buyer.get("name", "")
    region  = region_label(parties, buyer_id)
    date_pub  = r.get("date", "")[:10] if r.get("date") else ""

    # Date limite soumissions
    tp = tender.get("tenderPeriod", {})
    date_ouv = (tp.get("endDate") or tp.get("startDate") or "")[:10]

    # Montant (tender.value → award[0].value → bids[0].value, par priorité)
    montant_estime = None
    tv = tender.get("value", {})
    if tv and tv.get("amount"):
        montant_estime = float(tv["amount"])
    if montant_estime is None:
        for award in r.get("awards", []):
            av = award.get("value", {})
            if av and av.get("amount"):
                montant_estime = float(av["amount"])
                break
    if montant_estime is None:
        for bid in r.get("bids", []):
            v = bid.get("value")
            if v is not None:
                try:
                    montant_estime = float(v)
                    break
                except (TypeError, ValueError):
                    pass

    # Catégorie textuelle
    add_cats = tender.get("additionalProcurementCategories", [])
    categorie     = add_cats[0] if add_cats else "Travaux de construction"
    items         = tender.get("items", [])
    sous_categorie = items[0].get("description", "") if items else ""

    nb_soumissions = tender.get("numberOfTenderers") or 0

    statut = tender.get("status", "")

    # URL SEAO
    url_seao = ""
    for doc in tender.get("documents", []):
        if doc.get("url"):
            url_seao = doc["url"]
            break

    ao = {
        "ocid": ocid,
        "no_avis": no_avis,
        "titre": titre,
        "organisme": organisme,
        "region": region,
        "date_publication": date_pub,
        "date_ouverture": date_ouv,
        "montant_estime": montant_estime,
        "categorie": categorie,
        "sous_categorie": sous_categorie,
        "nb_soumissions": nb_soumissions,
        "statut": statut,
        "url_seao": url_seao,
        "fichier_source": fichier_source,
    }

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _strip_fo(s: str) -> str:
        return s[3:] if s.startswith("FO-") else s

    def _est_neq(s: str) -> bool:
        """Un NEQ québécois est exactement 10 chiffres."""
        return len(s) == 10 and s.isdigit()

    # ── Index id_stripped → nom  ET  id_interne → vrai_NEQ ──────────────────
    # Vieux format : party["id"]="FO-1097337", party["details"]["NEQ"]="1148164123"
    # Nouveau fmt  : party["id"]="FO-1148164123", party["details"]["neq"]="1148164123"
    id_to_nom: dict[str, str] = {}
    id_to_neq: dict[str, str] = {}   # id_interne → vrai NEQ (10 chiffres)

    for p in parties:
        pid  = _strip_fo(p.get("id", ""))
        pnom = p.get("name", "")
        det  = p.get("details", {})
        pneq = det.get("NEQ") or det.get("neq") or ""   # clé varie selon l'année
        if pid and pnom:
            id_to_nom[pid] = pnom
        if pneq and pnom:
            id_to_nom[pneq] = pnom          # aussi indexé par vrai NEQ
        if pid and pneq and pid != pneq:
            id_to_neq[pid] = pneq           # résolution id_interne → NEQ réel

    for t in tender.get("tenderers", []):
        tid  = _strip_fo(t.get("id", ""))
        tnom = t.get("name", "")
        if tid and tnom:
            id_to_nom.setdefault(tid, tnom)  # ne pas écraser les données parties

    # ── Gagnants depuis awards ────────────────────────────────────────────────
    gagnants_ids: set[str] = set()
    for award in r.get("awards", []):
        for s in award.get("suppliers", []):
            key = _strip_fo(s.get("id", ""))
            gagnants_ids.add(key)
            if key in id_to_neq:
                gagnants_ids.add(id_to_neq[key])   # aussi l'id_interne → NEQ résolu

    # ── Résolution d'un id brut → NEQ stocké ─────────────────────────────────
    def _resoudre_neq(raw: str) -> str:
        if _est_neq(raw):
            return raw                        # déjà un NEQ réel (format 2024+)
        if raw in id_to_neq:
            return id_to_neq[raw]             # résolu via party["details"]["NEQ"]
        # Fallback nom : si la party est CRC, utiliser son NEQ connu dans les données
        nom = id_to_nom.get(raw, "").upper()
        if "CHAMPAGNE" in nom:
            neq_via_nom = next(
                (v for k, v in id_to_neq.items() if "CHAMPAGNE" in id_to_nom.get(k, "").upper()),
                None
            )
            if neq_via_nom:
                return neq_via_nom
        return raw                            # conserver l'id_interne en dernier recours

    # ── Soumissions depuis bids (format réel SEAO) ────────────────────────────
    soumissions = []
    bids_raw = r.get("bids", [])

    if bids_raw:
        parsed = []
        for bid in bids_raw:
            raw  = _strip_fo(bid.get("id", ""))
            neq  = _resoudre_neq(raw)
            nom  = id_to_nom.get(neq) or id_to_nom.get(raw, "")
            v    = bid.get("value")
            montant = None
            if v is not None:
                try:
                    montant = float(v)
                except (TypeError, ValueError):
                    pass
            est_gagnant = raw in gagnants_ids or neq in gagnants_ids
            parsed.append({"neq": neq, "nom": nom, "montant": montant,
                           "gagnant": 1 if est_gagnant else 0})

        # Trier par montant croissant (None en dernier → rang le plus élevé)
        parsed.sort(key=lambda x: (x["montant"] is None, x["montant"] or 0))

        for rang, b in enumerate(parsed, 1):
            soumissions.append({
                "ocid":            ocid,
                "rang":            rang,
                "soumissionnaire": b["nom"],
                "neq":             b["neq"],
                "montant":         b["montant"],
                "gagnant":         b["gagnant"],
            })

    else:
        # Fallback pour AOs actifs (pas encore de bids) : tenderers sans montant
        for i, t in enumerate(tender.get("tenderers", []), 1):
            raw = _strip_fo(t.get("id", ""))
            neq = _resoudre_neq(raw)
            nom = t.get("name", "") or id_to_nom.get(neq) or id_to_nom.get(raw, "")
            montant_s = None
            for award in r.get("awards", []):
                for s in award.get("suppliers", []):
                    if _strip_fo(s.get("id", "")) == raw:
                        av = award.get("value", {})
                        if av and av.get("amount"):
                            montant_s = float(av["amount"])
            est_gagnant = raw in gagnants_ids or neq in gagnants_ids
            soumissions.append({
                "ocid":            ocid,
                "rang":            i,
                "soumissionnaire": nom,
                "neq":             neq,
                "montant":         montant_s,
                "gagnant":         1 if est_gagnant else 0,
            })

    return ao, soumissions


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def cmd_sync(verbose: bool = True, max_fichiers: int = 0) -> None:
    conn = get_db()

    # 1. Récupérer la liste des fichiers depuis CKAN
    print("Récupération de la liste CKAN...")
    time.sleep(2)
    ckan = fetch_json(CKAN_API)
    resources = ckan["result"]["resources"]

    # Garder uniquement les JSON hebdo/mensuel
    ressources_json = [
        r for r in resources
        if r.get("format", "").lower() == "json"
        and ("hebdo_" in r.get("name", "") or "mensuel_" in r.get("name", ""))
    ]
    # Dédupliquer par nom (il peut y avoir des doublons dans CKAN)
    vus_noms: set[str] = set()
    ressources_uniques = []
    for r in ressources_json:
        if r["name"] not in vus_noms:
            vus_noms.add(r["name"])
            ressources_uniques.append(r)

    print(f"  {len(ressources_uniques)} fichiers disponibles (hebdo + mensuel)")

    # Fichiers déjà importés
    importes = {
        row["nom"]
        for row in conn.execute("SELECT nom FROM fichiers_importes").fetchall()
    }

    a_telecharger = [
        r for r in ressources_uniques if r["name"] not in importes
    ]
    if max_fichiers > 0:
        a_telecharger = a_telecharger[:max_fichiers]
    print(f"  {len(importes)} déjà importés, {len(a_telecharger)} à télécharger"
          + (f" (limité à {max_fichiers})" if max_fichiers else ""))

    if not a_telecharger:
        print("Rien de nouveau.")
        conn.close()
        return

    total_ao = 0
    total_soum = 0

    for res in a_telecharger:
        nom = res["name"]
        url = res["url"]
        print(f"\nTéléchargement : {nom}")
        time.sleep(2)  # politesse serveur

        try:
            data = _charger_fichier(nom, url)
        except Exception as e:
            print(f"  ERREUR téléchargement : {e}")
            continue

        releases = data.get("releases", [])
        print(f"  {len(releases)} releases dans le fichier")

        nb_ao   = 0
        nb_soum = 0

        for r in releases:
            ao, soumissions = parse_release(r, nom)
            if ao is None:
                continue

            conn.execute("""
                INSERT OR REPLACE INTO appels_offres
                    (ocid, no_avis, titre, organisme, region, date_publication,
                     date_ouverture, montant_estime, categorie, sous_categorie,
                     nb_soumissions, statut, url_seao, fichier_source)
                VALUES
                    (:ocid, :no_avis, :titre, :organisme, :region, :date_publication,
                     :date_ouverture, :montant_estime, :categorie, :sous_categorie,
                     :nb_soumissions, :statut, :url_seao, :fichier_source)
            """, ao)

            for s in soumissions:
                conn.execute("""
                    INSERT INTO soumissions
                        (ocid, rang, soumissionnaire, neq, montant, gagnant)
                    VALUES
                        (:ocid, :rang, :soumissionnaire, :neq, :montant, :gagnant)
                    ON CONFLICT(ocid, soumissionnaire) DO UPDATE SET
                        rang=excluded.rang,
                        neq=excluded.neq,
                        montant=COALESCE(excluded.montant, soumissions.montant),
                        gagnant=excluded.gagnant
                """, s)
                nb_soum += 1

            nb_ao += 1

        conn.execute("""
            INSERT OR REPLACE INTO fichiers_importes(nom, date_import, nb_records, nb_filtres)
            VALUES (?, ?, ?, ?)
        """, (nom, datetime.now().isoformat()[:19], len(releases), nb_ao))
        conn.commit()

        print(f"  -> {nb_ao} AO construction région 04/17, {nb_soum} soumissions")
        total_ao   += nb_ao
        total_soum += nb_soum

    conn.close()
    print(f"\n=== SYNC TERMINÉ : {total_ao} nouveaux AO, {total_soum} soumissions ===")


# ---------------------------------------------------------------------------
# Resync (re-parse depuis cache)
# ---------------------------------------------------------------------------

def _upsert_ao(conn: sqlite3.Connection, ao: dict) -> None:
    conn.execute("""
        INSERT OR REPLACE INTO appels_offres
            (ocid, no_avis, titre, organisme, region, date_publication,
             date_ouverture, montant_estime, categorie, sous_categorie,
             nb_soumissions, statut, url_seao, fichier_source)
        VALUES
            (:ocid, :no_avis, :titre, :organisme, :region, :date_publication,
             :date_ouverture, :montant_estime, :categorie, :sous_categorie,
             :nb_soumissions, :statut, :url_seao, :fichier_source)
    """, ao)


def cmd_resync(max_fichiers: int = 0) -> None:
    if not DB_PATH.exists():
        print("DB introuvable. Lancer --sync d'abord.")
        return

    conn = get_db()

    # Fichiers à re-parser (les plus récents en premier)
    sql = "SELECT nom FROM fichiers_importes ORDER BY date_import DESC"
    if max_fichiers > 0:
        sql += f" LIMIT {max_fichiers}"
    noms = [r["nom"] for r in conn.execute(sql).fetchall()]

    if not noms:
        print("Aucun fichier importé. Lancer --sync d'abord.")
        conn.close()
        return

    limite_txt = f" (limité aux {max_fichiers} plus récents)" if max_fichiers > 0 else ""
    print(f"Re-parsing de {len(noms)} fichier(s){limite_txt}...")

    # URLs CKAN pour les fichiers absents du cache local
    noms_sans_cache = [n for n in noms if not (DATA_DIR / n).exists()]
    url_map: dict[str, str] = {}
    if noms_sans_cache:
        print(f"  {len(noms_sans_cache)} fichier(s) absent(s) du cache — récupération des URLs CKAN...")
        time.sleep(2)
        ckan = fetch_json(CKAN_API)
        url_map = {
            r["name"]: r["url"]
            for r in ckan["result"]["resources"]
            if r.get("format", "").lower() == "json"
        }

    total_fichiers = 0
    total_ao       = 0
    total_soum     = 0
    total_montants = 0

    for nom in noms:
        url = url_map.get(nom, "")
        if not (DATA_DIR / nom).exists() and not url:
            print(f"  SKIP {nom} — absent du cache et de CKAN")
            continue

        print(f"  {nom}", end="", flush=True)
        try:
            data = _charger_fichier(nom, url)
        except Exception as e:
            print(f" — ERREUR : {e}")
            continue

        releases = data.get("releases", [])

        # ── 1re passe : parser tous les releases filtrés ────────────────────
        parsed_releases: list[tuple[dict, list[dict]]] = []
        ocids: list[str] = []
        for r in releases:
            ao, soumissions = parse_release(r, nom)
            if ao is None:
                continue
            parsed_releases.append((ao, soumissions))
            ocids.append(ao["ocid"])

        if not ocids:
            print(f" — 0 AO région")
            continue

        # ── Sauvegarder les montant_manuel saisis manuellement ──────────────
        ph = ",".join("?" * len(ocids))
        manuels: dict[tuple[str, str], float] = {}
        for row in conn.execute(
            f"SELECT ocid, neq, montant_manuel FROM soumissions"
            f" WHERE ocid IN ({ph}) AND montant_manuel IS NOT NULL",
            ocids
        ).fetchall():
            manuels[(row["ocid"], row["neq"])] = row["montant_manuel"]

        # ── Supprimer les soumissions existantes pour ces AOs ───────────────
        conn.execute(f"DELETE FROM soumissions WHERE ocid IN ({ph})", ocids)

        # ── Upsert AOs et réinsérer soumissions ─────────────────────────────
        nb_ao   = 0
        nb_soum = 0
        nb_mont = 0

        for ao, soumissions in parsed_releases:
            _upsert_ao(conn, ao)
            nb_ao += 1
            for s in soumissions:
                montant_manuel = manuels.get((s["ocid"], s["neq"]))
                conn.execute("""
                    INSERT OR IGNORE INTO soumissions
                        (ocid, rang, soumissionnaire, neq, montant, gagnant, montant_manuel)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (s["ocid"], s["rang"], s["soumissionnaire"],
                      s["neq"], s["montant"], s["gagnant"], montant_manuel))
                nb_soum += 1
                if s["montant"] is not None:
                    nb_mont += 1

        conn.execute(
            "UPDATE fichiers_importes SET date_import=? WHERE nom=?",
            (datetime.now().isoformat()[:19], nom)
        )
        conn.commit()

        print(f" — {nb_ao} AO, {nb_soum} soumissions ({nb_mont} avec montant)")
        total_fichiers += 1
        total_ao       += nb_ao
        total_soum     += nb_soum
        total_montants += nb_mont

    conn.close()
    print(
        f"\n=== RESYNC TERMINÉ : {total_fichiers} fichiers re-parsés"
        f" — {total_ao} AO, {total_soum} soumissions"
        f", {total_montants} montants mis à jour ==="
    )


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def cmd_stats() -> None:
    if not DB_PATH.exists():
        print("DB introuvable. Lancer --sync d'abord.")
        return

    conn = get_db()

    nb_ao   = conn.execute("SELECT COUNT(*) FROM appels_offres").fetchone()[0]
    nb_soum = conn.execute("SELECT COUNT(*) FROM soumissions").fetchone()[0]
    nb_fich = conn.execute("SELECT COUNT(*) FROM fichiers_importes").fetchone()[0]

    print(f"\n{'='*50}")
    print(f"  SEAO DB — {DB_PATH.name}")
    print(f"{'='*50}")
    print(f"  Fichiers importés    : {nb_fich}")
    print(f"  Appels d'offres      : {nb_ao}")
    print(f"  Soumissions          : {nb_soum}")

    print("\n  Par région :")
    for row in conn.execute(
        "SELECT region, COUNT(*) c FROM appels_offres GROUP BY region ORDER BY c DESC"
    ).fetchall():
        print(f"    {row['region']:30s}: {row['c']}")

    print("\n  Par statut :")
    for row in conn.execute(
        "SELECT statut, COUNT(*) c FROM appels_offres GROUP BY statut ORDER BY c DESC"
    ).fetchall():
        print(f"    {row['statut']:20s}: {row['c']}")

    print("\n  10 derniers AO :")
    for row in conn.execute("""
        SELECT no_avis, titre, organisme, region, date_ouverture, montant_estime
        FROM appels_offres
        ORDER BY date_publication DESC
        LIMIT 10
    """).fetchall():
        montant = f"{row['montant_estime']:,.0f}$" if row['montant_estime'] else "n/d"
        print(f"    [{row['region']}] {row['no_avis']:15s} {row['titre'][:45]:45s} {montant}")

    print("\n  Top 10 compétiteurs :")
    for row in conn.execute("""
        SELECT soumissionnaire, COUNT(*) c, SUM(gagnant) wins
        FROM soumissions
        WHERE soumissionnaire != ''
        GROUP BY soumissionnaire
        ORDER BY c DESC
        LIMIT 10
    """).fetchall():
        print(f"    {row['soumissionnaire'][:40]:40s}: {row['c']} soum, {row['wins']} gagnés")

    conn.close()


# ---------------------------------------------------------------------------
# Show raw records (debug)
# ---------------------------------------------------------------------------

def cmd_show(n: int = 3) -> None:
    print(f"Récupération du fichier hebdo le plus récent...")
    time.sleep(2)
    ckan = fetch_json(CKAN_API)
    resources = ckan["result"]["resources"]
    hebdo = [r for r in resources if "hebdo_" in r.get("name","")]
    if not hebdo:
        print("Aucun fichier hebdo trouvé.")
        return

    url = hebdo[0]["url"]
    print(f"Téléchargement : {hebdo[0]['name']}")
    time.sleep(2)
    data = fetch_json(url)
    releases = data.get("releases", [])

    # Chercher des records construction en Mauricie/CdQ
    printed = 0
    for r in releases:
        ao, soumissions = parse_release(r, hebdo[0]["name"])
        if ao is None:
            continue
        print(f"\n{'='*60}")
        print(f"RECORD BRUT {printed+1}/{n}")
        print(json.dumps(r, ensure_ascii=False, indent=2)[:3000])
        print(f"\n--- PARSÉ ---")
        print(json.dumps(ao, ensure_ascii=False, indent=2))
        if soumissions:
            print(f"Soumissions ({len(soumissions)}):")
            for s in soumissions:
                print(f"  {s}")
        printed += 1
        if printed >= n:
            break

    if printed == 0:
        print("Aucun record construction Mauricie/CdQ dans ce fichier.")
        print("Affichage d'un record construction quelconque :")
        for r in releases:
            tender = r.get("tender", {})
            if tender.get("mainProcurementCategory") == "works":
                print(json.dumps(r, ensure_ascii=False, indent=2)[:3000])
                break


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="SEAO Scraper — CRC Cockpit")
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--sync",   action="store_true", help="Télécharge les nouveaux fichiers")
    grp.add_argument("--resync", action="store_true", help="Re-parse les fichiers depuis le cache local")
    grp.add_argument("--stats",  action="store_true", help="Affiche les stats de la DB")
    grp.add_argument("--reset",  action="store_true", help="Recrée la DB")
    grp.add_argument("--show",   type=int, metavar="N", help="Affiche N records bruts (debug)")
    parser.add_argument("--max", type=int, default=0, metavar="N",
                        help="Limite à N fichiers (--sync et --resync)")
    args = parser.parse_args()

    if args.sync:
        cmd_sync(max_fichiers=args.max)
    elif args.resync:
        cmd_resync(max_fichiers=args.max)
    elif args.stats:
        cmd_stats()
    elif args.reset:
        reset_db()
    elif args.show is not None:
        cmd_show(args.show)


if __name__ == "__main__":
    main()
