"""Contrat des cartes génératives (lot 4a, CONTRACT §14), côté cœur : types, validation, extraction, rendu dégradé.

Une carte est un objet JSON ``{type, id, titre?, donnees, actions: [{libelle, action, args, confirmation?}]}`` :

- ``type`` : un des :data:`TYPES` ci-dessous (un type inconnu est ignoré et journalisé : le front ne reçoit que ce
  qu'il sait afficher) ;
- ``id`` : identifiant stable de la carte (``c-…``), pour la remplacer ou la mettre à jour côté front ;
- ``donnees`` : contenu propre au type (champs obligatoires : :data:`TYPES`, les autres sont facultatifs) ;
- ``actions`` : boutons ; ``action`` ∈ :data:`ACTIONS` ; ``confirmation`` = id d'une action en attente
  (``actions_log``) que le bouton valide ou annule par ``POST /api/confirmations/{id}``, ou ``"step-up"`` (le bouton
  exige une preuve d'identité récente, carte ``identifiants`` du lot 5c).

Naissance d'une carte : (a) un outil ajoute une ligne ``CARTE:<json>`` à son résultat en canal web
(``tools/cartes.py``), extraite ici du flux ``stream-json`` ; (b) le cœur génère ``bonjour``, ``brief`` et
``a_noter`` au premier message du jour (``coeur/brief.py``) ; (c) le cœur transforme un message « Identifiants … à
renseigner » en carte ``identifiants``, et les captures d'une action validée en cartes ``capture``.

Sur Telegram, une carte devient du texte simple (:func:`rendu_texte`).
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

PREFIXE = "CARTE:"
_MOTIF = re.compile(r"^[ \t]*`?CARTE:(\{.*\})`?[ \t]*$", re.MULTILINE)
_MOTIF_IDENTIFIANTS = re.compile(
    r"Identifiants (?P<titre>[^\n]+?) à renseigner(?: \(champs manquants : (?P<champs>[^)]*)\))? : "
    r"/identifiants (?P<capacite>[a-z0-9_]+)"
)


@dataclass(frozen=True)
class TypeCarte:
    description: str
    obligatoires: tuple[str, ...] = ()


# Types v1 (spec lot 4a §4) + types qui couvrent la maquette de l'appli.
TYPES: dict[str, TypeCarte] = {
    "bonjour": TypeCarte(
        "Accueil du jour (maquette « hello ») : date, prénom, météo de la commune du foyer, 4 premiers événements ; "
        "feuilletable J+1, J+2 (GET /api/brief/jour?date=).",
        ("date", "prenom"),
    ),
    "meteo": TypeCarte(
        "Météo d'un jour pour la commune du foyer (outil meteo_lire, Open-Meteo) : ciel (`libelle`, `icone`), `min`, "
        "`max`, `pluie_pct`, `pluie_mm`, `vent_kmh`, `moments[]` (matin, après-midi, soir), `actuel` pour aujourd'hui.",
        ("commune", "date"),
    ),
    "brief": TypeCarte("Brief du jour rédigé par l'assistant (texte), bouton Écouter.", ("texte",)),
    "a_noter": TypeCarte(
        "« À noter » : éléments datés (échéances, anniversaires, rappels) triés, nombre aujourd'hui / cette semaine.",
        ("elements",),
    ),
    "agenda_jour": TypeCarte("Événements d'un jour (ou d'une période avec `jusqu_au`).", ("date", "evenements")),
    "evenement": TypeCarte(
        "Un événement proposé, ajouté ou modifié (`statut` : a_valider, ajoute, modifie, ajout_manuel ; "
        "`ancien_debut` pour une modification).",
        ("titre", "statut"),
    ),
    "courses": TypeCarte("Liste de courses par rayon (`rayons[].articles[]`), `restants`.", ("rayons",)),
    "repas_semaine": TypeCarte(
        "Planning des repas (`jours[]` : date, midi, soir ; source : maison, Kitchen Daily, invités, restaurant).",
        ("jours",),
    ),
    "kitchen_daily_proposition": TypeCarte(
        "Sélection de plats Kitchen Daily (`plats[]` : titre, quantite, image (URL réelle du site), badges), total, "
        "formule, filtres de préférences ; compacte sur l'accueil (`compact: true`), riche au geste.",
        ("plats",),
    ),
    "kitchen_daily_extras": TypeCarte("Extras d'une livraison (desserts, entrées, jus) et supplément.", ("categories",)),
    "echeance": TypeCarte("Une échéance (titre, date, rappels).", ("titre", "date")),
    "comparatif": TypeCarte("Comparatif d'offres (`offres[]` : nom, prix, detail, recommande).", ("offres",)),
    "fichier_classe": TypeCarte("Document reçu puis rangé (nom, catégorie, fiche complétée).", ("nom", "categorie")),
    "document_sensible": TypeCarte(
        "Document sensible verrouillé : ouverture après preuve d'identité récente (step-up).", ("nom",)
    ),
    "bus": TypeCarte("Prochains passages d'une ligne à un arrêt (ancienne capacité bus, conservé pour l'historique).",
                     ("passages",)),
    "transports_prochains": TypeCarte(
        "Prochains départs d'un ou plusieurs trajets (`trajets[]` : titre, mode, ligne, origine, destination, alerte, "
        "avertissement, temps_reel, `departs[]` : heure_theorique, heure_estimee, retard_min, dans_min, statut, resume, "
        "quai ; perturbations).",
        ("trajets",),
    ),
    "transports_etat": TypeCarte(
        "État d'un train ou d'un vol par numéro (statut, retard_min, depart, arrivee {lieu, prevu, estime}, arrets[], "
        "perturbations[], message, resume).",
        ("numero", "statut"),
    ),
    "routine_resultat": TypeCarte("Résultat d'une routine (cron d'une capacité) : texte, quand.", ("texte",)),
    "identifiants": TypeCarte(
        "Formulaire de saisie d'identifiants rendu par le front (step-up) : `cle` (lot 5c, identifiants_preparer → "
        "POST /api/coffre) ou `capacite` (secret manquant → POST /api/coffre/{capacite}) ; le modèle n'est jamais "
        "dans la boucle.",
    ),
    "reflexion": TypeCarte("Chemin de pensée : étapes (outil, libellé, durée).", ("etapes",)),
    # Couverture de la maquette (types additionnels, même contrat).
    "creneaux": TypeCarte("Créneaux libres (`creneaux[]`) d'un agenda pour un rendez-vous.", ("creneaux",)),
    "compte_rendu": TypeCarte("Compte rendu d'un enregistrement et actions proposées.", ("texte",)),
    "enregistrement": TypeCarte("Enregistrement long en cours d'analyse ou analysé (durée, statut).", ("statut",)),
    "menu_invites": TypeCarte("Menu pour des invités (plats, adaptations, courses à ajouter).", ("plats",)),
    "capacite_proposee": TypeCarte("Nouvelle capacité proposée par l'assistant (titre, ce qu'elle fera).", ("titre",)),
    "fichier_recu": TypeCarte("Photo ou document reçu (vignette), avant rangement.", ("nom",)),
    "journal": TypeCarte("Dernières actions de l'assistant et du foyer.", ("lignes",)),
    "suggestions": TypeCarte("Suggestions de messages (puces cliquables).", ("suggestions",)),
    "capture": TypeCarte("Capture d'écran d'une action validée (URL servie par l'appli).", ("url",)),
    # Capacité courses & promos (docs/spec-capacite-courses-promos.md §8.4) : compteur commun au foyer.
    "economies": TypeCarte(
        "Compteur « économisé grâce à l'assistant » : `total` (€), `depuis` (date), `achats_suivis`, `dont_estime` "
        "(en attente de ticket), `par_capacite[]`, `lignes[]` (date, libelle, reference, paye, ecart, statut). Seuls "
        "les choix suscités par l'assistant et suivis comptent ; jamais affichée tant qu'aucun achat n'est suivi.",
        ("total", "depuis"),
    ),
}

# Actions des boutons (le front les interprète ; aucune n'exécute quoi que ce soit sans passer par l'API).
ACTIONS: dict[str, str] = {
    "message": "envoie `args.texte` dans la conversation (ex. « remplace le dahl »)",
    "confirmer": "POST /api/confirmations/{confirmation} avec `args.decision` (valider | annuler)",
    "ouvrir": "ouvre un écran (`args.ecran` : agenda, courses, repas, echeances, fichiers, reglages… ; `args.date`…)",
    "ecouter": "lit `args.texte` (ou la carte) à voix haute : POST /api/voix/synthese, sinon synthèse du navigateur",
    "coffre": "ouvre le formulaire d'identifiants de `args.capacite` (POST /api/coffre/{capacite}, step-up)",
    "coffre_ecrire": "soumet le formulaire de la carte identifiants (`args.cle`) à POST /api/coffre, step-up (lot 5c)",
    "deverrouiller": "step-up puis ouverture du document sensible `args.id` (GET /api/sensibles/{id}, déchiffré dans "
                     "le navigateur, docs/documents-sensibles.md)",
    "routine": "PATCH /api/routines/{args.capacite}/{args.nom} avec `args.quand` (propriétaires, à valider)",
    "lien": "ouvre `args.url` (lien externe, nouvel onglet)",
}


def nouvel_id() -> str:
    return f"c-{secrets.token_hex(6)}"


def carte(
    type_: str, donnees: dict[str, Any], *, titre: str | None = None, actions: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    sortie: dict[str, Any] = {"type": type_, "id": nouvel_id(), "donnees": donnees, "actions": list(actions or [])}
    if titre:
        sortie["titre"] = titre
    return sortie


def erreurs_carte(c: Any) -> list[str]:
    """Liste des écarts au contrat (vide si la carte est valide)."""
    if not isinstance(c, dict):
        return ["la carte n'est pas un objet"]
    erreurs: list[str] = []
    type_ = c.get("type")
    if type_ not in TYPES:
        return [f"type inconnu : {str(type_)[:40]}"]
    donnees = c.get("donnees")
    if not isinstance(donnees, dict):
        erreurs.append("donnees doit être un objet")
    else:
        manquants = [k for k in TYPES[type_].obligatoires if k not in donnees]
        if manquants:
            erreurs.append(f"champs manquants dans donnees : {', '.join(manquants)}")
        if type_ == "identifiants" and not (donnees.get("cle") or donnees.get("capacite")):
            erreurs.append("champs manquants dans donnees : cle ou capacite")
    if "titre" in c and c["titre"] is not None and not isinstance(c["titre"], str):
        erreurs.append("titre doit être un texte")
    if type_ == "capture" and isinstance(donnees, dict) and not _url_capture_valide(donnees.get("url")):
        # Une carte vient aussi d'un résultat d'outil (contenu injecté possible) : une capture ne pointe que vers
        # l'appli, jamais vers un hôte extérieur (pixel de suivi) ni hors du dossier des captures.
        erreurs.append("capture : url doit être /api/captures/<nom>")
    actions = c.get("actions", [])
    if not isinstance(actions, list):
        erreurs.append("actions doit être une liste")
    else:
        for a in actions:
            if not isinstance(a, dict) or not isinstance(a.get("libelle"), str) or a.get("action") not in ACTIONS:
                erreurs.append("action invalide (libelle, action connue)")
                continue
            args = a.get("args", {})
            if "args" in a and not isinstance(args, dict):
                erreurs.append("args d'une action doit être un objet")
                continue
            conf = a.get("confirmation")
            if conf is not None and conf != "step-up" and (not isinstance(conf, int) or isinstance(conf, bool)):
                erreurs.append("confirmation doit être un identifiant d'action (entier) ou « step-up »")
            if a["action"] == "lien" and not _url_externe_valide((args or {}).get("url")):
                erreurs.append("lien : args.url doit être une URL http(s)")
    return erreurs


_RE_URL_CAPTURE = re.compile(r"^/api/captures/[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(png|jpg|jpeg)$")


def _url_capture_valide(url: Any) -> bool:
    return isinstance(url, str) and bool(_RE_URL_CAPTURE.fullmatch(url)) and ".." not in url


def _url_externe_valide(url: Any) -> bool:
    """``http://`` ou ``https://`` seulement : jamais ``javascript:``, ``data:``, ``file:``…"""
    return isinstance(url, str) and len(url) <= 2048 and url.lower().startswith(("https://", "http://"))


