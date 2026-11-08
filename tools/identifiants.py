"""Identifiants et moyens de paiement gérés en conversation (lot 5c) — outils `identifiants_*`.

Principe absolu : **le modèle ne voit jamais une valeur secrète**. Il manipule des références (clés du coffre),
des noms de champs et des métadonnées (libellé, type, propriétaire, 4 derniers chiffres d'une carte). Les valeurs
entrent par un canal sûr — la CLI du coffre sur le serveur (`sudo -u homeos homeos-coffre set <cle>`), la carte
sécurisée de l'appli web (`POST /api/coffre`, step-up, `enregistrer_saisie`) ou une quarantaine du filtre de
données sensibles (`coeur.filtre_sensible`) — et sortent uniquement vers les connecteurs (`ctx.secret()`).

Droits : une personne voit ses entrées et celles du foyer ; un propriétaire du foyer (`role: proprietaire`) voit
les métadonnées de toutes. Renommer, attribuer, supprimer : le propriétaire de l'entrée ou un propriétaire du foyer,
toujours par bouton Valider (`CONFIRMATION_REQUISE`, kind `identifiants_<action>`, exécuté par `executer`).

Un argument qui ressemble à une valeur secrète (mot de passe collé, carte, valeur déjà dans le coffre) est refusé
sans être enregistré ni journalisé, avec le conseil de changer ce mot de passe.
"""

from __future__ import annotations

import functools
import json
import os
import re
from collections.abc import Callable
from typing import Any, TypeVar

from tools import coffre, db
from tools.common import get_logger

log = get_logger("homeos.identifiants")

F = TypeVar("F", bound=Callable[..., Any])

PREFIXE_KIND = "identifiants_"
KINDS = ("identifiants_renommer", "identifiants_attribuer", "identifiants_supprimer",
         "identifiants_ranger_quarantaine")
COMMANDE_SAISIE = "sudo -u homeos homeos-coffre set {cle}"
ARGUMENT_REFUSE = "[valeur refusée, non journalisée]"
MESSAGE_VALEUR_COLLEE = (
    "Je ne l'enregistre pas : un mot de passe, un code ou un numéro de carte ne passe jamais par la conversation. "
    "Par précaution, change-le, puis saisis le nouveau hors conversation (je te donne la marche à suivre)."
)
MESSAGE_IDENTIFIANT_COLLE = (
    "Je ne mets ni identifiant ni adresse e-mail dans le nom d'une entrée : donne-lui un nom simple "
    "(ex. « Cantine de l'école »), l'identifiant se saisit avec le mot de passe, hors conversation."
)
MESSAGE_INCONNU = "Je ne sais pas qui me parle : je ne peux pas gérer d'identifiants pour cette conversation."
MESSAGE_RESERVE = "Seul le propriétaire de cette entrée (ou un propriétaire du foyer) peut la modifier."
QUESTION_VIRTUELLE = (
    "Avant d'enregistrer une carte : c'est bien une carte virtuelle à plafond ? Je n'accepte que celles-là "
    "(jamais la carte physique). Si oui, rappelle-moi avec virtuelle=true."
)
CHAMP_PAR_NATURE = {
    "carte_bancaire": {"carte_bancaire": "numero"},
    "mot_de_passe": {"identifiants": "mot_de_passe", "code": "code", "cle_api": "cle", "autre": "mot_de_passe"},
    "cle": {"cle_api": "cle", "identifiants": "mot_de_passe", "autre": "cle"},
    "iban": {"autre": "iban"},
    "nir": {"autre": "numero_securite_sociale"},
}


class ErreurIdentifiants(RuntimeError):
    """Demande refusée ; le message (en français) ne contient jamais de valeur."""


class ValeurCollee(ErreurIdentifiants):
    """Un argument ressemble à une valeur secrète : refus, rien n'est enregistré ni journalisé."""


# --- qui parle ------------------------------------------------------------------------------------------------


