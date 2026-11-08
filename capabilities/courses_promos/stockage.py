"""Base SQLite de la capacité (``data/capacites/courses_promos/promos.sqlite``, jamais versionnée).

Tables de ce lot (spec §4, simplifiée en attendant les adaptateurs d'enseigne) :

- ``tickets`` : un ticket lu (empreinte du contenu : un ticket lu deux fois n'est compté qu'une fois) ;
- ``achats`` : ses lignes telles qu'imprimées (article ou remise), avec l'article canonique rapproché ;
- ``articles_canoniques`` + ``alias`` : ce que le foyer achète dans ses mots (« café moulu »), et les libellés de
  ticket qui le désignent ; un libellé inconnu crée un article **provisoire** (``valide = 0``) jusqu'à ce qu'une
  personne le nomme (:func:`associer`) ;
- ``prix_observes`` : prix unitaire **payé** (remise immédiate déduite), en ajout seul, avec ``en_promo`` ;
- ``economies`` : registre du compteur (module :mod:`economies`).

Le prix de référence d'un article (spec §5.2) est la médiane des prix hors promotion observés sur 90 jours (au moins
deux relevés), sinon le dernier relevé hors promotion (« fragile »), sinon rien. Jamais un prix barré.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import tickets as tk

TZ = ZoneInfo("Europe/Paris")
NOM_BASE = "promos.sqlite"
FENETRE_REFERENCE_J = 90
QUESTIONS_MAX = 5
SOURCES = ("ticket", "conversation", "open_prices", "site", "catalogue")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
  id INTEGER PRIMARY KEY,
  empreinte TEXT NOT NULL UNIQUE,
  enseigne TEXT, magasin TEXT,
  date_achat TEXT NOT NULL, heure TEXT, total REAL,
  origine TEXT NOT NULL CHECK (origine IN ('photo', 'mail', 'fichier', 'texte')),
  statut TEXT NOT NULL CHECK (statut IN ('lu', 'a_verifier')),
  avertissements TEXT,
  recu_le TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS articles_canoniques (
  id INTEGER PRIMARY KEY,
  nom TEXT NOT NULL UNIQUE,
  rayon TEXT,
  valide INTEGER NOT NULL DEFAULT 0,       -- 0 : provisoire (libellé de ticket), 1 : nommé par une personne
  cree_le TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alias (
  texte_normalise TEXT PRIMARY KEY,
  article_id INTEGER NOT NULL REFERENCES articles_canoniques(id),
  origine TEXT NOT NULL CHECK (origine IN ('ticket', 'liste', 'conversation')),
  valide INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS achats (
  id INTEGER PRIMARY KEY,
  ticket_id INTEGER NOT NULL REFERENCES tickets(id),
  date_achat TEXT NOT NULL,
  enseigne TEXT, magasin TEXT,
  libelle TEXT NOT NULL,
  type TEXT NOT NULL CHECK (type IN ('article', 'remise')),
  quantite REAL NOT NULL, unite TEXT NOT NULL CHECK (unite IN ('piece', 'kg')),
  prix_unitaire REAL, montant REAL NOT NULL,
  remise REAL NOT NULL DEFAULT 0,          -- remises immédiates rattachées à l'article (≤ 0)
  article_id INTEGER REFERENCES articles_canoniques(id)
);
CREATE INDEX IF NOT EXISTS achats_article ON achats(article_id, date_achat);
CREATE TABLE IF NOT EXISTS prix_observes (
  id INTEGER PRIMARY KEY,
  article_id INTEGER REFERENCES articles_canoniques(id),
  libelle TEXT NOT NULL,
  enseigne TEXT, magasin TEXT,
  prix_unitaire REAL NOT NULL CHECK (prix_unitaire >= 0),   -- payé, remise immédiate déduite
  unite TEXT NOT NULL CHECK (unite IN ('piece', 'kg')),
  en_promo INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL CHECK (source IN ('ticket', 'conversation', 'open_prices', 'site', 'catalogue')),
  achat_id INTEGER REFERENCES achats(id),
  observe_le TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS prix_article ON prix_observes(article_id, observe_le);
"""


