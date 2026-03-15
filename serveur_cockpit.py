"""
CRC Cockpit — Serveur local FastAPI
Lancer : uvicorn serveur_cockpit:app --reload --port 8000
"""
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import subprocess, os, json, glob, re, sqlite3
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
def seao_dashboard():
    conn = _seao_conn()
    if not conn:
        return {"error": "seao.db introuvable — lancer seao_scraper.py --sync"}
    params        = _params(conn)
    mon_neq       = params.get("mon_neq", "")
    mon_nom_like  = params.get("mon_nom_like", "")

    total_ao  = conn.execute("SELECT COUNT(*) FROM appels_offres").fetchone()[0]
    ao_actifs = conn.execute("SELECT COUNT(*) FROM appels_offres WHERE statut='active'").fetchone()[0]

    # mes_soumissions = toutes les lignes CRC dans la table soumissions
    mes_ao = conn.execute(
        "SELECT COUNT(*) FROM soumissions WHERE neq=? OR soumissionnaire LIKE ?",
        (mon_neq, mon_nom_like)
    ).fetchone()[0] if (mon_neq or mon_nom_like) else 0

    ao_gagnes = 0
    pos_moy   = None
    if mon_neq or mon_nom_like:
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

    derniers = conn.execute("""
        SELECT ao.ocid, ao.no_avis, ao.titre, ao.organisme, ao.region,
               ao.date_publication, ao.montant_estime, ao.statut,
               mp.ma_marge_pct, mp.mon_montant
        FROM appels_offres ao
        LEFT JOIN mes_projets mp ON mp.ocid = ao.ocid
        ORDER BY ao.date_publication DESC LIMIT 10
    """).fetchall()

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
def seao_competiteurs():
    conn = _seao_conn()
    if not conn:
        return {"error": "seao.db introuvable"}
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


app.mount("/static", StaticFiles(directory=COCKPIT_DIR), name="static")