def appelant() -> dict[str, Any]:
    """{linux_user, prenom, role} de la personne courante (jeton du serveur d'outils, HOMEOS_PERSONNE)."""
    from tools import administration, capacites

    personne = capacites.personne_courante()
    qui = administration.appelant(personne) if personne else None
    if qui is None:
        raise ErreurIdentifiants(MESSAGE_INCONNU)
    return {"linux_user": capacites._slug(qui["linux_user"]), "prenom": qui["prenom"], "role": qui["role"]}


def peut_voir(entree: dict[str, Any], qui: dict[str, Any]) -> bool:
    return qui["role"] == "proprietaire" or entree.get("proprietaire") in (qui["linux_user"], coffre.PROPRIETAIRE_FOYER)


def peut_modifier(entree: dict[str, Any], qui: dict[str, Any]) -> bool:
    return qui["role"] == "proprietaire" or entree.get("proprietaire") == qui["linux_user"]


# --- arguments : jamais de valeur secrète --------------------------------------------------------------------

_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _ressemble_mot_de_passe(texte: str) -> bool:
    """Un seul « mot » de 8 caractères ou plus mêlant chiffres et symboles, majuscule interne, ou majuscules,
    minuscules et deux chiffres : c'est un mot de passe, pas un nom d'entrée (« Hunter2-Mot-De-Passe! »,
    « Soleil2024 »). Un nom d'entrée a d'ordinaire des espaces (« Impôts 2025 »)."""
    if any(c.isspace() for c in texte.strip()) or len(texte.strip()) < 8:
        return False
    t = texte.strip()
    chiffre = any(c.isdigit() for c in t)
    symbole = any(not c.isalnum() for c in t)
    majuscule_interne = any(c.isupper() for c in t[1:])
    lettres_et_chiffres = sum(c.isdigit() for c in t) >= 2 and any(c.isupper() for c in t) and any(c.islower() for c in t)
    return chiffre and (symbole or majuscule_interne or lettres_et_chiffres)


# Arguments en texte libre (nom donné par le foyer) : contrôles complets. Les autres (clé, type, capacité, id,
# champ) ont une forme imposée et ne passent que par le filtre de données sensibles.
ARGUMENTS_LIBRES = frozenset({"libelle"})


def analyser_argument(texte: Any, libre: bool = True) -> str | None:
    """« secret » (valeur secrète), « identifiant » (adresse e-mail) ou None pour un texte ordinaire."""
    if not isinstance(texte, str) or not texte.strip():
        return None
    from coeur import filtre_sensible

    if filtre_sensible.detecter(texte):
        return "secret"
    if not libre:
        return None
    if _ressemble_mot_de_passe(texte):
        return "secret"
    try:
        if coffre.masquer(texte) != texte:  # une valeur déjà dans le coffre
            return "secret"
    except Exception:  # noqa: BLE001 - coffre indisponible : les autres contrôles suffisent
        log.debug("coffre indisponible pour le contrôle d'un argument")
    if _RE_EMAIL.search(texte):
        return "identifiant"
    return None


def verifier_arguments(**arguments: Any) -> None:
    for nom, valeur in arguments.items():
        genre = analyser_argument(valeur, nom in ARGUMENTS_LIBRES)
        if genre == "secret":
            raise ValeurCollee(MESSAGE_VALEUR_COLLEE)
        if genre == "identifiant":
            raise ValeurCollee(MESSAGE_IDENTIFIANT_COLLE)


def arguments_journal(arguments: dict[str, Any]) -> dict[str, Any]:
    """Arguments à journaliser : une valeur suspecte est remplacée, les valeurs connues du coffre masquées."""
    from tools import capacites

    propres: dict[str, Any] = {}
    for nom, valeur in arguments.items():
        if analyser_argument(valeur, nom in ARGUMENTS_LIBRES) is not None:
            propres[nom] = ARGUMENT_REFUSE
        elif isinstance(valeur, (int, float, bool, type(None))):
            propres[nom] = valeur
        else:
            propres[nom] = str(capacites.masquer_secrets(str(valeur)))[:200]
    return propres


