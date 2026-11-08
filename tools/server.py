"""Serveur MCP « homeos » (§6 du contrat) : mêmes outils en stdio et en HTTP.

Lancement :
    python -m tools.server                                   stdio (développement, repli : tools/mcp.json, mcp.dev.json)
    python -m tools.server --transport http [--port 8090]    serveur persistant sous homeos (homeos-outils.service, lot 7)

En HTTP, l'identité de l'appelant vient de son jeton (`tools.serveur_http`, `tools.identite`) ; `SIGHUP` relit les
jetons et recharge les capacités. `stdout` est réservé au protocole MCP en stdio ; tout log part vers `stderr`.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable
from datetime import date
from typing import Any, TypeVar

os.umask(0o002)  # fichiers partagés par le groupe foyer (homeos et les personnes)

try:  # SDK mcp 1.x
    from mcp.server.fastmcp import FastMCP  # type: ignore[import-not-found]
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[import-not-found]
except ImportError:  # SDK mcp 2.x : FastMCP renommé MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP  # type: ignore[no-redef]
    from mcp.server.mcpserver.exceptions import ToolError  # type: ignore[no-redef]


class ErreurOutil(ToolError, ValueError):
    """Erreur métier renvoyée au modèle avec son message en français (ToolError = message transmis au client)."""

from tools import (
    agenda,
    anniversaires,
    cartes,
    courses,
    db,
    echeances,
    feedback,
    identite,
    journal,
    memoire,
    meteo,
    onboarding,
)
from tools.common import appliquer_proxy, get_logger, today

log = get_logger("homeos.server")

mcp = FastMCP(
    "homeos",
    instructions=(
        "Outils du foyer Firmin Assistant : agenda, courses, échéances, anniversaires, météo de la commune du "
        "foyer, mémoire Markdown, journal, onboarding, feedback, et les outils des capacités actives (préfixés par leur nom : kitchen_daily_*, "
        "bus_*…). Toutes les réponses sont en français. Une sortie commençant par "
        "CONFIRMATION_REQUISE:<id>:<résumé> doit être restituée telle quelle : le bot affichera les boutons. "
        "Une ligne CARTE:<json> (appli web) est affichée par l'appli : ne la recopie jamais."
    ),
)

F = TypeVar("F", bound=Callable[..., Any])


def outil(*args: Any, **kwargs: Any) -> Callable[[F], F]:
    """`@mcp.tool()` qui enregistre une version exécutée hors de la boucle asyncio (fil de travail, identité de la
    requête copiée, `tools.identite.en_thread`) et rend la fonction synchrone intacte (tests, runner)."""

    def deco(fn: F) -> F:
        nom = kwargs.get("name") or (args[0] if args and isinstance(args[0], str) else None) or fn.__name__
        # Lot 4a : en canal web, les cartes génératives de l'outil sont ajoutées à son résultat (tools.cartes).
        mcp.tool(*args, **cartes.sans_sortie_structuree(kwargs))(identite.en_thread(cartes.avec_cartes(fn, nom)))
        return fn

    return deco


def _journalise(kind: str, resume: Callable[..., str]) -> Callable[[F], F]:
    """Journalise chaque appel dans actions_log (done / error) sans jamais y mettre de secret."""

    def deco(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                resultat = fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                message = str(exc) or exc.__class__.__name__
                log.warning("%s : %s", kind, message)
                try:
                    db.log_action(kind, resume(*args, **kwargs), {"args": _sans_secret(kwargs)}, "error", message[:500])
                except Exception:
                    log.exception("journalisation impossible")
                raise ErreurOutil(message) from None
            # Les actions à confirmer sont déjà journalisées en `pending` par l'outil lui-même
            if not (isinstance(resultat, str) and resultat.startswith("CONFIRMATION_REQUISE:")):
                try:
                    db.log_action(kind, resume(*args, **kwargs), {"args": _sans_secret(kwargs)}, "done", _apercu(resultat))
                except Exception:
                    log.exception("journalisation impossible")
            return resultat

        return wrapper  # type: ignore[return-value]

    return deco


def _sans_secret(d: dict[str, Any]) -> dict[str, Any]:
    return {k: (str(v)[:300] if not isinstance(v, (int, float, bool, type(None))) else v) for k, v in d.items()}


def _apercu(valeur: Any) -> str:
    texte = str(valeur)
    return texte[:300]


# --- agenda -----------------------------------------------------------------


@outil()
@_journalise("agenda_lire", lambda debut, fin, agendas=None: f"agenda {debut} → {fin}")
def agenda_lire(debut: date, fin: date, agendas: list[str] | None = None) -> list[dict[str, Any]]:
    """Lit les événements des agendas iCal du foyer entre deux dates (incluses).

    Utilise-le pour répondre à « qu'est-ce qu'on a ce week-end ? », « Camille est libre jeudi ? », etc.
    - `debut`, `fin` : dates au format AAAA-MM-JJ (fuseau Europe/Paris).
    - `agendas` : noms d'agendas à filtrer (tels que dans la configuration : Foyer, Alex, Camille) ; vide = tous.
    Renvoie une liste triée d'événements : agenda, titre, debut, fin (ISO), lieu, journee_entiere.
    Les données proviennent d'un cache rafraîchi au plus toutes les 5 minutes.
    """
    return agenda.agenda_lire(debut, fin, agendas)


@outil()
@_journalise("agenda_ajouter", lambda agenda, titre, *a, **k: f"ajout « {titre} » dans {agenda}")
def agenda_ajouter(
    agenda: str,
    titre: str,
    debut: str,
    fin: str,
    lieu: str | None = None,
    description: str | None = None,
) -> str:
    """Propose l'ajout d'un événement dans un agenda du foyer.

    - `agenda` : nom de l'agenda cible (Foyer, Alex, Camille…).
    - `debut`, `fin` : date-heure ISO (ex. 2026-11-12T18:30) en heure de Paris.
    Si l'écriture Google est configurée, l'ajout n'est PAS immédiat : l'outil renvoie
    `CONFIRMATION_REQUISE:<id>:<résumé>` que tu dois restituer tel quel (la personne validera par bouton).
    Sinon, il renvoie un texte prêt à copier pour un ajout manuel : transmets-le.
    """
    return agenda.agenda_ajouter(agenda, titre, debut, fin, lieu, description)


# --- courses ----------------------------------------------------------------


@outil()
@_journalise("courses_lister", lambda: "liste des courses")
def courses_lister() -> list[dict[str, Any]]:
    """Liste de courses partagée du foyer, groupée par rayon.

    Chaque article : id, article, rayon, coche (déjà pris ou non), ajoute_par, ts.
    Présente-la par rayon, en listes simples, sans tableau.
    """
    return courses.lister()


@outil()
@_journalise("courses_ajouter", lambda articles, rayon=None: f"ajout {', '.join(articles)[:120]}")
def courses_ajouter(articles: list[str], rayon: str | None = None) -> list[dict[str, Any]]:
    """Ajoute un ou plusieurs articles à la liste de courses.

    - `articles` : noms courts (ex. ["lait", "pommes", "lessive"]). Sépare toujours en plusieurs éléments.
    - `rayon` : facultatif, force le rayon pour tous (fruits-légumes, boulangerie, laitier, viande-poisson,
      épicerie, surgelés, boissons, hygiène, maison, autre) ; sinon il est deviné.
    Un article déjà présent n'est pas dupliqué ; un article coché est réactivé.
    """
    return courses.ajouter(articles, rayon)


@outil()
@_journalise("courses_cocher", lambda articles: f"coche {', '.join(articles)[:120]}")
def courses_cocher(articles: list[str]) -> dict[str, list[str]]:
    """Coche des articles comme achetés (ils restent visibles, barrés, jusqu'à `courses_vider_coches`).

    La correspondance est insensible à la casse et accepte un nom partiel.
    Renvoie `coches` (trouvés) et `introuvables`.
    """
    return courses.cocher(articles)


@outil()
@_journalise("courses_vider_coches", lambda: "vide les articles cochés")
def courses_vider_coches() -> dict[str, int]:
    """Supprime définitivement tous les articles cochés de la liste (après les courses). Renvoie le nombre supprimé."""
    return {"supprimes": courses.vider_coches()}


# --- échéances --------------------------------------------------------------


@outil()
@_journalise("echeances_lister", lambda horizon_jours=60: f"échéances à {horizon_jours} j")
def echeances_lister(horizon_jours: int = 60) -> list[dict[str, Any]]:
    """Échéances du foyer (administratif, entretien, voiture, animaux…) dont la prochaine occurrence tombe dans l'horizon.

    Chaque échéance : id, titre, date (base), prochaine (occurrence calculée), dans_jours, rappels_jours,
    recurrence, notes, en_retard. Les échéances passées non récurrentes restent listées comme en retard.
    """
    return echeances.lister(horizon_jours)


@outil()
@_journalise("echeances_ajouter", lambda titre, date, *a, **k: f"échéance « {titre} » le {date}")
def echeances_ajouter(
    titre: str,
    date: str,
    rappels_jours: list[int] | None = None,
    recurrence: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Crée une échéance.

    - `date` : AAAA-MM-JJ (ou JJ/MM/AAAA).
    - `rappels_jours` : jours avant l'échéance où rappeler (défaut [30, 7, 1]) ; J-0 est toujours rappelé.
    - `recurrence` : hebdomadaire, mensuelle, bimestrielle, trimestrielle, semestrielle, annuelle, ou vide.
    - `notes` : précisions utiles (qui s'en occupe, où, montant…).
    """
    return echeances.ajouter(titre, date, rappels_jours, recurrence, notes)


@outil()
@_journalise("echeances_supprimer", lambda id: f"supprime échéance #{id}")
def echeances_supprimer(id: int) -> dict[str, Any]:
    """Supprime une échéance par son identifiant (obtenu via `echeances_lister`). Demande confirmation orale avant si le doute existe."""
    ok = echeances.supprimer(id)
    return {"id": id, "supprimee": ok, "message": "Échéance supprimée." if ok else "Aucune échéance avec cet identifiant."}


# --- anniversaires ----------------------------------------------------------


@outil()
@_journalise("anniversaires_lister", lambda horizon_jours=60: f"anniversaires à {horizon_jours} j")
def anniversaires_lister(horizon_jours: int = 60) -> list[dict[str, Any]]:
    """Anniversaires à venir dans l'horizon : prenom, prochaine (date), dans_jours, age (si année connue), lieu, notes, delai_cadeau_jours."""
    return anniversaires.lister(horizon_jours)


@outil()
@_journalise("anniversaires_ajouter", lambda prenom, date, *a, **k: f"anniversaire de {prenom} ({date})")
def anniversaires_ajouter(
    prenom: str,
    date: str,
    lieu: str | None = None,
    notes: str | None = None,
    delai_cadeau_jours: int = 21,
) -> dict[str, Any]:
    """Enregistre un anniversaire.

    - `date` : AAAA-MM-JJ si l'année de naissance est connue, sinon JJ/MM.
    - `lieu` : où on le fête habituellement, facultatif.
    - `delai_cadeau_jours` : nombre de jours avant pour l'alerte cadeau (défaut 21). Alertes à J-délai, J-7 et J-0.
    """
    return anniversaires.ajouter(prenom, date, lieu, notes, delai_cadeau_jours)


# --- mémoire ----------------------------------------------------------------


@outil()
@_journalise("memoire_lire", lambda chemin: f"lit {chemin}")
def memoire_lire(chemin: str) -> str:
    """Lit un fichier Markdown de la mémoire du foyer.

    Chemins relatifs autorisés : foyer/… (identite.md, regles.md, contacts.md, infos-pratiques.md,
    journal/AAAA-MM.md), personnes/… (alex.md, camille.md, enfants.md), alimentation/… (foyer.md,
    alex.md, camille.md, enfants.md, historique.md), voitures/<nom>.md, animaux/<nom>.md.
    Pas de `..`, pas de chemin absolu.
    Un fichier manquant est créé avec un en-tête.
    """
    memoire.verifier_prive(chemin, identite.utilisateur_courant())
    return memoire.lire(chemin)


@outil()
@_journalise("memoire_ecrire", lambda chemin, contenu: f"écrit {chemin}")
def memoire_ecrire(chemin: str, contenu: str) -> str:
    """Remplace entièrement le contenu d'un fichier de la mémoire (mêmes chemins que `memoire_lire`).

    À utiliser pour réorganiser une fiche. Pour simplement compléter, préfère `memoire_ajouter`.
    Chaque écriture est historisée (commit git automatique).
    """
    memoire.verifier_prive(chemin, identite.utilisateur_courant())
    return memoire.ecrire(chemin, contenu)


@outil()
@_journalise("memoire_ajouter", lambda chemin, ligne: f"ajoute à {chemin}")
def memoire_ajouter(chemin: str, ligne: str) -> str:
    """Ajoute une ligne à la fin d'un fichier de la mémoire (créé si besoin). Idéal pour noter une préférence, un contact, une info pratique."""
    memoire.verifier_prive(chemin, identite.utilisateur_courant())
    return memoire.ajouter(chemin, ligne)


# --- journal ----------------------------------------------------------------


@outil()
@_journalise("journal_ajouter", lambda texte: f"journal : {texte[:80]}")
def journal_ajouter(texte: str) -> str:
    """Ajoute une entrée datée au journal du foyer (data/foyer/journal/AAAA-MM.md).

    Pour les faits marquants : décision prise, rendez-vous passé, chose à retenir. Une phrase, pas plus.
    """
    return journal.ajouter(texte)


# --- onboarding -------------------------------------------------------------


@outil()
@_journalise("onboarding_etat", lambda: "état onboarding")
def onboarding_etat() -> dict[str, Any]:
    """État des modules de découverte (identité du foyer, alimentation, échéances) : faits ou restants, avec les réponses enregistrées."""
    return onboarding.etat()


@outil()
@_journalise("onboarding_terminer", lambda module, reponses=None: f"module {module} terminé")
def onboarding_terminer(module: str, reponses: dict[str, Any] | None = None) -> dict[str, Any]:
    """Marque un module d'onboarding comme terminé et mémorise les réponses recueillies.

    - `module` : nom du fichier du module sans extension (ex. 00-identite-foyer, 10-alimentation, 20-echeances).
    - `reponses` : dictionnaire question → réponse (texte libre).
    Pense aussi à écrire les réponses dans les fiches Markdown indiquées par le module (`memoire_ecrire`).
    """
    return onboarding.terminer(module, reponses)


# --- feedback ---------------------------------------------------------------


@outil()
@_journalise("feedback_noter", lambda ecran_ou_sujet, note, commentaire=None: f"feedback {ecran_ou_sujet} = {note}/5")
def feedback_noter(ecran_ou_sujet: str, note: int, commentaire: str | None = None) -> dict[str, Any]:
    """Enregistre un retour d'usage : un écran ou un sujet, une note de 1 à 5 et un commentaire libre (interviews, maquette)."""
    return feedback.noter(ecran_ou_sujet, note, commentaire)


# --- rappels ----------------------------------------------------------------


def rappels_du_jour_data(ref: date | None = None) -> dict[str, Any]:
    """Agrégat utilisé par l'outil et par le runner (brief du jour)."""
    ref = ref or today()
    return {
        "date": ref.isoformat(),
        "echeances": echeances.rappels_du_jour(ref),
        "anniversaires": anniversaires.rappels_du_jour(ref),
    }


@outil()
@_journalise("rappels_du_jour", lambda: "rappels du jour")
def rappels_du_jour() -> dict[str, Any]:
    """Échéances et anniversaires qui déclenchent un rappel aujourd'hui (J-n configurés, J-0, retards, alertes cadeau).

    Utilisé par le brief du jour ; utile aussi pour « il y a quelque chose à ne pas oublier aujourd'hui ? ».
    """
    return rappels_du_jour_data()


# --- météo ------------------------------------------------------------------


@outil()
@_journalise("meteo_lire", lambda jour=None, commune=None: f"météo {jour or 'aujourd’hui'} {commune or 'commune du foyer'}")
def meteo_lire(jour: str | None = None, commune: str | None = None) -> dict[str, Any]:
    """Météo d'un jour pour la commune du foyer (Open-Meteo) : ciel, températures min / max, risque et cumul de pluie,
    vent, matin / après-midi / soir, et conditions actuelles pour aujourd'hui.

    Utilise-le dès qu'on parle de météo, d'une sortie, d'une balade, du linge à étendre, d'un parapluie, d'une tenue
    pour les enfants, même sans le mot « météo ». Ne réponds jamais de mémoire.
    - `jour` : vide ou « aujourd'hui », « demain », « après-demain », un jour de la semaine (« samedi ») ou une date
      AAAA-MM-JJ ; prévisions jusqu'à 13 jours.
    - `commune` : laisse vide pour la commune du foyer, même si la personne l'abrège ou la nomme ; ne mets une autre
      commune que si la personne en demande explicitement une autre (vacances, sortie ailleurs). Jamais la grande
      ville voisine à la place de la commune du foyer.
    Renvoie commune, date, quand, libelle, min, max, pluie_pct, pluie_mm, vent_kmh, rafales_kmh, moments[], actuel.
    """
    return meteo.lire(jour, commune)  # MeteoIndisponible → ErreurOutil (message avec une solution), par _journalise


# --- Lot 3 : fichiers et photos ---
# Bloc placé juste avant `main()` (et non après `if __name__ == "__main__"`) : les outils doivent être
# enregistrés avant que `python -m tools.server` ne lance le serveur.

from tools import fichiers


@outil()
@_journalise(
    "fichiers_ranger",
    lambda chemin_source, categorie, nouveau_nom, *a, **k: f"range {nouveau_nom} dans {categorie}",
)
def fichiers_ranger(
    chemin_source: str,
    categorie: str,
    nouveau_nom: str,
    description: str,
    fiche: str | None = None,
) -> dict[str, Any]:
    """Range un fichier reçu (photo, PDF) dans les archives du foyer.

    - `chemin_source` : chemin du fichier reçu, tel que donné par la ligne `[fichier reçu: …]` (ou par `fichiers_entrants`).
    - `categorie` : une seule parmi voiture, animaux, enfants, maison, sante, administratif, factures, photos, autre.
    - `nouveau_nom` : nom parlant, sans date ni extension (ex. « facture-garage-vidange ») ; il est nettoyé, l'extension d'origine est conservée.
    - `description` : une ligne factuelle (ex. « Vidange + plaquettes, 84 200 km, 312 € ») ; jamais de détail médical.
    - `fiche` : facultatif, fiche mémoire où noter une ligne datée avec le lien du fichier
      (ex. « voitures/<modèle>.md », « animaux/<nom>.md », « foyer/maison.md », « personnes/enfants.md »).
    Déplace le fichier vers fichiers/<categorie>/<année>/, ajoute une ligne à l'index de la catégorie.
    """
    return fichiers.ranger(chemin_source, categorie, nouveau_nom, description, fiche)


@outil()
@_journalise("fichiers_lister", lambda categorie=None, limite=20: f"fichiers {categorie or 'toutes catégories'}")
def fichiers_lister(categorie: str | None = None, limite: int = 20) -> list[dict[str, Any]]:
    """Derniers fichiers rangés, du plus récent au plus ancien : date, categorie, nom, description, chemin.

    - `categorie` : filtre facultatif (voiture, animaux, enfants, maison, sante, administratif, factures, photos, autre).
    Pour relire un fichier, utilise Read sur son chemin (absolu).
    """
    return fichiers.lister(categorie, limite)


@outil()
@_journalise("fichiers_entrants", lambda: "fichiers non rangés")
def fichiers_entrants() -> list[dict[str, Any]]:
    """Fichiers reçus mais pas encore rangés (nom, chemin absolu, taille, âge). Ils sont supprimés après 30 jours."""
    return fichiers.entrants()


@outil()
@_journalise("photos_journal_ajouter", lambda chemin_source, legende, *a, **k: f"photo : {legende[:80]}")
def photos_journal_ajouter(chemin_source: str, legende: str, date: str | None = None) -> dict[str, Any]:
    """Ajoute une photo souvenir au journal du foyer.

    - `chemin_source` : chemin de la photo reçue (ligne `[fichier reçu: …]`).
    - `legende` : une phrase (ex. « Première balade de la saison au parc »).
    - `date` : AAAA-MM-JJ du souvenir si ce n'est pas aujourd'hui.
    La photo est réduite (1600 px, JPEG) dans photos/<année>/<mois>/ et une ligne 📷 est ajoutée au journal du mois.
    """
    return fichiers.photos_journal_ajouter(chemin_source, legende, date)


# --- fin Lot 3 ---


# --- Lot 5c : identifiants et moyens de paiement gérés en conversation ---
# Le modèle ne voit jamais une valeur : références, noms de champs et métadonnées seulement (tools.identifiants).

from tools import identifiants


@outil()
@identifiants.journalise("identifiants_lister")
def identifiants_lister() -> dict[str, Any]:
    """Les identifiants du coffre visibles par la personne (les siens et ceux du foyer ; tout pour un
    propriétaire du foyer), sans aucune valeur : cle, libelle, type (identifiants, carte_bancaire, code, cle_api,
    autre), proprietaire, champs_presents / champs_manquants (noms), carte (« ••• 1234 » au plus), virtuelle, maj,
    utilise_par ; plus `a_renseigner` : pour chaque capacité active, les identifiants requis encore manquants.
    Pour « quels mots de passe tu as ? », « il manque quoi pour la cantine ? »."""
    return identifiants.lister()


@outil()
@identifiants.journalise("identifiants_preparer")
def identifiants_preparer(
    libelle: str,
    type: str,
    proprietaire: str = "moi",
    capacite: str | None = None,
    virtuelle: bool | None = None,
) -> str:
    """Crée (ou modifie) une entrée du coffre SANS valeur et renvoie la marche à suivre pour saisir les valeurs
    hors conversation (commande sur le serveur ; dans l'appli, une carte sécurisée).

    - `libelle` : nom simple donné par le foyer (« Cantine de l'école ») — jamais un identifiant ni un mot de passe ;
    - `type` : identifiants | carte_bancaire | code | cle_api | autre ;
    - `proprietaire` : « moi » (personnel) ou « foyer » (commun) ;
    - `capacite` : la capacité qui utilisera ces identifiants (ex. « planity »), sinon entrée libre ;
    - `virtuelle` : carte bancaire seulement, true après que la personne a confirmé que c'est une carte virtuelle à
      plafond (sinon l'outil renvoie la question à poser).
    Sur une entrée existante (même libellé), les valeurs seront remplacées à la prochaine saisie.
    Transmets la marche à suivre ; ne demande ni ne répète jamais une valeur."""
    return identifiants.preparer(libelle, type, proprietaire, capacite, virtuelle)


@outil()
@identifiants.journalise("identifiants_renommer")
def identifiants_renommer(cle: str, libelle: str) -> str:
    """Change le nom lisible d'une entrée (`cle` donnée par identifiants_lister). Renvoie CONFIRMATION_REQUISE."""
    return identifiants.renommer(cle, libelle)


@outil()
@identifiants.journalise("identifiants_attribuer")
def identifiants_attribuer(cle: str, capacite: str) -> str:
    """Fait servir une entrée libre à une capacité qui déclare ce type d'identifiant (ex. une carte enregistrée
    pour la cantine). Contrôle que les champs requis sont saisis. Renvoie CONFIRMATION_REQUISE."""
    return identifiants.attribuer(cle, capacite)


@outil()
@identifiants.journalise("identifiants_supprimer")
def identifiants_supprimer(cle: str) -> str:
    """Supprime une entrée du coffre (propriétaire de l'entrée ou propriétaire du foyer). Renvoie
    CONFIRMATION_REQUISE."""
    return identifiants.supprimer(cle)


@outil()
@identifiants.journalise("identifiants_ranger_quarantaine")
def identifiants_ranger_quarantaine(id: str, cle: str, champ: str | None = None) -> str:
    """Range dans le coffre une donnée sensible masquée d'un message (ligne « [donnée sensible détectée : …,
    quarantaine q7f3] ») : `id` = q7f3, `cle` = entrée cible (préparée avant avec identifiants_preparer),
    `champ` facultatif (déduit : numero pour une carte, mot_de_passe…). La valeur ne passe jamais par toi.
    Seulement si la personne qui l'a envoyée le demande, dans les 10 minutes. Renvoie CONFIRMATION_REQUISE."""
    return identifiants.ranger_quarantaine(id, cle, champ)


# --- fin Lot 5c ---


# --- Lot 5a : capacités (plugins capabilities/<nom>/) ---
# Outils de base sur les capacités, puis chargement des capacités actives ou en essai (leurs outils sont
# préfixés par leur nom). Bloc placé juste avant `main()`, comme le lot 3.

from tools import capacites


@outil()
@_journalise("capacites_lister", lambda: "liste des capacités")
def capacites_lister() -> list[dict[str, Any]]:
    """Ce que l'assistant sait faire en plus des outils de base : une entrée par capacité (Kitchen Daily, bus…).

    Chaque capacité : nom, titre, description, statut (actif, essai = lecture seule, inactif, invalide), origine
    (humain ou assistant), personnes (vide = tout le foyer), commandes Telegram, dernier_smoke (dernière
    vérification automatique : ok, message, quand). Pour « qu'est-ce que tu sais faire ? » : réponds en langage courant.
    """
    return capacites.lister()


@outil()
@_journalise("capacites_statut", lambda nom: f"statut capacité {nom}")
def capacites_statut(nom: str) -> dict[str, Any]:
    """Détail d'une capacité (`nom` tel que donné par `capacites_lister`) : statut, crons, actions avec effet,
    identifiants requis (références seulement, jamais de valeur), réglages (parametres : question, valeur,
    renseigne), module de découverte, dernière vérification."""
    return capacites.statut_detaille(nom)


@outil()
@_journalise("capacites_parametrer", lambda nom, cle, valeur: f"réglage {nom}.{cle}")
def capacites_parametrer(nom: str, cle: str, valeur: str) -> dict[str, Any]:
    """Enregistre un réglage d'une capacité (réponse à une question de son module de découverte).

    - `nom` : la capacité (ex. bus) ; `cle` : le réglage déclaré (voir `capacites_statut` → parametres) ;
    - `valeur` : la réponse, convertie selon le type (texte, nombre, choix parmi la liste, booleen oui/non).
    Jamais pour un mot de passe ou un identifiant : ceux-là se saisissent hors de la conversation.
    Renvoie tous les réglages de la capacité.
    """
    return capacites.definir_parametre(nom, cle, valeur)


CAPACITES_CHARGEES = capacites.charger_actives(mcp)

# --- fin Lot 5a ---


# --- Lot 7 : serveur persistant (HTTP) ---


def recharger_capacites() -> dict[str, list[str]]:
    """Retire les outils des capacités puis recharge les capacités actives ou en essai (enable, disable, install,
    code modifié d'une capacité). Les outils de base ne changent pas (nouvelle version du dépôt : redémarrer)."""
    global CAPACITES_CHARGEES
    retires = capacites.decharger(mcp, CAPACITES_CHARGEES)
    CAPACITES_CHARGEES = capacites.charger_actives(mcp)
    log.info(
        "capacités rechargées : %d outil(s) retiré(s), chargées : %s",
        retires,
        ", ".join(f"{nom} ({len(outils)})" for nom, outils in sorted(CAPACITES_CHARGEES.items())) or "aucune",
    )
    return CAPACITES_CHARGEES


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m tools.server", description="Serveur MCP homeos")
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio",
                        help="stdio (défaut : lancé par claude via tools/mcp.json) ou http (homeos-outils.service)")
    parser.add_argument("--hote", default=None, help="adresse d'écoute en http (défaut 127.0.0.1, local uniquement)")
    parser.add_argument("--port", type=int, default=None, help="port en http (défaut 8090 ou HOMEOS_OUTILS_PORT)")
    args = parser.parse_args(argv)

    # Proxy sortant filtrant (lot 5d, install.sh --filtrer-sortant) : urllib et les sous-processus l'héritent,
    # Playwright le reçoit explicitement (tools.kitchen_daily.options_lancement).
    if appliquer_proxy():
        log.info("proxy sortant actif")
    db.connect().close()  # crée les tables au démarrage
    if args.transport == "stdio":
        log.info("serveur MCP homeos démarré (stdio)")
        mcp.run(transport="stdio")
        return

    import asyncio

    from tools import serveur_http

    jetons = serveur_http.Jetons()
    if jetons.charger() == 0:
        log.warning("aucun jeton valide dans %s : toutes les requêtes seront refusées (401)", jetons.chemin)

    def recharger() -> None:
        jetons.charger()
        recharger_capacites()

    hote = args.hote or os.environ.get("HOMEOS_OUTILS_HOTE") or serveur_http.HOTE_DEFAUT
    port = args.port or int(os.environ.get("HOMEOS_OUTILS_PORT") or serveur_http.PORT_DEFAUT)
    asyncio.run(serveur_http.servir(mcp, jetons, hote, port, recharger))


if __name__ == "__main__":
    main()