class ErreurStockage(ValueError):
    """Erreur métier en français, relayée telle quelle par l'assistant."""


def chemin_base(dossier: Path) -> Path:
    return Path(dossier) / NOM_BASE


@contextmanager
def ouvrir(dossier: Path) -> Iterator[sqlite3.Connection]:
    """Connexion à la base (créée au besoin), transaction validée à la sortie."""
    from . import economies

    Path(dossier).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(chemin_base(dossier))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.executescript(SCHEMA + economies.SCHEMA)
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _maintenant() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _rayon(texte: str) -> str | None:
    try:
        from tools import courses

        return courses.deviner_rayon(texte)
    except Exception:  # noqa: BLE001 - le rayon est un confort, jamais bloquant
        return None


# --- articles canoniques ---------------------------------------------------------------------------------------


def article_pour_libelle(conn: sqlite3.Connection, libelle: str, origine: str = "ticket") -> int:
    """Article canonique d'un libellé : alias connu, sinon article provisoire au nom du libellé normalisé."""
    cle = tk.normaliser(libelle)
    if not cle:
        raise ErreurStockage("Libellé vide.")
    row = conn.execute("SELECT article_id FROM alias WHERE texte_normalise = ?", (cle,)).fetchone()
    if row is not None:
        return int(row["article_id"])
    existant = conn.execute("SELECT id FROM articles_canoniques WHERE nom = ?", (cle,)).fetchone()
    if existant is not None:
        article_id = int(existant["id"])
    else:
        article_id = int(conn.execute(
            "INSERT INTO articles_canoniques (nom, rayon, valide, cree_le) VALUES (?, ?, 0, ?)",
            (cle, _rayon(libelle), _maintenant()),
        ).lastrowid or 0)
    conn.execute("INSERT INTO alias (texte_normalise, article_id, origine, valide) VALUES (?, ?, ?, 0)",
                 (cle, article_id, origine))
    return article_id


def _trouver_alias(conn: sqlite3.Connection, libelle: str) -> sqlite3.Row:
    cle = tk.normaliser(libelle)
    row = conn.execute("SELECT * FROM alias WHERE texte_normalise = ?", (cle,)).fetchone()
    if row is not None:
        return row
    candidats = conn.execute("SELECT * FROM alias WHERE texte_normalise LIKE ? ORDER BY length(texte_normalise)",
                             (f"%{cle}%",)).fetchall()
    if len(candidats) == 1:
        return candidats[0]
    if not candidats:
        raise ErreurStockage(f"Aucun libellé de ticket ne correspond à « {libelle} ».")
    raise ErreurStockage(f"Plusieurs libellés correspondent à « {libelle} » : "
                         + ", ".join(c["texte_normalise"] for c in candidats[:5]) + ". Lequel ?")