def journalise(kind: str) -> Callable[[F], F]:
    """Journalise l'appel dans actions_log (done / error) sans jamais y mettre de valeur, masque la sortie et
    convertit les erreurs en message pour le modèle (sans la valeur refusée)."""

    def deco(fn: F) -> F:
        @functools.wraps(fn)
        def enveloppe(*args: Any, **kwargs: Any) -> Any:
            from tools import capacites

            journal_args = arguments_journal(kwargs)
            try:
                resultat = fn(*args, **kwargs)
            except (ErreurIdentifiants, coffre.ErreurCoffre, capacites.ErreurCapacite, NotImplementedError) as exc:
                message = str(capacites.masquer_secrets(str(exc) or exc.__class__.__name__))
                if isinstance(exc, coffre.CoffreInaccessible):
                    message = "Le coffre n'est lisible que par le serveur d'outils : réessaie depuis l'appli ou Telegram."
                log.warning("%s refusé (%s)", kind, exc.__class__.__name__)
                _journal(kind, "refusé" if isinstance(exc, ValeurCollee) else "erreur", journal_args, "error", message)
                raise capacites.erreur_outil(message) from None
            resultat = capacites.masquer_secrets(resultat)
            if not (isinstance(resultat, str) and resultat.startswith("CONFIRMATION_REQUISE:")):
                _journal(kind, kind.replace("_", " "), journal_args, "done", str(resultat)[:300])
            return resultat

        return enveloppe  # type: ignore[return-value]

    return deco


def _journal(kind: str, resume: str, args: dict[str, Any], statut: str, resultat: str) -> None:
    try:
        db.log_action(kind, resume, {"args": args}, statut, resultat[:500])
    except Exception:
        log.exception("journalisation impossible")


# --- capacités -----------------------------------------------------------------------------------------------


def _capacites_chargees() -> list[Any]:
    from tools import capacites

    return [c for c, _statut in capacites.capacites_chargees()]


def _cle_capacite(secret: Any, proprietaire: str) -> str:
    if not secret.par_personne:
        return str(secret.cle)
    if proprietaire == coffre.PROPRIETAIRE_FOYER:
        raise ErreurIdentifiants("Ce service demande un compte personnel : l'entrée doit appartenir à une personne.")
    return f"{secret.base}/{proprietaire}"


def _secret_pour(nom_capacite: str, type_: str | None) -> tuple[Any, Any]:
    """(capacité, secret déclaré du type voulu) ; lève si la capacité ne déclare rien de tel."""
    from tools import administration

    try:
        cap = administration.trouver_capacite(nom_capacite)
    except administration.ErreurAdministration as exc:
        raise ErreurIdentifiants(str(exc)) from None
    if cap.manifeste is None or not cap.manifeste.secrets:
        raise ErreurIdentifiants(f"« {cap.titre} » n'utilise pas d'identifiants.")
    candidats = [s for s in cap.manifeste.secrets if type_ is None or s.type == type_]
    if not candidats:
        types = ", ".join(sorted({coffre.NOMS_TYPES[s.type] for s in cap.manifeste.secrets}))
        raise ErreurIdentifiants(f"« {cap.titre} » attend : {types} (pas {coffre.NOMS_TYPES.get(type_ or '', type_)}).")
    return cap, candidats[0]


# --- lister --------------------------------------------------------------------------------------------------


def _carte(infos: dict[str, Any]) -> str | None:
    return f"••• {infos['fin']}" if infos.get("fin") else None


