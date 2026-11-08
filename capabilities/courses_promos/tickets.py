"""Lecture des tickets de caisse : lignes d'achat (article, quantité, prix, date, magasin), sans donnée sensible.

Deux entrées, une seule sortie (:class:`Ticket`) :

- **ticket photographié** : la personne envoie la photo dans la conversation ; l'assistant la lit lui-même en suivant
  :func:`consigne_lecture` et renvoie un JSON que :func:`valider_lecture` contrôle strictement (clés connues, types,
  bornes, cohérence quantité × prix unitaire = montant, date plausible). Une erreur dit précisément quoi relire ;
- **ticket reçu par mail** (texte, HTML, ``.eml`` ou PDF) : :func:`texte_du_ticket` en tire le texte, :func:`masquer`
  retire ce qui est sensible, puis :func:`lire_texte` applique un gabarit générique déterministe (libellé + montant,
  ligne « 2 x 1,25 », remises négatives, total). Si le gabarit ne reconnaît rien, :class:`GabaritNonReconnu` porte le
  texte **déjà filtré** : c'est lui seul qui peut être confié au modèle (spec §3.2).

Ce qui ne sort jamais d'ici : numéro de carte bancaire, numéro de carte de fidélité, nom, téléphone, courriel, lignes
de paiement (CB, autorisation, rendu monnaie). Aucune dépendance au cadre ; ``pypdf`` est facultatif (PDF).
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import html
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from zoneinfo import ZoneInfo

TYPES_LIGNE = ("article", "remise")
UNITES = ("piece", "kg")
CLES_TICKET = frozenset({"enseigne", "magasin", "date", "heure", "lignes", "total", "lisible", "doutes"})
CLES_LIGNE = frozenset({"libelle", "type", "quantite", "unite", "prix_unitaire", "montant"})
LIGNES_MAX = 200
LIBELLE_MAX = 80
QUANTITE_MAX = 999
PRIX_MAX = 10_000.0
ECART_LIGNE = 0.02  # € : tolérance d'arrondi entre quantité × prix unitaire et montant
ECART_TOTAL = 0.05  # € : au-delà, le ticket est gardé mais « à vérifier »
ANCIENNETE_MAX_J = 400


class TicketInvalide(ValueError):
    """Lecture refusée ; le message dit en français ce qu'il faut relire ou corriger."""


class GabaritNonReconnu(TicketInvalide):
    """Le gabarit déterministe n'a rien reconnu : ``texte_filtre`` (sans donnée sensible) peut aller au modèle."""

    def __init__(self, message: str, texte_filtre: str) -> None:
        super().__init__(message)
        self.texte_filtre = texte_filtre


@dataclass
class LigneAchat:
    libelle: str
    montant: float
    quantite: float = 1.0
    unite: str = "piece"
    prix_unitaire: float | None = None
    type: str = "article"

    def en_dict(self) -> dict[str, Any]:
        return {"libelle": self.libelle, "type": self.type, "quantite": self.quantite, "unite": self.unite,
                "prix_unitaire": self.prix_unitaire, "montant": self.montant}


@dataclass
class Ticket:
    date: date
    lignes: list[LigneAchat]
    enseigne: str | None = None
    magasin: str | None = None
    heure: str | None = None
    total: float | None = None
    avertissements: list[str] = field(default_factory=list)

    @property
    def a_verifier(self) -> bool:
        return bool(self.avertissements)

    def somme_lignes(self) -> float:
        return round(sum(lg.montant for lg in self.lignes), 2)

    def empreinte(self) -> str:
        """Empreinte du contenu (même ticket lu deux fois, par photo puis par mail : même empreinte)."""
        canon = {
            "date": self.date.isoformat(),
            "total": self.total,
            "lignes": sorted((normaliser(lg.libelle), round(lg.montant, 2)) for lg in self.lignes),
        }
        return hashlib.sha256(json.dumps(canon, sort_keys=True).encode("utf-8")).hexdigest()

    def en_dict(self) -> dict[str, Any]:
        return {"enseigne": self.enseigne, "magasin": self.magasin, "date": self.date.isoformat(), "heure": self.heure,
                "total": self.total, "lignes": [lg.en_dict() for lg in self.lignes],
                "avertissements": list(self.avertissements)}


