"""Coffre à secrets du foyer : identifiants des services utilisés par les capacités (Planity, cantine…).

API unique (indépendante du backend) :

    lire(cle, champs=None) -> dict[str, str]   # lève SecretManquant (message utilisateur clair)
    ecrire(cle, champs: dict[str, str], fusion=False)
    supprimer(cle) -> bool
    lister() -> list[str]
    valeurs_connues() -> set[str]               # pour le masquage des sorties d'outils
    masquer(texte) -> str                       # remplace toute valeur connue par « ••• »

Les capacités ne manipulent que des références `secret://<cle>` (ex. `secret://planity/alex`) ;
seul ce module voit les valeurs. Une clé est en minuscules : `<capacite>[/<suite>]`, ex. `planity/alex`.

Entrée enrichie (lot 5c « identifiants en conversation ») : à côté des `champs` (les seules valeurs secrètes), des
métadonnées qu'on peut montrer au modèle : `type` (identifiants | carte_bancaire | code | cle_api | autre),
`libelle` (nom lisible donné par le foyer), `proprietaire` (linux_user ou `foyer`), `utilise_par` (capacités),
`cree`, `maj`, `par`, et pour une carte `virtuelle` (obligatoirement vraie) et `fin` (4 derniers chiffres, calculés à
l'écriture). Les métadonnées sont chiffrées avec le reste du fichier. Une entrée sans métadonnées (coffre du lot 5b)
est complétée à la lecture (`type: identifiants`, propriétaire déduit de la clé, sinon `foyer`) et réécrite au
format enrichi à la prochaine écriture. Les clés hors capacité vivent sous `libre/<slug>` ; les valeurs mises en
quarantaine par le filtre de données sensibles (`coeur.filtre_sensible`) sous `quarantaine/<id>`, 10 minutes.

    preparer_entree(cle, meta) / modifier_meta(cle, …) / deplacer(cle, nouvelle) / meta(cle)
    mettre_en_quarantaine(valeur, personne, nature, apercu) -> id ; quarantaine(id) ; purger_quarantaine()

Backends (`config.yaml` → `coffre: {backend: fichier}`) :
- `fichier` (défaut) : `/var/lib/homeos/coffre.json` (lot 5d ; `data/coffre.json` en développement ou sur un
  VPS non migré), chiffré en AES-256-GCM avec une clé dérivée (scrypt) de `COFFRE_CLE`
  (`/etc/homeos/coffre.env`, générée par install.sh). Mode 640, réservé à `homeos` : les sessions Claude
  des personnes ne peuvent lire ni le fichier ni la clé ; en conversation, seul le serveur d'outils (lot 7, sous
  homeos, identité par jeton) le lit. Écriture atomique
  sous verrou (`data/.coffre.lock`). Ce backend est aussi le repli et le cache local des futurs backends.
- `vaultwarden` : non implémenté dans ce lot (voir `docs/coffre.md`) → `NotImplementedError` explicite.

CLI (sur le serveur, en `homeos`, cwd = racine du dépôt) — aucune valeur n'est jamais passée en
argument ni affichée, sauf `get --revele` :

    python -m tools.coffre set <cle> [--champs identifiant,mot_de_passe] [--type T] [--libelle L]
                                     [--proprietaire moi|foyer|<linux_user>] [--virtuelle]   # saisie masquée
    python -m tools.coffre get <cle> [--champs] [--revele]
    python -m tools.coffre rm <cle> [--oui]
    python -m tools.coffre ls
    python -m tools.coffre statut
    python -m tools.coffre purger            # quarantaines expirées (healthcheck)

Sur le serveur, `homeos-coffre` (install/bin, copié dans /usr/local/bin) lance cette CLI depuis /srv/home-os :
`sudo -u homeos homeos-coffre set <cle>`.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import getpass
import hashlib
import json
import os
import re
import secrets as _aleatoire
import sys
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, TypeVar

from tools.common import etat_dir, get_logger, iso_now, load_config, load_secrets, now, who

log = get_logger(__name__)

FORMAT = "homeos-coffre"
VERSION = 1
NOM_FICHIER = "coffre.json"
NOM_VERROU = ".coffre.lock"
MODE_FICHIER = 0o640  # homeos (bot, runner, CLI) ; groupe homeos depuis le lot 5d (install.sh)
VARIABLE_CLE = "COFFRE_CLE"
LONGUEUR_MIN_CLE = 16
MASQUE = "•••"
LONGUEUR_MIN_MASQUAGE = 4  # une valeur plus courte (« 1 », « ab ») ne peut pas être masquée utilement
LONGUEUR_MAX_VALEUR = 4096
CHAMPS_DEFAUT = ("identifiant", "mot_de_passe")
PREFIXE_REFERENCE = "secret://"

# scrypt : ~0,1 s et 32 Mio par dérivation, mise en cache par processus. Résiste à une COFFRE_CLE
# choisie à la main (faible) ; la clé générée par install.sh fait déjà 256 bits.
KDF_DEFAUT: dict[str, Any] = {"nom": "scrypt", "n": 2**15, "r": 8, "p": 1}
KDF_MAXMEM = 128 * 1024 * 1024

RE_CLE = re.compile(r"^[a-z0-9_]+(?:/[a-z0-9_.-]+)*$")
RE_CHAMP = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
LONGUEUR_MAX_CLE = 120

# --- types d'entrée (lot 5c) -------------------------------------------------------------
TYPES = ("identifiants", "carte_bancaire", "code", "cle_api", "autre")
TYPE_DEFAUT = "identifiants"
# type → (champs attendus, champs facultatifs) ; « autre » : champs libres.
CHAMPS_TYPES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "identifiants": (("identifiant", "mot_de_passe"), ("url",)),
    "carte_bancaire": (("titulaire", "numero", "expiration", "cvc"), ()),
    "code": (("code",), ()),
    "cle_api": (("cle",), ()),
    "autre": ((), ()),
}
# Types dont les champs sont fermés : aucun autre nom de champ n'est accepté.
TYPES_FERMES = ("carte_bancaire", "code", "cle_api")
NOMS_TYPES = {
    "identifiants": "identifiants",
    "carte_bancaire": "carte bancaire",
    "code": "code",
    "cle_api": "clé d'API",
    "autre": "autre",
}
PROPRIETAIRE_FOYER = "foyer"
PREFIXE_LIBRE = "libre/"
PREFIXE_QUARANTAINE = "quarantaine/"
DUREE_QUARANTAINE = timedelta(minutes=10)
LONGUEUR_MAX_LIBELLE = 80
RE_PROPRIETAIRE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
RE_CAPACITE = re.compile(r"^[a-z0-9_]{1,40}$")
META_MODIFIABLES = ("type", "libelle", "proprietaire", "utilise_par", "virtuelle")
MESSAGE_CARTE_VIRTUELLE = (
    "Seule une carte virtuelle à plafond est acceptée dans le coffre (jamais la carte physique) : "
    "confirme qu'il s'agit d'une carte virtuelle plafonnée (--virtuelle)."
)


# --- erreurs ---------------------------------------------------------------------


class ErreurCoffre(RuntimeError):
    """Erreur du coffre ; le message (en français) ne contient jamais de valeur de secret."""


class CleCoffreAbsente(ErreurCoffre):
    """`COFFRE_CLE` absente ou trop courte."""


class CoffreInaccessible(ErreurCoffre):
    """Le fichier du coffre existe mais l'utilisateur courant n'a pas le droit de le lire (attendu hors homeos)."""


class CoffreIllisible(ErreurCoffre):
    """Déchiffrement impossible : mauvaise clé, fichier altéré ou format inconnu."""


class SecretManquant(ErreurCoffre):
    """Secret (ou champ) non renseigné ; le message invite à `/identifiants <capacite>`."""

    def __init__(self, cle: str, champs_manquants: list[str] | None = None) -> None:
        self.cle = cle
        self.capacite = cle.split("/", 1)[0]
        self.champs_manquants = list(champs_manquants or [])
        titre = self.capacite.replace("_", " ").capitalize()
        detail = f" (champs manquants : {', '.join(self.champs_manquants)})" if self.champs_manquants else ""
        super().__init__(f"Identifiants {titre} à renseigner{detail} : /identifiants {self.capacite}")


# --- validation --------------------------------------------------------------------


def normaliser_cle(cle: str) -> str:
    """`secret://Planity/Alex` → `planity/alex` ; lève ErreurCoffre si la clé est invalide."""
    texte = str(cle or "").strip()
    if texte.lower().startswith(PREFIXE_REFERENCE):
        texte = texte[len(PREFIXE_REFERENCE) :]
    texte = texte.strip("/").lower()
    if not texte or len(texte) > LONGUEUR_MAX_CLE or not RE_CLE.match(texte) or ".." in texte:
        raise ErreurCoffre(
            "Clé de coffre invalide : attendu « capacite/suite » en minuscules (lettres, chiffres, _ . -), "
            "ex. planity/alex."
        )
    return texte


def valider_champ(nom: str) -> str:
    nom = str(nom or "").strip().lower()
    if not RE_CHAMP.match(nom):
        raise ErreurCoffre(f"Nom de champ invalide : attendu [a-z][a-z0-9_]* (ex. identifiant, mot_de_passe), reçu {len(nom)} caractère(s).")
    return nom


def _valider_champs(champs: dict[str, str]) -> dict[str, str]:
    if not isinstance(champs, dict) or not champs:
        raise ErreurCoffre("Aucun champ à enregistrer.")
    propres: dict[str, str] = {}
    for nom, valeur in champs.items():
        n = valider_champ(nom)
        if not isinstance(valeur, str) or not valeur:
            raise ErreurCoffre(f"Valeur vide ou non textuelle pour le champ « {n} ».")
        if len(valeur) > LONGUEUR_MAX_VALEUR:
            raise ErreurCoffre(f"Valeur trop longue pour le champ « {n} » (max {LONGUEUR_MAX_VALEUR}).")
        propres[n] = valeur
    return propres


def normaliser_type(type_: Any) -> str:
    """« Carte bancaire », « carte », « cb » → `carte_bancaire` ; lève ErreurCoffre si le type est inconnu."""
    texte = unicodedata.normalize("NFKD", str(type_ or "").strip().casefold())
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    texte = re.sub(r"[\s'’-]+", "_", texte)
    alias = {
        "identifiant": "identifiants", "login": "identifiants", "compte": "identifiants",
        "carte": "carte_bancaire", "cb": "carte_bancaire", "carte_bleue": "carte_bancaire",
        "carte_de_paiement": "carte_bancaire", "code_pin": "code", "pin": "code", "codes": "code",
        "cle": "cle_api", "cle_d_api": "cle_api", "api": "cle_api", "jeton": "cle_api",
    }
    texte = alias.get(texte, texte)
    if texte not in TYPES:
        raise ErreurCoffre(f"Type d'entrée inconnu : attendu {', '.join(TYPES)}.")
    return texte


def champs_du_type(type_: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(champs attendus, champs facultatifs) d'un type d'entrée."""
    return CHAMPS_TYPES.get(type_, ((), ()))