def lister() -> dict[str, Any]:
    """Entrées visibles (métadonnées, jamais de valeur) et secrets requis manquants des capacités actives."""
    qui = appelant()
    titres = {c.nom: c.titre for c in _capacites_chargees()}
    entrees = []
    for infos in coffre.decrire():
        if not peut_voir(infos, qui):
            continue
        requis: list[str] = []
        for cap in _capacites_chargees():
            if cap.nom in infos["utilise_par"] and cap.manifeste is not None:
                for s in cap.manifeste.secrets:
                    requis.extend(c for c in s.champs if c not in requis)
        ligne = {
            "cle": infos["cle"],
            "libelle": infos["libelle"],
            "type": infos["type"],
            "proprietaire": "moi" if infos["proprietaire"] == qui["linux_user"] else infos["proprietaire"],
            "champs_presents": infos["champs"],
            "champs_manquants": coffre.champs_manquants(infos["type"], infos["champs"], requis),
            "maj": infos["maj"],
            "utilise_par": [titres.get(n, n) for n in infos["utilise_par"]],
        }
        if infos["type"] == "carte_bancaire":
            ligne["carte"] = _carte(infos)
            ligne["virtuelle"] = infos.get("virtuelle", False)
        entrees.append(ligne)
    a_renseigner = []
    from tools import capacites

    for cap in _capacites_chargees():
        if cap.manifeste is None or not capacites.personne_autorisee(cap.manifeste, qui["linux_user"]):
            continue
        for s in cap.manifeste.secrets:
            try:
                cle = _cle_capacite(s, qui["linux_user"])
            except ErreurIdentifiants:
                continue
            infos = coffre.meta(cle)
            presents = (infos or {}).get("champs") or []
            manquants = coffre.champs_manquants(s.type, presents, s.champs)
            if manquants:
                a_renseigner.append({
                    "capacite": cap.titre, "cle": cle, "type": s.type, "champs_manquants": manquants,
                    "prepare": infos is not None,
                })
    return {
        "identifiants": entrees,
        "a_renseigner": a_renseigner,
        "saisie": "Les valeurs se saisissent hors conversation (identifiants_preparer donne la marche à suivre).",
    }


# --- préparer (créer ou modifier une entrée, sans valeur) ----------------------------------------------------


def _canal_web() -> bool:
    try:
        from tools import cartes

        return cartes.canal_courant() == "web"
    except ImportError:  # pragma: no cover - avant le lot 4a
        return os.environ.get("HOMEOS_CANAL", "").strip().lower() == "web"


def carte_saisie(infos: dict[str, Any]) -> dict[str, Any]:
    """Carte `identifiants` (contrat §14) : formulaire sécurisé de l'appli, soumis à `/api/coffre` sous step-up."""
    attendus, facultatifs = coffre.champs_du_type(infos["type"])
    champs = list(attendus) + list(facultatifs) or ["valeur"]
    for c in infos.get("champs_requis") or []:
        if c not in champs:
            champs.append(c)
    return {
        "type": "identifiants",
        "id": f"c-id-{coffre.slug(infos['cle'], 30)}",
        "titre": infos["libelle"],
        "donnees": {"cle": infos["cle"], "type": infos["type"], "champs": champs,
                    "facultatifs": list(facultatifs), "libelle": infos["libelle"],
                    **({"virtuelle": True} if infos["type"] == "carte_bancaire" else {})},
        "actions": [{"libelle": "Enregistrer", "action": "coffre_ecrire", "args": {"cle": infos["cle"]},
                     "confirmation": "step-up"}],
    }


def procedure(cle: str) -> str:
    return (
        "Pour ta sécurité, les valeurs se saisissent hors conversation : sur le serveur, "
        f"`{COMMANDE_SAISIE.format(cle=cle)}`"
    )


def _entree_existante(libelle: str, qui: dict[str, Any]) -> dict[str, Any] | None:
    cible = libelle.strip().casefold()
    for infos in coffre.decrire():
        if peut_voir(infos, qui) and cible in (infos["cle"], str(infos["libelle"]).casefold()):
            return infos
    return None


