"""Horaires en français ↔ format des crons (lot 7a, administration par la conversation).

Format interne (manifestes, config.yaml, `tools.capacites.oncalendar`) : « HH:MM » (tous les jours),
« Wed 18:00 », « Mon..Fri 07:00 », « Mon,Thu 08:00 ».

    analyser("mercredi 12h")            → "Wed 12:00"
    analyser("tous les jours à 7h15")   → "07:15"
    analyser("du lundi au vendredi 7h") → "Mon..Fri 07:00"
    analyser("Wed 18:00")               → "Wed 18:00"
    lisible("Wed 18:00")                → "le mercredi à 18h"
"""

from __future__ import annotations

import re
import unicodedata

JOURS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
NOMS_FR = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")

# Noms acceptés (sans accents, minuscules) → index du jour.
_NOMS_EN = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_ALIAS_JOURS: dict[str, int] = {}
for _i, (_fr, _en, _long) in enumerate(zip(NOMS_FR, JOURS, _NOMS_EN, strict=True)):
    for _alias in (_fr, _fr + "s", _fr[:3], _en.lower(), _long):
        _ALIAS_JOURS[_alias] = _i

RE_INTERNE = re.compile(r"^(?:(?P<jours>[A-Za-z]{3}(?:(?:,|\.\.)[A-Za-z]{3})*)\s+)?(?P<h>\d{1,2}):(?P<m>\d{2})$")
RE_HEURE = re.compile(
    r"(?<![\d])(?P<h>\d{1,2})\s*(?:h|heures?|:)\s*(?P<m>\d{2})?(?:\s*(?P<demie>et\s+demie?|et\s+quart|moins\s+le\s+quart))?"
)
_MOT_JOUR = "|".join(sorted(_ALIAS_JOURS, key=len, reverse=True))
RE_PLAGE = re.compile(rf"\bdu\s+({_MOT_JOUR})\.?\s+au\s+({_MOT_JOUR})\b")
RE_JOUR = re.compile(rf"\b({_MOT_JOUR})\b\.?")

AIDE = (
    "Formats compris : « mercredi 12h », « tous les jours à 7h15 », « du lundi au vendredi 7h », "
    "« lundi et jeudi 8h30 », « le week-end à 9h », « Wed 18:00 »."
)


def _sans_accents(texte: str) -> str:
    t = unicodedata.normalize("NFKD", str(texte).casefold())
    return "".join(c for c in t if not unicodedata.combining(c))


def _heure(texte: str) -> tuple[int, int]:
    """Heure et minutes trouvées dans un texte français (sans accents)."""
    if re.search(r"\bmidi\b", texte) or re.search(r"\bminuit\b", texte):
        h = 12 if re.search(r"\bmidi\b", texte) else 0
        m = 0
        if re.search(r"\b(?:midi|minuit)\s+(?:et\s+demie?|trente)\b", texte):
            m = 30
        elif re.search(r"\b(?:midi|minuit)\s+et\s+quart\b", texte):
            m = 15
        else:
            apres = re.search(r"\b(?:midi|minuit)\s+(\d{2})\b", texte)
            if apres:
                m = int(apres.group(1))
        return h, m
    trouve = RE_HEURE.search(texte)
    if not trouve:
        raise ValueError("Il manque l'heure. " + AIDE)
    h, m = int(trouve.group("h")), int(trouve.group("m") or 0)
    demie = (trouve.group("demie") or "").replace("  ", " ")
    if demie.startswith("et demi"):
        m = 30
    elif demie == "et quart":
        m = 15
    elif demie.startswith("moins"):
        h, m = (h - 1) % 24, 45
    if re.search(r"\b(?:du soir|de l'apres-midi|de l apres-midi|de l'aprem)\b", texte) and h < 12:
        h += 12
    if h > 23 or m > 59:
        raise ValueError(f"Heure impossible : {h}h{m:02d}.")
    return h, m