# --- utilitaires -------------------------------------------------------------------------------------------------


def normaliser(texte: Any) -> str:
    """Minuscules, sans accents, ponctuation → espace : « CAFÉ MOUL.ARAB 250G » → « cafe moul arab 250g »."""
    t = unicodedata.normalize("NFKD", str(texte).casefold())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^\w%]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _nombre(valeur: str) -> float:
    return float(valeur.replace("−", "-").replace(" ", "").replace(",", "."))


# --- données sensibles ---------------------------------------------------------------------------------------------

_RE_COURRIEL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_RE_TELEPHONE = re.compile(r"(?<![\w.])(?:\+33\s?|0)[1-9](?:[ .-]?\d{2}){4}(?![\w.])")
_RE_CARTE_MASQUEE = re.compile(r"(?:[*Xx•#]{4,}[ -]?){1,4}(\d{4})\b")
_RE_FIDELITE = re.compile(
    r"\b(?:carte|client|fid[eé]lit[eé]|adh[eé]rent|compte)\b[^\n\d]{0,25}(?:n[°o]\s*)?[:#]?\s*(\d[\d ]{5,}\d)",
    re.IGNORECASE,
)
_RE_NOM_CLIENT = re.compile(r"\b(?:bonjour|client|cliente|titulaire|porteur)[ \t]*:?[ \t]+(?:m\.|mme|mr|m)?[ \t]*"
                            r"[A-Z][\w'-]+(?:[ \t]+[A-Z][\w'-]+)?", re.IGNORECASE)
# Lignes de paiement et de pied de ticket : jamais lues comme achats, jamais transmises.
_MOTS_PAIEMENT = re.compile(
    r"\b(?:CB|C\.B\.|CARTE BANCAIRE|CARTE BLEUE|VISA|MASTERCARD|MAESTRO|AMEX|SANS CONTACT|CONTACTLESS|AUTORISATION|"
    r"AUTOR|TRANSACTION|TRANS\.?|AID|TERMINAL|DEBIT|CRYPTOGRAMME|RENDU|RENDU MONNAIE|ESPECES|CHEQUE|TICKET CLIENT|"
    r"A CONSERVER|NUMERO DE COMMERCANT|NO COMMERCANT|SIRET|SIREN|TVA INTRA|N TVA)\b",
    re.IGNORECASE,
)


def masquer(texte: str) -> str:
    """Remplace ce qui est personnel ou bancaire par un marqueur (avant tout appel au modèle, avant tout stockage)."""
    from coeur import filtre_sensible

    t = filtre_sensible.masquer(texte or "")
    t = _RE_CARTE_MASQUEE.sub(lambda m: f"[carte bancaire ••{m.group(1)}]", t)
    t = _RE_FIDELITE.sub(lambda m: m.group(0)[: m.start(1) - m.start(0)] + "[carte fidélité]", t)
    t = _RE_COURRIEL.sub("[courriel]", t)
    t = _RE_TELEPHONE.sub("[téléphone]", t)
    t = _RE_NOM_CLIENT.sub(lambda m: m.group(0).split()[0] + " [nom]", t)
    return t


def _ligne_sensible(texte: str) -> bool:
    return "[" in masquer(texte) and "[" not in texte


# --- consigne de lecture d'une photo (pour le modèle) ---------------------------------------------------------------