def associer(conn: sqlite3.Connection, libelle: str, nom_article: str) -> dict[str, Any]:
    """Une personne nomme un libellé de ticket (« CAFE MOUL.ARAB 250G, c'est le café moulu ») : l'alias est validé,
    les achats et les prix déjà lus suivent, l'article provisoire disparaît s'il ne sert plus."""
    nom = " ".join(str(nom_article or "").split()).casefold()
    if not nom or len(nom) > 60:
        raise ErreurStockage("Nom d'article vide ou trop long (60 caractères au plus).")
    alias = _trouver_alias(conn, libelle)
    ancien = int(alias["article_id"])
    row = conn.execute("SELECT id FROM articles_canoniques WHERE nom = ?", (nom,)).fetchone()
    if row is None:
        cible = int(conn.execute(
            "INSERT INTO articles_canoniques (nom, rayon, valide, cree_le) VALUES (?, ?, 1, ?)",
            (nom, _rayon(nom), _maintenant()),
        ).lastrowid or 0)
    else:
        cible = int(row["id"])
        conn.execute("UPDATE articles_canoniques SET valide = 1 WHERE id = ?", (cible,))
    conn.execute("UPDATE alias SET article_id = ?, valide = 1 WHERE texte_normalise = ?",
                 (cible, alias["texte_normalise"]))
    if ancien != cible:
        conn.execute("UPDATE achats SET article_id = ? WHERE article_id = ?", (cible, ancien))
        conn.execute("UPDATE prix_observes SET article_id = ? WHERE article_id = ?", (cible, ancien))
        conn.execute("UPDATE economies SET article_id = ? WHERE article_id = ?", (cible, ancien))
        reste = conn.execute("SELECT 1 FROM alias WHERE article_id = ? LIMIT 1", (ancien,)).fetchone()
        if reste is None:
            conn.execute("DELETE FROM articles_canoniques WHERE id = ? AND valide = 0", (ancien,))
    return {"libelle": alias["texte_normalise"], "article": nom}


def article_par_nom(conn: sqlite3.Connection, recherche: str) -> list[sqlite3.Row]:
    """Articles dont le nom ou un alias contient la recherche (normalisée)."""
    cle = tk.normaliser(recherche)
    return conn.execute(
        "SELECT DISTINCT a.* FROM articles_canoniques a LEFT JOIN alias l ON l.article_id = a.id "
        "WHERE a.nom LIKE ? OR l.texte_normalise LIKE ? ORDER BY a.valide DESC, a.nom",
        (f"%{cle}%", f"%{cle}%"),
    ).fetchall()


# --- tickets ---------------------------------------------------------------------------------------------------


def enregistrer_ticket(conn: sqlite3.Connection, ticket: tk.Ticket, origine: str) -> dict[str, Any]:
    """Range un ticket validé ; un ticket déjà lu (même empreinte) est ignoré. Renvoie un résumé."""
    if origine not in ("photo", "mail", "fichier", "texte"):
        raise ErreurStockage(f"Origine de ticket inconnue : {origine}.")
    empreinte = ticket.empreinte()
    deja = conn.execute("SELECT id, date_achat FROM tickets WHERE empreinte = ?", (empreinte,)).fetchone()
    if deja is not None:
        return {"ticket_id": int(deja["id"]), "deja_lu": True, "date": deja["date_achat"]}
    jour = ticket.date.isoformat()
    ticket_id = int(conn.execute(
        "INSERT INTO tickets (empreinte, enseigne, magasin, date_achat, heure, total, origine, statut, avertissements, "
        "recu_le) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (empreinte, ticket.enseigne, ticket.magasin, jour, ticket.heure, ticket.total, origine,
         "a_verifier" if ticket.a_verifier else "lu", "\n".join(ticket.avertissements) or None, _maintenant()),
    ).lastrowid or 0)
    # Remises rattachées à l'article qui les précède (prix réellement payé).
    groupes: list[tuple[tk.LigneAchat, float]] = []
    for lg in ticket.lignes:
        if lg.type == "remise" and groupes:
            article, remise = groupes[-1]
            groupes[-1] = (article, round(remise + lg.montant, 2))
        elif lg.type == "article":
            groupes.append((lg, 0.0))
    nb = 0
    for lg, remise in groupes:
        article_id = article_pour_libelle(conn, lg.libelle)
        achat_id = int(conn.execute(
            "INSERT INTO achats (ticket_id, date_achat, enseigne, magasin, libelle, type, quantite, unite, "
            "prix_unitaire, montant, remise, article_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ticket_id, jour, ticket.enseigne, ticket.magasin, lg.libelle, "article", lg.quantite, lg.unite,
             lg.prix_unitaire, lg.montant, remise, article_id),
        ).lastrowid or 0)
        paye = max(0.0, lg.montant + remise)
        if lg.quantite > 0:
            conn.execute(
                "INSERT INTO prix_observes (article_id, libelle, enseigne, magasin, prix_unitaire, unite, en_promo, "
                "source, achat_id, observe_le) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (article_id, lg.libelle, ticket.enseigne, ticket.magasin, round(paye / lg.quantite, 3), lg.unite,
                 1 if remise < 0 else 0, "ticket", achat_id, jour),
            )
        nb += 1
    for lg in ticket.lignes:  # remises orphelines (avant tout article) : gardées telles quelles, jamais inventées
        if lg.type == "remise" and not groupes:
            conn.execute(
                "INSERT INTO achats (ticket_id, date_achat, enseigne, magasin, libelle, type, quantite, unite, "
                "montant) VALUES (?,?,?,?,?,?,?,?,?)",
                (ticket_id, jour, ticket.enseigne, ticket.magasin, lg.libelle, "remise", 1, "piece", lg.montant),
            )
    return {"ticket_id": ticket_id, "deja_lu": False, "date": jour, "articles": nb,
            "total": ticket.total, "a_verifier": ticket.a_verifier, "avertissements": list(ticket.avertissements),
            "questions": questions_rapprochement(conn, ticket_id)}


