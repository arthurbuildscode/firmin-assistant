"""Compteur d'économies (spec §7) : « économisé grâce à l'assistant », défini honnêtement.

> **Économie = prix de référence − prix payé**, par unité, multipliée par la quantité achetée, **seulement pour les
> choix que l'assistant a suscités et qu'on a pu suivre**.

Règles tenues par construction, pas par bonne volonté :

- le **prix de référence** est calculé ici, à partir des prix observés (:func:`stockage.reference`), au moment du
  choix, puis figé ; il n'est jamais passé par l'appelant, donc jamais un prix barré d'enseigne. Sans référence
  observée, rien n'est enregistré (:class:`EconomieRefusee`) ;
- une ligne naît « estimée » (prix promo prévu) ; le ticket la **confirme** avec le prix réellement payé, qui peut
  donner une ligne **négative** (promo terminée, autre format) : elle reste dans le total ;
- une estimation que les tickets ne confirment pas 14 jours après la fin de la promo, alors que des tickets plus
  récents sont arrivés, est **annulée** ;
- registre en **ajout seul** : confirmer ou annuler écrit une nouvelle ligne qui remplace l'ancienne (``remplace``) ;
- rien n'est affiché tant qu'aucun achat n'est suivi (pas de « 0 € », pas de chiffre d'exemple).

La table est prévue pour migrer vers un module commun du foyer (``tools/economies.py``, colonne ``capacite``,
spec §7.3) : une autre capacité (assurance, énergie) y écrira selon la même définition.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Paris")

CAPACITE = "courses_promos"
DELAI_ANNULATION_J = 14
ORIGINES = ("suggestion", "meilleur_jour")

SCHEMA = """
CREATE TABLE IF NOT EXISTS economies (
  id INTEGER PRIMARY KEY,
  capacite TEXT NOT NULL,
  origine TEXT NOT NULL CHECK (origine IN ('suggestion', 'meilleur_jour')),
  origine_id INTEGER,
  article_id INTEGER,
  libelle TEXT NOT NULL,
  quantite REAL NOT NULL CHECK (quantite > 0),
  prix_reference REAL NOT NULL CHECK (prix_reference > 0),   -- unitaire, figé au moment du choix
  prix_paye REAL,                                             -- unitaire ; NULL tant que non confirmé
  prix_estime REAL,                                           -- unitaire prévu (promo) pour une estimation
  montant REAL NOT NULL,                                      -- (référence − payé ou estimé) × quantité ; peut être < 0
  statut TEXT NOT NULL CHECK (statut IN ('estimee', 'confirmee', 'annulee')),
  preuve TEXT,                                                -- 'ticket:<id>', 'coche:<date>', motif d'annulation
  fin_promo TEXT,
  choisi_le TEXT NOT NULL,                                    -- date du choix suscité (jour)
  remplace INTEGER REFERENCES economies(id),
  cree_le TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS economies_remplace ON economies(remplace);
"""


class EconomieRefusee(ValueError):
    """Ce choix ne peut pas compter comme une économie (message en français)."""


def _maintenant() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _ligne(conn: sqlite3.Connection, id_: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM economies WHERE id = ?", (id_,)).fetchone()
    if row is None:
        raise EconomieRefusee(f"Ligne d'économie {id_} introuvable.")
    if conn.execute("SELECT 1 FROM economies WHERE remplace = ?", (id_,)).fetchone() is not None:
        raise EconomieRefusee(f"La ligne {id_} a déjà été confirmée ou annulée.")
    return row


def enregistrer_estimation(
    conn: sqlite3.Connection, *, origine: str, article_id: int, quantite: float, prix_promo: float,
    jour: date, fin_promo: date | None = None, origine_id: int | None = None, preuve: str | None = None,
) -> int:
    """Choix suscité par l'assistant (suggestion acceptée, achat le meilleur jour) : ligne « estimée »."""
    from . import stockage

    if origine not in ORIGINES:
        raise EconomieRefusee(f"Origine inconnue : {origine} (seuls les choix suscités par l'assistant comptent).")
    if quantite <= 0 or prix_promo < 0:
        raise EconomieRefusee("Quantité ou prix invalide.")
    ref = stockage.reference(conn, article_id, jour)
    if ref is None:
        raise EconomieRefusee("Pas de prix de référence observé pour cet article : ce choix ne compte pas.")
    article = conn.execute("SELECT nom FROM articles_canoniques WHERE id = ?", (article_id,)).fetchone()
    if article is None:
        raise EconomieRefusee("Article inconnu.")
    montant = round((ref["prix"] - prix_promo) * quantite, 2)
    return int(conn.execute(
        "INSERT INTO economies (capacite, origine, origine_id, article_id, libelle, quantite, prix_reference, "
        "prix_estime, montant, statut, preuve, fin_promo, choisi_le, cree_le) "
        "VALUES (?,?,?,?,?,?,?,?,?,'estimee',?,?,?,?)",
        (CAPACITE, origine, origine_id, article_id, article["nom"], quantite, ref["prix"], prix_promo, montant,
         preuve, fin_promo.isoformat() if fin_promo else None, jour.isoformat(), _maintenant()),
    ).lastrowid or 0)


def confirmer(conn: sqlite3.Connection, id_: int, prix_paye: float, preuve: str) -> int:
    """Le ticket dit ce qui a été payé : nouvelle ligne « confirmée » (éventuellement négative)."""
    ancienne = _ligne(conn, id_)
    if ancienne["statut"] != "estimee":
        raise EconomieRefusee("Seule une estimation se confirme.")
    if prix_paye < 0:
        raise EconomieRefusee("Prix payé invalide.")
    montant = round((ancienne["prix_reference"] - prix_paye) * ancienne["quantite"], 2)
    return int(conn.execute(
        "INSERT INTO economies (capacite, origine, origine_id, article_id, libelle, quantite, prix_reference, "
        "prix_paye, prix_estime, montant, statut, preuve, fin_promo, choisi_le, remplace, cree_le) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,'confirmee',?,?,?,?,?)",
        (ancienne["capacite"], ancienne["origine"], ancienne["origine_id"], ancienne["article_id"],
         ancienne["libelle"], ancienne["quantite"], ancienne["prix_reference"], prix_paye, ancienne["prix_estime"],
         montant, preuve, ancienne["fin_promo"], ancienne["choisi_le"], id_, _maintenant()),
    ).lastrowid or 0)


def annuler(conn: sqlite3.Connection, id_: int, motif: str) -> int:
    ancienne = _ligne(conn, id_)
    return int(conn.execute(
        "INSERT INTO economies (capacite, origine, origine_id, article_id, libelle, quantite, prix_reference, "
        "prix_paye, prix_estime, montant, statut, preuve, fin_promo, choisi_le, remplace, cree_le) "
        "VALUES (?,?,?,?,?,?,?,?,?,0,'annulee',?,?,?,?,?)",
        (ancienne["capacite"], ancienne["origine"], ancienne["origine_id"], ancienne["article_id"],
         ancienne["libelle"], ancienne["quantite"], ancienne["prix_reference"], ancienne["prix_paye"],
         ancienne["prix_estime"], motif[:120], ancienne["fin_promo"], ancienne["choisi_le"], id_, _maintenant()),
    ).lastrowid or 0)


def courantes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Lignes en vigueur (non remplacées)."""
    return conn.execute(
        "SELECT e.* FROM economies e WHERE NOT EXISTS (SELECT 1 FROM economies r WHERE r.remplace = e.id) "
        "ORDER BY e.choisi_le, e.id"
    ).fetchall()


def rapprocher_tickets(conn: sqlite3.Connection, aujourd_hui: date) -> dict[str, int]:
    """Confirme les estimations par les prix payés des tickets ; annule celles que les tickets ne confirment pas
    14 jours après la fin de la promo, si des tickets plus récents sont arrivés."""
    confirmees = annulees = 0
    dernier_ticket = conn.execute("SELECT max(date_achat) AS d FROM tickets").fetchone()["d"]
    for e in courantes(conn):
        if e["statut"] != "estimee" or e["article_id"] is None:
            continue
        fin = e["fin_promo"] or e["choisi_le"]
        achat = conn.execute(
            "SELECT p.prix_unitaire, a.ticket_id FROM prix_observes p JOIN achats a ON a.id = p.achat_id "
            "WHERE p.article_id = ? AND p.source = 'ticket' AND p.observe_le >= ? AND p.observe_le <= ? "
            "ORDER BY p.observe_le, p.id LIMIT 1",
            (e["article_id"], e["choisi_le"], fin),
        ).fetchone()
        if achat is not None:
            confirmer(conn, int(e["id"]), float(achat["prix_unitaire"]), f"ticket:{achat['ticket_id']}")
            confirmees += 1
            continue
        limite = date.fromisoformat(fin) + timedelta(days=DELAI_ANNULATION_J)
        if aujourd_hui > limite and dernier_ticket is not None and dernier_ticket > fin:
            annuler(conn, int(e["id"]), "non confirmée par les tickets")
            annulees += 1
    return {"confirmees": confirmees, "annulees": annulees}


def total(conn: sqlite3.Connection, depuis: date | None = None) -> dict[str, Any]:
    """Total du compteur depuis une date (choix suscités), avec la part encore estimée et le détail."""
    borne = depuis.isoformat() if depuis else "0000-00-00"
    lignes = [e for e in courantes(conn) if e["choisi_le"] >= borne and e["statut"] != "annulee"]
    somme = round(sum(float(e["montant"]) for e in lignes), 2)
    estime = round(sum(float(e["montant"]) for e in lignes if e["statut"] == "estimee"), 2)
    detail = []
    for e in lignes:
        paye = e["prix_paye"] if e["statut"] == "confirmee" else e["prix_estime"]
        q = float(e["quantite"])
        detail.append({
            "date": e["choisi_le"], "libelle": f"{e['libelle']} ×{q:g}" if q != 1 else e["libelle"],
            "reference": round(float(e["prix_reference"]) * q, 2),
            "paye": round(float(paye) * q, 2) if paye is not None else None,
            "ecart": round(float(e["montant"]), 2), "statut": e["statut"],
        })
    premier = min((e["choisi_le"] for e in lignes), default=None)
    return {"total": somme, "dont_estime": estime, "achats_suivis": len(lignes),
            "depuis": depuis.isoformat() if depuis else premier, "lignes": detail}


def donnees_carte(resume: dict[str, Any]) -> dict[str, Any] | None:
    """Données de la carte ``economies`` (CONTRACT §14) ; None tant qu'aucun achat n'est suivi."""
    if not resume.get("achats_suivis"):
        return None
    return {
        "total": resume["total"], "depuis": resume["depuis"], "achats_suivis": resume["achats_suivis"],
        "dont_estime": resume["dont_estime"],
        "par_capacite": [{"capacite": CAPACITE, "total": resume["total"]}],
        "lignes": resume["lignes"][-20:],
    }
