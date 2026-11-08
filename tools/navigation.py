"""Navigation web des capacités fabriquées par l'atelier (lot 7b) : un seul point d'entrée Playwright, sûr par défaut.

Le connecteur d'une capacité (`capabilities/<nom>/connecteur.py`) n'importe jamais Playwright directement : il ouvre
une `Session`, qui apporte ce que Kitchen Daily a appris (étude interne sur la navigation web par IA) :

- navigateur isolé `homeos-web` (lot 7, `tools.kitchen_daily.navigateur_distant`) ou Chromium local en
  développement, un seul à la fois par processus, proxy sortant transmis ;
- **hôtes autorisés seulement** : ceux du manifeste approuvés par un propriétaire (`capacites.hotes_autorises`) ;
  toute requête vers un autre hôte est bloquée et journalisée (le proxy filtrant reste la vraie frontière) ;
  https obligatoire hors 127.0.0.1/localhost ;
- **robots.txt** lu et respecté avant chaque page (refus → `RobotsRefuse`) ;
- **pas de contournement anti-bot** : une page de vérification humaine (Cloudflare, captcha) arrête tout
  (`VerificationHumaine`) et rend la main à la personne ;
- bannières de cookies refusées quand c'est possible, attente d'hydratation (`networkidle` borné) ;
- **écoute réseau** (`ecouter_json`) : les réponses JSON d'une API interne valent mieux que le DOM ;
- **captures**, instantané d'accessibilité (`aria_snapshot`) et **journal** (`journal.jsonl`, secrets masqués) dans
  `<etat>/capacites-etat/<nom>/` (700, homeos) ; en cas d'échec, `echec()` range capture + instantané dans
  `incidents/` : c'est « ce que l'atelier a appris » pour la correction suivante ;
- état de session (`storage_state`) par personne, en 600, jamais dans `data/` ni dans le navigateur distant.

CLI d'exploration (utilisée par l'atelier avant la génération, et par le sous-agent) :

    python -m tools.navigation explorer <url> --hotes a.fr,b.fr --sortie exploration/

écrit `page.html` (scripts retirés), `aria.txt`, `capture.png`, `reseau.json` (requêtes, hôtes bloqués),
`liens.json` (dont CGU et mentions), `cgu-<n>.txt` (texte des conditions d'utilisation liées), `robots.txt`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import time
import urllib.robotparser
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import urljoin, urlsplit, urlunsplit

from tools.common import get_logger, iso_now, now

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import Page

log = get_logger("homeos.navigation")

TIMEOUT_MS = 30_000
HYDRATATION_MS = 6_000
HOTES_LOCAUX = frozenset({"127.0.0.1", "localhost", "::1"})
CAPTURES_MAX = 60
AGENT_ROBOTS = "FirminAssistant"
MODE_PRIVE = 0o700

# Pages de vérification humaine : on s'arrête, on ne contourne jamais.
_MARQUEURS_ANTIBOT = re.compile(
    r"challenges\.cloudflare\.com|cf-challenge|cf_chl_|just a moment\.\.\.|attention required|"
    r"v[ée]rifi(?:ez|cation)[^<]{0,40}humain|verify (?:that )?you are (?:a )?human|are you a robot|"
    r"hcaptcha\.com/captcha|g-recaptcha[^>]*data-size=\"normal\"|captcha-delivery\.com|px-captcha",
    re.IGNORECASE,
)
_REFUS_COOKIES = re.compile(
    r"^\s*(continuer sans accepter|tout refuser|refuser(?: tout)?|je refuse|reject all|decline)\s*$", re.IGNORECASE
)
_LIENS_CGU = re.compile(r"cgu|cgv|conditions|mentions|legal|terms|robots", re.IGNORECASE)


# --- erreurs (messages destinés à la personne) -------------------------------------------------------------------


class ErreurSite(RuntimeError):
    """Échec sur le site, message simple pour la personne (le détail est dans le journal et les incidents)."""


class HoteRefuse(ErreurSite):
    pass


class RobotsRefuse(ErreurSite):
    pass


class VerificationHumaine(ErreurSite):
    pass


class SessionExpiree(ErreurSite):
    pass


# --- chemins -------------------------------------------------------------------------------------------------------


def dossier_prive(nom: str) -> Path:
    """`<etat>/capacites-etat/<nom>/` (700) : sessions, captures, journal, incidents d'une capacité."""
    from tools.common import etat_dir

    if not re.fullmatch(r"[a-z0-9_]+", nom or ""):
        raise ValueError(f"nom de capacité invalide : {nom!r}")
    chemin = etat_dir() / "capacites-etat" / nom
    _creer(chemin)
    return chemin