def luhn_valide(numero: str) -> bool:
    """Contrôle de Luhn d'un numéro de carte (chiffres seulement)."""
    if not numero.isdigit():
        return False
    total = 0
    for i, chiffre in enumerate(reversed(numero)):
        n = int(chiffre)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _expiration(valeur: str) -> str:
    """« 7/28 », « 07 / 2028 » → « 07/28 » ; lève ErreurCoffre (format, carte expirée) sans jamais citer la valeur."""
    m = re.fullmatch(r"\s*(\d{1,2})\s*/\s*(\d{2}|\d{4})\s*", valeur)
    if not m or not 1 <= int(m.group(1)) <= 12:
        raise ErreurCoffre("Date d'expiration invalide : attendu MM/AA (ex. 07/28). Rien n'a été enregistré.")
    mois, annee = int(m.group(1)), int(m.group(2))
    annee = annee + 2000 if annee < 100 else annee
    aujourdhui = now()
    if (annee, mois) < (aujourdhui.year, aujourdhui.month):
        raise ErreurCoffre("Cette carte est expirée : rien n'a été enregistré.")
    return f"{mois:02d}/{annee % 100:02d}"


def valider_valeurs(type_: str, champs: dict[str, str]) -> dict[str, str]:
    """Contrôle et normalise les valeurs selon le type (numéro par Luhn, expiration MM/AA, cryptogramme…).

    Ne contrôle pas la complétude (une saisie peut se faire champ par champ) : voir `champs_manquants`.
    Les messages d'erreur citent le nom du champ, jamais sa valeur."""
    propres = _valider_champs(champs)
    attendus, facultatifs = champs_du_type(type_)
    if type_ in TYPES_FERMES:
        inconnus = sorted(set(propres) - set(attendus) - set(facultatifs))
        if inconnus:
            raise ErreurCoffre(
                f"Champ(s) inattendu(s) pour {NOMS_TYPES[type_]} : {', '.join(inconnus)} "
                f"(attendus : {', '.join(attendus)})."
            )
    if type_ == "carte_bancaire":
        if "numero" in propres:
            numero = re.sub(r"[\s.-]", "", propres["numero"])
            if not (13 <= len(numero) <= 19) or not luhn_valide(numero):
                raise ErreurCoffre(
                    "Numéro de carte invalide (13 à 19 chiffres, contrôle de Luhn) : rien n'a été enregistré."
                )
            propres["numero"] = numero
        if "expiration" in propres:
            propres["expiration"] = _expiration(propres["expiration"])
        if "cvc" in propres:
            cvc = propres["cvc"].strip()
            if not re.fullmatch(r"\d{3,4}", cvc):
                raise ErreurCoffre("Cryptogramme invalide (3 ou 4 chiffres) : rien n'a été enregistré.")
            propres["cvc"] = cvc
        if "titulaire" in propres:
            titulaire = " ".join(propres["titulaire"].split())
            if not re.fullmatch(r"[^\d\n]{2,80}", titulaire):
                raise ErreurCoffre("Titulaire invalide (2 à 80 lettres, sans chiffre) : rien n'a été enregistré.")
            propres["titulaire"] = titulaire
    if type_ == "identifiants" and "url" in propres:
        url = propres["url"].strip()
        if not re.fullmatch(r"https?://[^\s]{3,}", url):
            raise ErreurCoffre("Adresse du site invalide (https://…) : rien n'a été enregistré.")
        propres["url"] = url
    return propres