def consigne_lecture(enseigne: str | None = None) -> str:
    """Consigne donnée à l'assistant pour lire un ticket photographié ; sa réponse passe par :func:`valider_lecture`."""
    precision = f" Le foyer fait d'habitude ses courses chez « {enseigne} »." if enseigne else ""
    return (
        "Lis le ticket de caisse photographié et réponds par un seul objet JSON, sans texte autour." + precision + "\n"
        "Schéma (aucune autre clé) :\n"
        '{"enseigne": "texte ou null", "magasin": "ville ou nom du magasin imprimé, ou null",\n'
        ' "date": "AAAA-MM-JJ", "heure": "HH:MM ou null", "total": nombre ou null, "lisible": true,\n'
        ' "doutes": ["libellés que tu n\'as pas pu lire avec certitude"],\n'
        ' "lignes": [{"libelle": "texte exact imprimé", "type": "article" ou "remise", "quantite": nombre,\n'
        '             "unite": "piece" ou "kg", "prix_unitaire": nombre ou null, "montant": nombre}]}\n'
        "Règles :\n"
        "- une ligne par article payé, dans l'ordre du ticket ; recopie le libellé tel qu'il est imprimé "
        "(abréviations comprises), sans l'interpréter ;\n"
        "- « 2 x 1,25 » sous un article : quantite 2, prix_unitaire 1.25 ; un article pesé : unite \"kg\", quantite = "
        "poids, prix_unitaire = prix au kilo ;\n"
        "- une remise, un bon ou une promotion déduite en caisse : ligne \"remise\" juste après l'article concerné, "
        "montant négatif ; ce qui est crédité sur une cagnotte de fidélité n'est pas une remise : ne le recopie pas ;\n"
        "- montants en euros avec un point décimal ; total = total à payer imprimé ;\n"
        "- ne recopie jamais : moyens de paiement, numéros de carte (bancaire ou fidélité), nom, adresse, téléphone, "
        "courriel, codes d'autorisation ;\n"
        "- ne devine jamais un prix : si un montant est illisible, mets la ligne dans \"doutes\" et ne la mets pas "
        "dans \"lignes\" ; si le ticket entier est illisible, réponds {\"lisible\": false, \"date\": null, "
        "\"lignes\": []}."
    )


def extraire_json(reponse: str | dict[str, Any]) -> dict[str, Any]:
    """Objet JSON de la réponse du modèle (bloc ```json``` toléré, rien d'autre)."""
    if isinstance(reponse, dict):
        return reponse
    texte = str(reponse or "").strip()
    bloc = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", texte, re.DOTALL)
    if bloc:
        texte = bloc.group(1)
    try:
        donnees = json.loads(texte)
    except ValueError as exc:
        raise TicketInvalide(f"Réponse illisible : un seul objet JSON est attendu ({exc.msg}).") from None
    if not isinstance(donnees, dict):
        raise TicketInvalide("Réponse illisible : un objet JSON est attendu, pas une liste ni une valeur.")
    return donnees


def _texte_court(valeur: Any, champ: str, maxi: int, obligatoire: bool = False) -> str | None:
    if valeur is None and not obligatoire:
        return None
    if not isinstance(valeur, str) or not valeur.strip():
        raise TicketInvalide(f"{champ} : texte non vide attendu.")
    valeur = " ".join(valeur.split())
    if len(valeur) > maxi:
        raise TicketInvalide(f"{champ} : {maxi} caractères au plus.")
    return valeur


def _nombre_json(valeur: Any, champ: str, *, mini: float | None = None, maxi: float | None = None,
                 nul: bool = False) -> float | None:
    if valeur is None and nul:
        return None
    if isinstance(valeur, bool) or not isinstance(valeur, (int, float)):
        raise TicketInvalide(f"{champ} : nombre attendu (point décimal), reçu {type(valeur).__name__}.")
    v = float(valeur)
    if not math.isfinite(v):
        raise TicketInvalide(f"{champ} : nombre invalide.")
    if mini is not None and v < mini:
        raise TicketInvalide(f"{champ} : doit être ≥ {mini:g}.")
    if maxi is not None and v > maxi:
        raise TicketInvalide(f"{champ} : doit être ≤ {maxi:g}.")
    return round(v, 3)


