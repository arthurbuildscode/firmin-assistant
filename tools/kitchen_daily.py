"""Kitchen Daily : livraisons, menu, sélection des plats, extras et interrupteur de livraison (Playwright).

Le site (Next.js) n'a pas d'API publique utilisable : on manipule le DOM avec Playwright (API sync,
Chromium headless) en réutilisant l'état de session (`storage_state` : cookies + localStorage) créé
une fois par `python -m tools.kitchen_daily login`. Voir `docs/kitchen-daily-site.md`.

CLI (sur le serveur, cwd = racine du dépôt, HOMEOS_DATA défini) :

    python -m tools.kitchen_daily login                     # e-mail et mot de passe demandés par getpass
    python -m tools.kitchen_daily statut                    # la session est-elle valide ?
    python -m tools.kitchen_daily livraisons                # JSON
    python -m tools.kitchen_daily menu <id>                 # JSON
    python -m tools.kitchen_daily appliquer <id> --dry-run '{"Tagliatelles carbonara": 2, ...}'
    python -m tools.kitchen_daily extras <id>               # JSON : desserts, entrées, jus (lecture seule)
    python -m tools.kitchen_daily extras-definir <id> '{"desserts": {"Crème au chocolat": 2}}' [--dry-run]
    python -m tools.kitchen_daily livraison <id> --voulue|--pas-voulue [--dry-run]

Principes :
- Les identifiants ne sont jamais écrits nulle part (ni argument, ni fichier, ni log) : seul l'état de
  session est conservé dans `/var/lib/homeos/kd/state.json` (lot 5d ; `data/.kd/` en développement ou sur
  un VPS non migré), mode 640, hors des dossiers que la session Claude peut lire (`Read`). Lot 7 (outils en
  http) : lu par le serveur d'outils et le runner sous homeos seulement (`/var/lib/homeos` en 700) ; en stdio
  (repli), aussi par les outils sous chaque personne via le groupe foyer.
- Navigateur (lot 7) : Chromium tourne sous l'utilisateur `homeos-web` (service `homeos-web`, `playwright
  run-server` local, adresse dans `/etc/homeos/navigateur.env`) ; ce module s'y connecte, lui passe l'état de
  session en mémoire et écrit lui-même captures et session rafraîchie. Sans cette adresse (développement,
  `HOMEOS_NAVIGATEUR=local`), Chromium est lancé localement comme avant. Un seul navigateur à la fois par
  processus (`HOMEOS_NAVIGATEURS_MAX`).
- Proxy sortant filtrant (install.sh --filtrer-sortant) : Chromium ne lit pas `HTTPS_PROXY`, le proxy lui
  est passé explicitement au lancement (`tools.common.proxy_sortant`), y compris au navigateur distant.
- Sélecteurs fondés sur le texte et les rôles (classes CSS hashées instables) ; les cartes de plats
  sont repérées par `.product-item` ou `[class*=productItem]`.
- Le parsing est fait par des fonctions pures sur le HTML (`parser_livraisons`, `parser_menu`) :
  le navigateur ne sert qu'à charger la page, annoter le DOM (`data-kd-*`) et cliquer.
- En lecture, on ne clique jamais « Remise à zéro » ni l'interrupteur « Je veux ma livraison ». La lecture des
  extras parcourt le tunnel avec les « Je valide » d'étape (qui n'enregistrent rien) sans jamais cliquer
  « Je valide ma commande ».
- Tunnel (même URL) : PLATS → DESSERTS → ENTRÉES, SOUPES & APÉROS → JUS ET SHOTS DETOX ;
  « Je valide » (`data-testid=confirmOrder`, barre du bas) passe à l'étape suivante, seul « Je valide ma commande »
  (même bouton, dernière étape) enregistre le tout (plats + extras). Enregistrer les plats ne doit donc jamais
  altérer les extras : ils sont relus avant, comparés à chaque étape et réappliqués si le site les a perdus.

Détection de l'état « sélectionné » d'un plat (heuristique, voir `detecter_selection`) — le champ
quantité affiche 1 partout et n'est donc PAS fiable. Signaux essayés dans l'ordre :
  1. compteur « 0 / N PLATS » → aucun plat sélectionné ;
  2. attributs ARIA (`aria-pressed`, `aria-checked`, `aria-selected`, `data-selected`) sur la carte ou le `+` ;
  3. classe d'état (`selected`, `active`, `checked`, `added`, `chosen`, `inCart`…) sur la carte, le `+`
     ou le conteneur du compteur, présente sur une partie seulement des cartes ;
  4. valeurs du champ quantité différentes d'une carte à l'autre (alors `quantite` = valeur) ;
  5. couleur calculée du bouton `+` : exactement deux couleurs, dont un groupe de taille égale au compteur ;
  6. repli explicite : `selectionne = None` et `detection_selection = "indeterminee"` (l'application
     d'une sélection passe de toute façon par « Remise à zéro », donc reste correcte).
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import contextvars
import functools
import getpass
import json
import os
import re
import sys
import threading
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from tools.common import TZ, chemin_kd, data_dir, get_logger, load_config, now, parse_date, proxy_sortant

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import BrowserContext, Page

# Navigateurs installés par install.sh dans /opt/playwright (lisible par tous) : utile quand la CLI
# ou le runner sont lancés hors systemd (environnement minimal du bot).
if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH") and Path("/opt/playwright").is_dir():
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/playwright"

log = get_logger(__name__)

URL_DEFAUT = "https://www.kitchen-daily.fr"
CHEMIN_LIVRAISONS = "/account/shippings/upcoming"
CHEMIN_DETAIL = "/account/shippings/details/{id}"
CHEMIN_SELECTION = "/account/shippings/edit/select-meal/quotidien/{id}"

TIMEOUT_MS = 30_000
ATTENTE_VALIDATION_MS = 15_000  # changement de page attendu après « Je valide ma commande »
ETAPES_TUNNEL_MAX = 6  # PLATS → DESSERTS → ENTRÉES → JUS (+ marge) avant « Je valide ma commande »
MODE_ETAT = 0o640  # homeos (serveur d'outils, runner) ; en stdio aussi les personnes via le groupe foyer
FORMULE_DEFAUT = 18

SELECTEUR_CARTE = ".product-item, [class*='productItem']"
SELECTEUR_QTY = "input.qty, input[class*='qty' i], input[name*='qty' i], input[type='number']"

MESSAGE_SESSION = (
    "Session Kitchen Daily expirée ou absente : lance `python -m tools.kitchen_daily login` sur le serveur "
    "(voir README, section Kitchen Daily)."
)

ETAPES_CONNUES = ("PLATS", "DESSERTS", "ENTRÉES, SOUPES & APÉROS", "JUS ET SHOTS DETOX")
CATEGORIES_EXTRAS = ("desserts", "entrees", "jus")
LIBELLES_EXTRAS = {"desserts": "Desserts", "entrees": "Entrées, soupes & apéros", "jus": "Jus et shots détox"}
QUANTITE_EXTRA_MAX = 20
ATTENTE_ETAPE_MS = 6_000  # changement d'étape du tunnel après « Je valide »
ATTENTE_QTE_MS = 4_000  # mise à jour du champ quantité après un clic + / −


class ErreurKitchenDaily(RuntimeError):
    """Erreur métier Kitchen Daily, message en français destiné à l'utilisateur."""


class SessionExpiree(ErreurKitchenDaily):
    """L'état de session est absent ou n'ouvre plus l'espace client."""

    def __init__(self, message: str = MESSAGE_SESSION) -> None:
        super().__init__(message)


class SupplementNonGratuit(ErreurKitchenDaily):
    """Un extra présumé gratuit (dessert offert) augmente le montant affiché : il faut une confirmation."""


# --- configuration & chemins -----------------------------------------------


def url_base() -> str:
    kd = load_config().get("kitchen_daily") or {}
    url = kd.get("url") if isinstance(kd, dict) else None
    return str(url or URL_DEFAUT).rstrip("/")


def formule_config() -> int:
    kd = load_config().get("kitchen_daily") or {}
    try:
        return int(kd.get("formule_plats", FORMULE_DEFAUT)) if isinstance(kd, dict) else FORMULE_DEFAUT
    except (TypeError, ValueError):
        return FORMULE_DEFAUT


def kd_dir() -> Path:
    """`/var/lib/homeos/kd/` (lot 5d), sinon `data/.kd/` (2770, ignoré par le git de data/ grâce à son .gitignore)."""
    d = chemin_kd()
    if not d.exists():
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o2770)
        except OSError:
            pass
    ignore = d / ".gitignore"
    if not ignore.exists():
        try:
            ignore.write_text("# État de session Kitchen Daily et captures : jamais versionnés\n*\n", encoding="utf-8")
        except OSError:
            pass
    return d


def state_path() -> Path:
    return kd_dir() / "state.json"


def captures_dir() -> Path:
    d = kd_dir() / "captures"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sauver_etat(context: BrowserContext) -> None:
    """Écrit le storage_state de façon atomique, mode 640, groupe foyer si possible."""
    cible = state_path()
    if cible.exists() and cible.stat().st_uid != os.getuid():
        return  # pas propriétaire (ex. outil MCP sous alex) : on ne rafraîchit pas
    tmp = cible.with_suffix(".tmp")
    ancien = os.umask(0o077)
    try:
        context.storage_state(path=str(tmp))
    finally:
        os.umask(ancien)
    os.chmod(tmp, MODE_ETAT)
    from tools.common import mode_outils

    if mode_outils() != "http":  # stdio (lot 5d) : les outils sous chaque personne lisent l'état via le groupe foyer
        try:
            import grp

            os.chown(tmp, -1, grp.getgrnam("foyer").gr_gid)
        except (KeyError, PermissionError, OSError, ImportError):
            pass
    os.replace(tmp, cible)


# --- utilitaires purs --------------------------------------------------------


def normaliser(texte: str) -> str:
    """Minuscules, sans accents, ponctuation → espace, espaces réduits (le « + » est conservé)."""
    t = str(texte).casefold().replace("œ", "oe").replace("æ", "ae")
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^\w+]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def resoudre_titres(
    selection: dict[str, int], titres_menu: list[str]
) -> tuple[dict[str, int], list[str], dict[str, list[str]]]:
    """Associe chaque titre demandé à un titre exact du menu.

    Correspondance normalisée exacte d'abord, puis inclusion unique (« carbonara » → « Tagliatelles carbonara »).
    Renvoie `(resolue {titre_menu: quantité}, introuvables, ambigus {demandé: [candidats]})`.
    """
    index = {normaliser(t): t for t in titres_menu}
    resolue: dict[str, int] = {}
    introuvables: list[str] = []
    ambigus: dict[str, list[str]] = {}
    for demande, quantite in selection.items():
        n = normaliser(demande)
        titre = index.get(n)
        if titre is None and n:
            candidats = [t for k, t in index.items() if n in k or k in n]
            if len(candidats) == 1:
                titre = candidats[0]
            elif len(candidats) > 1:
                ambigus[demande] = candidats
                continue
        if titre is None:
            introuvables.append(demande)
            continue
        resolue[titre] = resolue.get(titre, 0) + int(quantite)
    return resolue, introuvables, ambigus


def valider_quantites(selection: dict[str, Any]) -> dict[str, int]:
    if not isinstance(selection, dict) or not selection:
        raise ErreurKitchenDaily("Sélection vide : indique un dictionnaire {titre du plat: quantité}.")
    propre: dict[str, int] = {}
    for titre, q in selection.items():
        if isinstance(q, bool) or not isinstance(q, int | float | str):
            raise ErreurKitchenDaily(f"Quantité invalide pour « {titre} » : nombre entier attendu.")
        try:
            quantite = int(q)
        except ValueError:
            raise ErreurKitchenDaily(f"Quantité invalide pour « {titre} » : nombre entier attendu.") from None
        if quantite < 1 or quantite != float(q):
            raise ErreurKitchenDaily(f"Quantité invalide pour « {titre} » : entier ≥ 1 attendu.")
        propre[str(titre)] = quantite
    return propre


def _date_fr(jour: str, mois: str, annee: str) -> date:
    return date(int(annee), int(mois), int(jour))


def _montant(texte: str) -> float | None:
    brut = re.sub(r"[\s \xa0]", "", texte).replace(",", ".")
    try:
        return round(float(brut), 2)
    except ValueError:
        return None


def lire_compteur(texte: str) -> tuple[int, int] | None:
    """« 18 / 18 PLATS » → (18, 18)."""
    m = re.search(r"(\d+)\s*/\s*(\d+)\s*plats\b", texte, re.IGNORECASE)
    return (int(m.group(1)), int(m.group(2))) if m else None


# --- parsing : livraisons ----------------------------------------------------

# `_` = espace ou séparateur « | » de `lineariser` (texte découpé en plusieurs balises).
_SEP = r"[\s|]"
_RE_JOUR_DATE = re.compile(
    rf"\b(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche){_SEP}+(\d{{1,2}})/(\d{{1,2}})/(\d{{4}})", re.IGNORECASE
)
_RE_CRENEAU = re.compile(rf"\b(\d{{1,2}})\s*h\s*(\d{{2}})?{_SEP}*-{_SEP}*(\d{{1,2}})\s*h\s*(\d{{2}})?", re.IGNORECASE)
_RE_STATUT = re.compile(rf"(Livraison\s[^⟦€]*?){_SEP}*-{_SEP}*(\d{{5,}})")
_RE_MONTANT = re.compile(r"(\d[\d\s\u202f\xa0]*(?:[.,]\d{1,2})?)\s*€")
_RE_PRELEV = re.compile(rf"(Sera prélevée le|Prélevée le|Payée le){_SEP}+(\d{{1,2}})/(\d{{1,2}})/(\d{{4}})", re.IGNORECASE)
_RE_LIMITE = re.compile(
    rf"(?:À modifier avant le|A modifier avant le|Modifiable jusqu['’]au){_SEP}+(\d{{1,2}})/(\d{{1,2}})/(\d{{4}})"
    rf"(?:{_SEP}+à{_SEP}+(\d{{1,2}})\s*h\s*(\d{{2}})?)?",
    re.IGNORECASE,
)
_RE_LIEN_DETAIL = re.compile(r"/account/shippings/details/([A-Za-z0-9_-]+)")
_RE_MARQUEUR = re.compile(r"⟦KD:([A-Za-z0-9_-]+)⟧")
_RE_STATUT_SAUTEE = re.compile(r"annul|suspendu|saut|d[ée]sactiv|pas de livraison", re.IGNORECASE)