def _creer(chemin: Path) -> Path:
    if not chemin.exists():
        chemin.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(chemin, MODE_PRIVE)
    return chemin


def _ecrire_prive(chemin: Path, contenu: bytes | str) -> None:
    ancien = os.umask(0o077)
    try:
        tmp = chemin.with_name(f".{chemin.name}.{os.getpid()}.tmp")
        if isinstance(contenu, str):
            tmp.write_text(contenu, encoding="utf-8")
        else:
            tmp.write_bytes(contenu)
        os.replace(tmp, chemin)
    finally:
        os.umask(ancien)


# --- hôtes ---------------------------------------------------------------------------------------------------------


def normaliser_hotes(hotes: list[str] | tuple[str, ...]) -> list[str]:
    return sorted({str(h).strip().lower().rstrip(".") for h in hotes if str(h).strip()})


def hote_autorise(hote: str, hotes: list[str]) -> bool:
    """Hôte exact, ou `*.domaine` (sous-domaines, pas le domaine nu)."""
    hote = (hote or "").lower().rstrip(".")
    for h in hotes:
        if h.startswith("*."):
            if hote.endswith(h[1:]):
                return True
        elif hote == h:
            return True
    return False


def verifier_url(url: str, hotes: list[str]) -> str:
    """URL absolue http(s) vers un hôte autorisé (https obligatoire hors poste local) ; renvoie l'URL."""
    parties = urlsplit(url)
    hote = (parties.hostname or "").lower()
    if parties.scheme not in ("http", "https") or not hote:
        raise HoteRefuse(f"Adresse refusée : {url!r} (http ou https attendu).")
    if parties.scheme == "http" and hote not in HOTES_LOCAUX:
        raise HoteRefuse(f"Adresse refusée : {hote} doit être joint en https.")
    if not hote_autorise(hote, hotes):
        raise HoteRefuse(f"Le site {hote} n'est pas dans les sites approuvés pour cette capacité.")
    return url


def url_journal(url: str) -> str:
    """URL sans requête ni fragment (jetons, identifiants de session) pour le journal."""
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def est_page_antibot(html: str, titre: str = "") -> bool:
    return bool(_MARQUEURS_ANTIBOT.search(titre or "") or _MARQUEURS_ANTIBOT.search((html or "")[:200_000]))


# --- session -----------------------------------------------------------------------------------------------------


