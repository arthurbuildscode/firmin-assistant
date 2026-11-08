"""Échéances (administratif, entretien, voiture, animaux…) avec rappels à J-n."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from dateutil.relativedelta import relativedelta

from tools import db
from tools.common import iso_now, parse_date, today, who

RECURRENCES: dict[str, relativedelta] = {
    "hebdomadaire": relativedelta(weeks=1),
    "mensuelle": relativedelta(months=1),
    "bimestrielle": relativedelta(months=2),
    "trimestrielle": relativedelta(months=3),
    "semestrielle": relativedelta(months=6),
    "annuelle": relativedelta(years=1),
}

_ALIAS = {
    "hebdo": "hebdomadaire",
    "semaine": "hebdomadaire",
    "mois": "mensuelle",
    "mensuel": "mensuelle",
    "trimestre": "trimestrielle",
    "trimestriel": "trimestrielle",
    "semestre": "semestrielle",
    "semestriel": "semestrielle",
    "an": "annuelle",
    "annee": "annuelle",
    "année": "annuelle",
    "annuel": "annuelle",
    "yearly": "annuelle",
    "monthly": "mensuelle",
    "weekly": "hebdomadaire",
}


def normaliser_recurrence(recurrence: str | None) -> str | None:
    if not recurrence:
        return None
    r = recurrence.strip().casefold()
    if r in ("aucune", "none", "non", ""):
        return None
    r = _ALIAS.get(r, r)
    if r not in RECURRENCES:
        raise ValueError(f"Récurrence inconnue : « {recurrence} » (attendu : {', '.join(RECURRENCES)})")
    return r


def prochaine_occurrence(base: date, recurrence: str | None, ref: date) -> date:
    """Première occurrence >= ref pour une échéance récurrente ; la date elle-même sinon."""
    if not recurrence:
        return base
    pas = RECURRENCES[recurrence]
    d = base
    # garde-fou : au plus ~ 600 itérations (50 ans en mensuel)
    for _ in range(600):
        if d >= ref:
            return d
        d = d + pas
    return d


def _vers_dict(row: Any, ref: date) -> dict[str, Any]:
    e = dict(row)
    try:
        e["rappels_jours"] = json.loads(e.get("rappels_jours") or "[]")
    except json.JSONDecodeError:
        e["rappels_jours"] = []
    base = parse_date(e["date"])
    prochaine = prochaine_occurrence(base, e.get("recurrence"), ref)
    e["prochaine"] = prochaine.isoformat()
    e["dans_jours"] = (prochaine - ref).days
    return e


def lister(horizon_jours: int = 60, ref: date | None = None) -> list[dict[str, Any]]:
    """Échéances dont la prochaine occurrence tombe dans l'horizon (les échéances passées non récurrentes sont incluses, marquées en retard)."""
    ref = ref or today()
    with db.cursor() as conn:
        rows = conn.execute("SELECT * FROM echeances").fetchall()
    resultat: list[dict[str, Any]] = []
    for row in rows:
        e = _vers_dict(row, ref)
        e["en_retard"] = e["dans_jours"] < 0
        if e["dans_jours"] <= horizon_jours:
            resultat.append(e)
    resultat.sort(key=lambda e: (e["prochaine"], e["titre"].casefold()))
    return resultat


def ajouter(
    titre: str,
    date_echeance: str | date,
    rappels_jours: list[int] | None = None,
    recurrence: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    titre = " ".join(titre.strip().split())
    if not titre:
        raise ValueError("Le titre de l'échéance est vide.")
    d = parse_date(date_echeance)
    rappels = sorted({int(x) for x in (rappels_jours if rappels_jours is not None else [30, 7, 1]) if int(x) >= 0}, reverse=True)
    rec = normaliser_recurrence(recurrence)
    with db.cursor() as conn:
        cur = conn.execute(
            "INSERT INTO echeances (titre, date, rappels_jours, recurrence, notes, cree_par, ts) VALUES (?,?,?,?,?,?,?)",
            (titre, d.isoformat(), json.dumps(rappels), rec, notes, who(), iso_now()),
        )
        row = conn.execute("SELECT * FROM echeances WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _vers_dict(row, today())


def supprimer(echeance_id: int) -> bool:
    with db.cursor() as conn:
        cur = conn.execute("DELETE FROM echeances WHERE id = ?", (int(echeance_id),))
        return bool(cur.rowcount)


def rappels_du_jour(ref: date | None = None) -> list[dict[str, Any]]:
    """Échéances qui déclenchent un rappel à `ref` : J-n (n dans rappels_jours), J-0, ou en retard."""
    ref = ref or today()
    with db.cursor() as conn:
        rows = conn.execute("SELECT * FROM echeances").fetchall()
    rappels: list[dict[str, Any]] = []
    for row in rows:
        e = _vers_dict(row, ref)
        dans = e["dans_jours"]
        if dans == 0:
            motif = "aujourd'hui"
        elif dans < 0:
            motif = f"en retard de {-dans} j"
        elif dans in e["rappels_jours"]:
            motif = f"J-{dans}"
        else:
            continue
        rappels.append(
            {
                "type": "echeance",
                "id": e["id"],
                "titre": e["titre"],
                "date": e["prochaine"],
                "dans_jours": dans,
                "motif": motif,
                "notes": e.get("notes"),
                "recurrence": e.get("recurrence"),
            }
        )
    rappels.sort(key=lambda r: (r["dans_jours"], r["titre"].casefold()))
    return rappels


__all__ = ["ajouter", "lister", "prochaine_occurrence", "rappels_du_jour", "supprimer"]