def normaliser(c: dict[str, Any]) -> dict[str, Any]:
    c = dict(c)
    c["id"] = str(c.get("id") or nouvel_id())[:64]
    c.setdefault("actions", [])
    for a in c["actions"]:
        a.setdefault("args", {})
    return c


def valides(cartes: list[Any]) -> list[dict[str, Any]]:
    """Cartes conformes au contrat, normalisées ; les autres sont journalisées et écartées."""
    sortie: list[dict[str, Any]] = []
    for c in cartes:
        erreurs = erreurs_carte(c)
        if erreurs:
            log.warning("carte écartée : %s", "; ".join(erreurs))
            continue
        sortie.append(normaliser(c))
    return sortie


def extraire_cartes(texte: str) -> tuple[str, list[dict[str, Any]]]:
    """Retire les lignes ``CARTE:<json>`` du texte ; renvoie ``(texte_nettoye, cartes valides)``."""
    if PREFIXE not in (texte or ""):
        return texte, []
    brutes: list[Any] = []

    def collecter(m: re.Match[str]) -> str:
        try:
            brutes.append(json.loads(m.group(1)))
        except ValueError:
            log.warning("ligne CARTE: illisible ignorée")
        return ""

    nettoye = _MOTIF.sub(collecter, texte)
    nettoye = re.sub(r"\n{3,}", "\n\n", nettoye).strip()
    return nettoye, valides(brutes)