class Session:
    """Navigateur d'une capacité. Usage dans un connecteur :

        with Session.pour(ctx) as s:
            page = s.ouvrir(base + "/connexion")
            ...
            s.capture("apres-connexion")

    `nom` : capacité ; `hotes` : hôtes autorisés ; `personne` : à qui appartient l'état de session (None = foyer) ;
    `dossier` : remplace `dossier_prive(nom)` (tests) ; `session` : conserver cookies et stockage local entre deux
    utilisations (par personne, 600)."""

    def __init__(
        self,
        nom: str,
        hotes: list[str],
        personne: str | None = None,
        dossier: Path | None = None,
        session: bool = True,
        respecter_robots: bool = True,
    ) -> None:
        self.nom = nom
        self.hotes = normaliser_hotes(hotes)
        if not self.hotes:
            raise HoteRefuse("Aucun site approuvé pour cette capacité : un propriétaire doit d'abord l'approuver.")
        self.personne = personne
        self.dossier = _creer(Path(dossier)) if dossier is not None else dossier_prive(nom)
        self.garder_session = session
        self.respecter_robots = respecter_robots
        self.bloques: set[str] = set()
        self.requetes: list[dict[str, Any]] = []
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._ecoutes: list[tuple[re.Pattern[str], list[Any]]] = []
        self._pw: Any = None
        self._navigateur: Any = None
        self._contexte: Any = None
        self.page: Page | None = None
        self._semaphore_pris = False

    @classmethod
    def pour(cls, ctx: Any, **options: Any) -> Session:
        """Session pour la capacité de `ctx` : hôtes du manifeste approuvés, personne courante."""
        from tools import capacites

        if ctx.capacite is None:
            raise ErreurSite("Navigation impossible hors d'une capacité.")
        hotes = capacites.hotes_autorises(ctx.capacite, ctx.donnees)
        return cls(ctx.nom, hotes, personne=options.pop("personne", ctx.personne), **options)

    # -- chemins
    @property
    def fichier_session(self) -> Path:
        qui = re.sub(r"[^a-z0-9_]", "", (self.personne or "foyer").lower()) or "foyer"
        return _creer(self.dossier / "sessions") / f"{qui}.json"

    def oublier_session(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.fichier_session.unlink()

    # -- cycle de vie
    def __enter__(self) -> Self:
        from tools import kitchen_daily as kd

        if not kd._NAVIGATEURS.acquire(timeout=kd.ATTENTE_NAVIGATEUR_S):
            raise ErreurSite("Le navigateur est déjà occupé : réessaie dans une minute.")
        self._semaphore_pris = True
        try:
            self._demarrer()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def _demarrer(self) -> None:
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ErreurSite("Le navigateur n'est pas installé sur le serveur (Playwright).") from exc
        from tools import kitchen_daily as kd

        self._pw = sync_playwright().start()
        options = kd.options_lancement()
        if "proxy" in options and any(h in HOTES_LOCAUX for h in self.hotes):
            contourne = options["proxy"].get("bypass") or ""
            options["proxy"]["bypass"] = ",".join(x for x in (contourne, "127.0.0.1,localhost") if x)
        ws = kd.navigateur_distant()
        try:
            if ws:
                distantes = {k: v for k, v in options.items() if k in ("headless", "proxy")}
                self._navigateur = self._pw.chromium.connect(
                    ws, timeout=TIMEOUT_MS, headers={"x-playwright-launch-options": json.dumps(distantes)}
                )
            else:
                self._navigateur = self._pw.chromium.launch(**options)
        except PlaywrightError as exc:
            raise ErreurSite("Le navigateur du serveur ne répond pas (service homeos-web).") from exc
        etat = None
        if self.garder_session and self.fichier_session.exists():
            try:
                etat = json.loads(self.fichier_session.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                etat = None
        self._contexte = self._navigateur.new_context(
            storage_state=etat, locale="fr-FR", timezone_id="Europe/Paris", viewport={"width": 1280, "height": 1400}
        )
        self._contexte.route("**/*", self._filtrer)
        self.page = self._contexte.new_page()
        self.page.set_default_timeout(TIMEOUT_MS)
        self.page.set_default_navigation_timeout(TIMEOUT_MS)
        self.page.on("response", self._sur_reponse)

    def __exit__(self, *exc: object) -> None:
        try:
            if self._contexte is not None and self.garder_session and exc[0] is None:
                with contextlib.suppress(Exception):
                    _ecrire_prive(self.fichier_session, json.dumps(self._contexte.storage_state(), ensure_ascii=False))
            if self.bloques:
                self.journal("hotes_bloques", hotes=sorted(self.bloques))
        finally:
            for fermer in (
                lambda: self._navigateur and self._navigateur.close(),
                lambda: self._pw and self._pw.stop(),
            ):
                with contextlib.suppress(Exception):
                    fermer()
            self._navigateur = self._pw = self._contexte = self.page = None
            if self._semaphore_pris:
                from tools import kitchen_daily as kd

                kd._NAVIGATEURS.release()
                self._semaphore_pris = False

    # -- réseau
    def _filtrer(self, route: Any) -> None:
        url = route.request.url
        if url.startswith(("data:", "blob:", "about:")):
            route.continue_()
            return
        hote = (urlsplit(url).hostname or "").lower()
        if hote_autorise(hote, self.hotes):
            route.continue_()
            return
        self.bloques.add(hote)
        route.abort("blockedbyclient")

    def _sur_reponse(self, reponse: Any) -> None:
        try:
            requete = reponse.request
            if requete.resource_type in ("xhr", "fetch", "document"):
                self.requetes.append({"url": url_journal(reponse.url), "methode": requete.method,
                                      "statut": reponse.status, "type": requete.resource_type})
            if not self._ecoutes:
                return
            for motif, collecte in self._ecoutes:
                if motif.search(reponse.url):
                    with contextlib.suppress(Exception):
                        collecte.append(reponse.json())
        except Exception:
            log.debug("réponse non lue", exc_info=True)

    def ecouter_json(self, motif: str) -> list[Any]:
        """Collecte (liste remplie au fil de l'eau) les réponses JSON dont l'URL correspond à `motif` (regex)."""
        collecte: list[Any] = []
        self._ecoutes.append((re.compile(motif), collecte))
        return collecte

    # -- robots.txt
    def robots_autorise(self, url: str) -> bool:
        p = urlsplit(url)
        racine = f"{p.scheme}://{p.netloc}"
        if racine not in self._robots:
            analyseur: urllib.robotparser.RobotFileParser | None = urllib.robotparser.RobotFileParser()
            try:
                assert self._contexte is not None
                reponse = self._contexte.request.get(racine + "/robots.txt", timeout=10_000)
                if reponse.status >= 500:
                    analyseur = None  # serveur en panne : prudence, on ne navigue pas
                elif reponse.status >= 400:
                    analyseur.parse([])  # pas de robots.txt : tout est permis
                else:
                    analyseur.parse(reponse.text().splitlines())
            except Exception:  # noqa: BLE001
                analyseur = None
            self._robots[racine] = analyseur
        analyseur = self._robots[racine]
        if analyseur is None:
            return False
        return analyseur.can_fetch(AGENT_ROBOTS, url) and analyseur.can_fetch("*", url)

    # -- navigation
    @property
    def p(self) -> Page:
        if self.page is None:
            raise ErreurSite("Session de navigation fermée.")
        return self.page

    def ouvrir(self, url: str, attendre: str | None = None) -> Page:
        """Ouvre une page d'un site approuvé (robots.txt respecté), refuse les cookies, attend l'hydratation ;
        `attendre` : sélecteur qui doit apparaître. Lève `VerificationHumaine` sur une page anti-robot."""
        verifier_url(url, self.hotes)
        if self.respecter_robots and not self.robots_autorise(url):
            self.journal("robots_refus", url=url_journal(url))
            raise RobotsRefuse("Le site demande aux robots de ne pas visiter cette page : je m'arrête là.")
        page = self.p
        page.goto(url, wait_until="domcontentloaded")
        self.journal("page", url=url_journal(page.url))
        self.verifier_humain()
        self.refuser_cookies()
        self.attendre_hydratation(attendre)
        return page

    def verifier_humain(self) -> None:
        page = self.p
        try:
            html, titre = page.content(), page.title()
        except Exception:  # noqa: BLE001
            return
        if est_page_antibot(html, titre):
            self.capture("verification-humaine")
            self.journal("verification_humaine", url=url_journal(page.url))
            raise VerificationHumaine(
                "Le site demande de prouver qu'on est humain : je ne contourne pas ça, je te rends la main."
            )

    def refuser_cookies(self) -> None:
        page = self.p
        with contextlib.suppress(Exception):
            bouton = page.get_by_role("button", name=_REFUS_COOKIES)
            if bouton.count():
                bouton.first.click(timeout=2000)

    def attendre_hydratation(self, selecteur: str | None = None) -> None:
        page = self.p
        with contextlib.suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=HYDRATATION_MS)
        if selecteur:
            page.wait_for_selector(selecteur, timeout=TIMEOUT_MS)

    # -- traces
    def journal(self, etape: str, **details: Any) -> None:
        from tools import capacites

        ligne = {"quand": iso_now(), "etape": etape, "personne": self.personne, **details}
        try:
            with (self.dossier / "journal.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(capacites.masquer_secrets(ligne), ensure_ascii=False, default=str) + "\n")
        except OSError:
            log.debug("journal de navigation non écrit", exc_info=True)

    def capture(self, libelle: str) -> Path | None:
        """Capture d'écran pleine page dans `captures/` (les plus anciennes au-delà de 60 sont effacées)."""
        dossier = _creer(self.dossier / "captures")
        nom = f"{now().strftime('%Y%m%d-%H%M%S')}-{re.sub(r'[^a-z0-9-]+', '-', libelle.lower()).strip('-')[:40]}.png"
        try:
            _ecrire_prive(dossier / nom, self.p.screenshot(full_page=True))
        except Exception:  # noqa: BLE001
            return None
        anciennes = sorted(dossier.glob("*.png"))[:-CAPTURES_MAX]
        for f in anciennes:
            with contextlib.suppress(OSError):
                f.unlink()
        self.journal("capture", fichier=nom)
        return dossier / nom

    def instantane(self) -> str:
        """Arbre d'accessibilité de la page (texte compact), utile au diagnostic et à la réparation."""
        with contextlib.suppress(Exception):
            return str(self.p.locator("body").aria_snapshot())[:20_000]
        return ""

    def echec(self, message: str, exc: BaseException | None = None) -> ErreurSite:
        """Range capture, instantané et contexte dans `incidents/<horodatage>/` ; renvoie l'erreur simple à lever."""
        from tools import capacites

        dossier = _creer(self.dossier / "incidents" / now().strftime("%Y%m%d-%H%M%S"))
        with contextlib.suppress(Exception):
            _ecrire_prive(dossier / "capture.png", self.p.screenshot(full_page=True))
        with contextlib.suppress(Exception):
            _ecrire_prive(dossier / "aria.txt", self.instantane())
        contexte = {"message": message, "url": url_journal(self.page.url) if self.page else None,
                    "erreur": f"{exc.__class__.__name__}: {exc}" if exc else None, "requetes": self.requetes[-30:],
                    "hotes_bloques": sorted(self.bloques)}
        _ecrire_prive(dossier / "contexte.json",
                      json.dumps(capacites.masquer_secrets(contexte), ensure_ascii=False, indent=1, default=str))
        self.journal("echec", message=message, incident=dossier.name)
        return ErreurSite(message)


# --- exploration (atelier) -------------------------------------------------------------------------------------------


def explorer(url: str, hotes: list[str], sortie: Path, dossier: Path | None = None) -> dict[str, Any]:
    """Visite publique d'une page (sans session) pour l'atelier : HTML sans scripts, arbre d'accessibilité, capture,
    requêtes réseau, hôtes tiers bloqués, liens (CGU, mentions), robots.txt. Renvoie un résumé."""
    from bs4 import BeautifulSoup

    sortie = _creer(Path(sortie))
    resume: dict[str, Any] = {"url": url_journal(url), "quand": iso_now()}
    with Session("exploration", hotes, dossier=dossier or sortie / ".session", session=False) as s:
        p = urlsplit(url)
        with contextlib.suppress(Exception):
            assert s._contexte is not None
            rep = s._contexte.request.get(f"{p.scheme}://{p.netloc}/robots.txt", timeout=10_000)
            (sortie / "robots.txt").write_text(rep.text()[:50_000] if rep.status < 400 else "", encoding="utf-8")
        page = s.ouvrir(url)
        soupe = BeautifulSoup(page.content(), "html.parser")
        for balise in soupe(["script", "style", "noscript", "svg"]):
            balise.decompose()
        (sortie / "page.html").write_text(str(soupe)[:400_000], encoding="utf-8")
        (sortie / "aria.txt").write_text(s.instantane(), encoding="utf-8")
        with contextlib.suppress(Exception):
            (sortie / "capture.png").write_bytes(page.screenshot(full_page=True))
        liens = []
        for a in soupe.find_all("a", href=True)[:300]:
            texte = " ".join(a.get_text(" ").split())[:80]
            liens.append({"texte": texte, "href": a["href"][:300], "cgu": bool(_LIENS_CGU.search(texte + a["href"]))})
        (sortie / "liens.json").write_text(json.dumps(liens, ensure_ascii=False, indent=1), encoding="utf-8")
        titre = page.title()
        # Conditions d'utilisation : texte des deux premières pages liées (sites approuvés seulement, robots.txt
        # respecté) dans cgu-<n>.txt, à lire et à résumer dans le README de la capacité.
        cgu: list[str] = []
        for lien in [x for x in liens if x["cgu"] and "robots" not in x["href"]]:
            cible = urljoin(page.url, lien["href"])
            if cible in cgu or len(cgu) >= 2 or not hote_autorise(urlsplit(cible).hostname or "", s.hotes):
                continue
            try:
                texte_cgu = s.ouvrir(cible).inner_text("body")
            except Exception:  # noqa: BLE001 - page des conditions illisible : on continue sans elle
                log.info("conditions d'utilisation illisibles : %s", url_journal(cible))
                continue
            cgu.append(cible)
            (sortie / f"cgu-{len(cgu)}.txt").write_text(f"{url_journal(cible)}\n\n{texte_cgu[:60_000]}", encoding="utf-8")
        time.sleep(0.2)
        resume.update({"titre": titre, "cgu": [url_journal(c) for c in cgu], "requetes": s.requetes[:200],
                       "hotes_bloques": sorted(s.bloques)})
    (sortie / "reseau.json").write_text(json.dumps(resume, ensure_ascii=False, indent=1), encoding="utf-8")
    return resume


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.navigation", description="Navigation des capacités (atelier)")
    sub = parser.add_subparsers(dest="commande", required=True)
    p_exp = sub.add_parser("explorer", help="visite publique d'une page pour l'atelier")
    p_exp.add_argument("url")
    p_exp.add_argument("--hotes", required=True, help="hôtes autorisés, séparés par des virgules")
    p_exp.add_argument("--sortie", type=Path, default=Path("exploration"))
    args = parser.parse_args(argv)
    try:
        resume = explorer(args.url, args.hotes.split(","), args.sortie)
    except ErreurSite as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    sys.stdout.write(json.dumps({k: resume[k] for k in ("url", "titre", "hotes_bloques")}, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
