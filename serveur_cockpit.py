"""
CRC Cockpit — Serveur local FastAPI
Lancer : uvicorn serveur_cockpit:app --reload --port 8000
"""
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import subprocess, os, json, glob, re
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
SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "analyser_soumission.py")

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