def champs_manquants(type_: str, presents: Iterable[str], requis: Iterable[str] = ()) -> list[str]:
    """Champs attendus (par le type et, en plus, par une capacité) absents de l'entrée, dans l'ordre."""
    attendus = list(champs_du_type(type_)[0])
    for c in requis:
        if c not in attendus:
            attendus.append(c)
    deja = set(presents)
    return [c for c in attendus if c not in deja]


def slug(texte: str, longueur: int = 40) -> str:
    """« Cantine Villeneuve » → « cantine-villeneuve » (segment de clé du coffre)."""
    t = unicodedata.normalize("NFKD", str(texte or "").casefold())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return t[:longueur].strip("-")


def _personnes_config() -> set[str]:
    try:
        personnes = load_config().get("personnes") or []
    except Exception:  # noqa: BLE001 - configuration illisible : migration prudente (foyer)
        return set()
    return {str(p.get("linux_user")) for p in personnes if isinstance(p, dict) and p.get("linux_user")}


def proprietaire_deduit(cle: str, personnes: set[str] | None = None) -> str:
    """Migration : `planity/alex` → `alex` si c'est une personne du foyer, sinon `foyer`."""
    personnes = _personnes_config() if personnes is None else personnes
    dernier = cle.rsplit("/", 1)[-1] if "/" in cle else ""
    return dernier if dernier in personnes else PROPRIETAIRE_FOYER


def libelle_deduit(cle: str) -> str:
    base = cle.removeprefix(PREFIXE_LIBRE)
    return base.replace("/", " · ").replace("_", " ").replace("-", " ").strip().capitalize() or cle


def utilise_par_deduit(cle: str) -> list[str]:
    if cle.startswith((PREFIXE_LIBRE, PREFIXE_QUARANTAINE)):
        return []
    return [cle.split("/", 1)[0]]


def _valider_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Métadonnées modifiables, validées (aucune ne peut contenir de valeur secrète par construction)."""
    propres: dict[str, Any] = {}
    for nom, valeur in (meta or {}).items():
        if nom not in META_MODIFIABLES:
            raise ErreurCoffre(f"Métadonnée inconnue « {nom} » (attendu : {', '.join(META_MODIFIABLES)}).")
        if valeur is None:
            continue
        if nom == "type":
            propres[nom] = normaliser_type(valeur)
        elif nom == "libelle":
            texte = " ".join(str(valeur).split())
            if not texte or len(texte) > LONGUEUR_MAX_LIBELLE:
                raise ErreurCoffre(f"Nom de l'entrée vide ou trop long ({LONGUEUR_MAX_LIBELLE} caractères au plus).")
            propres[nom] = texte
        elif nom == "proprietaire":
            texte = str(valeur).strip().lower()
            if not RE_PROPRIETAIRE.match(texte):
                raise ErreurCoffre("Propriétaire invalide : une personne du foyer (linux_user) ou « foyer ».")
            propres[nom] = texte
        elif nom == "utilise_par":
            liste = [str(v).strip() for v in (valeur if isinstance(valeur, (list, tuple, set)) else [valeur])]
            if not all(RE_CAPACITE.match(v) for v in liste):
                raise ErreurCoffre("utilise_par : noms de capacités attendus (minuscules, chiffres, _).")
            propres[nom] = sorted(set(liste))
        elif nom == "virtuelle":
            if not isinstance(valeur, bool):
                raise ErreurCoffre("virtuelle : vrai ou faux attendu.")
            propres[nom] = valeur
    return propres


def normaliser_entree(cle: str, entree: Any, personnes: set[str] | None = None) -> dict[str, Any]:
    """Entrée au format enrichi ; complète une entrée du lot 5b (migration) sans jamais toucher aux valeurs."""
    entree = dict(entree) if isinstance(entree, dict) else {}
    champs = entree.get("champs")
    entree["champs"] = dict(champs) if isinstance(champs, dict) else {}
    if entree.get("type") not in TYPES:
        entree["type"] = TYPE_DEFAUT
    if not isinstance(entree.get("libelle"), str) or not entree["libelle"].strip():
        entree["libelle"] = libelle_deduit(cle)
    if not isinstance(entree.get("proprietaire"), str) or not entree["proprietaire"]:
        entree["proprietaire"] = proprietaire_deduit(cle, personnes)
    if not isinstance(entree.get("utilise_par"), list):
        entree["utilise_par"] = utilise_par_deduit(cle)
    entree.setdefault("cree", entree.get("maj"))
    if entree["type"] == "carte_bancaire":
        numero = entree["champs"].get("numero")
        if isinstance(numero, str) and len(numero) >= 4:
            entree["fin"] = numero[-4:]
    return entree


def meta_publique(cle: str, entree: dict[str, Any]) -> dict[str, Any]:
    """Ce qu'on peut montrer d'une entrée : métadonnées et NOMS des champs, jamais une valeur."""
    infos = {
        "cle": cle,
        "type": entree.get("type"),
        "libelle": entree.get("libelle"),
        "proprietaire": entree.get("proprietaire"),
        "utilise_par": list(entree.get("utilise_par") or []),
        "champs": sorted((entree.get("champs") or {}).keys()),
        "cree": entree.get("cree"),
        "maj": entree.get("maj"),
        "par": entree.get("par"),
    }
    if entree.get("type") == "carte_bancaire":
        infos["virtuelle"] = entree.get("virtuelle") is True
        if entree.get("fin"):
            infos["fin"] = str(entree["fin"])
    return infos


# --- primitives de chiffrement (backend fichier) -------------------------------------


def _b64(octets: bytes) -> str:
    return base64.b64encode(octets).decode("ascii")


def _deb64(texte: Any) -> bytes:
    if not isinstance(texte, str):
        raise CoffreIllisible("Coffre illisible : format inconnu.")
    try:
        return base64.b64decode(texte, validate=True)
    except (ValueError, TypeError):
        raise CoffreIllisible("Coffre illisible : format inconnu.") from None


_CACHE_CLES: dict[tuple[str, bytes, int, int, int], bytes] = {}


def deriver_cle(phrase: str, kdf: dict[str, Any]) -> bytes:
    """Clé AES-256 dérivée de `COFFRE_CLE` (scrypt, sel aléatoire par écriture), mise en cache."""
    if kdf.get("nom") != "scrypt":
        raise CoffreIllisible("Coffre illisible : dérivation de clé inconnue.")
    try:
        n, r, p = int(kdf["n"]), int(kdf["r"]), int(kdf["p"])
        sel = _deb64(kdf["sel"])
    except (KeyError, TypeError, ValueError):
        raise CoffreIllisible("Coffre illisible : paramètres de dérivation invalides.") from None
    if not (2**10 <= n <= 2**20 and 1 <= r <= 32 and 1 <= p <= 16 and len(sel) >= 16):
        raise CoffreIllisible("Coffre illisible : paramètres de dérivation hors limites.")
    empreinte = hashlib.sha256(phrase.encode("utf-8")).hexdigest()  # la phrase elle-même n'est pas gardée
    cle_cache = (empreinte, sel, n, r, p)
    if cle_cache not in _CACHE_CLES:
        _CACHE_CLES[cle_cache] = hashlib.scrypt(phrase.encode("utf-8"), salt=sel, n=n, r=r, p=p, maxmem=KDF_MAXMEM, dklen=32)
    return _CACHE_CLES[cle_cache]


def _aad(entete: dict[str, Any]) -> bytes:
    """Données associées authentifiées : format, version et paramètres de dérivation."""
    return json.dumps(
        {"format": entete.get("format"), "version": entete.get("version"), "kdf": entete.get("kdf")},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def chiffrer(contenu: dict[str, Any], phrase: str, kdf: dict[str, Any] | None = None) -> dict[str, Any]:
    """Chiffre `contenu` (JSON) → enveloppe sérialisable (AES-256-GCM, nonce et sel neufs)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    parametres = dict(kdf or KDF_DEFAUT)
    parametres["sel"] = _b64(_aleatoire.token_bytes(16))
    entete: dict[str, Any] = {"format": FORMAT, "version": VERSION, "kdf": parametres, "chiffrement": "AES-256-GCM"}
    nonce = _aleatoire.token_bytes(12)
    clair = json.dumps(contenu, ensure_ascii=False, sort_keys=True).encode("utf-8")
    chiffre = AESGCM(deriver_cle(phrase, parametres)).encrypt(nonce, clair, _aad(entete))
    return {**entete, "nonce": _b64(nonce), "donnees": _b64(chiffre)}