def preparer(libelle: str, type: str, proprietaire: str = "moi", capacite: str | None = None,
             virtuelle: bool | None = None) -> str:
    """Crée ou réserve une entrée (métadonnées seulement) et renvoie la procédure de saisie hors conversation."""
    verifier_arguments(libelle=libelle, type=type, proprietaire=proprietaire, capacite=capacite)
    qui = appelant()
    type_ = coffre.normaliser_type(type)
    if type_ == "carte_bancaire" and virtuelle is not True:
        return QUESTION_VIRTUELLE
    prop = str(proprietaire or "moi").strip().casefold()
    if prop in ("moi", "perso", "personnel", qui["linux_user"]):
        prop = qui["linux_user"]
    elif prop in ("foyer", "famille", "commun"):
        prop = coffre.PROPRIETAIRE_FOYER
    else:
        raise ErreurIdentifiants("Propriétaire : « moi » ou « foyer ».")
    meta: dict[str, Any] = {"type": type_, "libelle": libelle, "proprietaire": prop}
    if type_ == "carte_bancaire":
        meta["virtuelle"] = True
    requis: list[str] = []
    existante = _entree_existante(libelle, qui) if not capacite else None
    if capacite:
        cap, secret = _secret_pour(capacite, type_)
        cle = _cle_capacite(secret, prop)
        meta["utilise_par"] = [cap.nom]
        requis = list(secret.champs)
        existante = coffre.meta(cle)
    elif existante is not None:
        cle = existante["cle"]
    else:
        base = coffre.slug(libelle) or "entree"
        cle = f"{coffre.PREFIXE_LIBRE}{base}"
        autre = coffre.meta(cle)
        if autre is not None and not peut_modifier(autre, qui):
            cle = f"{cle}-{qui['linux_user']}"
    if existante is not None and not peut_modifier(existante, qui):
        raise ErreurIdentifiants(MESSAGE_RESERVE)
    if existante is not None:
        meta.pop("proprietaire")  # une modification ne change pas le propriétaire
    infos = coffre.preparer_entree(cle, meta)
    infos["champs_requis"] = requis
    attendus = coffre.champs_manquants(type_, [], requis)
    lignes = [
        f"Entrée « {infos['libelle']} » {'mise à jour' if existante else 'prête'} ({coffre.NOMS_TYPES[type_]}, "
        f"{'à toi' if infos['proprietaire'] == qui['linux_user'] else 'au foyer'}). "
        f"À saisir : {', '.join(attendus) or 'les valeurs'}"
        + (" (les valeurs déjà saisies seront remplacées)." if existante and existante.get("champs") else "."),
        procedure(cle),
    ]
    if _canal_web():
        from tools import cartes

        lignes.append("Dans l'appli, la carte sécurisée ci-dessous permet de les saisir directement.")
        lignes.append(cartes.ligne_carte(carte_saisie(infos)))
    return "\n".join(lignes)


# --- renommer, attribuer, supprimer : à valider par bouton ----------------------------------------------------


def _entree_modifiable(cle: str, qui: dict[str, Any]) -> dict[str, Any]:
    infos = coffre.meta(cle)
    if infos is None or str(infos["cle"]).startswith(coffre.PREFIXE_QUARANTAINE) or not peut_voir(infos, qui):
        raise ErreurIdentifiants("Je ne trouve pas cette entrée dans le coffre (identifiants_lister donne les clés).")
    if not peut_modifier(infos, qui):
        raise ErreurIdentifiants(MESSAGE_RESERVE)
    return infos


def _confirmation(kind: str, resume: str, qui: dict[str, Any], **donnees: Any) -> str:
    payload = {"action": kind, "demandeur": qui["linux_user"], "auteur": qui["prenom"], **donnees}
    aid = db.log_action(kind, resume[:300], payload, status="pending", acteur=qui["prenom"])
    return f"CONFIRMATION_REQUISE:{aid}:{resume[:300]}"


def renommer(cle: str, libelle: str) -> str:
    verifier_arguments(cle=cle, libelle=libelle)
    qui = appelant()
    infos = _entree_modifiable(cle, qui)
    nouveau = coffre._valider_meta({"libelle": libelle})["libelle"]
    return _confirmation("identifiants_renommer", f"Renommer « {infos['libelle']} » en « {nouveau} »", qui,
                         cle=infos["cle"], libelle=nouveau)


