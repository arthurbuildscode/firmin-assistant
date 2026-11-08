"""Filtre de données sensibles (lot 5c) : rien de secret n'atteint le modèle, quel que soit le canal.

Chaque message d'une personne (texte, transcription vocale, message stocké dans l'historique, bloc ``[contexte
récent]``) passe ici **avant** la construction du prompt. Détection déterministe, sans IA, volontairement
conservatrice (haute précision : en cas de doute on laisse passer ; le masquage des sorties d'outils par le coffre
reste la deuxième barrière) :

- numéro de carte bancaire : 13 à 19 chiffres (espaces ou tirets acceptés), préfixe d'un réseau connu, Luhn ;
- IBAN : pays + clé + BBAN, longueur du pays, modulo 97 ;
- numéro de sécurité sociale français (NIR) : 15 chiffres, clé 97 (Corse 2A/2B comprise) ;
- mot de passe ou code annoncé par le contexte (« mot de passe : … », « mdp … », « le code du portail est 1234 »,
  « identifiant x / y », « password … ») ;
- clés et jetons : préfixes connus (sk-, ghp_, AKIA…) ou chaîne de 20 caractères et plus mêlant trois classes
  de caractères avec une forte entropie.

Chaque valeur détectée est remplacée par un jeton neutre et typé : ``[carte bancaire ••1234]``, ``[IBAN ••7890]``,
``[numéro de sécurité sociale]``, ``[mot de passe masqué]``, ``[clé masquée]``. La valeur d'origine ne sort de
ce module que vers la quarantaine du coffre (``tools.coffre.mettre_en_quarantaine``), jamais vers un journal.

Module sans dépendance (bibliothèque standard) : utilisé par le cœur, par ``coeur.historique`` et par les outils
``identifiants_*`` (contrôle des arguments).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

NATURES = {
    "carte_bancaire": "carte bancaire",
    "iban": "IBAN",
    "nir": "numéro de sécurité sociale",
    "mot_de_passe": "mot de passe",
    "cle": "clé",
}


@dataclass(frozen=True)
class Detection:
    nature: str  # carte_bancaire | iban | nir | mot_de_passe | cle
    valeur: str = field(repr=False)  # telle qu'écrite dans le message (jamais journalisée)
    debut: int
    fin: int
    apercu: str = ""  # « ••1234 » (carte, IBAN), vide sinon

    @property
    def libelle(self) -> str:
        return NATURES[self.nature]

    @property
    def jeton(self) -> str:
        if self.nature == "carte_bancaire":
            return f"[carte bancaire {self.apercu}]"
        if self.nature == "iban":
            return f"[IBAN {self.apercu}]"
        if self.nature == "nir":
            return "[numéro de sécurité sociale]"
        if self.nature == "mot_de_passe":
            return "[mot de passe masqué]"
        return "[clé masquée]"

    @property
    def valeur_normalisee(self) -> str:
        """Valeur à garder en quarantaine : chiffres seuls pour une carte ou un NIR, IBAN en majuscules collées."""
        if self.nature in ("carte_bancaire", "nir"):
            return re.sub(r"[\s.-]", "", self.valeur).upper()
        if self.nature == "iban":
            return re.sub(r"\s", "", self.valeur).upper()
        return self.valeur

    def __repr__(self) -> str:  # jamais la valeur, même dans une trace
        return f"Detection({self.nature!r}, {self.apercu!r})"


@dataclass
class Filtrage:
    texte: str
    detections: list[Detection]

    @property
    def natures(self) -> list[str]:
        vues: list[str] = []
        for d in self.detections:
            if d.nature not in vues:
                vues.append(d.nature)
        return vues


# --- validateurs --------------------------------------------------------------------------------------------


def luhn(numero: str) -> bool:
    if not numero.isdigit():
        return False
    total = 0
    for i, c in enumerate(reversed(numero)):
        n = int(c) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def _reseau_carte(numero: str) -> bool:
    """Préfixes des réseaux (Visa, Mastercard, Amex, CB/Discover, UnionPay, JCB, Maestro)."""
    if numero[0] == "4":
        return True
    deux, quatre = int(numero[:2]), int(numero[:4])
    return (
        51 <= deux <= 55 or 2221 <= quatre <= 2720 or deux in (34, 37, 62, 35, 50, 56, 57, 58, 63, 67)
        or numero.startswith(("6011", "65", "644", "645", "646", "647", "648", "649"))
    )


LONGUEURS_IBAN = {
    "FR": 27, "MC": 27, "DE": 22, "BE": 16, "ES": 24, "IT": 27, "GB": 22, "CH": 21, "LU": 20, "NL": 18, "PT": 25,
    "AT": 20, "IE": 22, "DK": 18, "FI": 18, "SE": 24, "NO": 15, "PL": 28, "GR": 27, "AD": 24, "SM": 27,
}


def iban_valide(iban: str) -> bool:
    iban = iban.upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", iban):
        return False
    attendu = LONGUEURS_IBAN.get(iban[:2])
    if attendu is not None and len(iban) != attendu:
        return False
    if attendu is None and not 15 <= len(iban) <= 34:
        return False
    deplace = iban[4:] + iban[:4]
    nombre = "".join(str(int(c, 36)) for c in deplace)
    return int(nombre) % 97 == 1


def nir_valide(nir: str) -> bool:
    nir = nir.upper()
    if not re.fullmatch(r"[12]\d{4}(?:\d{2}|2A|2B)\d{6}\d{2}", nir):
        return False
    mois = int(nir[3:5])
    if not (1 <= mois <= 12 or 20 <= mois <= 42 or 50 <= mois <= 99):
        return False
    corps = nir[:13].replace("2A", "19").replace("2B", "18")
    return 97 - int(corps) % 97 == int(nir[13:])


def _entropie(texte: str) -> float:
    effectifs: dict[str, int] = {}
    for c in texte:
        effectifs[c] = effectifs.get(c, 0) + 1
    n = len(texte)
    return -sum(k / n * math.log2(k / n) for k in effectifs.values())


def _classes(texte: str) -> int:
    return sum((any(c.islower() for c in texte), any(c.isupper() for c in texte), any(c.isdigit() for c in texte),
                any(not c.isalnum() for c in texte)))


# --- motifs -------------------------------------------------------------------------------------------------

_RE_CARTE = re.compile(r"(?<![\w.,/-])\d(?:[ -]?\d){12,18}(?![\w/]|[.,]\d)")
_RE_IBAN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{2}\d{2}(?: ?[A-Za-z0-9]{4}){2,7}(?: ?[A-Za-z0-9]{1,4})?(?![A-Za-z0-9])")
_RE_NIR = re.compile(r"(?<![\w])[12] ?\d{2} ?\d{2} ?(?:\d{2}|2[ABab]) ?\d{3} ?\d{3} ?\d{2}(?![\w])")
_SEPARATEUR = r"(?:\s*[:=]\s*|\s+(?:est|c'est|c’est|is|vaut|:)\s*:?\s*|\s+)"
_RE_MOT_DE_PASSE = re.compile(
    r"\b(?:mot\s+de\s+passe|mots\s+de\s+passe|mdp|password|passwd|pwd|passcode)"
    r"(?P<milieu>(?:\s+(?:du|de|de\s+la|pour|sur|d'|d’|le|la|mon|ma|ton|ta|wifi|wi-fi|compte|site|\w+)){0,2}?)"
    r"(?P<sep>" + _SEPARATEUR + r")(?P<val>[^\s,;«»\"\[\]]{4,64})",
    re.IGNORECASE,
)
_RE_CODE = re.compile(
    r"\b(?:code(?:\s+(?:pin|secret|d'accès|d’accès|de\s+connexion|confidentiel|d'entrée|d’entrée))?|pin)"
    r"(?P<milieu>(?:\s+[\w'’-]+){0,3}?)"
    r"(?P<sep>\s*[:=]\s*|\s+(?:est|c'est|c’est|vaut)\s*:?\s*)(?P<val>[A-Za-z0-9#*]{4,12})(?![\w])",
    re.IGNORECASE,
)
_RE_IDENTIFIANT_MDP = re.compile(
    r"\b(?:identifiants?|login|user(?:name)?|utilisateur)\s*[:=]?\s*\S+\s*/\s*(?P<val>[^\s,;\[\]]{4,64})",
    re.IGNORECASE,
)
_RE_CLE_CONNUE = re.compile(
    r"(?<![\w-])(?:sk-(?:ant-|proj-|live_|test_)?[A-Za-z0-9_-]{16,}|sk_(?:live|test)_[A-Za-z0-9]{16,}"
    r"|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}|glpat-[A-Za-z0-9_-]{20,}|hf_[A-Za-z0-9]{30,})"
)
_RE_JETON = re.compile(r"[A-Za-z0-9_+/=.~#$%&*!?-]{20,}")
# Après « code » : ce qui n'est pas un secret (code postal, promo…).
_CODE_PUBLIC = re.compile(
    r"postal|promo|réduc|reduc|parrain|article|route|couleur|source|barre|commande|suivi|client|produit|"
    r"naf|ape|insee|iso|erreur|retour|cadeau|avantage|de la route|civil|pénal|penal|vestimentaire",
    re.IGNORECASE,
)
_MOTS_NON_SECRETS = {
    "oublié", "oublie", "perdu", "faux", "incorrect", "incorrecte", "bon", "mauvais", "trop", "pas", "plus",
    "bloqué", "bloque", "expiré", "expire", "changé", "change", "modifié", "valide", "invalide", "simple",
    "le", "la", "les", "un", "une", "du", "de", "des", "mon", "ma", "mes", "ton", "ta", "tes", "son", "sa",
    "sur", "dans", "pour", "avec", "que", "qui", "quoi", "est", "était", "sera", "?", "svp", "stp", "requis",
    "obligatoire", "différent", "different", "identique", "fort", "faible", "temporaire", "reçu", "recu",
}


def _chevauche(debut: int, fin: int, prises: list[Detection]) -> bool:
    return any(debut < d.fin and d.debut < fin for d in prises)


def _cartes(texte: str) -> list[Detection]:
    trouvees = []
    for m in _RE_CARTE.finditer(texte):
        brut = m.group(0)
        chiffres = re.sub(r"[ -]", "", brut)
        if not (13 <= len(chiffres) <= 19) or len(set(chiffres)) < 2:
            continue
        separateurs = re.findall(r"[ -]", brut)
        if separateurs and len(set(separateurs)) > 1:  # « 06 12-34 … » : pas une carte écrite d'un trait
            continue
        if not (_reseau_carte(chiffres) and luhn(chiffres)):
            continue
        trouvees.append(Detection("carte_bancaire", brut, m.start(), m.end(), f"••{chiffres[-4:]}"))
    return trouvees


def _ibans(texte: str) -> list[Detection]:
    trouvees = []
    for m in _RE_IBAN.finditer(texte):
        brut = m.group(0)
        compact = re.sub(r"\s", "", brut).upper()
        if not compact[:2].isalpha() or not any(c.isdigit() for c in compact[4:]):
            continue
        if iban_valide(compact):
            trouvees.append(Detection("iban", brut, m.start(), m.end(), f"••{compact[-4:]}"))
    return trouvees


def _nirs(texte: str) -> list[Detection]:
    trouvees = []
    for m in _RE_NIR.finditer(texte):
        compact = re.sub(r"\s", "", m.group(0)).upper()
        if nir_valide(compact):
            trouvees.append(Detection("nir", m.group(0), m.start(), m.end()))
    return trouvees


def _val_mot_de_passe(m: re.Match[str]) -> tuple[str, int, int] | None:
    val = m.group("val").rstrip(".,)")
    if val.casefold() in _MOTS_NON_SECRETS or len(val) < 4:
        return None
    explicite = bool(re.search(r"[:=]", m.group("sep")))
    melange = any(c.isdigit() for c in val) or any(not c.isalnum() for c in val) or any(c.isupper() for c in val[1:])
    if not explicite and not melange:
        return None  # « mon mot de passe soleil… » sans « : » ni chiffre : on laisse passer (doute)
    return val, m.start("val"), m.start("val") + len(val)


def _mots_de_passe(texte: str) -> list[Detection]:
    trouvees = []
    for m in _RE_MOT_DE_PASSE.finditer(texte):
        if m.group("milieu") and _CODE_PUBLIC.search(m.group("milieu")):
            continue
        res = _val_mot_de_passe(m)
        if res:
            trouvees.append(Detection("mot_de_passe", res[0], res[1], res[2]))
    for m in _RE_CODE.finditer(texte):
        if _CODE_PUBLIC.search(m.group("milieu") or "") or _CODE_PUBLIC.search(m.group(0)[:20]):
            continue
        val = m.group("val")
        if not any(c.isdigit() for c in val):
            continue
        trouvees.append(Detection("mot_de_passe", val, m.start("val"), m.end("val")))
    for m in _RE_IDENTIFIANT_MDP.finditer(texte):
        val = m.group("val").rstrip(".,)")
        if len(val) >= 4 and val.casefold() not in _MOTS_NON_SECRETS:
            trouvees.append(Detection("mot_de_passe", val, m.start("val"), m.start("val") + len(val)))
    return trouvees


def _cles(texte: str) -> list[Detection]:
    trouvees = [Detection("cle", m.group(0), m.start(), m.end()) for m in _RE_CLE_CONNUE.finditer(texte)]
    for m in _RE_JETON.finditer(texte):
        jeton = m.group(0).strip(".!?")
        debut = m.start() + m.group(0).index(jeton) if jeton else m.start()
        if len(jeton) < 20 or "://" in texte[max(0, m.start() - 12) : m.end()] or "@" in texte[m.start() : m.end() + 1]:
            continue
        if texte[max(0, m.start() - 1) : m.start()] in ("/", "\\") or jeton.startswith("/"):
            continue  # chemin de fichier
        if _classes(jeton) < 3 or _entropie(jeton) < 3.5 or not any(c.isdigit() for c in jeton):
            continue
        if re.fullmatch(r"[0-9a-fA-F-]+", jeton):  # empreinte, identifiant hexadécimal (UUID…) : pas une clé
            continue
        trouvees.append(Detection("cle", jeton, debut, debut + len(jeton)))
    return trouvees


def detecter(texte: str) -> list[Detection]:
    """Détections sans chevauchement, dans l'ordre du texte (priorité : carte, IBAN, NIR, mot de passe, clé)."""
    if not texte:
        return []
    prises: list[Detection] = []
    for detecteur in (_cartes, _ibans, _nirs, _mots_de_passe, _cles):
        for d in detecteur(texte):
            if not _chevauche(d.debut, d.fin, prises):
                prises.append(d)
    return sorted(prises, key=lambda d: d.debut)