def questions_rapprochement(conn: sqlite3.Connection, ticket_id: int, maxi: int = QUESTIONS_MAX) -> list[str]:
    """Libellés du ticket qui n'ont pas encore de nom dans les mots du foyer (les plus achetés d'abord)."""
    rows = conn.execute(
        "SELECT DISTINCT a.libelle, (SELECT count(DISTINCT ticket_id) FROM achats x WHERE x.article_id = a.article_id) "
        "AS n FROM achats a JOIN articles_canoniques c ON c.id = a.article_id "
        "WHERE a.ticket_id = ? AND c.valide = 0 ORDER BY n DESC, a.id LIMIT ?",
        (ticket_id, maxi),
    ).fetchall()
    return [r["libelle"] for r in rows]


# --- lectures --------------------------------------------------------------------------------------------------


def reference(conn: sqlite3.Connection, article_id: int, jour: date,
              fenetre_j: int = FENETRE_REFERENCE_J) -> dict[str, Any] | None:
    """Prix de référence (spec §5.2) : médiane hors promo sur la fenêtre (≥ 2 relevés), sinon dernier relevé hors
    promo (« fragile »), sinon None. Même unité que le dernier relevé."""
    debut = (jour - timedelta(days=fenetre_j)).isoformat()
    rows = conn.execute(
        "SELECT prix_unitaire, unite, observe_le FROM prix_observes WHERE article_id = ? AND en_promo = 0 "
        "AND observe_le >= ? AND observe_le <= ? ORDER BY observe_le, id",
        (article_id, debut, jour.isoformat()),
    ).fetchall()
    if not rows:
        return None
    unite = rows[-1]["unite"]
    prix = [float(r["prix_unitaire"]) for r in rows if r["unite"] == unite]
    if len(prix) >= 2:
        return {"prix": round(statistics.median(prix), 2), "unite": unite, "releves": len(prix), "fragile": False}
    return {"prix": round(prix[-1], 2), "unite": unite, "releves": 1, "fragile": True}