def valider_lecture(reponse: str | dict[str, Any], *, aujourd_hui: date) -> Ticket:
    """Contrôle strict de la lecture d'un ticket par le modèle. Lève :class:`TicketInvalide` avec ce qu'il faut
    relire. Un écart entre la somme des lignes et le total n'est pas une erreur : le ticket est « à vérifier »."""
    d = extraire_json(reponse)
    inconnues = sorted(set(d) - CLES_TICKET)
    if inconnues:
        raise TicketInvalide(f"Clés inconnues : {', '.join(inconnues)} (schéma de la consigne uniquement).")
    lisible = d.get("lisible", True)
    if not isinstance(lisible, bool):
        raise TicketInvalide("lisible : true ou false.")
    if not lisible:
        raise TicketInvalide("Ticket illisible : demande une photo plus nette, à plat, bien éclairée.")
    brut_date = d.get("date")
    try:
        jour = date.fromisoformat(str(brut_date)) if isinstance(brut_date, str) else None
    except ValueError:
        jour = None
    if jour is None:
        raise TicketInvalide("date : AAAA-MM-JJ attendu (date imprimée sur le ticket).")
    if jour > aujourd_hui + timedelta(days=1) or jour < aujourd_hui - timedelta(days=ANCIENNETE_MAX_J):
        raise TicketInvalide(f"date : {jour.isoformat()} n'est pas plausible, relis la date du ticket.")
    heure = d.get("heure")
    if heure is not None and (not isinstance(heure, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", heure)):
        raise TicketInvalide("heure : HH:MM ou null.")
    enseigne = _texte_court(d.get("enseigne"), "enseigne", 60)
    magasin = _texte_court(d.get("magasin"), "magasin", 80)
    total = _nombre_json(d.get("total"), "total", mini=0, maxi=PRIX_MAX, nul=True)
    doutes = d.get("doutes", [])
    if not isinstance(doutes, list) or not all(isinstance(x, str) for x in doutes) or len(doutes) > 20:
        raise TicketInvalide("doutes : liste de textes (20 au plus).")
    lignes_brutes = d.get("lignes")
    if not isinstance(lignes_brutes, list) or not lignes_brutes:
        raise TicketInvalide("lignes : liste non vide attendue.")
    if len(lignes_brutes) > LIGNES_MAX:
        raise TicketInvalide(f"lignes : {LIGNES_MAX} au plus.")

    avertissements: list[str] = []
    lignes: list[LigneAchat] = []
    for i, brute in enumerate(lignes_brutes, start=1):
        quoi = f"ligne {i}"
        if not isinstance(brute, dict):
            raise TicketInvalide(f"{quoi} : objet attendu.")
        inconnues = sorted(set(brute) - CLES_LIGNE)
        if inconnues:
            raise TicketInvalide(f"{quoi} : clés inconnues {', '.join(inconnues)}.")
        libelle = _texte_court(brute.get("libelle"), f"{quoi}, libelle", LIBELLE_MAX, obligatoire=True)
        assert libelle is not None
        quoi = f"ligne {i} (« {libelle} »)"
        type_ = brute.get("type", "article")
        if type_ not in TYPES_LIGNE:
            raise TicketInvalide(f"{quoi} : type « article » ou « remise ».")
        unite = brute.get("unite", "piece")
        if unite not in UNITES:
            raise TicketInvalide(f"{quoi} : unite « piece » ou « kg ».")
        quantite = _nombre_json(brute.get("quantite", 1), f"{quoi}, quantite", maxi=QUANTITE_MAX)
        assert quantite is not None
        if quantite <= 0:
            raise TicketInvalide(f"{quoi}, quantite : doit être > 0.")
        if unite == "piece" and type_ == "article" and quantite != int(quantite):
            raise TicketInvalide(f"{quoi} : quantité décimale pour un article à la pièce (unite \"kg\" ?).")
        pu = _nombre_json(brute.get("prix_unitaire"), f"{quoi}, prix_unitaire", mini=0, maxi=PRIX_MAX, nul=True)
        montant = _nombre_json(brute.get("montant"), f"{quoi}, montant", mini=-PRIX_MAX, maxi=PRIX_MAX)
        assert montant is not None
        if type_ == "article" and montant < 0:
            raise TicketInvalide(f"{quoi} : montant négatif, est-ce une remise ?")
        if type_ == "remise" and montant >= 0:
            raise TicketInvalide(f"{quoi} : une remise a un montant négatif.")
        if type_ == "remise" and not lignes:
            raise TicketInvalide(f"{quoi} : une remise suit l'article qu'elle concerne.")
        if pu is not None and type_ == "article":
            attendu = round(quantite * pu, 2)
            if abs(attendu - montant) > max(ECART_LIGNE, 0.01 * montant):
                raise TicketInvalide(
                    f"{quoi} : {quantite:g} × {pu:.2f} = {attendu:.2f}, pas {montant:.2f} ; relis cette ligne."
                )
        if _ligne_sensible(libelle):
            avertissements.append(f"ligne {i} écartée : elle contenait une donnée personnelle ou bancaire")
            continue
        lignes.append(LigneAchat(libelle=libelle, montant=montant, quantite=quantite, unite=unite,
                                 prix_unitaire=pu if pu is not None else (round(montant / quantite, 3)
                                                                          if type_ == "article" else None),
                                 type=type_))
    if not any(lg.type == "article" for lg in lignes):
        raise TicketInvalide("Aucun article lisible sur ce ticket.")
    ticket = Ticket(date=jour, lignes=lignes, enseigne=masquer(enseigne) if enseigne else None,
                    magasin=masquer(magasin) if magasin else None, heure=heure, total=total)
    for x in doutes:
        avertissements.append(f"lecture incertaine : {masquer(' '.join(x.split()))[:LIBELLE_MAX]}")
    _controler_total(ticket, avertissements)
    ticket.avertissements = avertissements
    return ticket


def _controler_total(ticket: Ticket, avertissements: list[str]) -> None:
    if ticket.total is None:
        return
    ecart = round(ticket.total - ticket.somme_lignes(), 2)
    if abs(ecart) > ECART_TOTAL:
        avertissements.append(f"écart de {ecart:+.2f} € entre la somme des lignes et le total imprimé")


# --- texte d'un ticket reçu par mail ---------------------------------------------------------------------------------


class _HtmlEnTexte(HTMLParser):
    """HTML de mail → texte : une ligne par rangée de tableau ou bloc, cellules séparées par deux espaces."""

    BLOCS = frozenset({"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table", "section"})
    IGNORES = frozenset({"script", "style", "head", "title"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.morceaux: list[str] = []
        self._ignore = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.IGNORES:
            self._ignore += 1
        elif tag in self.BLOCS:
            self.morceaux.append("\n")
        elif tag in ("td", "th"):
            self.morceaux.append("  ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.IGNORES and self._ignore:
            self._ignore -= 1
        elif tag in self.BLOCS:
            self.morceaux.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignore:
            self.morceaux.append(data.replace("\xa0", " "))

    def texte(self) -> str:
        lignes = ["  ".join(p.strip() for p in re.split(r"\s{2,}", ligne) if p.strip())
                  for ligne in "".join(self.morceaux).splitlines()]
        return "\n".join(x for x in lignes if x)


def html_en_texte(contenu: str) -> str:
    analyseur = _HtmlEnTexte()
    analyseur.feed(contenu)
    analyseur.close()
    return html.unescape(analyseur.texte())


def pdf_en_texte(contenu: bytes) -> str:
    try:
        from pypdf import PdfReader  # facultatif : tools/requirements.txt
    except ImportError:  # pragma: no cover - dépend de l'installation
        raise TicketInvalide("Lecture des PDF indisponible sur ce serveur (module pypdf absent).") from None
    import io

    try:
        lecteur = PdfReader(io.BytesIO(contenu))
        if len(lecteur.pages) > 10:
            raise TicketInvalide("PDF trop long pour un ticket (10 pages au plus).")
        return "\n".join((page.extract_text() or "") for page in lecteur.pages)
    except TicketInvalide:
        raise
    except Exception:  # noqa: BLE001 - PDF abîmé ou chiffré : message simple
        raise TicketInvalide("PDF illisible (abîmé ou protégé).") from None


def texte_du_ticket(contenu: bytes | str, nom: str = "") -> str:
    """Texte brut d'un ticket reçu par mail : ``.eml`` (partie HTML, texte ou PDF joint), HTML, PDF ou texte."""
    nom = nom.lower()
    if isinstance(contenu, bytes) and (nom.endswith(".pdf") or contenu[:5] == b"%PDF-"):
        return pdf_en_texte(contenu)
    if nom.endswith(".eml") or (isinstance(contenu, bytes) and re.match(rb"(?i)(received|from|return-path|mime-version)"
                                                                        rb":", contenu[:200] or b"")):
        brut = contenu if isinstance(contenu, bytes) else contenu.encode("utf-8")
        message = email.message_from_bytes(brut, policy=email.policy.default)
        for partie in message.walk():
            if partie.get_content_type() == "application/pdf":
                charge = partie.get_payload(decode=True)
                if isinstance(charge, bytes):
                    return pdf_en_texte(charge)
        corps = message.get_body(preferencelist=("html", "plain"))  # type: ignore[attr-defined]
        if corps is None:
            raise TicketInvalide("Mail sans corps lisible.")
        texte = corps.get_content()
        return html_en_texte(texte) if corps.get_content_type() == "text/html" else str(texte)
    texte = contenu.decode("utf-8", errors="replace") if isinstance(contenu, bytes) else contenu
    if nom.endswith((".html", ".htm")) or re.search(r"(?i)<(html|table|body|div)\b", texte[:2000]):
        return html_en_texte(texte)
    return texte


# --- gabarit générique déterministe ----------------------------------------------------------------------------------

_PRIX = r"-?\d{1,5}[,.]\d{2}"
_RE_DATE = re.compile(r"\b(\d{2})[/.-](\d{2})[/.-](\d{2}|\d{4})\b(?:\D{0,6}(\d{2})[:hH](\d{2}))?")
_RE_TOTAL = re.compile(rf"^\s*(?:TOTAL|NET|MONTANT)\b[^\d-]*?(?:A PAYER|TTC|\b)[^\d-]*({_PRIX})\s*(?:€|EUR)?\s*$",
                       re.IGNORECASE)
_RE_SOUS_TOTAL = re.compile(r"^\s*(?:SOUS[- ]?TOTAL|S/TOTAL|TOTAL HT|TOTAL TVA|TOTAL REMISES?|TOTAL ECONOMIES?|"
                            r"VOS ECONOMIES|NB(?:RE)? (?:D')?ARTICLES?|NOMBRE D'ARTICLES)", re.IGNORECASE)
_RE_LIGNE_QTE_SEULE = re.compile(
    rf"^\s*(?P<q>\d{{1,3}}(?:[,.]\d{{1,3}})?)\s*(?P<kg>[kK][gG])?\s*[xX*×]\s*(?P<pu>{_PRIX})\s*(?:€|EUR)?\s*(?:/\s*[kK][gG])?\s*"
    rf"(?:(?P<montant>{_PRIX})\s*(?:€|EUR)?)?\s*(?:[A-Z]\d?|\d|\*)?\s*$",
)
_RE_ARTICLE = re.compile(
    rf"^\s*(?P<lib>\S.*?)\s{{2,}}(?:(?P<q>\d{{1,3}}(?:[,.]\d{{1,3}})?)\s*(?P<kg>[kK][gG])?\s*[xX*×]\s*(?P<pu>{_PRIX})\s*"
    rf"(?:€|EUR)?\s*(?:/\s*[kK][gG])?\s+)?(?P<montant>{_PRIX})\s*(?:€|EUR)?\s*(?:[A-Z]\d?|\d|\*)?\s*$"
)
_RE_REMISE = re.compile(r"\b(?:REMISE|REDUC|R[EÉ]DUCTION|BON|PROMO|RABAIS|IMMEDIATE?|COUPON|OFFRE|LOT|DONT)\b",
                        re.IGNORECASE)
_RE_IGNORER = re.compile(r"^\s*(?:TVA|HT|TTC|TAUX|BASE|MERCI|A BIENTOT|BIENVENUE|CAISSE|CAISSIER|HOTESSE|"
                         r"TICKET|OUVERT|DU LUNDI|HORAIRES|WWW\.|HTTP|BONJOUR|VOICI|CHER|CHERE|EUR\s*$)", re.IGNORECASE)


def _date_du_texte(texte: str) -> tuple[date | None, str | None]:
    for m in _RE_DATE.finditer(texte):
        jj, mm, aa, h, mi = m.groups()
        annee = int(aa) + (2000 if len(aa) == 2 else 0)
        try:
            jour = date(annee, int(mm), int(jj))
        except ValueError:
            continue
        heure = f"{int(h):02d}:{mi}" if h and mi and int(h) < 24 and int(mi) < 60 else None
        return jour, heure
    return None, None


def lire_texte(texte: str, *, enseigne: str | None = None, aujourd_hui: date | None = None) -> Ticket:
    """Gabarit générique d'un ticket texte (mail, PDF). Le texte est d'abord filtré (:func:`masquer`) ; si rien
    n'est reconnu, :class:`GabaritNonReconnu` porte ce texte filtré pour une lecture par le modèle."""
    aujourd_hui = aujourd_hui or datetime.now(ZoneInfo("Europe/Paris")).date()
    filtre = masquer(texte or "")
    lignes_texte = [ligne.rstrip() for ligne in filtre.replace("\r", "").replace("\t", "  ").splitlines()]
    jour, heure = _date_du_texte(filtre)
    lignes: list[LigneAchat] = []
    total: float | None = None
    magasin: str | None = None
    en_attente: str | None = None  # libellé seul sur sa ligne, prix sur la ligne suivante
    avertissements: list[str] = []
    for brute in lignes_texte:
        ligne = brute.replace("−", "-").replace("\xa0", " ")
        if not ligne.strip():
            continue
        if total is not None:
            break  # après le total : paiement, TVA, fidélité ; rien n'est un achat
        if _MOTS_PAIEMENT.search(ligne) or "[" in ligne:
            continue
        m_total = _RE_TOTAL.match(ligne)
        if m_total and not _RE_SOUS_TOTAL.match(ligne):
            total = _nombre(m_total.group(1))
            continue
        if _RE_SOUS_TOTAL.match(ligne) or _RE_IGNORER.match(ligne):
            continue
        m_q = _RE_LIGNE_QTE_SEULE.match(ligne)
        if m_q:
            unite = "kg" if (m_q.group("kg") or "/kg" in ligne.replace(" ", "").lower()) else "piece"
            q, pu = _nombre(m_q.group("q")), _nombre(m_q.group("pu"))
            if en_attente and m_q.group("montant"):  # « LAIT 1L » puis « 6 x 0,99   5,94 »
                lignes.append(LigneAchat(libelle=en_attente, montant=_nombre(m_q.group("montant")), quantite=q,
                                         unite=unite, prix_unitaire=pu))
            elif lignes and lignes[-1].type == "article":  # « CAFE  6,98 » puis « 2 x 3,49 »
                precedente = lignes[-1]
                precedente.quantite, precedente.unite, precedente.prix_unitaire = q, unite, pu
                if m_q.group("montant"):
                    precedente.montant = _nombre(m_q.group("montant"))
            en_attente = None
            continue
        m = _RE_ARTICLE.match(ligne)
        if not m:
            texte_ligne = " ".join(ligne.split())
            if magasin is None and not lignes and not _RE_DATE.search(ligne) and len(texte_ligne) <= 60:
                magasin = texte_ligne
            en_attente = texte_ligne[:LIBELLE_MAX] if re.search(r"[A-Za-z]{2}", texte_ligne) else None
            continue
        en_attente = None
        libelle = " ".join(m.group("lib").split())[:LIBELLE_MAX]
        montant = _nombre(m.group("montant"))
        if montant < 0 or (_RE_REMISE.search(libelle) and montant <= 0):
            if lignes:
                lignes.append(LigneAchat(libelle=libelle, montant=-abs(montant), type="remise"))
            continue
        q = _nombre(m.group("q")) if m.group("q") else 1.0
        pu = _nombre(m.group("pu")) if m.group("pu") else round(montant / q, 3)
        unite = "kg" if (m.group("kg") or "/kg" in ligne.replace(" ", "").lower()) else "piece"
        lignes.append(LigneAchat(libelle=libelle, montant=montant, quantite=q, unite=unite, prix_unitaire=pu))
    if not any(lg.type == "article" for lg in lignes) or jour is None:
        raise GabaritNonReconnu("Mise en page de ticket non reconnue.", filtre)
    for lg in lignes:
        if (lg.type == "article" and lg.prix_unitaire is not None
                and abs(round(lg.quantite * lg.prix_unitaire, 2) - lg.montant) > max(ECART_LIGNE, 0.01 * lg.montant)):
            avertissements.append(f"« {lg.libelle} » : quantité × prix unitaire ≠ montant")
    if jour > aujourd_hui + timedelta(days=1):
        raise GabaritNonReconnu("Date du ticket dans le futur : mise en page mal comprise.", filtre)
    ticket = Ticket(date=jour, lignes=lignes, enseigne=enseigne, magasin=magasin, heure=heure, total=total)
    _controler_total(ticket, avertissements)
    ticket.avertissements = avertissements
    return ticket