def dechiffrer(enveloppe: dict[str, Any], phrase: str) -> dict[str, Any]:
    """Inverse de `chiffrer` ; lève CoffreIllisible (mauvaise clé, altération, format)."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not isinstance(enveloppe, dict) or enveloppe.get("format") != FORMAT:
        raise CoffreIllisible("Coffre illisible : ce n'est pas un coffre Firmin Assistant.")
    if enveloppe.get("version") != VERSION or enveloppe.get("chiffrement") != "AES-256-GCM":
        raise CoffreIllisible("Coffre illisible : version ou chiffrement non pris en charge.")
    kdf = enveloppe.get("kdf")
    if not isinstance(kdf, dict):
        raise CoffreIllisible("Coffre illisible : paramètres de dérivation absents.")
    nonce = _deb64(enveloppe.get("nonce"))
    chiffre = _deb64(enveloppe.get("donnees"))
    if len(nonce) != 12:
        raise CoffreIllisible("Coffre illisible : nonce invalide.")
    try:
        clair = AESGCM(deriver_cle(phrase, kdf)).decrypt(nonce, chiffre, _aad(enveloppe))
    except InvalidTag:
        raise CoffreIllisible(
            "Coffre illisible : COFFRE_CLE incorrecte ou fichier coffre.json altéré (restaurer depuis la sauvegarde)."
        ) from None
    try:
        contenu = json.loads(clair.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CoffreIllisible("Coffre illisible : contenu déchiffré invalide.") from None
    if not isinstance(contenu, dict) or not isinstance(contenu.get("secrets"), dict):
        raise CoffreIllisible("Coffre illisible : structure inattendue.")
    return contenu


# --- backends ------------------------------------------------------------------------------


T = TypeVar("T")


class Backend(Protocol):
    nom: str

    def lire_entree(self, cle: str) -> dict[str, Any] | None: ...
    def ecrire(self, cle: str, champs: dict[str, str], fusion: bool = False,
               meta: dict[str, Any] | None = None) -> dict[str, Any]: ...
    def supprimer(self, cle: str) -> bool: ...
    def entrees(self) -> dict[str, dict[str, Any]]: ...
    def modifier(self, fn: Callable[[dict[str, dict[str, Any]]], T]) -> T: ...


def phrase_coffre() -> str:
    """`COFFRE_CLE` (environnement, sinon /etc/homeos/coffre.env ou data/secrets.env). Lève CleCoffreAbsente."""
    phrase = os.environ.get(VARIABLE_CLE) or load_secrets().get(VARIABLE_CLE, "")
    phrase = phrase.strip()
    if len(phrase) < LONGUEUR_MIN_CLE:
        raise CleCoffreAbsente(
            f"{VARIABLE_CLE} absente ou trop courte (/etc/homeos/coffre.env, lisible par homeos) : relancer install.sh (qui la génère) "
            "ou l'ajouter à la main (openssl rand -hex 32)."
        )
    return phrase


class BackendFichier:
    """`coffre.json` chiffré (AES-256-GCM, clé dérivée de COFFRE_CLE), dans le dossier d'état (`tools.common.etat_dir`)."""

    nom = "fichier"

    def __init__(self, dossier: Path | None = None) -> None:
        self.dossier = dossier or etat_dir()

    @property
    def chemin(self) -> Path:
        return self.dossier / NOM_FICHIER

    # -- lecture
    def _charger(self) -> dict[str, Any]:
        if not self.chemin.exists():
            return {"secrets": {}}
        try:
            enveloppe = json.loads(self.chemin.read_text(encoding="utf-8"))
        except PermissionError:
            raise CoffreInaccessible(f"Coffre illisible pour l'utilisateur courant (droits de {self.chemin}).") from None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise CoffreIllisible("Coffre illisible : fichier coffre.json corrompu (restaurer depuis la sauvegarde).") from None
        contenu = dechiffrer(enveloppe, phrase_coffre())
        # Migration douce (lot 5c) : métadonnées complétées en mémoire, écrites à la prochaine écriture.
        personnes = _personnes_config()
        contenu["secrets"] = {
            cle: normaliser_entree(cle, entree, personnes) for cle, entree in contenu["secrets"].items()
        }
        return contenu

    def entrees(self) -> dict[str, dict[str, Any]]:
        return dict(self._charger()["secrets"])

    def lire_entree(self, cle: str) -> dict[str, Any] | None:
        entree = self._charger()["secrets"].get(cle)
        return entree if isinstance(entree, dict) else None

    # -- écriture
    @contextmanager
    def _verrou(self) -> Iterator[None]:
        self.dossier.mkdir(parents=True, exist_ok=True)
        chemin = self.dossier / NOM_VERROU
        ancien = os.umask(0o007)
        try:
            fd = os.open(chemin, os.O_RDWR | os.O_CREAT, 0o660)
        finally:
            os.umask(ancien)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _sauver(self, contenu: dict[str, Any]) -> None:
        """Écriture atomique : fichier temporaire 600 → 640 + groupe foyer → fsync → os.replace."""
        enveloppe = chiffrer(contenu, phrase_coffre())
        tmp = self.chemin.with_name(f".{NOM_FICHIER}.{os.getpid()}.tmp")
        ancien = os.umask(0o077)
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(enveloppe, fh, indent=1)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
        finally:
            os.umask(ancien)
        try:
            os.chmod(tmp, MODE_FICHIER)
            try:
                import grp

                os.chown(tmp, -1, grp.getgrnam("foyer").gr_gid)
            except (KeyError, PermissionError, OSError, ImportError):
                pass
            os.replace(tmp, self.chemin)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        try:
            fd = os.open(self.dossier, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass

    def modifier(self, fn: Callable[[dict[str, dict[str, Any]]], T]) -> T:
        """Lecture-modification-écriture sous verrou ; les quarantaines expirées sont purgées au passage.

        Si `fn` lève, rien n'est écrit."""
        with self._verrou():
            contenu = self._charger()
            resultat = fn(contenu["secrets"])
            _purger(contenu["secrets"])
            self._sauver(contenu)
        return resultat

    def ecrire(self, cle: str, champs: dict[str, str], fusion: bool = False,
               meta: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.modifier(lambda secrets: _appliquer_ecriture(secrets, cle, champs, fusion, meta))

    def supprimer(self, cle: str) -> bool:
        def retirer(secrets: dict[str, dict[str, Any]]) -> bool:
            return secrets.pop(cle, None) is not None

        if cle not in self._charger()["secrets"]:
            return False
        return self.modifier(retirer)


class BackendVaultwarden:
    """Vaultwarden (API Bitwarden, chiffrement côté client) : pas encore implémenté.

    Voir docs/coffre.md (« Backend vaultwarden ») pour ce qu'il reste à faire.
    """

    nom = "vaultwarden"

    def __init__(self) -> None:
        raise NotImplementedError(
            "Backend de coffre « vaultwarden » pas encore implémenté (lot 5b) : le déchiffrement côté client "
            "des items Bitwarden est décrit dans docs/coffre.md. En attendant, mettre `coffre: {backend: fichier}` "
            "dans data/config.yaml (backend par défaut)."
        )


BACKENDS: dict[str, type] = {"fichier": BackendFichier, "vaultwarden": BackendVaultwarden}


def nom_backend() -> str:
    conf = load_config().get("coffre") or {}
    nom = conf.get("backend") if isinstance(conf, dict) else None
    return str(nom or "fichier").strip().lower()


def backend() -> Backend:
    nom = nom_backend()
    if nom not in BACKENDS:
        raise ErreurCoffre(f"Backend de coffre inconnu « {nom} » (config.yaml → coffre.backend : fichier | vaultwarden).")
    return BACKENDS[nom]()


# --- API publique ------------------------------------------------------------------------------


def lire(cle: str, champs: list[str] | tuple[str, ...] | None = None) -> dict[str, str]:
    """Champs du secret `cle` (référence `secret://…` acceptée).

    Lève `SecretManquant` si le secret est absent ou s'il manque un des `champs` demandés.
    """
    cle = normaliser_cle(cle)
    entree = backend().lire_entree(cle)
    valeurs = dict((entree or {}).get("champs") or {})
    if not valeurs:
        raise SecretManquant(cle, list(champs or []))
    if champs:
        manquants = [valider_champ(c) for c in champs if valider_champ(c) not in valeurs]
        if manquants:
            raise SecretManquant(cle, manquants)
    return valeurs


def _nouvelle_entree(cle: str) -> dict[str, Any]:
    entree = normaliser_entree(cle, {"cree": iso_now()})
    entree["maj"], entree["par"] = entree["cree"], who()
    return entree


def _finaliser(entree: dict[str, Any]) -> None:
    """Règles qui dépendent du type : une carte est virtuelle (obligatoire) et garde ses 4 derniers chiffres."""
    if entree.get("type") == "carte_bancaire":
        if entree.get("virtuelle") is not True:
            raise ErreurCoffre(MESSAGE_CARTE_VIRTUELLE)
        numero = (entree.get("champs") or {}).get("numero")
        if isinstance(numero, str) and len(numero) >= 4:
            entree["fin"] = numero[-4:]
        else:
            entree.pop("fin", None)
    else:
        entree.pop("fin", None)
        entree.pop("virtuelle", None)


def _changement_de_type(entree: dict[str, Any], type_: str) -> None:
    if type_ != entree.get("type") and entree.get("champs"):
        raise ErreurCoffre(
            f"Cette entrée contient déjà des valeurs de type {NOMS_TYPES.get(str(entree.get('type')), '?')} : "
            "supprime-la d'abord pour en changer le type."
        )


def _appliquer_ecriture(
    secrets: dict[str, dict[str, Any]], cle: str, champs: dict[str, str], fusion: bool, meta: dict[str, Any] | None
) -> dict[str, Any]:
    existant = secrets.get(cle)
    entree = normaliser_entree(cle, existant) if existant else _nouvelle_entree(cle)
    changements = _valider_meta(meta or {})
    type_ = changements.get("type", entree["type"])
    if fusion:
        _changement_de_type(entree, type_)
    entree.update(changements)
    nouveaux = valider_valeurs(type_, champs)
    entree["champs"] = {**(entree["champs"] if fusion else {}), **nouveaux}
    _finaliser(entree)
    entree["maj"], entree["par"] = iso_now(), who()
    secrets[cle] = entree
    return entree


def ecrire(cle: str, champs: dict[str, str], fusion: bool = False, meta: dict[str, Any] | None = None) -> None:
    """Enregistre `champs` sous `cle` (remplace les valeurs, ou les complète si `fusion`).

    Les métadonnées d'une entrée existante sont conservées ; `meta` (type, libelle, proprietaire, utilise_par,
    virtuelle) les complète. Les valeurs sont contrôlées selon le type (carte : Luhn, MM/AA, cryptogramme)."""
    cle = normaliser_cle(cle)
    backend().ecrire(cle, _valider_champs(champs), fusion=fusion, meta=meta)
    log.info("coffre : secret %s enregistré (%d champ(s)) par %s", cle, len(champs), who())


def preparer_entree(cle: str, meta: dict[str, Any]) -> dict[str, Any]:
    """Crée (ou met à jour) une entrée SANS valeur : métadonnées seulement, pour une saisie hors conversation.

    Une entrée existante garde ses valeurs ; changer son type est refusé tant qu'elle en contient."""
    cle = normaliser_cle(cle)
    changements = _valider_meta(meta)

    def appliquer(secrets: dict[str, dict[str, Any]]) -> dict[str, Any]:
        existant = secrets.get(cle)
        entree = normaliser_entree(cle, existant) if existant else _nouvelle_entree(cle)
        _changement_de_type(entree, changements.get("type", entree["type"]))
        entree.update(changements)
        _finaliser(entree)
        if existant:
            entree["maj"], entree["par"] = iso_now(), who()
        secrets[cle] = entree
        return meta_publique(cle, entree)

    infos = backend().modifier(appliquer)
    log.info("coffre : entrée %s préparée par %s", cle, who())
    return infos


def modifier_meta(cle: str, **meta: Any) -> dict[str, Any]:
    """Change les métadonnées d'une entrée existante (libellé, capacités qui l'utilisent…) ; jamais les valeurs."""
    cle = normaliser_cle(cle)
    changements = _valider_meta(meta)

    def appliquer(secrets: dict[str, dict[str, Any]]) -> dict[str, Any]:
        if cle not in secrets:
            raise ErreurCoffre(f"Aucune entrée « {cle} » dans le coffre.")
        entree = normaliser_entree(cle, secrets[cle])
        _changement_de_type(entree, changements.get("type", entree["type"]))
        entree.update(changements)
        _finaliser(entree)
        entree["maj"], entree["par"] = iso_now(), who()
        secrets[cle] = entree
        return meta_publique(cle, entree)

    return backend().modifier(appliquer)


def deplacer(cle: str, nouvelle: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Range une entrée sous une autre clé (ex. `libre/cantine` → `cantine` quand une capacité l'utilise)."""
    cle, nouvelle = normaliser_cle(cle), normaliser_cle(nouvelle)
    changements = _valider_meta(meta or {})

    def appliquer(secrets: dict[str, dict[str, Any]]) -> dict[str, Any]:
        if cle not in secrets:
            raise ErreurCoffre(f"Aucune entrée « {cle} » dans le coffre.")
        cible = secrets.get(nouvelle)
        if nouvelle != cle and cible is not None and (cible.get("champs") or {}):
            raise ErreurCoffre(f"L'entrée « {nouvelle} » existe déjà avec des valeurs : supprime-la d'abord.")
        entree = normaliser_entree(cle, secrets.pop(cle))
        entree.update(changements)
        _finaliser(entree)
        entree["maj"], entree["par"] = iso_now(), who()
        secrets[nouvelle] = entree
        return meta_publique(nouvelle, entree)

    infos = backend().modifier(appliquer)
    log.info("coffre : entrée %s rangée sous %s par %s", cle, nouvelle, who())
    return infos


def meta(cle: str) -> dict[str, Any] | None:
    """Métadonnées et noms des champs d'une entrée (jamais de valeur), None si elle n'existe pas."""
    cle = normaliser_cle(cle)
    entree = backend().lire_entree(cle)
    return meta_publique(cle, entree) if entree is not None else None


def supprimer(cle: str) -> bool:
    cle = normaliser_cle(cle)
    supprime = backend().supprimer(cle)
    if supprime:
        log.info("coffre : secret %s supprimé par %s", cle, who())
    return supprime


def lister() -> list[str]:
    """Clés présentes (triées), jamais de valeur."""
    return sorted(backend().entrees())


def decrire(quarantaines: bool = False) -> list[dict[str, Any]]:
    """Métadonnées sans valeur : clé, type, libellé, propriétaire, noms des champs, fin de carte, date et auteur.

    Les quarantaines (`quarantaine/<id>`) n'y figurent qu'avec `quarantaines=True`."""
    return [
        meta_publique(cle, entree)
        for cle, entree in sorted(backend().entrees().items())
        if quarantaines or not cle.startswith(PREFIXE_QUARANTAINE)
    ]


# --- quarantaine (filtre de données sensibles, coeur.filtre_sensible) ---------------------------------------

RE_ID_QUARANTAINE = re.compile(r"^q[0-9a-f]{4,8}$")


def _expiree(entree: dict[str, Any], instant: datetime | None = None) -> bool:
    info = entree.get("quarantaine") if isinstance(entree, dict) else None
    try:
        expire = datetime.fromisoformat(str((info or {}).get("expire")))
    except ValueError:
        return True
    return (instant or now()) >= expire


def _purger(secrets: dict[str, dict[str, Any]]) -> int:
    expirees = [c for c, e in secrets.items() if c.startswith(PREFIXE_QUARANTAINE) and _expiree(e)]
    for cle in expirees:
        del secrets[cle]
    return len(expirees)


def mettre_en_quarantaine(valeur: str, personne: str, nature: str, apercu: str = "") -> str:
    """Garde une valeur détectée dans un message (chiffrée dans le coffre, 10 minutes) ; renvoie un id court.

    Seule la personne qui l'a envoyée peut la ranger ensuite dans le coffre (`ranger_quarantaine`)."""
    if not isinstance(valeur, str) or not valeur or len(valeur) > LONGUEUR_MAX_VALEUR:
        raise ErreurCoffre("Valeur à mettre en quarantaine vide ou trop longue.")
    if not RE_PROPRIETAIRE.match(str(personne or "")):
        raise ErreurCoffre("Quarantaine : personne inconnue.")
    b = backend()
    if isinstance(b, BackendFichier):
        phrase_coffre()  # sans clé, échouer avant de toucher au disque


    def appliquer(secrets: dict[str, dict[str, Any]]) -> str:
        while True:
            ident = "q" + _aleatoire.token_hex(2)
            if PREFIXE_QUARANTAINE + ident not in secrets:
                break
        instant = now()
        secrets[PREFIXE_QUARANTAINE + ident] = {
            "champs": {"valeur": valeur},
            "type": "autre",
            "libelle": f"Quarantaine ({nature})",
            "proprietaire": personne,
            "utilise_par": [],
            "cree": instant.isoformat(timespec="seconds"),
            "maj": instant.isoformat(timespec="seconds"),
            "par": personne,
            "quarantaine": {
                "nature": str(nature)[:40],
                "apercu": str(apercu)[:12],
                "expire": (instant + DUREE_QUARANTAINE).isoformat(timespec="seconds"),
            },
        }
        return ident

    return b.modifier(appliquer)


def _id_quarantaine(ident: str) -> str:
    ident = str(ident or "").strip().lower()
    if not RE_ID_QUARANTAINE.match(ident):
        raise ErreurCoffre("Identifiant de quarantaine invalide (ex. q7f3).")
    return ident


def quarantaine(ident: str) -> dict[str, Any]:
    """Métadonnées d'une quarantaine (personne, nature, aperçu, expiration), jamais la valeur ; lève si expirée."""
    ident = _id_quarantaine(ident)
    entree = backend().lire_entree(PREFIXE_QUARANTAINE + ident)
    if entree is None or _expiree(entree):
        raise ErreurCoffre("Cette donnée n'est plus en quarantaine (10 minutes passées, ou déjà rangée) : rien à ranger.")
    info = dict(entree.get("quarantaine") or {})
    return {"id": ident, "personne": entree.get("proprietaire"), **info}


def ranger_quarantaine(ident: str, cle: str, champ: str, personne: str) -> dict[str, Any]:
    """Écrit la valeur d'une quarantaine dans le champ `champ` de l'entrée `cle` (contrôles du type), puis
    efface la quarantaine. La valeur ne sort jamais de ce module. Renvoie les métadonnées de l'entrée."""
    ident, cle, champ = _id_quarantaine(ident), normaliser_cle(cle), valider_champ(champ)
    if cle.startswith(PREFIXE_QUARANTAINE):
        raise ErreurCoffre("Une quarantaine ne se range pas dans une autre quarantaine.")

    def appliquer(secrets: dict[str, dict[str, Any]]) -> dict[str, Any]:
        source = secrets.get(PREFIXE_QUARANTAINE + ident)
        if source is None or _expiree(source):
            raise ErreurCoffre("Cette donnée n'est plus en quarantaine (10 minutes passées, ou déjà rangée) : rien à ranger.")
        if source.get("proprietaire") != personne:
            raise ErreurCoffre("Cette donnée a été envoyée par une autre personne : seule elle peut la ranger.")
        valeur = str((source.get("champs") or {}).get("valeur") or "")
        entree = _appliquer_ecriture(secrets, cle, {champ: valeur}, True, None)
        del secrets[PREFIXE_QUARANTAINE + ident]
        return meta_publique(cle, entree)

    infos = backend().modifier(appliquer)
    log.info("coffre : quarantaine %s rangée dans %s (champ %s) par %s", ident, cle, champ, who())
    return infos


def oublier_quarantaine(ident: str) -> bool:
    """Efface une quarantaine tout de suite (la personne ne veut pas la ranger)."""
    ident = _id_quarantaine(ident)
    return supprimer(PREFIXE_QUARANTAINE + ident)


def purger_quarantaine() -> int:
    """Efface les quarantaines expirées (healthcheck, chaque message du cœur) ; renvoie leur nombre."""
    try:
        entrees = backend().entrees()
    except (ErreurCoffre, NotImplementedError):
        return 0
    if not any(c.startswith(PREFIXE_QUARANTAINE) and _expiree(e) for c, e in entrees.items()):
        return 0
    return backend().modifier(_purger)


def _variantes(valeur: str) -> set[str]:
    """Formes d'affichage courantes d'un numéro de carte (groupes de 4 séparés par une espace ou un tiret)."""
    if not (valeur.isdigit() and 13 <= len(valeur) <= 19):
        return set()
    groupes = [valeur[i : i + 4] for i in range(0, len(valeur), 4)]
    return {" ".join(groupes), "-".join(groupes)}


def valeurs_connues() -> set[str]:
    """Toutes les valeurs du coffre (≥ 4 caractères), pour masquer les sorties d'outils.

    Ne lève jamais : un coffre absent ou illisible n'a rien pu fournir, donc rien à masquer.
    """
    try:
        entrees = backend().entrees()
    except (ErreurCoffre, NotImplementedError) as exc:
        if not isinstance(exc, (CleCoffreAbsente, CoffreInaccessible)):
            log.warning("coffre illisible pour le masquage : %s", exc.__class__.__name__)
        return set()
    valeurs: set[str] = set()
    for entree in entrees.values():
        for valeur in (entree.get("champs") or {}).values():
            if isinstance(valeur, str) and len(valeur) >= LONGUEUR_MIN_MASQUAGE:
                valeurs.add(valeur)
                valeurs |= _variantes(valeur)
    return valeurs


def masquer(texte: str, valeurs: set[str] | None = None) -> str:
    """Remplace toute valeur de secret connue par « ••• » (les plus longues d'abord)."""
    if not texte:
        return texte
    for valeur in sorted(valeurs if valeurs is not None else valeurs_connues(), key=len, reverse=True):
        if valeur in texte:
            texte = texte.replace(valeur, MASQUE)
    return texte


# --- CLI -------------------------------------------------------------------------------------


def _ecrire(texte: str) -> None:
    sys.stdout.write(texte + "\n")


def _erreur(texte: str) -> None:
    sys.stderr.write(texte + "\n")


def _lire_valeur(invite: str) -> str:
    """Une valeur sans écho : getpass sur un terminal, sinon une ligne de stdin (usage scripté, tube).

    Le texte saisi n'est jamais réécrit ni sur stdout ni sur stderr.
    """
    if sys.stdin is not None and sys.stdin.isatty():
        return getpass.getpass(invite)
    sys.stderr.write(invite)
    sys.stderr.flush()
    ligne = sys.stdin.readline() if sys.stdin is not None else ""
    if not ligne:
        raise EOFError
    sys.stderr.write("\n")
    return ligne.rstrip("\r\n")


def _saisir(champ: str, facultatif: bool = False) -> str | None:
    """Saisie masquée d'une valeur, demandée deux fois (on ne voit pas ce qu'on tape).

    Un champ facultatif (ex. url) laissé vide est ignoré (None)."""
    valeur = _lire_valeur(f"{champ} ({'facultatif, ' if facultatif else ''}saisie masquée) : ")
    if not valeur and facultatif:
        return None
    if not valeur:
        raise ErreurCoffre(f"Valeur vide pour « {champ} » : abandon, rien n'a été enregistré.")
    confirmation = _lire_valeur(f"{champ} (confirmation) : ")
    if confirmation != valeur:
        raise ErreurCoffre(f"Les deux saisies de « {champ} » diffèrent : abandon, rien n'a été enregistré.")
    return valeur


def _liste_champs(texte: str | None, defaut: Iterable[str] = CHAMPS_DEFAUT) -> list[str]:
    noms = [c for c in (texte or "").split(",") if c.strip()] if texte else list(defaut)
    propres: list[str] = []
    for nom in noms:
        n = valider_champ(nom)
        if n not in propres:
            propres.append(n)
    return propres


class _Analyseur(argparse.ArgumentParser):
    """argparse qui ne réécrit jamais un argument inattendu (ce pourrait être une valeur collée)."""

    def error(self, message: str) -> None:  # type: ignore[override]
        if "unrecognized arguments" in message or "invalid choice" in message:
            message = (
                "argument inattendu (non affiché) : une valeur ne se passe jamais en argument, "
                "elle est demandée en saisie masquée — voir --help"
            )
        super().error(message)


def main(argv: list[str] | None = None) -> int:
    parser = _Analyseur(
        prog="python -m tools.coffre",
        description="Coffre à secrets du foyer. Les valeurs se saisissent masquées ; aucune n'est affichée sauf get --revele.",
    )
    sub = parser.add_subparsers(dest="commande", required=True)
    p_set = sub.add_parser("set", help="enregistre ou met à jour un secret (saisie masquée)")
    p_set.add_argument("cle", help="ex. planity/alex (jamais de valeur ici)")
    p_set.add_argument("--champs", help="noms des champs, séparés par des virgules (défaut : identifiant,mot_de_passe)")
    p_set.add_argument("--remplacer", action="store_true", help="efface les champs non saisis (défaut : ils sont conservés)")
    p_set.add_argument("--type", help=f"type de l'entrée : {', '.join(TYPES)} (défaut : celui de l'entrée, sinon identifiants)")
    p_set.add_argument("--libelle", help="nom lisible (ex. « Cantine de l'école ») — jamais une valeur")
    p_set.add_argument("--proprietaire", help="linux_user de la personne, ou foyer (défaut : déduit de la clé)")
    p_set.add_argument("--virtuelle", action="store_true",
                       help="carte bancaire : confirme qu'il s'agit d'une carte VIRTUELLE à plafond (obligatoire)")
    p_get = sub.add_parser("get", help="noms des champs présents (valeurs seulement avec --revele)")
    p_get.add_argument("cle")
    p_get.add_argument("--champs", action="store_true", help="affiche les noms des champs (comportement par défaut)")
    p_get.add_argument("--revele", action="store_true", help="affiche aussi les VALEURS (attention à l'écran et à l'historique)")
    p_rm = sub.add_parser("rm", help="supprime un secret")
    p_rm.add_argument("cle")
    p_rm.add_argument("--oui", action="store_true", help="ne pas demander de confirmation")
    sub.add_parser("ls", help="liste les clés et les noms de champs (jamais les valeurs)")
    sub.add_parser("statut", help="backend, fichier, lisibilité, nombre de secrets")
    sub.add_parser("purger", help="efface les quarantaines expirées (healthcheck)")
    args = parser.parse_args(argv)
    # Auteur des modifications : la personne derrière `sudo -u homeos` si connue.
    if not os.environ.get("HOMEOS_WHO"):
        try:
            os.environ["HOMEOS_WHO"] = os.environ.get("SUDO_USER") or getpass.getuser()
        except (KeyError, OSError):
            pass

    try:
        if args.commande == "set":
            cle = normaliser_cle(args.cle)
            if cle.startswith(PREFIXE_QUARANTAINE):
                raise ErreurCoffre("Les quarantaines ne se saisissent pas à la main.")
            existante = meta(cle)
            type_ = normaliser_type(args.type) if args.type else (existante or {}).get("type") or TYPE_DEFAUT
            attendus, facultatifs = champs_du_type(type_)
            if existante is None and not args.type:
                attendus, facultatifs = CHAMPS_DEFAUT, ()
            champs = _liste_champs(args.champs, (*attendus, *facultatifs) if attendus else CHAMPS_DEFAUT)
            meta_cli: dict[str, Any] = {}
            if args.type:
                meta_cli["type"] = type_
            if args.libelle:
                meta_cli["libelle"] = args.libelle
            if args.proprietaire:
                meta_cli["proprietaire"] = args.proprietaire
            if args.virtuelle:
                meta_cli["virtuelle"] = True
            if type_ == "carte_bancaire" and not args.virtuelle and not (existante or {}).get("virtuelle"):
                raise ErreurCoffre(MESSAGE_CARTE_VIRTUELLE)
            _ecrire(f"Secret {cle} ({NOMS_TYPES[type_]}) — champs : {', '.join(champs)}")
            valeurs = {}
            for c in champs:
                v = _saisir(c, facultatif=c in facultatifs and not args.champs)
                if v is not None:
                    valeurs[c] = v
            ecrire(cle, valeurs, fusion=not args.remplacer, meta=meta_cli or None)
            noms = sorted(valeurs)
            del valeurs
            _ecrire(f"Enregistré : {cle} ({', '.join(noms)}).")
            return 0
        if args.commande == "get":
            cle = normaliser_cle(args.cle)
            entree = backend().lire_entree(cle)
            if not entree or not entree.get("champs"):
                _erreur(f"Aucun secret « {cle} » dans le coffre.")
                return 1
            noms = sorted(entree["champs"])
            _ecrire(f"{cle} : {', '.join(noms)} (mis à jour {entree.get('maj') or '?'} par {entree.get('par') or '?'})")
            if args.revele:
                _erreur("Attention : valeurs affichées en clair ci-dessous.")
                for nom in noms:
                    _ecrire(f"  {nom} = {entree['champs'][nom]}")
            return 0
        if args.commande == "rm":
            cle = normaliser_cle(args.cle)
            if not args.oui:
                reponse = input(f"Supprimer définitivement le secret « {cle} » ? Tape OUI : ")
                if reponse.strip() != "OUI":
                    _erreur("Abandon.")
                    return 1
            if not supprimer(cle):
                _erreur(f"Aucun secret « {cle} » dans le coffre.")
                return 1
            _ecrire(f"Supprimé : {cle}.")
            return 0
        if args.commande == "ls":
            lignes = decrire(quarantaines=True)
            if not lignes:
                _ecrire("Coffre vide.")
            for ligne in lignes:
                details = [NOMS_TYPES.get(str(ligne["type"]), str(ligne["type"])), f"« {ligne['libelle']} »",
                           f"à {ligne['proprietaire']}"]
                if ligne.get("fin"):
                    details.append(f"••• {ligne['fin']}" + (" (virtuelle)" if ligne.get("virtuelle") else ""))
                if ligne["utilise_par"]:
                    details.append("utilisé par " + ", ".join(ligne["utilise_par"]))
                _ecrire(
                    f"{ligne['cle']} : {', '.join(ligne['champs']) or '(aucune valeur, à saisir)'} — "
                    f"{', '.join(details)} (mis à jour {ligne['maj'] or '?'})"
                )
            return 0
        if args.commande == "purger":
            _ecrire(f"Quarantaines expirées effacées : {purger_quarantaine()}.")
            return 0
        if args.commande == "statut":
            nom = nom_backend()
            b = backend()
            if nom == "fichier":
                phrase_coffre()  # lève CleCoffreAbsente : `set` échouerait
            chemin = getattr(b, "chemin", None)
            _ecrire(f"Backend : {nom}" + (f" ({chemin}, {'présent' if chemin.exists() else 'pas encore créé'})" if chemin else ""))
            _ecrire(f"Secrets : {len(b.entrees())} — coffre lisible.")
            return 0
    except NotImplementedError as exc:
        _erreur(str(exc))
        return 1
    except ErreurCoffre as exc:
        _erreur(str(exc))
        return 1
    except (KeyboardInterrupt, EOFError):
        _erreur("\nAbandon, rien n'a été enregistré.")
        return 130
    parser.error("commande inconnue")
    return 2


if __name__ == "__main__":
    sys.exit(main())
