"""CRC Budget Manager — SQLite helper pour budget.db"""
import sqlite3, os
from datetime import datetime

BUDGET_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "budget.db")

def _conn():
    c = sqlite3.connect(BUDGET_DB)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = _conn()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS projets_budget (
            projet_id    TEXT PRIMARY KEY,
            budget_total REAL DEFAULT 0,
            date_creation TEXT
        );
        CREATE TABLE IF NOT EXISTS postes (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            projet_id    TEXT NOT NULL REFERENCES projets_budget(projet_id),
            code         TEXT NOT NULL,
            nom          TEXT NOT NULL,
            budget_prevu REAL DEFAULT 0,
            UNIQUE(projet_id, code)
        );
        CREATE TABLE IF NOT EXISTS depenses (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            poste_id     INTEGER NOT NULL REFERENCES postes(id),
            projet_id    TEXT NOT NULL,
            type         TEXT NOT NULL CHECK(type IN ('P','E','C','X')),
            reference    TEXT DEFAULT '',
            fournisseur  TEXT DEFAULT '',
            detail       TEXT DEFAULT '',
            montant      REAL NOT NULL,
            date_depense TEXT DEFAULT ''
        );
    """)
    c.commit()
    c.close()

init_db()