def _jours(texte: str) -> str:
    """Partie « jours » du format interne ("" = tous les jours)."""
    if re.search(r"\b(?:tous les jours|chaque jour|quotidien(?:nement)?|tous les soirs|tous les matins)\b", texte):
        return ""
    if re.search(r"\b(?:en semaine|jours? ouvres?|jours? de semaine)\b", texte):
        return "Mon..Fri"
    if re.search(r"\bweek-?ends?\b", texte):
        return "Sat,Sun"
    plage = RE_PLAGE.search(texte)
    if plage:
        debut, fin = _ALIAS_JOURS[plage.group(1)], _ALIAS_JOURS[plage.group(2)]
        if debut == fin:
            return JOURS[debut]
        if debut > fin:
            raise ValueError("Plage de jours à l'envers (ex. « du lundi au vendredi »).")
        return f"{JOURS[debut]}..{JOURS[fin]}"
    indices = sorted({_ALIAS_JOURS[m.group(1)] for m in RE_JOUR.finditer(texte)})
    return ",".join(JOURS[i] for i in indices)


def normaliser_interne(quand: str) -> str:
    """« wed 8:00 » → « Wed 08:00 » ; lève ValueError si le format interne est invalide."""
    m = RE_INTERNE.fullmatch(str(quand).strip())
    if not m:
        raise ValueError(f"Horaire invalide « {quand} ». " + AIDE)
    h, mn = int(m.group("h")), int(m.group("m"))
    if h > 23 or mn > 59:
        raise ValueError(f"Heure impossible : « {quand} ».")
    jours = m.group("jours") or ""
    if jours:
        morceaux = re.split(r"(,|\.\.)", jours)
        for i in range(0, len(morceaux), 2):
            morceaux[i] = morceaux[i].capitalize()
            if morceaux[i] not in JOURS:
                raise ValueError(f"Jour inconnu « {morceaux[i]} ». " + AIDE)
        jours = "".join(morceaux) + " "
    return f"{jours}{h:02d}:{mn:02d}"


def analyser(texte: str) -> str:
    """Horaire en français (ou déjà au format interne) → format interne. Lève ValueError (message en français)."""
    brut = str(texte or "").strip()
    if not brut:
        raise ValueError("Horaire vide. " + AIDE)
    if RE_INTERNE.fullmatch(brut):
        return normaliser_interne(brut)
    t = _sans_accents(brut)
    t = re.sub(r"[’`]", "'", t)
    t = re.sub(r"\s+", " ", t)
    h, m = _heure(t)
    jours = _jours(t)
    resultat = f"{jours} {h:02d}:{m:02d}".strip()
    return normaliser_interne(resultat)


def _heure_lisible(h: int, m: int) -> str:
    if (h, m) == (12, 0):
        return "midi"
    if (h, m) == (0, 0):
        return "minuit"
    return f"{h}h{m:02d}" if m else f"{h}h"


def lisible(quand: str) -> str:
    """Format interne → français courant (« le mercredi à 18h », « tous les jours à 7h15 »)."""
    try:
        normal = normaliser_interne(quand)
    except ValueError:
        return str(quand)
    *jours, heure = normal.split(" ")
    h, m = (int(x) for x in heure.split(":"))
    a = f"à {_heure_lisible(h, m)}"
    if not jours:
        return f"tous les jours {a}"
    spec = jours[0]
    if spec == "Mon..Fri":
        return f"en semaine {a}"
    if spec == "Sat,Sun":
        return f"le week-end {a}"
    if ".." in spec and "," not in spec:
        debut, fin = spec.split("..")
        return f"du {NOMS_FR[JOURS.index(debut)]} au {NOMS_FR[JOURS.index(fin)]} {a}"
    noms: list[str] = []
    for morceau in spec.split(","):
        if ".." in morceau:
            debut, fin = morceau.split("..")
            noms.append(f"du {NOMS_FR[JOURS.index(debut)]} au {NOMS_FR[JOURS.index(fin)]}")
        else:
            noms.append(f"le {NOMS_FR[JOURS.index(morceau)]}")
    texte = noms[0] if len(noms) == 1 else ", ".join(noms[:-1]) + " et " + noms[-1]
    return f"{texte} {a}"