def prix_habituels(conn: sqlite3.Connection, recherche: str | None, jour: date, limite: int = 15) -> list[dict[str, Any]]:
    """Par article : nombre de tickets, dernier prix payé, référence, plus bas observé (90 jours)."""
    if recherche and recherche.strip():
        articles = article_par_nom(conn, recherche)
    else:
        articles = conn.execute(
            "SELECT c.* FROM articles_canoniques c JOIN achats a ON a.article_id = c.id GROUP BY c.id "
            "ORDER BY count(DISTINCT a.ticket_id) DESC, max(a.date_achat) DESC LIMIT ?", (limite,),
        ).fetchall()
    debut = (jour - timedelta(days=FENETRE_REFERENCE_J)).isoformat()
    sortie: list[dict[str, Any]] = []
    for a in articles[:limite]:
        dernier = conn.execute(
            "SELECT prix_unitaire, unite, en_promo, observe_le, magasin FROM prix_observes WHERE article_id = ? "
            "ORDER BY observe_le DESC, id DESC LIMIT 1", (a["id"],),
        ).fetchone()
        if dernier is None:
            continue
        nb = conn.execute("SELECT count(DISTINCT ticket_id) AS n FROM achats WHERE article_id = ?",
                          (a["id"],)).fetchone()["n"]
        bas = conn.execute(
            "SELECT min(prix_unitaire) AS p FROM prix_observes WHERE article_id = ? AND unite = ? AND observe_le >= ?",
            (a["id"], dernier["unite"], debut),
        ).fetchone()["p"]
        ref = reference(conn, int(a["id"]), jour)
        sortie.append({
            "article": a["nom"], "nomme": bool(a["valide"]), "rayon": a["rayon"], "achats": int(nb),
            "dernier_achat": dernier["observe_le"], "dernier_prix": round(float(dernier["prix_unitaire"]), 2),
            "dernier_en_promo": bool(dernier["en_promo"]), "unite": "€/kg" if dernier["unite"] == "kg" else "€/pièce",
            "reference": ref["prix"] if ref else None, "reference_fragile": bool(ref and ref["fragile"]),
            "plus_bas_90j": round(float(bas), 2) if bas is not None else None, "magasin": dernier["magasin"],
        })
    return sortie


def historique(conn: sqlite3.Connection, depuis: date | None, recherche: str | None = None,
               limite: int = 10) -> dict[str, Any]:
    """Tickets récents (avec leurs lignes), ou achats d'un article quand ``recherche`` est donnée."""
    borne = depuis.isoformat() if depuis else "0000-00-00"
    if recherche and recherche.strip():
        ids = [int(a["id"]) for a in article_par_nom(conn, recherche)]
        if not ids:
            return {"achats": [], "message": f"Aucun achat de « {recherche} » dans les tickets lus."}
        marques = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT a.date_achat, a.magasin, a.libelle, a.quantite, a.unite, a.montant, a.remise, c.nom "
            f"FROM achats a JOIN articles_canoniques c ON c.id = a.article_id WHERE a.article_id IN ({marques}) "
            f"AND a.date_achat >= ? ORDER BY a.date_achat DESC, a.id DESC LIMIT ?",
            (*ids, borne, limite * 5),
        ).fetchall()
        return {"achats": [{
            "date": r["date_achat"], "article": r["nom"], "libelle": r["libelle"], "quantite": r["quantite"],
            "unite": r["unite"], "paye": round(float(r["montant"]) + float(r["remise"]), 2),
            "remise": round(float(r["remise"]), 2) or None, "magasin": r["magasin"],
        } for r in rows]}
    tix = conn.execute(
        "SELECT * FROM tickets WHERE date_achat >= ? ORDER BY date_achat DESC, id DESC LIMIT ?", (borne, limite),
    ).fetchall()
    sortie = []
    for t in tix:
        lignes = conn.execute(
            "SELECT a.libelle, a.quantite, a.unite, a.montant, a.remise, c.nom, c.valide FROM achats a "
            "LEFT JOIN articles_canoniques c ON c.id = a.article_id WHERE a.ticket_id = ? ORDER BY a.id", (t["id"],),
        ).fetchall()
        sortie.append({
            "date": t["date_achat"], "enseigne": t["enseigne"], "magasin": t["magasin"], "total": t["total"],
            "a_verifier": t["statut"] == "a_verifier", "articles": len(lignes),
            "lignes": [{"libelle": r["libelle"], "article": r["nom"] if r["valide"] else None,
                        "quantite": r["quantite"], "unite": r["unite"],
                        "paye": round(float(r["montant"]) + float(r["remise"] or 0), 2)} for r in lignes[:40]],
        })
    return {"tickets": sortie}
