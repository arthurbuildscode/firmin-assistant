"""Connecteur du site du salon (Playwright via `tools.navigation.Session`, jamais directement).

Parcours observé pendant l'exploration (voir README.md) :
- `/connexion` : formulaire « Adresse e-mail » / « Mot de passe », bouton « Se connecter » ;
- `/creneaux` : liste hydratée en JavaScript depuis `/api/creneaux` (JSON : id, libelle, prestation) ;
- `/reserver?id=<id>` : récapitulatif, bouton « Confirmer la réservation » → page « Rendez-vous confirmé ».

Lecture d'abord (`lister_creneaux`), action ensuite (`reserver`, appelée seulement par `executer` après le bouton
Valider). Les identifiants viennent du coffre (`ctx.secret`), jamais d'ailleurs.
"""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from tools.navigation import ErreurSite, Session, SessionExpiree

CLE_SECRET = "salon_demo"


def base(ctx: Any) -> str:
    return str(ctx.parametre("site")).rstrip("/")


# --- analyse pure (testée sans navigateur) ---------------------------------------------------------------------


def parser_creneaux(html: str) -> list[dict[str, str]]:
    """Créneaux affichés sur `/creneaux` (repli si l'API JSON n'a pas été entendue)."""
    soupe = BeautifulSoup(html, "html.parser")
    creneaux = []
    for li in soupe.select("li.creneau"):
        quand = li.select_one(".quand")
        prestation = li.select_one(".prestation")
        creneaux.append({
            "id": str(li.get("data-id") or ""),
            "libelle": quand.get_text(strip=True) if quand else "",
            "prestation": prestation.get_text(strip=True) if prestation else "",
        })
    return [c for c in creneaux if c["id"]]


def filtrer(creneaux: list[dict[str, Any]], prestation: str = "") -> list[dict[str, Any]]:
    if not prestation:
        return creneaux
    cible = prestation.strip().casefold()
    return [c for c in creneaux if cible in str(c.get("prestation", "")).casefold()]


def lire_reference(texte: str) -> str | None:
    m = re.search(r"\bRDV-\d+\b", texte)
    return m.group(0) if m else None


# --- navigation -------------------------------------------------------------------------------------------------


def _se_connecter(s: Session, ctx: Any) -> None:
    """Ouvre `/creneaux` ; si le site renvoie vers la connexion, remplit le formulaire avec le coffre."""
    page = s.ouvrir(base(ctx) + "/creneaux")
    if "/connexion" not in page.url:
        return
    compte = ctx.secret(CLE_SECRET)  # lève SecretManquant (« Identifiants … à renseigner ») si absent
    page.get_by_label(re.compile("e-mail", re.IGNORECASE)).fill(compte["identifiant"])
    page.get_by_label(re.compile("mot de passe", re.IGNORECASE)).fill(compte["mot_de_passe"])
    page.get_by_role("button", name=re.compile("se connecter", re.IGNORECASE)).click()
    page.wait_for_load_state("domcontentloaded")
    s.attendre_hydratation()
    if "/connexion" in page.url:
        s.capture("connexion-refusee")
        raise SessionExpiree("Le site du salon refuse la connexion : vérifie tes identifiants dans l'appli.")


def lister_creneaux(ctx: Any, prestation: str = "") -> list[dict[str, Any]]:
    """Créneaux libres (lecture seule)."""
    with Session.pour(ctx) as s:
        api = s.ecouter_json(r"/api/creneaux")
        try:
            _se_connecter(s, ctx)
            s.attendre_hydratation("li.creneau, #liste[aria-busy='false']")
        except ErreurSite:
            raise
        except Exception as exc:
            if exc.__class__.__name__ == "SecretManquant":
                raise
            raise s.echec("Je n'ai pas réussi à lire les créneaux du salon.", exc) from None
        creneaux = api[-1]["creneaux"] if api and isinstance(api[-1], dict) else parser_creneaux(s.p.content())
        s.journal("creneaux", nombre=len(creneaux))
    return filtrer([{k: c.get(k) for k in ("id", "libelle", "prestation")} for c in creneaux], prestation)


def reserver(ctx: Any, creneau_id: str) -> dict[str, Any]:
    """Réserve le créneau `creneau_id` (ACTION AVEC EFFET : seulement depuis `executer`, après validation)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", creneau_id or ""):
        raise ErreurSite("Créneau inconnu.")
    with Session.pour(ctx) as s:
        try:
            _se_connecter(s, ctx)
            page = s.ouvrir(f"{base(ctx)}/reserver?id={creneau_id}")
            recap = page.locator(".recap")
            if not recap.count():
                raise ErreurSite("Ce créneau n'est plus disponible.")
            libelle = recap.inner_text().strip()
            s.capture("avant-reservation")
            page.get_by_role("button", name=re.compile("confirmer la réservation", re.IGNORECASE)).click()
            page.wait_for_load_state("domcontentloaded")
            texte = page.inner_text("body")
            s.capture("apres-reservation")
        except ErreurSite:
            raise
        except Exception as exc:
            if exc.__class__.__name__ == "SecretManquant":
                raise
            raise s.echec("La réservation n'a pas abouti sur le site du salon.", exc) from None
        reference = lire_reference(texte)
        if "confirmé" not in texte.casefold() or not reference:
            raise s.echec("Le site n'a pas confirmé la réservation : rien n'est réservé.")
        s.journal("reservation", reference=reference)
    return {"reference": reference, "creneau": libelle}


def verifier_site(ctx: Any) -> dict[str, Any]:
    """Smoke test : le site répond, robots.txt permet la page de connexion, le formulaire est là."""
    with Session.pour(ctx, session=False) as s:
        page = s.ouvrir(base(ctx) + "/connexion")
        if not page.get_by_role("button", name=re.compile("se connecter", re.IGNORECASE)).count():
            raise s.echec("La page de connexion du salon a changé.")
    return {"ok": True, "message": "site du salon joignable, formulaire de connexion présent"}
