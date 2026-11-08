"""Anniversaires : prochaine occurrence annuelle, âge, alertes cadeau (J-délai, J-7, J-0)."""

from __future__ import annotations

from datetime import date
from typing import Any

from tools import db
from tools.common import iso_now, parse_date, today

ANNEE_INCONNUE = 1900  # année « bouchon » quand on ne connaît que le jour et le mois


def _prochaine(naissance: date, ref: date) -> date:
    """Prochain anniversaire >= ref (29 février → 28 février les années non bissextiles)."""
    for annee in (ref.year, ref.year + 1):
        try:
            d = naissance.replace(year=annee)
        except ValueError:
            d = date(annee, 2, 28)
        if d >= ref:
            return d
    return naissance.replace(year=ref.year + 1)  # inatteignable en pratique


def _vers_dict(row: Any, ref: date) -> dict[str, Any]:
    a = dict(row)
    naissance = parse_date(a["date"])
    prochaine = _prochaine(naissance, ref)
    a["prochaine"] = prochaine.isoformat()
    a["dans_jours"] = (prochaine - ref).days
    a["age"] = (prochaine.year - naissance.year) if naissance.year > ANNEE_INCONNUE else None
    return a


def lister(horizon_jours: int = 60, ref: date | None = None) -> list[dict[str, Any]]:
    """Anniversaires à venir dans l'horizon, triés par date."""
    ref = ref or today()
    with db.cursor() as conn:
        rows = conn.execute("SELECT * FROM anniversaires").fetchall()
    resultat = [a for a in (_vers_dict(r, ref) for r in rows) if a["dans_jours"] <= horizon_jours]
    resultat.sort(key=lambda a: (a["dans_jours"], a["prenom"].casefold()))
    return resultat


def ajouter(
    prenom: str,
    date_naissance: str | date,
    lieu: str | None = None,
    notes: str | None = None,
    delai_cadeau_jours: int = 21,
) -> dict[str, Any]:
    prenom = " ".join(prenom.strip().split())
    if not prenom:
        raise ValueError("Le prénom est vide.")
    texte = str(date_naissance).strip()
    # « 12/05 » ou « 05-12 » sans année → année inconnue
    if isinstance(date_naissance, str) and texte.count("/") == 1 and texte.replace("/", "").isdigit():
        jour, mois = (int(x) for x in texte.split("/"))
        d = date(ANNEE_INCONNUE, mois, jour)
    elif isinstance(date_naissance, str) and texte.count("-") == 1 and texte.replace("-", "").isdigit():
        mois, jour = (int(x) for x in texte.split("-"))
        d = date(ANNEE_INCONNUE, mois, jour)
    else:
        d = parse_date(date_naissance)
    delai = max(0, int(delai_cadeau_jours))
    with db.cursor() as conn:
        cur = conn.execute(
            "INSERT INTO anniversaires (prenom, date, lieu, notes, delai_cadeau_jours, ts) VALUES (?,?,?,?,?,?)",
            (prenom, d.isoformat(), lieu, notes, delai, iso_now()),
        )
        row = conn.execute("SELECT * FROM anniversaires WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _vers_dict(row, today())


def supprimer(anniversaire_id: int) -> bool:
    with db.cursor() as conn:
        cur = conn.execute("DELETE FROM anniversaires WHERE id = ?", (int(anniversaire_id),))
        return bool(cur.rowcount)


def rappels_du_jour(ref: date | None = None) -> list[dict[str, Any]]:
    """Anniversaires qui déclenchent une alerte à `ref` : J-délai cadeau, J-7, J-0."""
    ref = ref or today()
    with db.cursor() as conn:
        rows = conn.execute("SELECT * FROM anniversaires").fetchall()
    rappels: list[dict[str, Any]] = []
    for row in rows:
        a = _vers_dict(row, ref)
        dans = a["dans_jours"]
        seuils = {int(a["delai_cadeau_jours"]): "cadeau à prévoir", 7: "J-7", 0: "c'est aujourd'hui"}
        if dans not in seuils:
            continue
        rappels.append(
            {
                "type": "anniversaire",
                "id": a["id"],
                "prenom": a["prenom"],
                "date": a["prochaine"],
                "dans_jours": dans,
                "motif": seuils[dans],
                "age": a["age"],
                "lieu": a.get("lieu"),
                "notes": a.get("notes"),
            }
        )
    rappels.sort(key=lambda r: (r["dans_jours"], r["prenom"].casefold()))
    return rappels