def carte_identifiants(texte: str) -> dict[str, Any] | None:
    """Message « Identifiants X à renseigner : /identifiants x » (secret manquant) → carte ``identifiants``."""
    m = _MOTIF_IDENTIFIANTS.search(texte or "")
    if not m:
        return None
    champs = [c.strip() for c in (m.group("champs") or "").split(",") if c.strip()]
    capacite = m.group("capacite")
    return carte(
        "identifiants",
        {"capacite": capacite, "titre": m.group("titre").strip(), "champs_manquants": champs},
        titre=f"Identifiants {m.group('titre').strip()}",
        actions=[{"libelle": "Renseigner", "action": "coffre", "args": {"capacite": capacite}}],
    )


# --- rendu dégradé (Telegram) ---------------------------------------------------------------------------------------


def _lignes_evenements(evenements: Any, limite: int = 6) -> list[str]:
    lignes = []
    for e in (evenements or [])[:limite]:
        if not isinstance(e, dict):
            continue
        debut = str(e.get("debut") or "")
        heure = "journée" if e.get("journee_entiere") else (debut[11:16] if len(debut) >= 16 else debut)
        lignes.append(f"• {heure} {e.get('titre') or ''}".rstrip())
    return lignes


def rendu_texte(c: dict[str, Any]) -> str:
    """Texte simple d'une carte pour Telegram (gras et listes seulement, §7) ; les boutons de confirmation sont
    rendus par le bot à partir de ``CONFIRMATION_REQUISE``, pas ici."""
    t = c.get("type")
    d = c.get("donnees") or {}
    titre = c.get("titre")
    lignes: list[str] = [f"**{titre}**"] if titre else []
    if t == "courses":
        for rayon in d.get("rayons") or []:
            articles = [a.get("article") for a in rayon.get("articles") or [] if not a.get("coche")]
            if articles:
                lignes.append(f"• {rayon.get('rayon')} : {', '.join(str(a) for a in articles)}")
    elif t in ("agenda_jour", "bonjour"):
        lignes += _lignes_evenements(d.get("evenements")) or ["• rien à l'agenda"]
    elif t == "a_noter":
        for e in (d.get("elements") or [])[:8]:
            lignes.append(f"• {e.get('date') or ''} {e.get('titre') or ''}".strip())
    elif t == "evenement":
        lignes.append(f"• {d.get('titre')} — {d.get('debut') or ''}".rstrip(" —"))
    elif t == "bus":
        for p in (d.get("passages") or [])[:4]:
            lignes.append(f"• {p.get('resume') or p.get('heure_estimee') or p.get('heure_theorique') or ''}")
    elif t == "meteo":
        temp = " / ".join(f"{round(v)}°" for v in (d.get("min"), d.get("max")) if isinstance(v, (int, float)))
        lignes.append(f"• {d.get('libelle') or ''} {temp}".strip())
        if isinstance(d.get("pluie_pct"), (int, float)):
            lignes.append(f"• pluie : {round(d['pluie_pct'])} %")
    elif t == "transports_prochains":
        for tr in (d.get("trajets") or [])[:4]:
            if len(d.get("trajets") or []) > 1:
                lignes.append(str(tr.get("titre") or ""))
            if tr.get("alerte"):
                lignes.append(f"• {tr['alerte']}")
            for p in (tr.get("departs") or [])[:3]:
                lignes.append(f"• {p.get('resume') or p.get('heure_estimee') or p.get('heure_theorique') or ''}")
    elif t == "transports_etat":
        lignes.append(str(d.get("resume") or d.get("message") or d.get("statut") or ""))
    elif t in ("brief", "routine_resultat", "compte_rendu"):
        lignes.append(str(d.get("texte") or ""))
    elif t == "identifiants":
        lignes.append(f"Identifiants à renseigner : /identifiants {d.get('capacite')}")
    elif t == "economies":
        total = f"{float(d.get('total') or 0):.2f}".replace(".", ",")
        ligne = f"{total} € depuis le {d.get('depuis')}"
        if d.get("achats_suivis"):
            ligne += f" ({d['achats_suivis']} achat(s) suivi(s)"
            if d.get("dont_estime"):
                ligne += f", dont {float(d['dont_estime']):.2f} € en attente de ticket".replace(".", ",")
            ligne += ")"
        lignes.append(ligne)
    elif t == "kitchen_daily_proposition":
        for p in (d.get("plats") or [])[:20]:
            lignes.append(f"• {p.get('titre')} ×{p.get('quantite') or 1}")
    else:
        for cle in ("texte", "nom", "titre"):
            if d.get(cle):
                lignes.append(str(d[cle]))
                break
    return "\n".join(x for x in lignes if x).strip()
