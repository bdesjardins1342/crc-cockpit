"""
CRC Cockpit — Serveur local FastAPI
Lancer : uvicorn serveur_cockpit:app --reload --port 8000
"""
from fastapi import FastAPI, BackgroundTasks, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import subprocess, os, json, glob, re, sqlite3
import csv, io as _io
from datetime import datetime
from pathlib import Path

app = FastAPI(title="CRC Cockpit API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

RACINE = r"C:\Users\BenoitDesjardins\Documents\Claude\Projet 2026"
COCKPIT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(COCKPIT_DIR, "analyser_soumission.py")

# ─── Jobs en cours ──────────────────────────────────────────────────────────
jobs = {}  # {job_id: {"status": ..., "output": ..., "projet": ...}}

def run_analyse(job_id: str, projet: str, forcer: bool):
    jobs[job_id]["status"] = "running"
    cmd = ["python", SCRIPT, "--projet", projet]
    if forcer:
        cmd.append("--forcer")
    try:
        result = subprocess.run(
            cmd, cwd=RACINE, capture_output=True, text=True, encoding="utf-8"
        )
        jobs[job_id]["status"] = "done" if result.returncode == 0 else "error"
        jobs[job_id]["output"] = result.stdout + result.stderr
        jobs[job_id]["fin"] = datetime.now().isoformat()
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["output"] = str(e)

# ─── Routes ─────────────────────────────────────────────────────────────────

@app.get("/")
def cockpit():
    for nom in ["cockpit.html", "cockpit v1.1.html"]:
        chemin = os.path.join(COCKPIT_DIR, nom)
        if os.path.exists(chemin):
            return FileResponse(chemin)
    return {"error": "cockpit.html introuvable"}


@app.get("/projets")
def lister_projets():
    """Liste tous les projets détectés."""
    projets = []
    for d in Path(RACINE).iterdir():
        if d.is_dir() and re.match(r'S-\d{2}-\d{3}', d.name):
            analyse_dir = d / "Analyse"
            a_analyse = analyse_dir.exists() and any(analyse_dir.glob("0*.md"))
            projets.append({
                "nom": d.name,
                "a_analyse": a_analyse,
                "date_analyse": None
            })
            if a_analyse:
                mtime = max(f.stat().st_mtime for f in analyse_dir.glob("0*.md"))
                projets[-1]["date_analyse"] = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
    projets.sort(key=lambda x: x["nom"], reverse=True)
    return {"projets": projets}


@app.post("/analyser")
def lancer_analyse(body: dict, background_tasks: BackgroundTasks):
    """Lance une analyse en arrière-plan."""
    projet = body.get("projet", "")
    forcer = body.get("forcer", False)
    if not projet:
        return {"error": "Projet requis"}
    job_id = datetime.now().strftime("%H%M%S")
    jobs[job_id] = {
        "status": "pending",
        "projet": projet,
        "output": "",
        "debut": datetime.now().isoformat(),
        "fin": None
    }
    background_tasks.add_task(run_analyse, job_id, projet, forcer)
    return {"job_id": job_id, "projet": projet}


@app.get("/job/{job_id}")
def statut_job(job_id: str):
    """Statut d'un job d'analyse."""
    if job_id not in jobs:
        return {"error": "Job introuvable"}
    return jobs[job_id]


@app.get("/livrables/{projet_encoded}")
def lire_livrables(projet_encoded: str):
    """Retourne la liste et le contenu des livrables .md."""
    projet = projet_encoded.replace("__", " ").replace("_", " ")
    # Chercher le dossier projet
    for d in Path(RACINE).iterdir():
        if d.is_dir() and projet.lower() in d.name.lower():
            analyse_dir = d / "Analyse"
            if not analyse_dir.exists():
                return {"livrables": []}
            livrables = []
            for f in sorted(analyse_dir.glob("0*.md")):
                livrables.append({
                    "nom": f.name,
                    "contenu": f.read_text(encoding="utf-8")
                })
            return {"projet": d.name, "livrables": livrables}
    return {"error": "Projet introuvable"}


@app.get("/ping")
def ping():
    return {"status": "ok", "heure": datetime.now().isoformat()}


# ─── SEAO ────────────────────────────────────────────────────────────────────

SEAO_DB = os.path.join(COCKPIT_DIR, "seao.db")


@app.on_event("startup")
def migrate_seao_db():
    if not os.path.exists(SEAO_DB):
        return
    conn = sqlite3.connect(SEAO_DB)
    for sql in [
        "ALTER TABLE soumissions ADD COLUMN montant_manuel REAL",
        "ALTER TABLE appels_offres ADD COLUMN source TEXT DEFAULT 'seao'",
    ]:
        try:
            conn.execute(sql)
            conn.commit()
        except Exception:
            pass  # colonne déjà existante
    conn.close()


def _seao_conn():
    if not os.path.exists(SEAO_DB):
        return None
    conn = sqlite3.connect(SEAO_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _params(conn) -> dict:
    return {r["cle"]: r["valeur"] for r in conn.execute("SELECT cle, valeur FROM parametres").fetchall()}


@app.get("/seao/dashboard")
def seao_dashboard(annee: str = "", date_debut: str = "", date_fin: str = ""):
    conn = _seao_conn()
    if not conn:
        return {"error": "seao.db introuvable — lancer seao_scraper.py --sync"}
    params        = _params(conn)
    mon_neq       = params.get("mon_neq", "")
    mon_nom_like  = params.get("mon_nom_like", "")

    # Filtre temporel pour les KPIs
    ao_where, ao_vals = [], []
    if annee:
        annees = [a.strip() for a in annee.split(',') if a.strip()]
        if len(annees) == 1:
            ao_where.append("strftime('%Y', date_publication) = ?"); ao_vals.append(annees[0])
        elif len(annees) > 1:
            ao_where.append(f"strftime('%Y', date_publication) IN ({','.join('?'*len(annees))})"); ao_vals.extend(annees)
    if date_debut:
        ao_where.append("date_publication >= ?"); ao_vals.append(date_debut)
    if date_fin:
        ao_where.append("date_publication <= ?"); ao_vals.append(date_fin)
    ao_wc  = ("WHERE " + " AND ".join(ao_where)) if ao_where else ""
    # filtre pour joindre avec soumissions via ocid
    s_wc   = ("WHERE ao.ocid = s.ocid AND (" + " AND ".join(ao_where).replace("date_publication", "ao.date_publication") + ")") if ao_where else "WHERE ao.ocid = s.ocid"

    total_ao  = conn.execute(f"SELECT COUNT(*) FROM appels_offres {ao_wc}", ao_vals).fetchone()[0]
    ao_actifs = conn.execute(f"SELECT COUNT(*) FROM appels_offres {('WHERE statut='+repr('active')+(' AND '+' AND '.join(ao_where)) if ao_where else 'WHERE statut='+repr('active'))}", ao_vals).fetchone()[0]

    mes_ao, ao_gagnes, pos_moy = 0, 0, None
    if mon_neq or mon_nom_like:
        join_crc = f"""
            SELECT COUNT(*) FROM soumissions s
            JOIN appels_offres ao ON ao.ocid = s.ocid
            {ao_wc.replace('WHERE','WHERE ao.') if ao_wc else ''}
            {'AND' if ao_wc else 'WHERE'} (s.neq=? OR s.soumissionnaire LIKE ?)
        """
        # Build simpler direct queries with sub-filter
        if ao_where:
            ocid_sub = f"SELECT ocid FROM appels_offres {ao_wc}"
            mes_ao = conn.execute(
                f"SELECT COUNT(*) FROM soumissions WHERE ocid IN ({ocid_sub}) AND (neq=? OR soumissionnaire LIKE ?)",
                ao_vals + [mon_neq, mon_nom_like]
            ).fetchone()[0]
            ao_gagnes = conn.execute(
                f"SELECT COUNT(*) FROM soumissions WHERE ocid IN ({ocid_sub}) AND (neq=? OR soumissionnaire LIKE ?) AND gagnant=1",
                ao_vals + [mon_neq, mon_nom_like]
            ).fetchone()[0]
            row = conn.execute(
                f"SELECT AVG(rang) FROM soumissions WHERE ocid IN ({ocid_sub}) AND (neq=? OR soumissionnaire LIKE ?)",
                ao_vals + [mon_neq, mon_nom_like]
            ).fetchone()
        else:
            mes_ao = conn.execute(
                "SELECT COUNT(*) FROM soumissions WHERE neq=? OR soumissionnaire LIKE ?",
                (mon_neq, mon_nom_like)
            ).fetchone()[0]
            ao_gagnes = conn.execute(
                "SELECT COUNT(*) FROM soumissions WHERE (neq=? OR soumissionnaire LIKE ?) AND gagnant=1",
                (mon_neq, mon_nom_like)
            ).fetchone()[0]
            row = conn.execute(
                "SELECT AVG(rang) FROM soumissions WHERE neq=? OR soumissionnaire LIKE ?",
                (mon_neq, mon_nom_like)
            ).fetchone()
        pos_moy = round(row[0], 1) if row[0] else None

    profit_total = 0.0
    for row in conn.execute(
        "SELECT ma_marge_pct, mon_montant FROM mes_projets WHERE ma_marge_pct IS NOT NULL AND mon_montant IS NOT NULL"
    ).fetchall():
        profit_total += float(row["ma_marge_pct"]) / 100.0 * float(row["mon_montant"])

    taux = round(ao_gagnes / mes_ao * 100, 1) if mes_ao > 0 else 0

    alertes = conn.execute("""
        SELECT mp.ocid, ao.titre, ao.no_avis
        FROM mes_projets mp JOIN appels_offres ao ON ao.ocid = mp.ocid
        WHERE mp.mon_montant IS NOT NULL AND mp.ma_marge_pct IS NULL
    """).fetchall()

    derniers = conn.execute(f"""
        SELECT ao.ocid, ao.no_avis, ao.titre, ao.organisme, ao.region,
               ao.date_publication, ao.montant_estime, ao.statut,
               mp.ma_marge_pct, mp.mon_montant
        FROM appels_offres ao
        LEFT JOIN mes_projets mp ON mp.ocid = ao.ocid
        {ao_wc}
        ORDER BY ao.date_publication DESC LIMIT 10
    """, ao_vals).fetchall()

    conn.close()
    return {
        "kpis": {
            "total_ao": total_ao, "ao_actifs": ao_actifs, "mes_ao": mes_ao,
            "ao_gagnes": ao_gagnes, "taux_succes": taux,
            "profit_total": round(profit_total, 2), "position_moyenne": pos_moy,
        },
        "alertes_marge": [dict(r) for r in alertes],
        "derniers_ao":   [dict(r) for r in derniers],
        "params": params,
    }


@app.get("/seao/annees_disponibles")
def seao_annees_disponibles():
    conn = _seao_conn()
    if not conn:
        return {"annees": []}
    rows = conn.execute("""
        SELECT DISTINCT strftime('%Y', date_publication) AS annee
        FROM appels_offres
        WHERE date_publication IS NOT NULL AND date_publication != ''
        ORDER BY annee DESC
    """).fetchall()
    conn.close()
    return {"annees": [r["annee"] for r in rows if r["annee"]]}


@app.get("/seao/appels")
def seao_appels(
    annee: str = "", date_debut: str = "", date_fin: str = "",
    competiteur: str = "", page: int = 1, par_page: int = 200
):
    conn = _seao_conn()
    if not conn:
        return {"error": "seao.db introuvable"}
    params        = _params(conn)
    mon_neq       = params.get("mon_neq", "")
    mon_nom_like  = params.get("mon_nom_like", "")
    seuil_ec  = float(params.get("ecart_marge_eleve", "5"))
    marge_min = float(params.get("marge_min_viable", "8"))

    where, vals = [], []
    if annee:
        annees = [a.strip() for a in annee.split(',') if a.strip()]
        if len(annees) == 1:
            where.append("strftime('%Y', ao.date_publication) = ?"); vals.append(annees[0])
        elif len(annees) > 1:
            placeholders = ','.join('?' * len(annees))
            where.append(f"strftime('%Y', ao.date_publication) IN ({placeholders})")
            vals.extend(annees)
    if date_debut:
        where.append("ao.date_publication >= ?"); vals.append(date_debut)
    if date_fin:
        where.append("ao.date_publication <= ?"); vals.append(date_fin)
    if competiteur:
        where.append("ao.ocid IN (SELECT ocid FROM soumissions WHERE soumissionnaire LIKE ?)")
        vals.append(f"%{competiteur}%")
    wc = "WHERE " + " AND ".join(where) if where else ""

    query = f"""
        SELECT ao.ocid, ao.no_avis, ao.titre, ao.organisme, ao.region,
               ao.date_publication, ao.montant_estime, ao.statut, ao.url_seao,
               COALESCE(ao.source, 'seao') AS source,
               mp.ma_marge_pct, mp.mon_montant, mp.notes,
               crc.rang  AS mon_rang,   COALESCE(crc.montant, crc.montant_manuel) AS mon_montant,
               w.soumissionnaire AS gagnant_nom, w.montant AS gagnant_montant,
               s2.montant AS second_montant
        FROM appels_offres ao
        LEFT JOIN mes_projets mp ON mp.ocid = ao.ocid
        LEFT JOIN soumissions crc ON crc.ocid = ao.ocid AND (crc.neq = ? OR crc.soumissionnaire LIKE ?)
        LEFT JOIN (SELECT ocid, soumissionnaire, COALESCE(montant,montant_manuel) AS montant FROM soumissions WHERE gagnant=1) w  ON w.ocid  = ao.ocid
        LEFT JOIN (SELECT ocid, COALESCE(montant,montant_manuel) AS montant FROM soumissions WHERE rang=2) s2 ON s2.ocid = ao.ocid
        {wc}
        ORDER BY ao.date_publication DESC
        LIMIT ? OFFSET ?
    """
    offset = (page - 1) * par_page
    rows  = conn.execute(query, [mon_neq, mon_nom_like] + vals + [par_page, offset]).fetchall()
    total = conn.execute(f"SELECT COUNT(*) FROM appels_offres ao {wc}", vals).fetchone()[0]

    results = []
    for r in rows:
        row = dict(r)
        ecart = None
        if row["mon_rang"] == 1:
            if row["second_montant"] and row["mon_montant"] and row["mon_montant"] > 0:
                ecart = round((row["second_montant"] - row["mon_montant"]) / row["mon_montant"] * 100, 1)
        elif row["mon_montant"] and row["gagnant_montant"] and row["gagnant_montant"] > 0:
            ecart = round((row["mon_montant"] - row["gagnant_montant"]) / row["gagnant_montant"] * 100, 1)
        row["ecart_pct"] = ecart

        if row["mon_rang"] is None:
            flag = "pas_soumis"
        elif row["mon_rang"] == 1:
            flag = "aurait_pu_monter" if ecart is not None and ecart > seuil_ec else "gagne"
        else:
            flag = "marge_trop_basse" if ecart is not None and ecart < marge_min else "perdu"
        row["flag"] = flag
        results.append(row)

    conn.close()
    return {"appels": results, "total": total, "page": page, "par_page": par_page}


@app.get("/seao/appel/{no_avis:path}")
def seao_appel(no_avis: str):
    conn = _seao_conn()
    if not conn:
        return {"error": "seao.db introuvable"}
    ao = conn.execute(
        "SELECT * FROM appels_offres WHERE no_avis=? OR ocid=?", (no_avis, no_avis)
    ).fetchone()
    if not ao:
        return {"error": "AO introuvable"}
    ao = dict(ao)

    soumissions = conn.execute(
        "SELECT * FROM soumissions WHERE ocid=? ORDER BY rang", (ao["ocid"],)
    ).fetchall()
    mes_donnees = conn.execute(
        "SELECT * FROM mes_projets WHERE ocid=?", (ao["ocid"],)
    ).fetchone()

    soum_list = [dict(s) for s in soumissions]

    def meff(s):
        return s["montant"] or s.get("montant_manuel")

    calculs = {}
    w  = next((s for s in soum_list if s["gagnant"]), None) \
         or next((s for s in soum_list if s["rang"] == 1), None)
    s2 = next((s for s in soum_list if s["rang"] == 2), None)
    wm  = meff(w)  if w  else None
    s2m = meff(s2) if s2 else None
    if wm and s2m and wm > 0:
        diff = s2m - wm
        calculs["ecart_1er_2e_pct"]     = round(diff / wm * 100, 1)
        calculs["ecart_1er_2e_montant"] = round(diff, 2)

    conn.close()
    return {
        "ao": ao,
        "soumissions": soum_list,
        "mes_donnees": dict(mes_donnees) if mes_donnees else None,
        "calculs": calculs,
    }


@app.get("/seao/competiteurs")
def seao_competiteurs(q: str = ""):
    conn = _seao_conn()
    if not conn:
        return {"error": "seao.db introuvable"}
    if q:
        rows = conn.execute("""
            SELECT soumissionnaire, neq,
                   COUNT(*) AS nb_soumissions, SUM(gagnant) AS nb_victoires,
                   ROUND(AVG(CASE WHEN montant IS NOT NULL THEN montant END), 0) AS montant_moyen,
                   MIN(montant) AS montant_min, MAX(montant) AS montant_max
            FROM soumissions WHERE soumissionnaire != '' AND soumissionnaire LIKE ?
            GROUP BY soumissionnaire ORDER BY nb_soumissions DESC LIMIT 50
        """, (f"%{q}%",)).fetchall()
    else:
        rows = conn.execute("""
            SELECT soumissionnaire, neq,
                   COUNT(*) AS nb_soumissions, SUM(gagnant) AS nb_victoires,
                   ROUND(AVG(CASE WHEN montant IS NOT NULL THEN montant END), 0) AS montant_moyen,
                   MIN(montant) AS montant_min, MAX(montant) AS montant_max
            FROM soumissions WHERE soumissionnaire != ''
            GROUP BY soumissionnaire ORDER BY nb_soumissions DESC LIMIT 30
        """).fetchall()
    conn.close()
    return {"competiteurs": [dict(r) for r in rows]}


@app.get("/seao/competiteur/{identifiant:path}")
def seao_competiteur(identifiant: str):
    conn = _seao_conn()
    if not conn:
        return {"error": "seao.db introuvable"}
    stats = conn.execute("""
        SELECT soumissionnaire, neq,
               COUNT(*) AS nb_soumissions, SUM(gagnant) AS nb_victoires,
               ROUND(AVG(montant), 0) AS montant_moyen,
               MIN(montant) AS montant_min, MAX(montant) AS montant_max
        FROM soumissions WHERE neq=? OR soumissionnaire=?
        GROUP BY soumissionnaire
    """, (identifiant, identifiant)).fetchone()
    if not stats:
        return {"error": "Compétiteur introuvable"}
    ao_list = conn.execute("""
        SELECT ao.no_avis, ao.titre, ao.organisme, ao.date_publication,
               s.rang, s.montant, s.gagnant
        FROM soumissions s JOIN appels_offres ao ON ao.ocid = s.ocid
        WHERE s.neq=? OR s.soumissionnaire=?
        ORDER BY ao.date_publication DESC LIMIT 50
    """, (identifiant, identifiant)).fetchall()
    conn.close()
    return {"stats": dict(stats), "appels": [dict(r) for r in ao_list]}


@app.get("/seao/parametres")
def seao_get_params():
    conn = _seao_conn()
    if not conn:
        return {"error": "seao.db introuvable"}
    p = _params(conn); conn.close()
    return p


@app.post("/seao/parametres")
def seao_set_params(body: dict):
    conn = _seao_conn()
    if not conn:
        return {"error": "seao.db introuvable"}
    for cle, valeur in body.items():
        conn.execute("INSERT OR REPLACE INTO parametres(cle, valeur) VALUES (?,?)", (cle, str(valeur)))
    conn.commit(); conn.close()
    return {"ok": True}


@app.post("/seao/marge")
def seao_marge(body: dict):
    conn = _seao_conn()
    if not conn:
        return {"error": "seao.db introuvable"}
    no_avis = body.get("no_avis", "")
    ao = conn.execute("SELECT ocid FROM appels_offres WHERE no_avis=?", (no_avis,)).fetchone()
    if not ao:
        return {"error": "AO introuvable"}
    conn.execute("""
        INSERT OR REPLACE INTO mes_projets(ocid, ma_marge_pct, mon_montant, notes, date_maj)
        VALUES (?,?,?,?,?)
    """, (ao["ocid"], body.get("ma_marge_pct"), body.get("mon_montant"),
          body.get("notes", ""), datetime.now().isoformat()[:19]))
    conn.commit(); conn.close()
    return {"ok": True}


@app.post("/seao/montant_manuel")
def seao_set_montant_manuel(body: dict):
    conn = _seao_conn()
    if not conn:
        return {"error": "seao.db introuvable"}
    soum_id = body.get("soum_id")
    if not soum_id:
        return {"error": "soum_id requis"}
    montant = body.get("montant_manuel")
    conn.execute("UPDATE soumissions SET montant_manuel=? WHERE id=?", (montant, soum_id))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/seao/sync")
def seao_sync(background_tasks: BackgroundTasks):
    log_path = os.path.join(COCKPIT_DIR, "logs", "sync_seao.log")
    def run_sync():
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"\n[{datetime.now().isoformat(timespec='seconds')}] Début sync API\n")
        subprocess.run(
            ["python", os.path.join(COCKPIT_DIR, "seao_scraper.py"), "--sync"],
            cwd=COCKPIT_DIR,
            stdout=open(log_path, "a", encoding="utf-8"),
            stderr=subprocess.STDOUT,
        )
    background_tasks.add_task(run_sync)
    return {"status": "started", "message": "Sync SEAO lancé en arrière-plan"}


@app.post("/seao/ao_prive")
def seao_creer_ao_prive(body: dict):
    conn = _seao_conn()
    if not conn:
        return {"error": "seao.db introuvable"}
    params      = _params(conn)
    mon_neq     = params.get("mon_neq", "")
    mon_nom_raw = params.get("mon_nom_like", "").replace("%", "").strip() or "CRC"

    ts      = datetime.now().strftime("%Y%m%d%H%M%S")
    ocid    = f"prive-{ts}"
    no_avis = f"PRIVE-{ts}"

    titre          = body.get("titre") or "AO privé"
    organisme      = body.get("organisme", "")
    date_ouverture = body.get("date_ouverture", "")
    montant_estime = body.get("montant_estime")
    region         = body.get("region", "")
    mon_rang       = body.get("mon_rang")
    mon_montant    = body.get("mon_montant")
    ma_marge_pct   = body.get("ma_marge_pct")
    gagnant_nom    = body.get("gagnant_nom", "")
    gagnant_montant= body.get("gagnant_montant")
    notes          = body.get("notes", "")

    conn.execute("""
        INSERT INTO appels_offres
            (ocid, no_avis, titre, organisme, region,
             date_publication, date_ouverture, montant_estime, statut, source)
        VALUES (?,?,?,?,?,?,?,?,'ferme','prive')
    """, (ocid, no_avis, titre, organisme, region,
          date_ouverture, date_ouverture, montant_estime))

    if mon_rang is not None:
        conn.execute("""
            INSERT INTO soumissions (ocid, neq, soumissionnaire, rang, montant, gagnant)
            VALUES (?,?,?,?,?,?)
        """, (ocid, mon_neq, mon_nom_raw, mon_rang, mon_montant, 1 if mon_rang == 1 else 0))

    if gagnant_nom and mon_rang != 1:
        conn.execute("""
            INSERT INTO soumissions (ocid, soumissionnaire, rang, montant, gagnant)
            VALUES (?,?,1,?,1)
        """, (ocid, gagnant_nom, gagnant_montant))

    if ma_marge_pct is not None or mon_montant is not None:
        conn.execute("""
            INSERT OR REPLACE INTO mes_projets (ocid, ma_marge_pct, mon_montant, notes, date_maj)
            VALUES (?,?,?,?,?)
        """, (ocid, ma_marge_pct, mon_montant, notes, datetime.now().isoformat()[:19]))

    conn.commit()
    conn.close()
    return {"ok": True, "no_avis": no_avis}


# ─── Budget ──────────────────────────────────────────────────────────────────
import budget_manager as _bm

def _bconn():
    return _bm._conn()


@app.get("/budget/projets")
def budget_projets():
    c = _bconn()
    rows = c.execute("""
        SELECT p.projet_id, p.budget_total, p.date_creation,
               COALESCE(SUM(CASE WHEN d.type IN ('E','C','X') THEN d.montant ELSE 0 END), 0) AS engage,
               COALESCE(SUM(CASE WHEN d.type = 'P' THEN d.montant ELSE 0 END), 0) AS paye,
               COUNT(DISTINCT po.id) AS nb_postes
        FROM projets_budget p
        LEFT JOIN postes po ON po.projet_id = p.projet_id
        LEFT JOIN depenses d ON d.projet_id = p.projet_id
        GROUP BY p.projet_id
        ORDER BY p.date_creation DESC
    """).fetchall()
    c.close()
    return {"projets": [dict(r) for r in rows]}


@app.get("/budget/projet/{projet_id:path}")
def budget_get_projet(projet_id: str):
    c = _bconn()
    projet = c.execute("SELECT * FROM projets_budget WHERE projet_id=?", (projet_id,)).fetchone()
    if not projet:
        return {"error": "Projet introuvable"}
    postes = c.execute("""
        SELECT po.id, po.code, po.nom, po.budget_prevu,
               COALESCE(SUM(CASE WHEN d.type IN ('E','C','X') THEN d.montant ELSE 0 END), 0) AS engage,
               COALESCE(SUM(CASE WHEN d.type = 'P' THEN d.montant ELSE 0 END), 0) AS paye
        FROM postes po
        LEFT JOIN depenses d ON d.poste_id = po.id
        WHERE po.projet_id = ?
        GROUP BY po.id
        ORDER BY po.code
    """, (projet_id,)).fetchall()
    postes_data = []
    for po in postes:
        deps = c.execute("""
            SELECT id, type, reference, fournisseur, detail, montant, date_depense
            FROM depenses WHERE poste_id=? ORDER BY date_depense DESC, id DESC
        """, (po["id"],)).fetchall()
        postes_data.append({**dict(po), "depenses": [dict(d) for d in deps]})
    c.close()
    return {"projet": dict(projet), "postes": postes_data}


@app.post("/budget/projet")
def budget_creer_projet(body: dict):
    c = _bconn()
    pid   = body.get("projet_id", "").strip()
    total = float(body.get("budget_total", 0) or 0)
    if not pid:
        return {"error": "projet_id requis"}
    try:
        c.execute("INSERT INTO projets_budget(projet_id, budget_total, date_creation) VALUES(?,?,?)",
                  (pid, total, datetime.now().strftime("%Y-%m-%d")))
    except sqlite3.IntegrityError:
        c.execute("UPDATE projets_budget SET budget_total=? WHERE projet_id=?", (total, pid))
    c.commit(); c.close()
    return {"ok": True}


@app.post("/budget/poste")
def budget_creer_poste(body: dict):
    c = _bconn()
    pid    = body.get("projet_id", "")
    code   = body.get("code", "").strip()
    nom    = body.get("nom", "").strip()
    budget = float(body.get("budget_prevu", 0) or 0)
    po_id  = body.get("id")
    if not (pid and code and nom):
        return {"error": "projet_id, code, nom requis"}
    if po_id:
        c.execute("UPDATE postes SET code=?, nom=?, budget_prevu=? WHERE id=?",
                  (code, nom, budget, po_id))
    else:
        try:
            c.execute("INSERT INTO postes(projet_id, code, nom, budget_prevu) VALUES(?,?,?,?)",
                      (pid, code, nom, budget))
        except sqlite3.IntegrityError:
            c.execute("UPDATE postes SET nom=?, budget_prevu=? WHERE projet_id=? AND code=?",
                      (nom, budget, pid, code))
    c.commit(); c.close()
    return {"ok": True}


@app.post("/budget/depense")
def budget_sauver_depense(body: dict):
    c = _bconn()
    dep_id   = body.get("id")
    poste_id = body.get("poste_id")
    proj_id  = body.get("projet_id", "")
    typ      = body.get("type", "P")
    ref      = body.get("reference", "")
    four     = body.get("fournisseur", "")
    det      = body.get("detail", "")
    mont     = float(body.get("montant", 0) or 0)
    dt       = body.get("date_depense", "")
    if dep_id:
        c.execute("""UPDATE depenses SET poste_id=?, type=?, reference=?, fournisseur=?,
                     detail=?, montant=?, date_depense=? WHERE id=?""",
                  (poste_id, typ, ref, four, det, mont, dt, dep_id))
    else:
        if not poste_id:
            return {"error": "poste_id requis"}
        c.execute("""INSERT INTO depenses(poste_id, projet_id, type, reference, fournisseur,
                     detail, montant, date_depense) VALUES(?,?,?,?,?,?,?,?)""",
                  (poste_id, proj_id, typ, ref, four, det, mont, dt))
    c.commit(); c.close()
    return {"ok": True}


@app.delete("/budget/depense/{dep_id}")
def budget_supprimer_depense(dep_id: int):
    c = _bconn()
    c.execute("DELETE FROM depenses WHERE id=?", (dep_id,))
    c.commit(); c.close()
    return {"ok": True}


@app.post("/budget/depense/{dep_id}/deplacer")
def budget_deplacer_depense(dep_id: int, body: dict):
    c = _bconn()
    po_id = body.get("poste_id")
    if not po_id:
        return {"error": "poste_id requis"}
    c.execute("UPDATE depenses SET poste_id=? WHERE id=?", (po_id, dep_id))
    c.commit(); c.close()
    return {"ok": True}


@app.post("/budget/import_pdf")
async def budget_import_pdf(file: UploadFile = File(...)):
    try:
        from pdfminer.high_level import extract_text
    except ImportError:
        return {"error": "pdfminer non installé: pip install pdfminer.six"}
    import io as _tmpio
    content = await file.read()
    if not content:
        return {"error": "Fichier vide"}
    try:
        texte = extract_text(_tmpio.BytesIO(content))
    except Exception as e:
        return {"error": f"Impossible de lire ce PDF (peut-être scanné/image) : {str(e)[:120]}"}
    if not texte or not texte.strip():
        return {"error": "Aucun texte détecté — ce PDF semble être une image scannée sans couche texte"}
    lignes = []
    for line in texte.split('\n'):
        line = line.strip()
        if not line or len(line) < 4:
            continue
        amounts = re.findall(r'(?<!\d)(\d{1,3}(?:[ \xa0]\d{3})*(?:[.,]\d{2})?)(?!\d)', line)
        if amounts:
            raw = amounts[-1].replace('\xa0', '').replace(' ', '').replace(',', '.')
            try:
                m = float(raw)
                if 50 <= m <= 50_000_000:
                    lignes.append({"ligne": line[:120], "montant": round(m, 2)})
            except ValueError:
                pass
    return {"lignes": lignes[:60]}


@app.get("/budget/export/{projet_id:path}")
def budget_export(projet_id: str):
    c = _bconn()
    rows = c.execute("""
        SELECT po.code, po.nom, po.budget_prevu,
               d.date_depense, d.type, d.reference, d.fournisseur, d.detail, d.montant
        FROM depenses d
        JOIN postes po ON po.id = d.poste_id
        WHERE d.projet_id=?
        ORDER BY po.code, d.date_depense
    """, (projet_id,)).fetchall()
    c.close()
    out = _io.StringIO()
    w = csv.writer(out)
    w.writerow(["Code", "Poste", "Budget prévu", "Date", "Type", "Référence", "Fournisseur", "Détail", "Montant"])
    for r in rows:
        w.writerow(list(r))
    out.seek(0)
    fn = f"budget_{projet_id.replace('/', '-').replace(' ', '_')}_{datetime.now().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter(['\ufeff' + out.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=\"{fn}\""}
    )


app.mount("/static", StaticFiles(directory=COCKPIT_DIR), name="static")