def _soupe(html: str) -> Any:
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:  # pragma: no cover
        raise ErreurKitchenDaily("Dépendance manquante : beautifulsoup4 (pip install -r tools/requirements.txt).") from exc
    return BeautifulSoup(html, "html.parser")


def lineariser(html: str) -> str:
    """Texte de la page, morceaux séparés par « | », liens de détail remplacés par un marqueur ⟦KD:<id>⟧."""
    from bs4 import Comment, NavigableString, Tag

    soupe = _soupe(html)
    morceaux: list[str] = []
    for el in soupe.descendants:
        if isinstance(el, Tag) and el.name == "a":
            m = _RE_LIEN_DETAIL.search(str(el.get("href") or ""))
            if m:
                morceaux.append(f"⟦KD:{m.group(1)}⟧")
        elif isinstance(el, NavigableString) and el.parent is not None:
            if el.parent.name in ("script", "style", "noscript", "template") or isinstance(el, Comment):
                continue
            t = re.sub(r"\s+", " ", str(el)).strip()
            if t:
                morceaux.append(t)
    return " | ".join(morceaux)


def parser_livraisons(html: str, reference: datetime | None = None) -> list[dict[str, Any]]:
    """Parse la page `/account/shippings/upcoming` (HTML) en liste de livraisons."""
    ref = reference or now()
    texte = lineariser(html)
    debuts = list(_RE_JOUR_DATE.finditer(texte))
    livraisons: list[dict[str, Any]] = []
    for i, m in enumerate(debuts):
        segment = texte[m.start() : debuts[i + 1].start() if i + 1 < len(debuts) else len(texte)]
        jour_livraison = _date_fr(m.group(2), m.group(3), m.group(4))
        lien = _RE_MARQUEUR.search(segment)
        creneau = _RE_CRENEAU.search(segment)
        statut = _RE_STATUT.search(segment)
        montant = _RE_MONTANT.search(segment)
        prelev = _RE_PRELEV.search(segment)
        limite = _RE_LIMITE.search(segment)
        limite_dt: datetime | None = None
        if limite:
            h = int(limite.group(4)) if limite.group(4) else 23
            mn = int(limite.group(5)) if limite.group(5) else (59 if not limite.group(4) else 0)
            d = _date_fr(limite.group(1), limite.group(2), limite.group(3))
            limite_dt = datetime(d.year, d.month, d.day, h, mn, tzinfo=TZ)
        creneau_txt = None
        if creneau:
            h1, m1, h2, m2 = creneau.groups()
            creneau_txt = f"{int(h1)}h{m1 or ''} - {int(h2)}h{m2 or ''}"
        livraisons.append(
            {
                "id": lien.group(1) if lien else None,
                "date": jour_livraison.isoformat(),
                "jour": m.group(1).capitalize(),
                "creneau": creneau_txt,
                "numero": statut.group(2) if statut else None,
                "statut": re.sub(r"[\s|]+", " ", statut.group(1)).strip() if statut else None,
                "montant": _montant(montant.group(1)) if montant else None,
                "prelevement_le": _date_fr(*prelev.group(2, 3, 4)).isoformat() if prelev else None,
                "payee": bool(prelev and prelev.group(1).lower().startswith("pay")),
                "modifiable_jusqua": limite_dt.isoformat() if limite_dt else None,
                "modifiable": bool(limite_dt and limite_dt > ref),
                # Interrupteur « Je veux ma livraison » : lu sur la page de détail (`_lire_livraisons_page`) ;
                # ici seulement déduit d'un statut explicite (« annulée », « suspendue »…), sinon inconnu.
                "livraison_voulue": False if statut and _RE_STATUT_SAUTEE.search(statut.group(1)) else None,
            }
        )
    # Une même livraison peut apparaître deux fois (ex. bandeau + liste) : on garde la plus complète.
    uniques: dict[str, dict[str, Any]] = {}
    sans_id: list[dict[str, Any]] = []
    for liv in livraisons:
        if liv["id"] is None:
            sans_id.append(liv)
        elif liv["id"] not in uniques or sum(v is not None for v in liv.values()) > sum(
            v is not None for v in uniques[liv["id"]].values()
        ):
            uniques[liv["id"]] = liv
    resultat = list(uniques.values()) + [liv for liv in sans_id if liv["numero"]]
    return sorted(resultat, key=lambda x: x["date"])