def attribuer(cle: str, capacite: str) -> str:
    verifier_arguments(cle=cle, capacite=capacite)
    qui = appelant()
    infos = _entree_modifiable(cle, qui)
    cap, secret = _secret_pour(capacite, infos["type"])
    cible = _cle_capacite(secret, infos["proprietaire"])
    manquants = coffre.champs_manquants(secret.type, infos["champs"], secret.champs)
    if manquants:
        raise ErreurIdentifiants(
            f"« {infos['libelle']} » n'est pas complète pour « {cap.titre} » : il manque {', '.join(manquants)}. "
            "Complète d'abord la saisie (identifiants_preparer)."
        )
    existante = coffre.meta(cible)
    if cible != infos["cle"] and existante is not None and existante["champs"]:
        raise ErreurIdentifiants(f"« {cap.titre} » a déjà ses identifiants : supprime-les d'abord pour les remplacer.")
    return _confirmation("identifiants_attribuer", f"Utiliser « {infos['libelle']} » pour « {cap.titre} »", qui,
                         cle=infos["cle"], cible=cible, capacite=cap.nom)


def supprimer(cle: str) -> str:
    verifier_arguments(cle=cle)
    qui = appelant()
    infos = _entree_modifiable(cle, qui)
    detail = f" ({_carte(infos)})" if _carte(infos) else ""
    return _confirmation("identifiants_supprimer", f"Supprimer « {infos['libelle']} »{detail} du coffre", qui,
                         cle=infos["cle"])


def ranger_quarantaine(id: str, cle: str, champ: str | None = None) -> str:
    """Prépare le rangement d'une donnée en quarantaine dans une entrée (bouton Valider ; la valeur ne passe
    jamais par le modèle)."""
    verifier_arguments(id=id, cle=cle, champ=champ)
    qui = appelant()
    info = coffre.quarantaine(id)
    if info.get("personne") != qui["linux_user"]:
        raise ErreurIdentifiants("Cette donnée a été envoyée par une autre personne : seule elle peut la ranger.")
    infos = _entree_modifiable(cle, qui)
    nature = str(info.get("nature") or "")
    if not champ:
        champ = CHAMP_PAR_NATURE.get(nature, {}).get(infos["type"])
        if not champ:
            raise ErreurIdentifiants(f"Dans quel champ de « {infos['libelle']} » ranger cette donnée ?")
    champ = coffre.valider_champ(champ)
    attendus, facultatifs = coffre.champs_du_type(infos["type"])
    if infos["type"] in coffre.TYPES_FERMES and champ not in (*attendus, *facultatifs):
        raise ErreurIdentifiants(f"Champs possibles pour « {infos['libelle']} » : {', '.join(attendus)}.")
    apercu = f" {info['apercu']}" if info.get("apercu") else ""
    nom_nature = nature.replace("_", " ")
    return _confirmation(
        "identifiants_ranger_quarantaine",
        f"Ranger la donnée reçue ({nom_nature}{apercu}) dans « {infos['libelle']} » (champ {champ})", qui,
        quarantaine=info["id"], cle=infos["cle"], champ=champ,
    )


# --- exécution (après Valider, tools.runner sous homeos) ------------------------------------------------------


def _verifier_droits_execution(payload: dict[str, Any], cle: str) -> dict[str, Any]:
    """Revérifie au moment du clic : l'entrée existe toujours et le demandeur a toujours le droit."""
    from tools import administration

    demandeur = str(payload.get("demandeur") or "")
    qui = administration.appelant(demandeur) if demandeur else None
    if qui is None:
        raise ErreurIdentifiants(MESSAGE_INCONNU)
    qui = {"linux_user": demandeur, "prenom": qui["prenom"], "role": qui["role"]}
    infos = coffre.meta(cle)
    if infos is None:
        raise ErreurIdentifiants("Cette entrée n'existe plus dans le coffre.")
    if not peut_modifier(infos, qui):
        raise ErreurIdentifiants(MESSAGE_RESERVE)
    return infos