def _remplacer(texte: str, detections: list[Detection]) -> str:
    morceaux, position = [], 0
    for d in sorted(detections, key=lambda d: d.debut):
        morceaux.append(texte[position : d.debut])
        morceaux.append(d.jeton)
        position = d.fin
    morceaux.append(texte[position:])
    return "".join(morceaux)


def filtrer(texte: str) -> Filtrage:
    """Texte masqué et détections (avec les valeurs, à ne transmettre qu'à la quarantaine)."""
    detections = detecter(texte or "")
    return Filtrage(_remplacer(texte or "", detections) if detections else (texte or ""), detections)


def masquer(texte: str) -> str:
    """Texte avec chaque donnée sensible remplacée par son jeton (pour l'historique, les journaux, les aperçus)."""
    return filtrer(texte).texte


def filtrer_plusieurs(*textes: str) -> tuple[list[str], list[Detection]]:
    """Masque plusieurs textes (le message et le prompt qui le contient) : chaque valeur détectée dans l'un est
    remplacée partout. Renvoie (textes masqués, détections uniques par valeur)."""
    uniques: dict[str, Detection] = {}
    for t in textes:
        for d in detecter(t or ""):
            uniques.setdefault(d.valeur, d)
    sorties = []
    for t in textes:
        sortie = filtrer(t or "").texte
        for valeur, d in sorted(uniques.items(), key=lambda kv: len(kv[0]), reverse=True):
            if valeur in sortie:
                sortie = sortie.replace(valeur, d.jeton)
        sorties.append(sortie)
    return sorties, list(uniques.values())