def prochaine_modifiable(livraisons: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Première livraison encore modifiable et non sautée (`livraison_voulue` différent de False)."""
    candidates = [
        liv for liv in livraisons if liv.get("modifiable") and liv.get("id") and liv.get("livraison_voulue") is not False
    ]
    return min(candidates, key=lambda x: x["date"]) if candidates else None


# --- parsing : menu ----------------------------------------------------------

_RE_CLASSE_ETAT = re.compile(r"(?<!un)(?<!in)(?<!not)(?<!not_)(?<!not-)(selected|active|checked|added|chosen|incart|in-cart)", re.IGNORECASE)
_TEXTES_PLUS = {"+", "＋"}
_TEXTES_MOINS = {"-", "−", "–", "—"}


def _txt(el: Any) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el is not None else ""


def _role_bouton(bouton: Any) -> str | None:
    if bouton.get("data-kd-role") in ("plus", "moins"):
        return str(bouton.get("data-kd-role"))
    texte = _txt(bouton)
    etiquette = f"{bouton.get('aria-label') or ''} {bouton.get('title') or ''}".lower()
    if texte in _TEXTES_PLUS or re.search(r"ajout|plus|augment|incr", etiquette):
        return "plus"
    if texte in _TEXTES_MOINS or re.search(r"moins|retir|diminu|decr", etiquette):
        return "moins"
    return None


def _cartes(soupe: Any) -> list[Any]:
    toutes = soupe.select(SELECTEUR_CARTE)
    ids = {id(c) for c in toutes}
    externes = [c for c in toutes if not any(id(p) in ids for p in c.parents)]
    if externes and all(c.get("data-kd-index") is not None for c in externes):
        externes.sort(key=lambda c: int(c.get("data-kd-index")))
    return externes


def _titre_carte(carte: Any) -> str:
    el = carte.select_one("h1, h2, h3, h4, h5, h6") or carte.select_one("[class*='title' i]")
    return _txt(el)


def _badges_carte(carte: Any, titre: str) -> list[str]:
    candidats = carte.select("[class*='badge' i], [class*='tag' i], [class*='label' i]")
    ids = {id(c) for c in candidats}
    badges: list[str] = []
    vus: set[str] = set()
    for el in candidats:
        if any(id(d) in ids for d in el.find_all(True)):
            continue  # conteneur de badges : on garde les feuilles
        t = _txt(el)
        n = normaliser(t)
        if not t or len(t) > 40 or n == normaliser(titre) or n in vus or el.name in ("button", "input"):
            continue
        if re.fullmatch(r"[\d\s+\-−/]*", t):
            continue
        vus.add(n)
        badges.append(t)
    return badges


def _plus_et_qty(carte: Any) -> tuple[Any, Any]:
    qty = carte.select_one(SELECTEUR_QTY)
    plus = None
    for b in carte.select("button, [role='button']"):
        if _role_bouton(b) == "plus":
            plus = b
    if plus is None and qty is not None:
        suivant = qty.find_next_sibling(["button"])
        plus = suivant
    return plus, qty


def _signaux_carte(carte: Any) -> dict[str, Any]:
    plus, qty = _plus_et_qty(carte)
    aria: bool | None = None
    for el in (carte, plus):
        if el is None:
            continue
        for attr in ("aria-pressed", "aria-checked", "aria-selected", "data-selected"):
            v = el.get(attr)
            if v is not None and str(v).lower() in ("true", "false"):
                aria = aria or str(v).lower() == "true"
    elements_classe = [carte, plus, qty.parent if qty is not None else None]
    classe = any(
        _RE_CLASSE_ETAT.search(str(tok))
        for el in elements_classe
        if el is not None
        for tok in (el.get("class") or [])
    )
    valeur_qty: int | None = None
    if qty is not None:
        brut = qty.get("data-kd-qty", qty.get("value"))
        try:
            valeur_qty = int(str(brut).strip()) if brut is not None and str(brut).strip() != "" else None
        except ValueError:
            valeur_qty = None
    return {
        "aria": aria,
        "classe": classe,
        "qty": valeur_qty,
        "couleur_plus": plus.get("data-kd-bg") if plus is not None else None,
        "a_plus": plus is not None,
    }


def detecter_selection(
    signaux: list[dict[str, Any]], compteur: tuple[int, int] | None
) -> tuple[str, list[bool | None], list[int | None]]:
    """Heuristique documentée en tête de module. Renvoie (méthode, sélectionnés, quantités)."""
    n = len(signaux)
    inconnu: tuple[str, list[bool | None], list[int | None]] = ("indeterminee", [None] * n, [None] * n)
    if n == 0:
        return inconnu

    def avec_quantites(methode: str, sel: list[bool]) -> tuple[str, list[bool | None], list[int | None]]:
        nb = sum(sel)
        if compteur is not None and nb == compteur[0]:
            quant: list[int | None] = [1 if s else 0 for s in sel]
        else:
            quant = [None if s else 0 for s in sel]
        return methode, list(sel), quant

    if compteur is not None and compteur[0] == 0:
        return "compteur_zero", [False] * n, [0] * n
    if any(s["aria"] is not None for s in signaux):
        return avec_quantites("aria", [bool(s["aria"]) for s in signaux])
    classes = [bool(s["classe"]) for s in signaux]
    if any(classes) and not all(classes):
        return avec_quantites("classe", classes)
    qtys = [s["qty"] for s in signaux]
    if all(q is not None for q in qtys) and len(set(qtys)) > 1:
        return "quantite", [bool(q and q > 0) for q in qtys], [max(int(q or 0), 0) for q in qtys]
    couleurs = [s["couleur_plus"] for s in signaux]
    if compteur is not None and all(couleurs) and len(set(couleurs)) == 2:
        groupes = {c: [i for i, x in enumerate(couleurs) if x == c] for c in set(couleurs)}
        egaux = [c for c, idx in groupes.items() if len(idx) == compteur[0]]
        if len(egaux) == 1:
            return avec_quantites("couleur", [c == egaux[0] for c in couleurs])
    return inconnu


def parser_menu(html: str, livraison_id: str = "") -> dict[str, Any]:
    """Parse la page de sélection des plats (HTML, éventuellement annoté par le navigateur)."""
    soupe = _soupe(html)
    texte = soupe.get_text(" ", strip=True)
    compteur = lire_compteur(texte)
    cartes = _cartes(soupe)
    signaux = [_signaux_carte(c) for c in cartes]
    methode, selections, quantites = detecter_selection(signaux, compteur)
    plats: list[dict[str, Any]] = []
    for i, carte in enumerate(cartes):
        titre = _titre_carte(carte)
        plats.append(
            {
                "index": int(carte.get("data-kd-index", i)),
                "titre": titre,
                "badges": _badges_carte(carte, titre),
                "selectionne": selections[i],
                "quantite": quantites[i],
            }
        )
    formule = compteur[1] if compteur else None
    if formule is None:
        m = re.search(r"formule\s+(\d+)\s+plats", texte, re.IGNORECASE)
        formule = int(m.group(1)) if m else formule_config()
    texte_maj = texte.upper()
    etapes = [e for e in ETAPES_CONNUES if e in texte_maj]
    menu: dict[str, Any] = {
        "livraison_id": livraison_id,
        "formule_plats": formule,
        "compteur": {"selectionnes": compteur[0], "formule": compteur[1]} if compteur else None,
        "plats": plats,
        "etapes": etapes,
        "detection_selection": methode,
    }
    if methode == "indeterminee":
        menu["avertissement"] = (
            "Je ne sais pas lire quels plats sont déjà cochés sur le site ; la sélection proposée remplacera "
            "entièrement l'actuelle."
        )
    return menu


# --- parsing : étapes du tunnel et extras ---------------------------------------

_RE_TOKEN_COURANT = re.compile(r"(?:^|[_-])current$", re.IGNORECASE)
_RE_TOKEN_OFFERT = re.compile(r"offer|offert|gift|cadeau", re.IGNORECASE)
_RE_TOKEN_PROMO = re.compile(r"(?:^|[_-])promo(?:tion)?$", re.IGNORECASE)
_RE_PRIX_UNITE = re.compile(r"(\d+)\s*[.,]\s*(\d{2})\s*€\s*/\s*unit", re.IGNORECASE)
_RE_PRIX = re.compile(r"(\d+)\s*[.,]\s*(\d{2})\s*€")
_RE_PROMO_DETAIL = re.compile(r"(\d+[.,]\d{2})\s*€\s*[ÉEé]conomisez\s*(\d+[.,]\d{2})\s*€", re.IGNORECASE)
# Barre du bas d'une étape d'extras : « 1 Desserts - 0 € », « 0 Jus et shots detox - 0 € »
_RE_BARRE_EXTRAS = re.compile(r"(\d+)\s+([^\W\d][^|€]*?)\s+-\s+(\d+(?:\s*[.,]\s*\d{1,2})?)\s*€")


def categorie_etape(libelle: str) -> str | None:
    """« Desserts », « Entrées, Soupes & Apéros », « jus », « Plats »… → desserts | entrees | jus | plats."""
    n = normaliser(libelle)
    if re.search(r"\bdesserts?\b", n):
        return "desserts"
    if re.search(r"\b(entrees?|soupes?|aperos?)\b", n):
        return "entrees"
    if re.search(r"\b(jus|shots?|detox)\b", n):
        return "jus"
    if re.search(r"\bplats?\b", n):
        return "plats"
    return None


def lire_barre_extras(texte: str) -> tuple[str, int, float | None] | None:
    """Texte de la page → (catégorie, nombre d'articles, montant de l'étape en €) d'après la barre du bas."""
    for m in _RE_BARRE_EXTRAS.finditer(texte):
        cat = categorie_etape(m.group(2))
        if cat in CATEGORIES_EXTRAS:
            return cat, int(m.group(1)), _montant(m.group(3))
    return None


def detecter_etape(soupe: Any) -> str | None:
    """Étape courante du tunnel : onglet `…__current`, puis titre « Je choisis mes … », barre du bas, compteur de plats."""
    if isinstance(soupe, str):
        soupe = _soupe(soupe)
    for el in soupe.find_all(True, class_=True):
        if any(_RE_TOKEN_COURANT.search(str(t)) for t in el.get("class") or []):
            texte = _txt(el)
            if texte and len(texte) <= 40 and (cat := categorie_etape(texte)):
                return cat
    for chaine in soupe.find_all(string=re.compile(r"je\s+choisis\s+mes", re.IGNORECASE)):
        conteneur = chaine.parent
        for _ in range(3):
            if conteneur is None or not re.fullmatch(r"(?i)\s*je\s+choisis\s+mes\s*", _txt(conteneur)):
                break
            conteneur = conteneur.parent
        m = re.search(r"je\s+choisis\s+mes\s+(.{2,60})", _txt(conteneur), re.IGNORECASE)
        if m and (cat := categorie_etape(m.group(1))):
            return cat
    texte = soupe.get_text(" ", strip=True)
    barre = lire_barre_extras(texte)
    if barre:
        return barre[0]
    if lire_compteur(texte):
        return "plats"
    return None


def _classes_proches(carte: Any, profondeur: int = 3) -> list[str]:
    jetons: list[str] = []
    el = carte
    for _ in range(profondeur + 1):
        if el is None or not hasattr(el, "get"):
            break
        jetons += [str(t) for t in el.get("class") or []]
        el = el.parent
    return jetons


def _produit_extra(carte: Any, i: int) -> dict[str, Any]:
    titre = _titre_carte(carte)
    texte = _txt(carte)
    prix = _RE_PRIX_UNITE.search(texte) or _RE_PRIX.search(texte)
    qty = carte.select_one(SELECTEUR_QTY)
    quantite: int | None = None
    if qty is not None:
        brut = qty.get("data-kd-qty", qty.get("value"))
        try:
            quantite = int(str(brut).strip()) if brut is not None and str(brut).strip() != "" else None
        except ValueError:
            quantite = None
    promo = any(
        _RE_TOKEN_PROMO.search(str(t)) for el in carte.find_all(True, class_=True) for t in el.get("class") or []
    )
    detail = _RE_PROMO_DETAIL.search(texte)
    return {
        "index": int(carte.get("data-kd-index", i)),
        "titre": titre,
        "prix_unitaire": float(f"{prix.group(1)}.{prix.group(2)}") if prix else None,
        "quantite": quantite,
        # Éligible au dessert offert (picto 🎁, classe `…__isOfferedExtras` observée sur le <li> parent).
        "offert_possible": any(_RE_TOKEN_OFFERT.search(t) for t in _classes_proches(carte)) or "🎁" in texte,
        "promo": promo,
        "prix_barre": _montant(detail.group(1)) if detail else None,
        "economie": _montant(detail.group(2)) if detail else None,
        "stock_limite": bool(re.search(r"stock\s+limit", texte, re.IGNORECASE)),
        "badges": _badges_carte(carte, titre),
    }


def parser_etape_extras(html: str) -> dict[str, Any]:
    """Parse une étape d'extras du tunnel (HTML, éventuellement annoté) : produits, prix, quantités, barre du bas."""
    soupe = _soupe(html)
    etape = detecter_etape(soupe)
    barre = lire_barre_extras(soupe.get_text(" ", strip=True))
    produits = [_produit_extra(c, i) for i, c in enumerate(_cartes(soupe))]
    return {
        "etape": etape,
        "libelle": LIBELLES_EXTRAS.get(etape or ""),
        "nombre": barre[1] if barre else None,
        "montant": barre[2] if barre else None,
        "produits": produits,
    }


def quantites_extras(etapes: dict[str, dict[str, Any]]) -> dict[str, dict[str, int]]:
    """{catégorie: {titre: quantité}} (toutes les quantités, zéros compris) depuis les étapes lues."""
    return {
        cat: {p["titre"]: int(p.get("quantite") or 0) for p in e.get("produits", []) if p.get("titre")}
        for cat, e in etapes.items()
    }


def valider_extras(extras: Any) -> dict[str, dict[str, int]]:
    """{"desserts": {titre: qte}, "entrees": {...}, "jus": {...}} ; qte entier ≥ 0 (0 = retirer)."""
    if not isinstance(extras, dict) or not extras:
        raise ErreurKitchenDaily(
            'Extras vides : indique {"desserts": {titre: quantité}, "entrees": {...}, "jus": {...}} (0 pour retirer).'
        )
    propre: dict[str, dict[str, int]] = {}
    for cle, produits in extras.items():
        cat = categorie_etape(str(cle))
        if cat not in CATEGORIES_EXTRAS:
            raise ErreurKitchenDaily(f"Catégorie d'extras inconnue « {cle} » : desserts, entrees ou jus.")
        if not isinstance(produits, dict) or not produits:
            raise ErreurKitchenDaily(f"« {cle} » : dictionnaire {{titre: quantité}} attendu.")
        for titre, q in produits.items():
            if isinstance(q, bool) or not isinstance(q, int | float | str):
                raise ErreurKitchenDaily(f"Quantité invalide pour « {titre} » : entier attendu.")
            try:
                n = int(q)
            except ValueError:
                raise ErreurKitchenDaily(f"Quantité invalide pour « {titre} » : entier attendu.") from None
            if n < 0 or n != float(q) or n > QUANTITE_EXTRA_MAX:
                raise ErreurKitchenDaily(f"Quantité invalide pour « {titre} » : entier entre 0 et {QUANTITE_EXTRA_MAX}.")
            propre.setdefault(cat, {})[str(titre)] = n
    return propre


def _resoudre_un(demande: str, titres: list[str]) -> tuple[str | None, str | None]:
    """(titre exact, erreur)."""
    resolue, _, ambigus = resoudre_titres({demande: 1}, titres)
    if resolue:
        return next(iter(resolue)), None
    if ambigus:
        return None, f"« {demande} » est ambigu ({' / '.join(ambigus[demande])})"
    return None, f"« {demande} » absent"


def resoudre_extras(
    demandes: dict[str, dict[str, int]], etapes: dict[str, dict[str, Any]]
) -> tuple[dict[str, dict[str, int]], list[dict[str, Any]]]:
    """Titres demandés → titres exacts des étapes lues. Renvoie (cibles des seuls produits à changer, changements).

    Lève une erreur listant les titres disponibles si un titre est absent ou ambigu, ou si une étape est illisible.
    """
    cibles: dict[str, dict[str, int]] = {}
    changements: list[dict[str, Any]] = []
    erreurs: list[str] = []
    for cat, produits in demandes.items():
        etape = etapes.get(cat) or {}
        disponibles = [p for p in etape.get("produits", []) if p.get("titre")]
        if not disponibles:
            erreurs.append(f"{LIBELLES_EXTRAS[cat]} : étape illisible ou sans produit")
            continue
        par_titre = {p["titre"]: p for p in disponibles}
        manques: list[str] = []
        for demande, voulu in produits.items():
            titre, erreur = _resoudre_un(demande, list(par_titre))
            if titre is None:
                manques.append(str(erreur))
                continue
            p = par_titre[titre]
            avant = int(p.get("quantite") or 0)
            if p.get("quantite") is None:
                erreurs.append(f"{LIBELLES_EXTRAS[cat]} : quantité de « {titre} » illisible")
                continue
            if voulu != avant:
                cibles.setdefault(cat, {})[titre] = voulu
                changements.append(
                    {
                        "categorie": cat,
                        "titre": titre,
                        "avant": avant,
                        "apres": voulu,
                        "prix_unitaire": p.get("prix_unitaire"),
                        "offert_possible": bool(p.get("offert_possible")),
                    }
                )
        if manques:
            erreurs.append(
                f"{LIBELLES_EXTRAS[cat]} : " + ", ".join(manques) + " — disponibles : " + " ; ".join(par_titre)
            )
    if erreurs:
        raise ErreurKitchenDaily("Extras à corriger — " + " | ".join(erreurs) + ". Reprends les titres exacts.")
    return cibles, changements


def _cout_extras(etapes: dict[str, dict[str, Any]], quantites: dict[str, dict[str, int]]) -> float | None:
    """Montant des extras : somme quantité × prix, moins un dessert offert (le moins cher des éligibles choisis)."""
    total = 0.0
    eligibles: list[float] = []
    for cat, e in etapes.items():
        for p in e.get("produits", []):
            q = quantites.get(cat, {}).get(p.get("titre"), 0)
            if not q:
                continue
            if p.get("prix_unitaire") is None:
                return None
            total += q * float(p["prix_unitaire"])
            if p.get("offert_possible"):
                eligibles.append(float(p["prix_unitaire"]))
    if eligibles:
        total -= min(eligibles)
    return round(total, 2)


def estimer_extras(etapes: dict[str, dict[str, Any]], changements: list[dict[str, Any]]) -> dict[str, Any]:
    """Supplément estimé et nature du changement : `gratuit` (dessert offert seul, sans retrait) ou à confirmer."""
    avant = quantites_extras(etapes)
    apres = {cat: dict(v) for cat, v in avant.items()}
    for c in changements:
        apres.setdefault(c["categorie"], {})[c["titre"]] = c["apres"]
    cout_avant, cout_apres = _cout_extras(etapes, avant), _cout_extras(etapes, apres)
    supplement = None if cout_avant is None or cout_apres is None else round(cout_apres - cout_avant, 2)
    retraits = any(c["apres"] < c["avant"] for c in changements)
    gratuit = bool(
        changements
        and not retraits
        and supplement is not None
        and supplement <= 0.005
        and all(c["offert_possible"] for c in changements)
    )
    return {"supplement_estime": supplement, "retraits": retraits, "gratuit": gratuit}


def _fmt_euros(x: float | None, signe: bool = False) -> str:
    if x is None:
        return "montant inconnu"
    texte = f"{abs(x):.2f}".replace(".", ",")
    if signe:
        return ("+" if x >= 0 else "−") + texte + " €"
    return ("−" if x < 0 else "") + texte + " €"


def resume_changements(changements: list[dict[str, Any]]) -> str:
    morceaux = []
    for c in changements:
        delta = c["apres"] - c["avant"]
        if c["apres"] == 0:
            morceaux.append(f"retirer {c['titre']}")
        else:
            morceaux.append(f"{'+' if delta > 0 else '−'}{abs(delta)} {c['titre']} (→ {c['apres']})")
    return " ; ".join(morceaux)


# --- parsing : interrupteur « Je veux ma livraison » ----------------------------

_RE_JE_VEUX = re.compile(r"je\s+veux\s+ma\s+livraison", re.IGNORECASE)


def parser_interrupteur(html: str) -> bool | None:
    """État de l'interrupteur « Je veux ma livraison » (page de détail) : True, False ou None si illisible.

    Lit d'abord l'annotation du navigateur (`data-kd-etat`, valeur réelle de `checked`), sinon les attributs
    statiques (`checked`, `aria-checked`, `aria-pressed`) du premier interrupteur proche du libellé.
    """
    soupe = _soupe(html)
    marque = soupe.select_one("[data-kd-etat]")
    if marque is not None and str(marque.get("data-kd-etat")).lower() in ("true", "false"):
        return str(marque.get("data-kd-etat")).lower() == "true"
    for chaine in soupe.find_all(string=_RE_JE_VEUX):
        el = chaine.parent
        for _ in range(6):
            if el is None:
                break
            sw = el.select_one("[role='switch'], input[type='checkbox'], [aria-checked], [aria-pressed]")
            if sw is not None:
                if sw.name == "input":
                    return sw.has_attr("checked")
                v = str(sw.get("aria-checked", sw.get("aria-pressed", ""))).lower()
                return {"true": True, "false": False}.get(v)
            el = el.parent
    return None


# --- navigateur ----------------------------------------------------------------

T = TypeVar("T")


def _hors_boucle_asyncio(fn: Callable[..., T]) -> Callable[..., T]:
    """L'API sync de Playwright refuse de tourner dans un thread qui a une boucle asyncio (serveur MCP) :
    on exécute alors l'appel dans un thread dédié."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return fn(*args, **kwargs)
        # Copie du contexte : l'identité de la requête (tools.identite, lot 7) suit l'appel dans le fil.
        contexte = contextvars.copy_context()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="kd") as ex:
            return ex.submit(contexte.run, fn, *args, **kwargs).result()

    return wrapper


# Un seul Chromium à la fois par processus (serveur d'outils persistant : deux conversations simultanées ne
# lancent pas deux navigateurs sur un VPS de 4 Go) ; HOMEOS_NAVIGATEURS_MAX pour en autoriser plus.
_NAVIGATEURS = threading.BoundedSemaphore(max(1, int(os.environ.get("HOMEOS_NAVIGATEURS_MAX", "1") or 1)))
ATTENTE_NAVIGATEUR_S = 180
FICHIER_NAVIGATEUR = "navigateur.env"


def navigateur_distant() -> str | None:
    """Adresse du navigateur distant `homeos-web` (lot 7) : `HOMEOS_NAVIGATEUR_WS`, sinon
    `/etc/homeos/navigateur.env` ; None = Chromium lancé localement (développement, retour arrière).
    `HOMEOS_NAVIGATEUR=local` force le lancement local."""
    if os.environ.get("HOMEOS_NAVIGATEUR", "").strip().lower() == "local":
        return None
    ws = os.environ.get("HOMEOS_NAVIGATEUR_WS", "").strip()
    if not ws:
        from tools.common import _lire_env, secrets_dir

        ws = _lire_env(secrets_dir() / FICHIER_NAVIGATEUR).get("HOMEOS_NAVIGATEUR_WS", "").strip()
    if ws and not re.match(r"^wss?://(127\.0\.0\.1|localhost|\[::1\]):\d+/", ws):
        log.warning("HOMEOS_NAVIGATEUR_WS ignoré : seul un navigateur local (127.0.0.1) est accepté")
        return None
    return ws or None


def _ouvrir_navigateur(p: Any) -> Any:
    """Chromium : distant (`homeos-web`, sans accès à data/ ni à /var/lib/homeos) si configuré, sinon local."""
    from playwright.sync_api import Error as PlaywrightError

    ws = navigateur_distant()
    options = options_lancement()
    if ws:
        # Le serveur Playwright (playwright run-server, utilisateur homeos-web) lance Chromium avec les options
        # sûres transmises ici (headless, proxy) ; le sandbox se règle côté serveur.
        distantes = {k: v for k, v in options.items() if k in ("headless", "proxy")}
        try:
            return p.chromium.connect(
                ws, timeout=TIMEOUT_MS, headers={"x-playwright-launch-options": json.dumps(distantes)}
            )
        except PlaywrightError as exc:
            raise ErreurKitchenDaily(
                "Navigateur distant indisponible (service homeos-web) : `systemctl status homeos-web`, "
                "ou `HOMEOS_NAVIGATEUR=local` pour lancer Chromium localement."
            ) from exc
    try:
        return p.chromium.launch(**options)
    except PlaywrightError as exc:
        raise ErreurKitchenDaily(
            "Chromium introuvable ou impossible à lancer : `PLAYWRIGHT_BROWSERS_PATH=/opt/playwright "
            ".venv/bin/playwright install --with-deps chromium`."
        ) from exc


@contextmanager
def navigateur(avec_session: bool = True) -> Iterator[Page]:
    """Ouvre Chromium headless (contexte fr-FR, Europe/Paris) avec l'état de session ; le rafraîchit en sortie.

    Avec le navigateur distant (`homeos-web`), l'état de session est lu ici et transmis en mémoire ; l'état
    rafraîchi et les captures d'écran reviennent en octets et sont écrits par ce processus : le navigateur ne voit
    jamais `data/` ni `/var/lib/homeos`."""
    if not _NAVIGATEURS.acquire(timeout=ATTENTE_NAVIGATEUR_S):
        raise ErreurKitchenDaily("Kitchen Daily est déjà en cours d'utilisation : réessaie dans une minute.")
    try:
        with _navigateur(avec_session) as page:
            yield page
    finally:
        _NAVIGATEURS.release()


@contextmanager
def _navigateur(avec_session: bool = True) -> Iterator[Page]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ErreurKitchenDaily(
            "Playwright n'est pas installé : `.venv/bin/pip install -r tools/requirements.txt` puis "
            "`PLAYWRIGHT_BROWSERS_PATH=/opt/playwright .venv/bin/playwright install --with-deps chromium`."
        ) from exc
    etat = state_path()
    if avec_session and not etat.exists():
        raise SessionExpiree()
    if avec_session and not os.access(etat, os.R_OK):
        raise ErreurKitchenDaily(
            f"État de session Kitchen Daily illisible par l'utilisateur courant ({etat}) : vérifier le mode 640 "
            "et le groupe foyer."
        )
    try:
        session = json.loads(etat.read_text(encoding="utf-8")) if avec_session else None
    except ValueError:
        raise SessionExpiree() from None
    with sync_playwright() as p:
        browser = _ouvrir_navigateur(p)
        try:
            context = browser.new_context(
                storage_state=session,
                locale="fr-FR",
                timezone_id="Europe/Paris",
                viewport={"width": 1280, "height": 1600},
            )
            page = context.new_page()
            page.set_default_timeout(TIMEOUT_MS)
            page.set_default_navigation_timeout(TIMEOUT_MS)
            yield page  # sans écouteur, Playwright refuse automatiquement les boîtes de dialogue
            if avec_session:
                try:
                    _sauver_etat(context)
                except OSError as exc:
                    log.info("état de session non rafraîchi (%s)", exc.__class__.__name__)
        finally:
            browser.close()


def options_lancement() -> dict[str, Any]:
    """Options de `chromium.launch` : headless, proxy filtrant s'il est activé, sandbox si demandée.

    `HOMEOS_CHROMIUM_SANDBOX=1` active le sandbox de Chromium (profil AppArmor `homeos-chromium` installé
    par install.sh, qui autorise les espaces de noms utilisateur sous Ubuntu 24.04).
    """
    options: dict[str, Any] = {"headless": True}
    proxy = proxy_sortant()
    serveur = proxy.get("HTTPS_PROXY") or proxy.get("https_proxy")
    if serveur:
        options["proxy"] = {"server": serveur, "bypass": proxy.get("NO_PROXY") or proxy.get("no_proxy") or ""}
    if os.environ.get("HOMEOS_CHROMIUM_SANDBOX") == "1":
        options["chromium_sandbox"] = True
    return options


def _essayer(action: Callable[[], Any]) -> bool:
    try:
        action()
        return True
    except Exception:  # noqa: BLE001 - élément optionnel absent
        return False


def fermer_cookies(page: Page) -> None:
    """Bannière Didomi : « Continuer sans accepter » (jamais « Accepter » si le refus est possible)."""
    refus = page.get_by_role("button", name=re.compile(r"continuer sans accepter", re.IGNORECASE))
    if _essayer(lambda: refus.first.click(timeout=3000)):
        return
    lien = page.get_by_text(re.compile(r"^\s*continuer sans accepter\s*$", re.IGNORECASE))
    if _essayer(lambda: lien.first.click(timeout=1500)):
        return
    _essayer(lambda: page.locator("#didomi-notice-disagree-button").click(timeout=1000))


def _ouvrir(page: Page, chemin: str) -> None:
    page.goto(url_base() + chemin, wait_until="domcontentloaded")
    fermer_cookies(page)


def _verifier_session(page: Page) -> None:
    """Sur une page /account : « Mes livraisons » ou « Mon Espace » doit apparaître, sinon session expirée."""
    if "/account" not in page.url:
        raise SessionExpiree()
    repere = page.get_by_text(re.compile(r"mes livraisons|mon espace", re.IGNORECASE)).first
    try:
        repere.wait_for(state="visible", timeout=15_000)
    except Exception:  # noqa: BLE001
        raise SessionExpiree() from None


def _lire_livraisons_page(page: Page, interrupteurs: bool = False) -> list[dict[str, Any]]:
    """Livraisons à venir ; avec `interrupteurs`, ouvre aussi le détail de chaque livraison modifiable pour lire
    l'interrupteur « Je veux ma livraison » (`livraison_voulue`, None si illisible). La page finit sur le dernier
    détail ouvert."""
    _ouvrir(page, CHEMIN_LIVRAISONS)
    _verifier_session(page)
    _essayer(lambda: page.get_by_text(re.compile(r"voir le d[ée]tail", re.IGNORECASE)).first.wait_for(timeout=10_000))
    livs = parser_livraisons(page.content())
    if interrupteurs:
        for liv in livs:
            if not (liv.get("id") and liv.get("modifiable")) or liv.get("livraison_voulue") is not None:
                continue
            try:
                _ouvrir_detail(page, liv["id"])
                page.wait_for_timeout(600)  # hydratation : l'état réel arrive après le premier rendu
                liv["livraison_voulue"] = lire_interrupteur_page(page).get("etat")
            except SessionExpiree:
                raise
            except ErreurKitchenDaily as exc:
                log.info("interrupteur de livraison illisible (%s) : %s", liv["id"], exc)
    return livs


def _ouvrir_detail(page: Page, livraison_id: str) -> None:
    """Page de détail d'une livraison (bandeau, interrupteur « Je veux ma livraison », formule)."""
    _ouvrir(page, CHEMIN_DETAIL.format(id=livraison_id))
    _verifier_session(page)
    try:
        page.get_by_text(_RE_JE_VEUX).first.wait_for(timeout=15_000)
    except Exception:  # noqa: BLE001
        raise ErreurKitchenDaily(
            f"Interrupteur « Je veux ma livraison » introuvable sur le détail de la livraison « {livraison_id} » "
            "(identifiant erroné, livraison passée ou site modifié)."
        ) from None
    _essayer(lambda: page.wait_for_load_state("networkidle", timeout=5000))


# Repère l'interrupteur « Je veux ma livraison » : le plus petit élément portant le libellé, puis, en remontant
# ses ancêtres, un interrupteur (role=switch, case à cocher, aria-checked/pressed) ou une paire de boutons Oui/Non.
# Annote l'élément à cliquer (data-kd-interrupteur, ou data-kd-oui / data-kd-non) et son état réel (data-kd-etat).
_JS_INTERRUPTEUR = r"""
() => {
  document.querySelectorAll('[data-kd-interrupteur],[data-kd-oui],[data-kd-non],[data-kd-etat]').forEach(e => {
    ['data-kd-interrupteur', 'data-kd-oui', 'data-kd-non', 'data-kd-etat'].forEach(a => e.removeAttribute(a));
  });
  const re = /je\s+veux\s+ma\s+livraison/i;
  const ancres = Array.from(document.querySelectorAll('body *'))
    .filter(e => re.test(e.textContent || '') && !Array.from(e.children).some(c => re.test(c.textContent || '')));
  const actif = (b) => {
    const a = b.getAttribute('aria-pressed') || b.getAttribute('aria-checked') || b.getAttribute('aria-selected');
    if (a === 'true') return true;
    return Array.from(b.classList).some(c => /(^|[_-])(active|selected|checked|current|on|isActive|isSelected)$/i.test(c));
  };
  for (const ancre of ancres) {
    let el = ancre;
    for (let i = 0; i < 6 && el; i++, el = el.parentElement) {
      const sw = el.querySelector("[role=switch], input[type=checkbox], [aria-checked], [aria-pressed]");
      if (sw) {
        let etat = null;
        if (sw.tagName === 'INPUT') etat = !!sw.checked;
        else {
          const v = sw.getAttribute('aria-checked') || sw.getAttribute('aria-pressed');
          if (v === 'true' || v === 'false') etat = v === 'true';
        }
        sw.setAttribute('data-kd-interrupteur', '1');
        if (etat !== null) sw.setAttribute('data-kd-etat', String(etat));
        return {trouve: true, mode: 'interrupteur', etat: etat};
      }
      const boutons = Array.from(el.querySelectorAll('button, [role=button], label, span'));
      const oui = boutons.find(b => /^\s*oui\s*$/i.test(b.textContent || ''));
      const non = boutons.find(b => /^\s*non\s*$/i.test(b.textContent || ''));
      if (oui && non) {
        oui.setAttribute('data-kd-oui', '1');
        non.setAttribute('data-kd-non', '1');
        const etat = actif(oui) && !actif(non) ? true : (actif(non) && !actif(oui) ? false : null);
        if (etat !== null) oui.setAttribute('data-kd-etat', String(etat));
        return {trouve: true, mode: 'oui_non', etat: etat};
      }
    }
  }
  return {trouve: false, mode: null, etat: null};
}
"""


def _attendre_interrupteur(page: Page, attendu: bool, delai_ms: int) -> bool | None:
    """Relit l'interrupteur jusqu'à ce qu'il vaille `attendu` ou que le délai expire ; renvoie le dernier état lu."""
    fin = datetime.now(TZ).timestamp() + delai_ms / 1000
    etat = lire_interrupteur_page(page).get("etat")
    while etat != attendu and datetime.now(TZ).timestamp() < fin:
        page.wait_for_timeout(250)
        etat = lire_interrupteur_page(page).get("etat")
    return etat


def lire_interrupteur_page(page: Page) -> dict[str, Any]:
    """{trouve, mode (interrupteur | oui_non), etat (True/False/None)} sur la page de détail chargée, sans clic."""
    info = dict(page.evaluate(_JS_INTERRUPTEUR))
    if info.get("etat") is not None:
        info["etat"] = bool(info["etat"])
    return info


# Annote le DOM : index des cartes, valeur réelle des champs quantité, rôle et couleur des boutons.
_JS_ANNOTER = """
(sel) => {
  const cartes = Array.from(document.querySelectorAll(sel))
    .filter(c => !(c.parentElement && c.parentElement.closest(sel)));
  const titres = [];
  cartes.forEach((c, i) => {
    c.setAttribute('data-kd-index', String(i));
    const t = c.querySelector('h1,h2,h3,h4,h5,h6') || c.querySelector("[class*='title' i]");
    titres.push(t ? (t.textContent || '').replace(/\\s+/g, ' ').trim() : '');
    const inp = c.querySelector("input.qty, input[class*='qty' i], input[name*='qty' i], input[type='number']");
    if (inp) inp.setAttribute('data-kd-qty', inp.value);
    c.querySelectorAll('button, [role=button]').forEach(b => {
      const s = getComputedStyle(b);
      b.setAttribute('data-kd-bg', s.backgroundColor + '|' + s.color + '|' + s.opacity);
      const txt = (b.textContent || '').trim();
      const lab = ((b.getAttribute('aria-label') || '') + ' ' + (b.getAttribute('title') || '')).toLowerCase();
      if (txt === '+' || txt === '＋' || /ajout|plus|augment|incr/.test(lab)) b.setAttribute('data-kd-role', 'plus');
      else if (['-', '−', '–', '—'].includes(txt) || /moins|retir|diminu|decr/.test(lab)) b.setAttribute('data-kd-role', 'moins');
    });
    if (inp && !c.querySelector('[data-kd-role=plus]')) {
      const n = inp.nextElementSibling;
      if (n && n.tagName === 'BUTTON') n.setAttribute('data-kd-role', 'plus');
    }
  });
  return titres;
}
"""

# Marque le « Je valide » de la barre du bas : celui dont un ancêtre proche contient « N / M PLATS »
# sans contenir le bandeau « motivation ».
_JS_MARQUER_VALIDER = """
() => {
  document.querySelectorAll('[data-kd-valider]').forEach(e => e.removeAttribute('data-kd-valider'));
  const boutons = Array.from(document.querySelectorAll('button, [role=button], a'))
    .filter(b => /^\\s*je\\s+valide\\s*$/i.test(b.textContent || ''));
  let choix = null;
  for (const b of boutons) {
    let n = b;
    for (let i = 0; i < 7 && n; i++, n = n.parentElement) {
      const t = n.textContent || '';
      if (/motivation/i.test(t)) break;
      if (/\\d+\\s*\\/\\s*\\d+\\s*plats/i.test(t)) { choix = b; break; }
    }
  }
  if (choix) choix.setAttribute('data-kd-valider', '1');
  return {boutons: boutons.length, trouve: !!choix};
}
"""


def _bouton_je_valide_hors_motivation(page: Page) -> Any | None:
    """Le « Je valide » d'étape (barre du bas), jamais celui du bandeau « niveau de motivation »."""
    boutons = page.get_by_role("button", name=re.compile(r"^\s*je\s+valide\s*$", re.IGNORECASE))
    choix = None
    for i in range(boutons.count()):
        b = boutons.nth(i)
        try:
            contexte = b.locator("xpath=ancestor::*[position()<=6]").last.inner_text(timeout=2000)
        except Exception:  # noqa: BLE001
            contexte = ""
        if not re.search(r"motivation", contexte, re.IGNORECASE):
            choix = b
    return choix


def annoter(page: Page) -> list[str]:
    return list(page.evaluate(_JS_ANNOTER, SELECTEUR_CARTE))


def compteur_page(page: Page) -> tuple[int, int] | None:
    return lire_compteur(page.locator("body").inner_text())


def attendre_page_selection(page: Page, livraison_id: str = "") -> None:
    try:
        page.locator(SELECTEUR_CARTE).first.wait_for(state="attached", timeout=TIMEOUT_MS)
        page.get_by_text(re.compile(r"\d+\s*/\s*\d+\s*plats", re.IGNORECASE)).first.wait_for(timeout=TIMEOUT_MS)
    except Exception:  # noqa: BLE001
        if "/account" not in page.url or page.get_by_text(re.compile(r"connectez-vous", re.IGNORECASE)).count():
            raise SessionExpiree() from None
        raise ErreurKitchenDaily(
            f"Page de sélection des plats introuvable pour la livraison « {livraison_id} » "
            "(identifiant erroné, livraison verrouillée ou site modifié)."
        ) from None
    _essayer(lambda: page.wait_for_load_state("networkidle", timeout=8000))


def lire_menu_page(page: Page, livraison_id: str = "") -> dict[str, Any]:
    """Lit le menu sur une page de sélection déjà chargée (aucun clic)."""
    annoter(page)
    return parser_menu(page.content(), livraison_id)


def _ouvrir_selection(page: Page, livraison_id: str) -> None:
    _ouvrir(page, CHEMIN_SELECTION.format(id=livraison_id))
    attendre_page_selection(page, livraison_id)


def _attendre_compteur(page: Page, attendu: int, timeout_ms: int = 5000) -> tuple[int, int] | None:
    fin = datetime.now(TZ).timestamp() + timeout_ms / 1000
    c = compteur_page(page)
    while (c is None or c[0] != attendu) and datetime.now(TZ).timestamp() < fin:
        page.wait_for_timeout(150)
        c = compteur_page(page)
    return c


def _nom_capture(livraison_id: str, suffixe: str) -> Path:
    horodatage = now().strftime("%Y-%m-%d_%H%M%S")
    sur = re.sub(r"[^A-Za-z0-9_-]", "", livraison_id) or "kd"
    return captures_dir() / f"{horodatage}-{sur}-{suffixe}.png"


# --- tunnel : étapes, quantités, validation -------------------------------------


def _bouton_final(page: Page) -> Any:
    return page.get_by_role("button", name=re.compile(r"je\s+valide\s+ma\s+commande", re.IGNORECASE))


def _bouton_etape(page: Page) -> Any | None:
    """« Je valide » d'étape : le bouton `data-testid=confirmOrder` de la barre du bas,
    sinon celui hors du bandeau « motivation »."""
    conf = page.locator('[data-testid="confirmOrder"]')
    try:
        if conf.count() == 1 and re.fullmatch(r"\s*je\s+valide\s*", conf.first.inner_text(timeout=2000), re.IGNORECASE):
            return conf.first
    except Exception:  # noqa: BLE001, S110
        pass  # repli ci-dessous
    return _bouton_je_valide_hors_motivation(page)


def etape_courante(page: Page) -> str | None:
    return detecter_etape(page.content())


def _attendre_changement_etape(page: Page, precedente: str | None) -> str | None:
    """Attend que l'étape détectée change. Étape non détectable (None) : rend la main après 1,5 s."""
    debut = datetime.now(TZ).timestamp()
    page.wait_for_timeout(250)
    while True:
        etape = etape_courante(page)
        ecoule = (datetime.now(TZ).timestamp() - debut) * 1000
        if etape is not None and etape != precedente:
            _essayer(lambda: page.wait_for_load_state("networkidle", timeout=3000))
            page.wait_for_timeout(250)  # rendu des cartes de l'étape
            return etape
        if (etape is None and ecoule >= 1500) or ecoule >= ATTENTE_ETAPE_MS:
            return etape
        page.wait_for_timeout(150)


def _parcourir_tunnel(page: Page, sur_etape: Callable[[str | None], None] | None = None) -> list[str | None]:
    """Depuis l'étape PLATS chargée, clique les « Je valide » d'étape jusqu'à la dernière étape (où « Je valide ma
    commande » est visible) et appelle `sur_etape(etape)` sur chaque étape après PLATS. Ne clique JAMAIS
    « Je valide ma commande » : rien n'est enregistré par ce parcours."""
    marque = page.evaluate(_JS_MARQUER_VALIDER)
    premier = page.locator('[data-kd-valider="1"]').first if marque.get("trouve") else _bouton_etape(page)
    if premier is None:
        raise ErreurKitchenDaily("Bouton « Je valide » de la barre du bas introuvable : rien n'a été enregistré.")
    precedente = etape_courante(page)
    premier.scroll_into_view_if_needed()
    premier.click()
    vues: list[str | None] = []
    for _ in range(ETAPES_TUNNEL_MAX):
        etape = _attendre_changement_etape(page, precedente)
        if etape is not None and etape == precedente:
            raise ErreurKitchenDaily(
                f"Le site reste sur l'étape « {etape} » après « Je valide » "
                f"({'sélection de plats incomplète ?' if etape == 'plats' else 'étape bloquée'}). "
                "Rien n'a été enregistré."
            )
        vues.append(etape)
        if sur_etape is not None:
            sur_etape(etape)
        if _bouton_final(page).count():
            return vues
        suivant = _bouton_etape(page)
        if suivant is None:
            raise ErreurKitchenDaily(
                "Tunnel Kitchen Daily inattendu : ni « Je valide » d'étape ni « Je valide ma commande ». "
                "Rien n'a été enregistré (le site a peut-être changé)."
            )
        precedente = etape
        suivant.scroll_into_view_if_needed()
        suivant.click()
    raise ErreurKitchenDaily("Tunnel Kitchen Daily trop long : « Je valide ma commande » jamais atteint. Rien n'est enregistré.")


def _valider_commande(page: Page) -> str:
    """Clique « Je valide ma commande » (dernière étape) et renvoie la confirmation observée."""
    final = _bouton_final(page)
    if not final.count():
        raise ErreurKitchenDaily("« Je valide ma commande » introuvable : rien n'a été enregistré.")
    enregistrements: list[str] = []

    def _sur_reponse(resp: Any) -> None:
        try:
            if resp.request.method in ("POST", "PUT", "PATCH") and "/orders/" in resp.url and "/bulk" in resp.url:
                enregistrements.append(f"{resp.status} {resp.url}")
        except Exception:  # noqa: BLE001, S110
            pass  # observation seulement : une réponse illisible ne doit pas interrompre la validation

    page.on("response", _sur_reponse)
    url_avant = page.url
    final.first.scroll_into_view_if_needed()
    final.first.click()
    confirmation = "non détectée"
    try:
        page.wait_for_url(lambda u: u != url_avant, timeout=ATTENTE_VALIDATION_MS)
        confirmation = "changement de page"
    except Exception:  # noqa: BLE001
        toast = page.get_by_text(re.compile(r"enregistr|mise à jour|bien été|succ[eè]s", re.IGNORECASE))
        if _essayer(lambda: toast.first.wait_for(timeout=5000)):
            confirmation = "message de confirmation"
    _essayer(lambda: page.wait_for_load_state("networkidle", timeout=8000))
    if enregistrements:
        confirmation += f" ; enregistrement {enregistrements[-1].split(' ')[0]}"
    return confirmation


def lire_etape_page(page: Page) -> dict[str, Any]:
    """Étape d'extras courante (lecture seule)."""
    annoter(page)
    return parser_etape_extras(page.content())


def _barre_page(page: Page) -> tuple[str, int, float | None] | None:
    return lire_barre_extras(re.sub(r"\s+", " ", page.locator("body").inner_text()))


def _qte_carte(page: Page, idx: int) -> int | None:
    annoter(page)  # le DOM peut être re-rendu après chaque clic
    champ = page.locator(f'[data-kd-index="{idx}"]').locator(SELECTEUR_QTY).first
    try:
        return int(champ.input_value(timeout=2000).strip())
    except (ValueError, Exception):  # noqa: BLE001
        return None


def _regler_quantites(page: Page, cibles: dict[str, int], tolerer_absents: bool = False) -> dict[str, list[int]]:
    """Amène chaque produit de l'étape courante à la quantité voulue avec + / −. Renvoie {titre: [avant, après]}."""
    faits: dict[str, list[int]] = {}
    for titre, voulu in cibles.items():
        titres = annoter(page)
        idx = next((i for i, t in enumerate(titres) if normaliser(t) == normaliser(titre)), None)
        if idx is None:
            if tolerer_absents:
                continue
            raise ErreurKitchenDaily(f"« {titre} » introuvable sur l'étape. Rien n'a été enregistré.")
        avant = q = _qte_carte(page, idx)
        if q is None:
            raise ErreurKitchenDaily(f"Quantité de « {titre} » illisible. Rien n'a été enregistré.")
        for _ in range(QUANTITE_EXTRA_MAX * 2):
            if q == voulu:
                break
            role = "plus" if q < voulu else "moins"
            bouton = page.locator(f'[data-kd-index="{idx}"] [data-kd-role="{role}"]').first
            if bouton.count() == 0:
                raise ErreurKitchenDaily(f"Bouton « {'+' if role == 'plus' else '−'} » introuvable pour « {titre} ».")
            bouton.scroll_into_view_if_needed()
            bouton.click()
            fin = datetime.now(TZ).timestamp() + ATTENTE_QTE_MS / 1000
            nouveau = _qte_carte(page, idx)
            while nouveau == q and datetime.now(TZ).timestamp() < fin:
                page.wait_for_timeout(120)
                nouveau = _qte_carte(page, idx)
            if nouveau is None or nouveau == q:
                raise ErreurKitchenDaily(
                    f"Le site refuse de passer « {titre} » de {q} à {voulu} (stock épuisé ou limite ?). "
                    "Rien n'a été enregistré."
                )
            q = nouveau
        if q != voulu:
            raise ErreurKitchenDaily(f"« {titre} » : {q} au lieu de {voulu}. Rien n'a été enregistré.")
        faits[titre] = [avant, q]
    page.wait_for_timeout(200)  # mise à jour de la barre du bas
    return faits


def _lire_extras_tunnel(page: Page, capture: Path | None = None, etape_capture: str | None = None) -> dict[str, dict[str, Any]]:
    """Parcourt le tunnel en lecture seule (aucun « Je valide ma commande ») et lit chaque étape d'extras.

    `capture` : capture d'écran prise sur l'étape `etape_capture`. Laisse la page sur la dernière étape, sans rien
    enregistrer : recharger la page de sélection avant toute autre action."""
    etapes: dict[str, dict[str, Any]] = {}

    def lire(etape: str | None) -> None:
        if etape not in CATEGORIES_EXTRAS:
            return
        lu = lire_etape_page(page)
        lu["etape"] = etape
        lu["libelle"] = LIBELLES_EXTRAS[etape]
        etapes[etape] = lu
        if capture is not None and etape == etape_capture:
            page.screenshot(path=str(capture), full_page=True)

    _parcourir_tunnel(page, lire)
    return etapes


def _verifier_extras(relus: dict[str, dict[str, int]], attendus: dict[str, dict[str, int]], stricte: bool = True) -> str:
    """« conforme » ou « écarts : … ». `stricte=False` ignore les produits disparus de l'étape."""
    ecarts: list[str] = []
    for cat, produits in attendus.items():
        if cat not in relus:
            ecarts.append(f"{LIBELLES_EXTRAS.get(cat, cat)} illisible")
            continue
        index = {normaliser(t): q for t, q in relus[cat].items()}
        for titre, q in produits.items():
            lu = index.get(normaliser(titre))
            if lu is None:
                if stricte:
                    ecarts.append(f"{titre} absent")
            elif lu != q:
                ecarts.append(f"{titre} : {lu} au lieu de {q}")
    return "conforme" if not ecarts else "écarts : " + " ; ".join(ecarts)


def _plats_coches(menu: dict[str, Any]) -> dict[str, int] | None:
    if menu.get("detection_selection") == "indeterminee":
        return None
    avec_q = menu.get("detection_selection") == "quantite"
    return {
        normaliser(p["titre"]): (int(p.get("quantite") or 1) if avec_q else 1)
        for p in menu.get("plats", [])
        if p.get("selectionne")
    }


def appliquer_sur_page(
    page: Page,
    livraison_id: str,
    selection: dict[str, int],
    dry_run: bool,
    recharger: Callable[[], None] | None = None,
    extras_attendus: dict[str, dict[str, int]] | None = None,
) -> dict[str, Any]:
    """Applique une sélection sur une page de sélection déjà chargée.

    « Remise à zéro » → clics sur « + » → vérification du compteur → capture « avant » →
    (si pas dry_run) tunnel : « Je valide » de chaque étape → « Je valide ma commande » → `recharger()` →
    capture « après » → relecture.

    `extras_attendus` ({catégorie: {titre: quantité}}, lus avant toute modification) : à chaque étape d'extras,
    les quantités affichées sont comparées et réappliquées si le site les a perdues (ex. « Remise à zéro » qui
    viderait aussi les extras) ; après l'enregistrement, les extras sont relus (`verification_extras`).
    """
    selection = valider_quantites(selection)
    titres = annoter(page)
    resolue, introuvables, ambigus = resoudre_titres(selection, [t for t in titres if t])
    if introuvables or ambigus:
        details = []
        if introuvables:
            details.append("introuvables : " + ", ".join(f"« {t} »" for t in introuvables))
        if ambigus:
            details.append("ambigus : " + ", ".join(f"« {t} »" for t in ambigus))
        raise ErreurKitchenDaily("Plats absents du menu (" + " ; ".join(details) + ").")
    somme = sum(resolue.values())
    compteur = compteur_page(page)
    formule = compteur[1] if compteur else formule_config()
    if somme != formule:
        raise ErreurKitchenDaily(f"La sélection compte {somme} plats, la formule en demande {formule}.")

    # 1. Remise à zéro (uniquement ici, jamais en lecture)
    page.on("dialog", lambda d: d.accept())
    if not (compteur and compteur[0] == 0):
        page.get_by_role("button", name=re.compile(r"remise\s+[àa]\s+z[ée]ro", re.IGNORECASE)).last.click()
        c = _attendre_compteur(page, 0, 4000)
        if c is None or c[0] != 0:
            # Éventuelle modale de confirmation
            _essayer(
                lambda: page.get_by_role("dialog")
                .get_by_role("button", name=re.compile(r"^\s*(oui|confirmer|valider|ok)\s*$", re.IGNORECASE))
                .first.click(timeout=2000)
            )
            c = _attendre_compteur(page, 0, 4000)
        if c is None or c[0] != 0:
            raise ErreurKitchenDaily(f"« Remise à zéro » sans effet (compteur : {_fmt_compteur(c)}).")

    # 2. Clics sur « + »
    total = 0
    for titre, quantite in resolue.items():
        for _ in range(quantite):
            titres = annoter(page)  # le DOM peut être re-rendu après chaque clic
            idx = next((i for i, t in enumerate(titres) if normaliser(t) == normaliser(titre)), None)
            if idx is None:
                raise ErreurKitchenDaily(f"Carte « {titre} » introuvable pendant l'application.")
            plus = page.locator(f'[data-kd-index="{idx}"] [data-kd-role="plus"]').first
            if plus.count() == 0:
                raise ErreurKitchenDaily(f"Bouton « + » introuvable pour « {titre} ».")
            plus.scroll_into_view_if_needed()
            plus.click()
            total += 1
            c = _attendre_compteur(page, total, 5000)
            if c is None or c[0] != total:
                raise ErreurKitchenDaily(
                    f"Le clic sur « + » pour « {titre} » n'a pas été pris en compte (compteur : {_fmt_compteur(c)})."
                )

    compteur = compteur_page(page)
    avant = _nom_capture(livraison_id, "avant")
    page.screenshot(path=str(avant), full_page=True)
    resultat: dict[str, Any] = {
        "ok": bool(compteur and compteur[0] == somme),
        "dry_run": dry_run,
        "livraison_id": livraison_id,
        "compteur": _fmt_compteur(compteur),
        "selection_appliquee": resolue,
        "captures": {"avant": str(avant)},
    }
    if not resultat["ok"]:
        raise ErreurKitchenDaily(f"Compteur inattendu après sélection : {_fmt_compteur(compteur)} au lieu de {somme}.")
    if dry_run:
        return resultat

    # 3. Le tunnel du site : « Je valide » (barre du bas) sur PLATS → DESSERTS → ENTRÉES → JUS, sans rien
    #    changer aux extras (sauf pour rétablir `extras_attendus`), puis « Je valide ma commande » qui seul
    #    enregistre (POST …/orders/<id>/bulk). Observé : « Je valide » change d'étape (même URL).
    reappliques: dict[str, dict[str, int]] = {}

    def retablir_extras(etape: str | None) -> None:
        if not extras_attendus or etape not in extras_attendus:
            return
        lus = quantites_extras({etape: lire_etape_page(page)})[etape]
        index = {normaliser(t): q for t, q in lus.items()}
        ecarts = {
            t: q for t, q in extras_attendus[etape].items() if normaliser(t) in index and index[normaliser(t)] != q
        }
        if ecarts:
            _regler_quantites(page, ecarts, tolerer_absents=True)
            reappliques[etape] = ecarts

    _parcourir_tunnel(page, retablir_extras)
    resultat["confirmation"] = _valider_commande(page)
    if reappliques:
        resultat["extras_reappliques"] = reappliques

    # 4. Relecture et capture « après »
    if recharger is not None:
        recharger()
    menu = lire_menu_page(page, livraison_id)
    apres = _nom_capture(livraison_id, "apres")
    page.screenshot(path=str(apres), full_page=True)
    resultat["captures"]["apres"] = str(apres)
    relu = menu.get("compteur") or {}
    resultat["compteur"] = f"{relu.get('selectionnes')} / {relu.get('formule')}" if relu else "illisible"
    resultat["verification_plats"] = _verifier_plats(menu, resolue)
    # Le compteur « 18 / 18 » ne prouve rien (la sélection par défaut l'affiche aussi) :
    # l'enregistrement n'est réussi que si les plats relus sont ceux demandés, avec les bonnes quantités.
    resultat["ok"] = bool(
        relu
        and relu.get("selectionnes") == somme
        and resultat["verification_plats"].startswith(("conforme", "non vérifiable"))
    )
    if extras_attendus is not None and recharger is not None:
        try:
            relus = quantites_extras(_lire_extras_tunnel(page))
            resultat["verification_extras"] = _verifier_extras(relus, extras_attendus, stricte=False)
        except ErreurKitchenDaily as exc:
            resultat["verification_extras"] = f"non vérifiable ({exc})"
    return resultat


def _fmt_compteur(c: tuple[int, int] | None) -> str:
    return f"{c[0]} / {c[1]}" if c else "illisible"


def _verifier_plats(menu: dict[str, Any], resolue: dict[str, int]) -> str:
    if menu.get("detection_selection") == "indeterminee":
        return "non vérifiable (état des cartes illisible), compteur seul"
    # Les quantités ne sont comparées que si le site les expose (détection « quantite »).
    avec_quantites = menu.get("detection_selection") == "quantite"
    coches = {
        normaliser(p["titre"]): (int(p.get("quantite") or 1) if avec_quantites else 1)
        for p in menu["plats"]
        if p.get("selectionne")
    }
    voulus = {normaliser(t): (int(q) if avec_quantites else 1) for t, q in resolue.items()}
    if coches == voulus:
        return "conforme"
    manquants = set(voulus) - set(coches)
    en_trop = set(coches) - set(voulus)
    quantites = [t for t in set(voulus) & set(coches) if voulus[t] != coches[t]]
    if not manquants and not en_trop and quantites:
        return f"écarts : {len(quantites)} quantité(s) différente(s)"
    return f"écarts : {len(manquants)} manquant(s), {len(en_trop)} en trop"


# --- extras et interrupteur sur la page ------------------------------------------


def appliquer_extras_sur_page(
    page: Page,
    livraison_id: str,
    demandes: dict[str, dict[str, int]],
    dry_run: bool,
    recharger: Callable[[], None] | None = None,
    supplement_max: float | None = None,
) -> dict[str, Any]:
    """Règle les quantités d'extras demandées sur une page de sélection chargée (étape PLATS), sans toucher aux plats.

    `demandes` : {catégorie: {titre: quantité}} (titres comparés sans accents ni majuscules ; produits non cités
    inchangés ; 0 retire). Tunnel : « Je valide » des étapes, + / − sur les étapes concernées, capture « avant »,
    puis (sauf dry_run) « Je valide ma commande », `recharger()`, relecture des plats (inchangés ?) et des extras.
    `supplement_max` (ex. 0 pour un dessert offert) : si le montant affiché des étapes augmente davantage, ou n'est
    pas lisible, lève `SupplementNonGratuit` AVANT « Je valide ma commande » (rien n'est enregistré).
    """
    demandes = valider_extras(demandes)
    plats_avant = _plats_coches(lire_menu_page(page, livraison_id))
    reglages: dict[str, dict[str, list[int]]] = {}
    resolues: dict[str, dict[str, int]] = {}
    supplement = 0.0
    montants_lisibles = True

    def regler(etape: str | None) -> None:
        nonlocal supplement, montants_lisibles
        if etape not in demandes:
            return
        lu = lire_etape_page(page)
        titres = [p["titre"] for p in lu["produits"] if p.get("titre")]
        cibles: dict[str, int] = {}
        erreurs = []
        for demande, q in demandes[etape].items():
            titre, erreur = _resoudre_un(demande, titres)
            if titre is None:
                erreurs.append(str(erreur))
            else:
                cibles[titre] = q
        if erreurs:
            raise ErreurKitchenDaily(
                f"{LIBELLES_EXTRAS[etape]} : " + ", ".join(erreurs) + " — disponibles : " + " ; ".join(titres)
                + ". Rien n'a été enregistré."
            )
        barre_avant = _barre_page(page)
        reglages[etape] = _regler_quantites(page, cibles)
        resolues[etape] = cibles
        barre_apres = _barre_page(page)
        if barre_avant and barre_apres and barre_avant[2] is not None and barre_apres[2] is not None:
            supplement += barre_apres[2] - barre_avant[2]
        else:
            montants_lisibles = False

    _parcourir_tunnel(page, regler)
    manquantes = [LIBELLES_EXTRAS[c] for c in demandes if c not in reglages]
    if manquantes:
        raise ErreurKitchenDaily(
            "Étape(s) non trouvée(s) dans le tunnel : " + ", ".join(manquantes) + ". Rien n'a été enregistré."
        )
    supplement = round(supplement, 2)
    if supplement_max is not None and (not montants_lisibles or supplement > supplement_max + 0.005):
        raise SupplementNonGratuit(
            "Le montant affiché par le site augmente "
            + (f"de {_fmt_euros(supplement)}" if montants_lisibles else "(montant illisible)")
            + " : ce n'est pas un extra offert. Rien n'a été enregistré."
        )
    avant = _nom_capture(livraison_id, "extras-avant")
    page.screenshot(path=str(avant), full_page=True)
    resultat: dict[str, Any] = {
        "ok": True,
        "dry_run": dry_run,
        "livraison_id": livraison_id,
        "extras_appliques": resolues,
        "reglages": reglages,
        "supplement_affiche": supplement if montants_lisibles else None,
        "captures": {"avant": str(avant)},
    }
    if dry_run:
        return resultat

    resultat["confirmation"] = _valider_commande(page)
    if recharger is None:
        resultat["verification_extras"] = "non vérifiée (pas de relecture)"
        return resultat
    recharger()
    plats_apres = _plats_coches(lire_menu_page(page, livraison_id))
    if plats_avant is None or plats_apres is None:
        resultat["verification_plats"] = "non vérifiable (état des cartes illisible)"
    else:
        resultat["verification_plats"] = "inchangés" if plats_avant == plats_apres else "MODIFIÉS"
    apres = _nom_capture(livraison_id, "extras-apres")
    relus = _lire_extras_tunnel(page, capture=apres, etape_capture=next(iter(resolues)))
    if apres.exists():
        resultat["captures"]["apres"] = str(apres)
    resultat["extras_relus"] = {cat: {t: q for t, q in v.items() if q} for cat, v in quantites_extras(relus).items()}
    resultat["verification_extras"] = _verifier_extras(quantites_extras(relus), resolues)
    resultat["ok"] = resultat["verification_extras"] == "conforme" and resultat["verification_plats"] != "MODIFIÉS"
    return resultat


_RE_CONFIRMER = re.compile(r"^\s*(oui|confirmer|je\s+confirme|valider|je\s+valide|ok|continuer|d['’]accord)\b", re.IGNORECASE)


def _confirmer_modale(page: Page) -> bool:
    """Clique le bouton de confirmation d'une modale visible (hors bannière cookies et bandeau motivation)."""
    for selecteur in ("[role=dialog]", "[role=alertdialog]", "[class*='modal' i]", "[class*='popin' i]", "[class*='popup' i]"):
        conteneurs = page.locator(selecteur)
        for i in range(conteneurs.count()):
            c = conteneurs.nth(i)
            try:
                if not c.is_visible() or c.locator("xpath=ancestor-or-self::*[@id='didomi-host']").count():
                    continue
                if re.search(r"motivation|cookies", c.inner_text(timeout=1000), re.IGNORECASE):
                    continue
            except Exception as exc:  # noqa: BLE001
                log.debug("modale ignorée (%s)", exc.__class__.__name__)
                continue
            bouton = c.get_by_role("button", name=_RE_CONFIRMER)
            if bouton.count() and _essayer(lambda b=bouton: b.first.click(timeout=2000)):
                return True
    return False


def _basculer_interrupteur(page: Page, voulue: bool, info: dict[str, Any]) -> None:
    """Un seul clic effectif : l'interrupteur, sinon son <label>, sinon son parent, sinon un clic synthétique."""
    if info.get("mode") == "oui_non":
        page.locator('[data-kd-oui="1"]' if voulue else '[data-kd-non="1"]').first.click()
        return
    sw = page.locator('[data-kd-interrupteur="1"]').first
    if _essayer(lambda: sw.click(timeout=3000)):
        return
    label = sw.locator("xpath=ancestor::label[1]")
    if label.count() and _essayer(lambda: label.first.click(timeout=3000)):
        return
    if _essayer(lambda: sw.locator("xpath=..").click(timeout=3000)):
        return
    sw.dispatch_event("click")


def definir_livraison_sur_page(
    page: Page,
    livraison_id: str,
    voulue: bool,
    dry_run: bool,
    recharger: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Bascule l'interrupteur « Je veux ma livraison » (page de détail chargée), confirme une éventuelle modale,
    puis relit l'état (après `recharger()` si fourni). Ne clique rien si l'état est déjà le bon ou en dry_run."""
    info = lire_interrupteur_page(page)
    if not info.get("trouve") or info.get("etat") is None:
        raise ErreurKitchenDaily(
            "Interrupteur « Je veux ma livraison » introuvable ou illisible : rien n'a été modifié (le site a peut-être changé)."
        )
    resultat: dict[str, Any] = {
        "ok": True,
        "dry_run": dry_run,
        "livraison_id": livraison_id,
        "avant": info["etat"],
        "voulue": voulue,
        "apres": info["etat"],
        "captures": {},
    }
    if info["etat"] == voulue:
        resultat["deja"] = True
        return resultat
    avant = _nom_capture(livraison_id, "livraison-avant")
    page.screenshot(path=str(avant), full_page=True)
    resultat["captures"]["avant"] = str(avant)
    if dry_run:
        return resultat

    page.on("dialog", lambda d: d.accept())
    _basculer_interrupteur(page, voulue, info)
    page.wait_for_timeout(800)
    resultat["modale_confirmee"] = _confirmer_modale(page)
    fin = datetime.now(TZ).timestamp() + 5
    etat = lire_interrupteur_page(page).get("etat")
    while etat != voulue and datetime.now(TZ).timestamp() < fin:
        page.wait_for_timeout(200)
        etat = lire_interrupteur_page(page).get("etat")
    _essayer(lambda: page.wait_for_load_state("networkidle", timeout=5000))
    if recharger is not None:
        recharger()
        # Page Next.js : l'interrupteur est d'abord rendu avec l'état par défaut, puis mis à jour après
        # l'hydratation (observé : relecture trop précoce → « relu à oui » alors que le site
        # avait bien enregistré l'annulation). On attend que l'état se stabilise, 10 s au plus.
        etat = _attendre_interrupteur(page, voulue, 10_000)
        resultat["relu_apres_rechargement"] = True
    apres = _nom_capture(livraison_id, "livraison-apres")
    page.screenshot(path=str(apres), full_page=True)
    resultat["captures"]["apres"] = str(apres)
    resultat["apres"] = etat
    resultat["ok"] = etat == voulue
    return resultat


# --- API publique (une session navigateur par appel) ---------------------------


@_hors_boucle_asyncio
def session_valide() -> bool:
    try:
        with navigateur() as page:
            _ouvrir(page, CHEMIN_LIVRAISONS)
            _verifier_session(page)
        return True
    except SessionExpiree:
        return False


@_hors_boucle_asyncio
def livraisons(interrupteurs: bool = True) -> list[dict[str, Any]]:
    """Livraisons à venir ; `livraison_voulue` lu sur le détail de chaque livraison modifiable."""
    with navigateur() as page:
        return _lire_livraisons_page(page, interrupteurs=interrupteurs)


@_hors_boucle_asyncio
def menu(livraison_id: str) -> dict[str, Any]:
    with navigateur() as page:
        _ouvrir_selection(page, livraison_id)
        return lire_menu_page(page, livraison_id)


def _trouver_livraison(livs: list[dict[str, Any]], livraison_id: str) -> dict[str, Any] | None:
    return next((liv for liv in livs if liv.get("id") == livraison_id), None)


@_hors_boucle_asyncio
def menu_avec_limite(livraison_id: str) -> dict[str, Any]:
    """Menu + date limite de modification (une seule session navigateur)."""
    with navigateur() as page:
        livs = _lire_livraisons_page(page)
        liv = _trouver_livraison(livs, livraison_id)
        _ouvrir_selection(page, livraison_id)
        m = lire_menu_page(page, livraison_id)
    m["livraison"] = liv
    m["date_limite"] = liv.get("modifiable_jusqua") if liv else None
    m["modifiable"] = liv.get("modifiable") if liv else None
    return m


@_hors_boucle_asyncio
def appliquer_selection(livraison_id: str, selection: dict[str, int], dry_run: bool = False) -> dict[str, Any]:
    """Enregistre les plats. Avant (hors dry_run), les extras sont relus par un parcours du tunnel sans validation,
    pour être conservés tels quels (voir `appliquer_sur_page`, `extras_attendus`)."""
    with navigateur() as page:
        _ouvrir_selection(page, livraison_id)

        def recharger() -> None:
            _ouvrir_selection(page, livraison_id)

        extras_avant: dict[str, dict[str, int]] | None = None
        if not dry_run:
            try:
                extras_avant = quantites_extras(_lire_extras_tunnel(page)) or None
            except ErreurKitchenDaily as exc:
                log.info("extras non relus avant l'enregistrement des plats : %s", exc)
            recharger()  # rien n'a été enregistré par la lecture : on repart de la page fraîche
        return appliquer_sur_page(page, livraison_id, selection, dry_run, recharger, extras_attendus=extras_avant)


def _liv_modifiable(livs: list[dict[str, Any]], livraison_id: str) -> dict[str, Any]:
    liv = _trouver_livraison(livs, livraison_id)
    if liv is None:
        raise ErreurKitchenDaily(
            f"Livraison « {livraison_id} » introuvable parmi les livraisons à venir (utilise kitchen_daily_livraisons)."
        )
    if not liv.get("modifiable"):
        raise ErreurKitchenDaily(
            f"La livraison du {_jj_mm(liv.get('date'))} n'est plus modifiable "
            f"(limite : {liv.get('modifiable_jusqua') or 'inconnue'})."
        )
    return liv


def _synthese_extras(etapes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    choisis = {cat: {t: q for t, q in v.items() if q} for cat, v in quantites_extras(etapes).items()}
    offerts = [
        p["titre"] for e in etapes.values() for p in e.get("produits", []) if p.get("offert_possible") and p.get("titre")
    ]
    pris = [t for cat, v in choisis.items() for t in v if t in offerts]
    return {
        "selection": choisis,
        "dessert_offert": {
            "eligibles": offerts,
            "deja_choisi": pris[0] if pris else None,
            "regle": "1 dessert offert par livraison parmi les produits « offert_possible » ; les suivants sont payants.",
        },
        "montant_estime": _cout_extras(etapes, quantites_extras(etapes)),
    }


@_hors_boucle_asyncio
def extras(livraison_id: str) -> dict[str, Any]:
    """Extras d'une livraison (desserts, entrées-soupes-apéros, jus), lus en parcourant le tunnel sans valider."""
    with navigateur() as page:
        livs = _lire_livraisons_page(page)
        liv = _trouver_livraison(livs, livraison_id)
        _ouvrir_selection(page, livraison_id)
        etapes = _lire_extras_tunnel(page)
    resultat: dict[str, Any] = {
        "livraison_id": livraison_id,
        "livraison_date": liv.get("date") if liv else None,
        "modifiable": liv.get("modifiable") if liv else None,
        "date_limite": liv.get("modifiable_jusqua") if liv else None,
        **{cat: etapes.get(cat) for cat in CATEGORIES_EXTRAS},
        **_synthese_extras(etapes),
    }
    absentes = [LIBELLES_EXTRAS[c] for c in CATEGORIES_EXTRAS if c not in etapes]
    if absentes:
        resultat["avertissement"] = "Étape(s) non lue(s) : " + ", ".join(absentes) + "."
    return resultat


@_hors_boucle_asyncio
def appliquer_extras(
    livraison_id: str, demandes: dict[str, dict[str, int]], dry_run: bool = False, supplement_max: float | None = None
) -> dict[str, Any]:
    with navigateur() as page:
        _ouvrir_selection(page, livraison_id)

        def recharger() -> None:
            _ouvrir_selection(page, livraison_id)

        return appliquer_extras_sur_page(page, livraison_id, demandes, dry_run, recharger, supplement_max)


@_hors_boucle_asyncio
def definir_extras(livraison_id: str, demandes: dict[str, Any]) -> str:
    """Outil `kitchen_daily_extras_definir` : relit les extras, calcule les changements, puis
    - dessert offert seul (aucun retrait, montant inchangé) : l'enregistre aussitôt, sans confirmation ;
    - sinon : action `pending` (kind `kitchen_daily_extras_definir`) → `CONFIRMATION_REQUISE:…`.
    """
    from tools import db

    propres = valider_extras(demandes)
    with navigateur() as page:
        liv = _liv_modifiable(_lire_livraisons_page(page), livraison_id)
        _ouvrir_selection(page, livraison_id)
        etapes = _lire_extras_tunnel(page)
        cibles, changements = resoudre_extras(propres, etapes)
        if not changements:
            return f"Rien à changer : les extras de la livraison du {_jj_mm(liv.get('date'))} sont déjà ainsi."
        estimation = estimer_extras(etapes, changements)
        if estimation["gratuit"]:
            _ouvrir_selection(page, livraison_id)

            def recharger() -> None:
                _ouvrir_selection(page, livraison_id)

            try:
                res = appliquer_extras_sur_page(page, livraison_id, cibles, False, recharger, supplement_max=0.0)
            except SupplementNonGratuit as exc:
                log.info("dessert offert présumé mais montant en hausse : confirmation demandée (%s)", exc)
                estimation["gratuit"] = False
            else:
                if not res.get("ok"):
                    raise ErreurKitchenDaily(
                        f"Le site n'a pas gardé l'extra offert ({res.get('verification_extras')}). Rien n'est garanti : "
                        "vérifie sur le site."
                    )
                return (
                    f"Dessert offert enregistré pour la livraison du {_jj_mm(liv.get('date'))} (sans frais, "
                    f"vérifié sur le site) : {resume_changements(changements)}."
                )
    return _enregistrer_extras(liv, cibles, changements, estimation, db.log_action)


def _enregistrer_extras(
    liv: dict[str, Any],
    cibles: dict[str, dict[str, int]],
    changements: list[dict[str, Any]],
    estimation: dict[str, Any],
    log_action: Callable[..., int],
) -> str:
    supplement = estimation.get("supplement_estime")
    resume = f"Extras du {_jj_mm(liv.get('date'))} : {resume_changements(changements)}"
    resume += f" ({_fmt_euros(supplement, signe=True)} estimé)" if supplement is not None else " (montant inconnu)"
    if len(resume) > 280:
        resume = resume[:277] + "…"
    payload = {
        "livraison_id": liv["id"],
        "livraison_date": liv.get("date"),
        "extras": cibles,
        "changements": changements,
        "supplement_estime": supplement,
    }
    aid = log_action("kitchen_daily_extras_definir", resume, payload, status="pending")
    return f"CONFIRMATION_REQUISE:{aid}:{resume}"


def executer_extras(payload: dict[str, Any]) -> str:
    """Exécuteur du runner pour `kitchen_daily_extras_definir` (après « Valider »)."""
    livraison_id = str(payload.get("livraison_id") or "")
    cibles = payload.get("extras") or {}
    if not livraison_id or not cibles:
        raise ErreurKitchenDaily("Action incomplète : livraison ou extras manquants.")
    res = appliquer_extras(livraison_id, cibles, dry_run=False)
    capture = res.get("captures", {}).get("apres") or res.get("captures", {}).get("avant")
    if not res.get("ok"):
        raise ErreurKitchenDaily(
            "Le site n'a PAS gardé les extras : à la relecture, "
            f"{res.get('verification_extras', '?')} (plats : {res.get('verification_plats', '?')}). "
            f"Capture : {capture}"
        )
    lignes = [
        (
            f"Extras Kitchen Daily enregistrés pour la livraison du {_jj_mm(payload.get('livraison_date'))} "
            f"(vérification : {res.get('verification_extras')}, plats {res.get('verification_plats', '?')})."
        )
    ]
    for cat, produits in (res.get("extras_relus") or {}).items():
        if produits:
            lignes.append(
                f"• {LIBELLES_EXTRAS.get(cat, cat)} : " + ", ".join(f"{t} ×{q}" if q > 1 else t for t, q in produits.items())
            )
    if res.get("supplement_affiche") is not None:
        lignes.append(f"Montant des étapes modifiées : {_fmt_euros(res['supplement_affiche'], signe=True)}.")
    texte = "\n".join(lignes)
    if res.get("captures", {}).get("apres"):
        texte += f"\nCAPTURE:{res['captures']['apres']}"
    return texte


@_hors_boucle_asyncio
def definir_livraison(livraison_id: str, voulue: bool, dry_run: bool = False) -> dict[str, Any]:
    """Bascule « Je veux ma livraison » sur le détail de la livraison et relit l'état après rechargement."""
    with navigateur() as page:
        _ouvrir_detail(page, livraison_id)

        def recharger() -> None:
            _ouvrir_detail(page, livraison_id)

        return definir_livraison_sur_page(page, livraison_id, voulue, dry_run, recharger)


@_hors_boucle_asyncio
def proposer_livraison(livraison_id: str, voulue: bool) -> str:
    """Outil `kitchen_daily_livraison_definir` : vérifie l'état actuel puis crée l'action `pending` (toujours à confirmer)."""
    from tools import db

    if not isinstance(voulue, bool):
        raise ErreurKitchenDaily("`voulue` doit valoir true (livraison voulue) ou false (sauter la semaine).")
    with navigateur() as page:
        liv = _liv_modifiable(_lire_livraisons_page(page), livraison_id)
        _ouvrir_detail(page, livraison_id)
        etat = lire_interrupteur_page(page).get("etat")
    return _enregistrer_livraison(liv, voulue, etat, db.log_action)


def _enregistrer_livraison(
    liv: dict[str, Any], voulue: bool, etat: bool | None, log_action: Callable[..., int]
) -> str:
    jour = f"{(liv.get('jour') or '').lower()} {_jj_mm(liv.get('date'))}".strip()
    if etat is None:
        raise ErreurKitchenDaily(
            f"Je ne sais pas lire l'interrupteur « Je veux ma livraison » de la livraison du {jour} : rien n'est préparé."
        )
    if etat == voulue:
        return f"Rien à faire : la livraison du {jour} est déjà {'prévue' if voulue else 'désactivée (semaine sautée)'}."
    resume = f"Réactiver la livraison du {jour}" if voulue else f"Pas de livraison le {jour} (semaine sautée)"
    payload = {"livraison_id": liv["id"], "livraison_date": liv.get("date"), "voulue": voulue}
    aid = log_action("kitchen_daily_livraison_definir", resume, payload, status="pending")
    return f"CONFIRMATION_REQUISE:{aid}:{resume}"


def executer_livraison(payload: dict[str, Any]) -> str:
    """Exécuteur du runner pour `kitchen_daily_livraison_definir` (après « Valider »)."""
    livraison_id = str(payload.get("livraison_id") or "")
    voulue = payload.get("voulue")
    if not livraison_id or not isinstance(voulue, bool):
        raise ErreurKitchenDaily("Action incomplète : livraison ou état voulu manquant.")
    res = definir_livraison(livraison_id, voulue, dry_run=False)
    date_txt = _jj_mm(payload.get("livraison_date"))
    if not res.get("ok"):
        raise ErreurKitchenDaily(
            f"Le site n'a PAS gardé le changement : l'interrupteur de la livraison du {date_txt} est relu à "
            f"« {'oui' if res.get('apres') else 'non' if res.get('apres') is False else 'illisible'} ». "
            f"Capture : {res.get('captures', {}).get('apres')}"
        )
    if res.get("deja"):
        return f"La livraison du {date_txt} était déjà {'prévue' if voulue else 'désactivée'} : rien n'a changé."
    texte = (
        f"Livraison du {date_txt} réactivée (relu sur le site)." if voulue
        else f"Pas de livraison le {date_txt} : semaine sautée (relu sur le site)."
    )
    if res.get("captures", {}).get("apres"):
        texte += f"\nCAPTURE:{res['captures']['apres']}"
    return texte


@_hors_boucle_asyncio
def donnees_rappel() -> dict[str, Any]:
    """Pour `runner rappel kitchen-daily` : livraisons réelles + menu de la prochaine livraison modifiable."""
    with navigateur() as page:
        livs = _lire_livraisons_page(page, interrupteurs=True)
        cible = prochaine_modifiable(livs)
        m = None
        if cible is not None:
            _ouvrir_selection(page, cible["id"])
            m = lire_menu_page(page, cible["id"])
    return {
        "livraisons": livs,
        "livraison_cible": cible,
        "menu": m,
        "date_limite": cible.get("modifiable_jusqua") if cible else None,
    }


# --- préférences, historique, proposition, exécution ---------------------------


def preferences() -> str:
    """Concatène `data/alimentation/*.md` (historique en dernier, 80 dernières lignes)."""
    dossier = data_dir() / "alimentation"
    if not dossier.exists():
        return "Aucune préférence alimentaire enregistrée (data/alimentation/ est vide)."
    morceaux: list[str] = []
    for fichier in sorted(p for p in dossier.glob("*.md") if p.name != "historique.md"):
        try:
            morceaux.append(f"## alimentation/{fichier.name}\n{fichier.read_text(encoding='utf-8').strip()}")
        except OSError:
            continue
    historique = dossier / "historique.md"
    if historique.exists():
        lignes = historique.read_text(encoding="utf-8").strip().splitlines()
        morceaux.append("## alimentation/historique.md (dernières lignes)\n" + "\n".join(lignes[-80:]))
    return "\n\n".join(morceaux) or "Aucune préférence alimentaire enregistrée."


def historique_ajouter(livraison_date: str, plats: list[str], notes: str | None = None) -> str:
    from tools import memoire

    if not plats:
        raise ErreurKitchenDaily("Liste de plats vide : rien à ajouter à l'historique.")
    jour = parse_date(livraison_date).strftime("%d/%m/%Y")
    ligne = f"- {jour} — Kitchen Daily : " + " ; ".join(str(p).strip() for p in plats if str(p).strip())
    if notes and notes.strip():
        ligne += f" — {notes.strip()}"
    return memoire.ajouter("alimentation/historique.md", ligne)


def _jj_mm(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        return parse_date(iso).strftime("%d/%m")
    except ValueError:
        return str(iso)


@_hors_boucle_asyncio
def proposer(livraison_id: str, selection: dict[str, Any], justification: str) -> str:
    """Vérifie la sélection contre le menu réel puis crée l'action `pending` (kind kitchen_daily_selection)."""
    from tools import db

    propre = valider_quantites(selection)
    with navigateur() as page:
        livs = _lire_livraisons_page(page)
        liv = _trouver_livraison(livs, livraison_id)
        if liv is None:
            raise ErreurKitchenDaily(
                f"Livraison « {livraison_id} » introuvable parmi les livraisons à venir (utilise kitchen_daily_livraisons)."
            )
        if not liv.get("modifiable"):
            raise ErreurKitchenDaily(
                f"La livraison du {_jj_mm(liv.get('date'))} n'est plus modifiable "
                f"(limite : {liv.get('modifiable_jusqua') or 'inconnue'})."
            )
        _ouvrir_selection(page, livraison_id)
        m = lire_menu_page(page, livraison_id)
    return _enregistrer_proposition(liv, m, propre, justification, db.log_action)


def _enregistrer_proposition(
    liv: dict[str, Any],
    m: dict[str, Any],
    propre: dict[str, int],
    justification: str,
    log_action: Callable[..., int],
) -> str:
    formule = int(m.get("formule_plats") or formule_config())
    somme = sum(propre.values())
    if somme != formule:
        raise ErreurKitchenDaily(f"La sélection compte {somme} plats ; la formule en demande exactement {formule}.")
    resolue, introuvables, ambigus = resoudre_titres(propre, [p["titre"] for p in m["plats"] if p.get("titre")])
    if introuvables or ambigus:
        msg = []
        if introuvables:
            msg.append("absents du menu : " + ", ".join(f"« {t} »" for t in introuvables))
        if ambigus:
            msg.append(
                "ambigus : "
                + ", ".join(f"« {t} » ({' / '.join(c)})" for t, c in ambigus.items())
            )
        raise ErreurKitchenDaily("Titres à corriger — " + " ; ".join(msg) + ". Reprends les titres exacts du menu.")
    resume = f"{somme} plats pour la livraison du {_jj_mm(liv.get('date'))}"
    payload = {
        "livraison_id": liv["id"],
        "livraison_date": liv.get("date"),
        "selection": resolue,
        "justification": str(justification or "")[:1000],
    }
    aid = log_action("kitchen_daily_selection", resume, payload, status="pending")
    return f"CONFIRMATION_REQUISE:{aid}:{resume}"


def executer_selection(payload: dict[str, Any]) -> str:
    """Exécuteur du runner pour `kitchen_daily_selection` (après « Valider » dans Telegram)."""
    livraison_id = str(payload.get("livraison_id") or "")
    selection = payload.get("selection") or {}
    if not livraison_id or not selection:
        raise ErreurKitchenDaily("Action incomplète : livraison ou sélection manquante.")
    res = appliquer_selection(livraison_id, selection, dry_run=False)
    if not res.get("ok"):
        raise ErreurKitchenDaily(
            "Le site n'a PAS gardé la sélection : à la relecture, "
            f"{res.get('verification_plats', '?')} (compteur relu : {res.get('compteur')}). "
            "Rien n'est enregistré. "
            f"Capture : {res.get('captures', {}).get('apres') or res.get('captures', {}).get('avant')}"
        )
    plats = [f"{t} ×{q}" if q > 1 else t for t, q in res["selection_appliquee"].items()]
    note_hist = ""
    try:
        historique_ajouter(str(payload.get("livraison_date") or now().date().isoformat()), plats, "sélection validée")
    except Exception as exc:  # noqa: BLE001
        log.warning("historique non mis à jour : %s", exc.__class__.__name__)
        note_hist = "\n(historique non mis à jour)"
    entete = (
        f"Sélection Kitchen Daily enregistrée pour la livraison du {_jj_mm(payload.get('livraison_date'))} "
        f"(compteur {res['compteur']}, vérification : {res.get('verification_plats', '?')})."
    )
    lignes = [entete, *[f"• {p}" for p in plats]]
    verif_extras = str(res.get("verification_extras") or "")
    if verif_extras.startswith("écarts"):
        lignes.append(f"Attention, extras (desserts, entrées, jus) différents d'avant : {verif_extras}.")
    texte = "\n".join(lignes) + note_hist
    apres = res.get("captures", {}).get("apres")
    if apres:
        texte += f"\nCAPTURE:{apres}"
    return texte


# --- login ---------------------------------------------------------------------


def _expurger(message: str, *secrets: str) -> str:
    for s in secrets:
        if s:
            message = message.replace(s, "***")
    return message


def login(email: str, mot_de_passe: str) -> None:
    """Connexion interactive (appelée par la CLI uniquement) ; seul l'état de session est sauvegardé."""
    with navigateur(avec_session=False) as page:
        try:
            page.goto(url_base() + "/", wait_until="domcontentloaded")
            fermer_cookies(page)
            compte = page.get_by_role("button", name=re.compile(r"^\s*compte\s*$", re.IGNORECASE))
            if not _essayer(lambda: compte.first.click(timeout=5000)):
                lien = page.get_by_role("link", name=re.compile(r"^\s*compte\s*$", re.IGNORECASE))
                if not _essayer(lambda: lien.first.click(timeout=3000)):
                    page.get_by_text("Compte", exact=True).first.click()
            # On s'ancre sur le champ mot de passe de la modale (visible), puis on remonte au formulaire :
            # la page d'accueil a aussi un champ e-mail (newsletter) qu'il ne faut pas remplir.
            champ_mdp = page.locator("input[type='password']:visible")
            champ_mdp.first.wait_for(state="visible", timeout=TIMEOUT_MS)
            formulaire = champ_mdp.first.locator("xpath=ancestor::form[1]")
            if formulaire.count() == 0:
                formulaire = champ_mdp.first.locator("xpath=ancestor::*[.//input[@type='password']][3]")
            champ_mail = formulaire.locator("input[type='email'], input[name*='mail' i], input[type='text']").first
            champ_mail.click()
            champ_mail.fill(email)
            champ_mdp.first.click()
            champ_mdp.first.fill(mot_de_passe)
            bouton = formulaire.get_by_role("button", name=re.compile(r"connexion", re.IGNORECASE))
            if bouton.count() == 0:
                bouton = page.get_by_role("button", name=re.compile(r"^\s*connexion\s*$", re.IGNORECASE))
            bouton.first.click()
            try:
                # La modale disparaît quand la connexion est acceptée ; « Mon Espace » n'est qu'une icône
                # (texte masqué), donc on vérifie l'espace client lui-même : « Mes livraisons » y est visible.
                champ_mdp.first.wait_for(state="hidden", timeout=TIMEOUT_MS)
                page.goto(url_base() + "/account/shippings/upcoming", wait_until="domcontentloaded")
                page.get_by_text(re.compile(r"mes livraisons", re.IGNORECASE)).first.wait_for(state="visible", timeout=TIMEOUT_MS)
            except Exception:  # noqa: BLE001
                # Diagnostic : capture + messages d'erreur visibles du site (sans aucun identifiant).
                kd_dir()
                capture = captures_dir() / "login-echec.png"
                _essayer(lambda: page.screenshot(path=str(capture), full_page=True))
                textes = []
                for sel in ("[class*='error' i]", "[class*='alert' i]", "[role='alert']", "form p", "form span"):
                    for t in page.locator(sel).all_inner_texts()[:5]:
                        t = _expurger(t.strip(), email, mot_de_passe)
                        if t and t not in textes:
                            textes.append(t[:160])
                detail = (" | ".join(textes[:4])) or "aucun message d'erreur visible"
                raise ErreurKitchenDaily(
                    "Connexion non confirmée (« Mon Espace » absent) : identifiants refusés ou captcha. "
                    f"Aucun identifiant n'a été enregistré. Site : {detail}. Capture : {capture}"
                ) from None
            context = page.context
            kd_dir()
            cible = state_path()
            if cible.exists() and cible.stat().st_uid != os.getuid():
                raise ErreurKitchenDaily(f"{cible} appartient à un autre utilisateur : relance la commande sous cet utilisateur.")
            _sauver_etat(context)
        except ErreurKitchenDaily:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ErreurKitchenDaily(
                "Échec de la connexion : " + _expurger(str(exc).splitlines()[0][:300], email, mot_de_passe)
            ) from None


# --- CLI -----------------------------------------------------------------------


def _imprimer(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n")


def _confirmer_cli(question: str) -> bool:
    if input(f"{question} Tape OUI : ").strip() != "OUI":
        sys.stderr.write("Abandon.\n")
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    os.umask(0o002)
    parser = argparse.ArgumentParser(prog="python -m tools.kitchen_daily", description="Kitchen Daily (Playwright)")
    sub = parser.add_subparsers(dest="commande", required=True)
    sub.add_parser("login", help="crée l'état de session (identifiants demandés, jamais enregistrés)")
    sub.add_parser("statut", help="vérifie que la session est valide")
    sub.add_parser("livraisons", help="livraisons à venir (JSON)")
    p_menu = sub.add_parser("menu", help="plats d'une livraison (JSON)")
    p_menu.add_argument("id")
    p_app = sub.add_parser("appliquer", help="applique une sélection (--dry-run conseillé)")
    p_app.add_argument("id")
    p_app.add_argument("selection", help='JSON {"titre": quantité, ...}')
    p_app.add_argument("--dry-run", action="store_true", help="s'arrête avant « Je valide »")
    p_ext = sub.add_parser("extras", help="desserts, entrées, jus d'une livraison (JSON, lecture seule)")
    p_ext.add_argument("id")
    p_extd = sub.add_parser("extras-definir", help="règle des quantités d'extras (--dry-run conseillé)")
    p_extd.add_argument("id")
    p_extd.add_argument("extras", help='JSON {"desserts": {"titre": quantité}, "entrees": {...}, "jus": {...}} (0 retire)')
    p_extd.add_argument("--dry-run", action="store_true", help="s'arrête avant « Je valide ma commande »")
    p_liv = sub.add_parser("livraison", help="interrupteur « Je veux ma livraison » (--dry-run conseillé)")
    p_liv.add_argument("id")
    groupe = p_liv.add_mutually_exclusive_group(required=True)
    groupe.add_argument("--voulue", dest="voulue", action="store_true", help="livraison voulue (réactiver)")
    groupe.add_argument("--pas-voulue", dest="voulue", action="store_false", help="pas de livraison (sauter la semaine)")
    p_liv.add_argument("--dry-run", action="store_true", help="lit l'état et repère l'interrupteur sans cliquer")
    args = parser.parse_args(argv)

    try:
        if args.commande == "login":
            email = getpass.getpass("E-mail Kitchen Daily (masqué) : ").strip()
            mdp = getpass.getpass("Mot de passe Kitchen Daily : ")
            if not email or not mdp:
                sys.stderr.write("E-mail ou mot de passe vide : abandon.\n")
                return 2
            login(email, mdp)
            del email, mdp
            sys.stdout.write(f"Session enregistrée dans {state_path()} (mode 640).\n")
            return 0
        if args.commande == "statut":
            ok = session_valide()
            sys.stdout.write(("Session Kitchen Daily valide." if ok else MESSAGE_SESSION) + "\n")
            return 0 if ok else 1
        if args.commande == "livraisons":
            _imprimer(livraisons())
            return 0
        if args.commande == "menu":
            _imprimer(menu(args.id))
            return 0
        if args.commande == "appliquer":
            try:
                selection = json.loads(args.selection)
            except json.JSONDecodeError:
                sys.stderr.write("Sélection : JSON invalide.\n")
                return 2
            if not args.dry_run:
                reponse = input("Enregistrer réellement la sélection sur Kitchen Daily ? Tape OUI : ")
                if reponse.strip() != "OUI":
                    sys.stderr.write("Abandon.\n")
                    return 1
            _imprimer(appliquer_selection(args.id, selection, dry_run=args.dry_run))
            return 0
        if args.commande == "extras":
            _imprimer(extras(args.id))
            return 0
        if args.commande == "extras-definir":
            try:
                demandes = valider_extras(json.loads(args.extras))
            except json.JSONDecodeError:
                sys.stderr.write("Extras : JSON invalide.\n")
                return 2
            if not args.dry_run and not _confirmer_cli("Enregistrer réellement ces extras sur Kitchen Daily (payants possibles) ?"):
                return 1
            _imprimer(appliquer_extras(args.id, demandes, dry_run=args.dry_run))
            return 0
        if args.commande == "livraison":
            action = "réactiver la livraison" if args.voulue else "SAUTER la livraison (pas de livraison cette semaine)"
            if not args.dry_run and not _confirmer_cli(f"Vraiment {action} {args.id} sur Kitchen Daily ?"):
                return 1
            _imprimer(definir_livraison(args.id, args.voulue, dry_run=args.dry_run))
            return 0
    except ErreurKitchenDaily as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    parser.error("commande inconnue")
    return 2


if __name__ == "__main__":
    sys.exit(main())