def executer(kind: str, payload: dict[str, Any]) -> str:
    """Applique une action `identifiants_*` validée ; renvoie le message pour la personne (jamais de valeur)."""
    if isinstance(payload, str):
        payload = json.loads(payload or "{}")
    os.environ.setdefault("HOMEOS_WHO", str(payload.get("auteur") or "?"))  # coffre : « par » = l'auteur
    cle = str(payload.get("cle") or "")
    if kind == "identifiants_ranger_quarantaine":
        _verifier_droits_execution(payload, cle)
        infos = coffre.ranger_quarantaine(str(payload.get("quarantaine")), cle, str(payload.get("champ")),
                                          str(payload.get("demandeur")))
        detail = f" ({_carte(infos)})" if infos["type"] == "carte_bancaire" and _carte(infos) else ""
        return f"Fait. Rangé dans « {infos['libelle']} »{detail}, champ {payload.get('champ')}. Rien n'est resté dans la conversation."
    infos = _verifier_droits_execution(payload, cle)
    if kind == "identifiants_renommer":
        nouveau = coffre.modifier_meta(cle, libelle=payload.get("libelle"))
        return f"Fait. « {infos['libelle']} » s'appelle maintenant « {nouveau['libelle']} »."
    if kind == "identifiants_attribuer":
        cap, secret = _secret_pour(str(payload.get("capacite")), infos["type"])
        manquants = coffre.champs_manquants(secret.type, infos["champs"], secret.champs)
        if manquants:
            raise ErreurIdentifiants(f"Il manque {', '.join(manquants)} : rien n'a été changé.")
        nouveau = coffre.deplacer(cle, str(payload.get("cible")), {"utilise_par": [cap.nom]})
        return f"Fait. « {nouveau['libelle']} » sert maintenant à « {cap.titre} »."
    if kind == "identifiants_supprimer":
        coffre.supprimer(cle)
        return f"Fait. « {infos['libelle']} » est supprimée du coffre."
    raise ErreurIdentifiants(f"Action inconnue : « {kind} ».")


def executeur(kind: str) -> Callable[[dict[str, Any]], str]:
    return functools.partial(executer, kind)


# --- saisie par l'appli web (carte sécurisée) ----------------------------------------------------------------


def enregistrer_saisie(cle: str, champs: dict[str, str], linux_user: str) -> dict[str, Any]:
    """Pour `POST /api/coffre` (appli web, step-up) : écrit les valeurs saisies dans la carte sécurisée.

    Contrôle le droit de la personne sur l'entrée (préparée en conversation) et les valeurs selon le type ; les
    valeurs remplacent les précédentes. Renvoie `{ok, champs}` (noms seulement). Jamais de valeur journalisée."""
    from tools import administration

    qui_brut = administration.appelant(linux_user)
    if qui_brut is None:
        raise ErreurIdentifiants(MESSAGE_INCONNU)
    qui = {"linux_user": linux_user, "prenom": qui_brut["prenom"], "role": qui_brut["role"]}
    infos = coffre.meta(cle)
    if infos is None or str(infos["cle"]).startswith(coffre.PREFIXE_QUARANTAINE):
        raise ErreurIdentifiants("Entrée inconnue : prépare-la d'abord en conversation.")
    if not peut_modifier(infos, qui):
        raise ErreurIdentifiants(MESSAGE_RESERVE)
    valeurs = {str(k): str(v) for k, v in (champs or {}).items() if str(v)}
    coffre.ecrire(infos["cle"], valeurs, fusion=infos["type"] == "identifiants")
    db.log_action("identifiants_saisie", f"valeurs saisies pour « {infos['libelle']} »",
                  {"cle": infos["cle"], "champs": sorted(valeurs)}, acteur=qui["prenom"])
    return {"ok": True, "champs": sorted(valeurs)}
