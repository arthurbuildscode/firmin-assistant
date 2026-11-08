"""Capacité transports : interface des fournisseurs, accès réseau, outils, seuils, veille des réservations, contrat."""

from __future__ import annotations

import inspect
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from capabilities.transports import crons, interface, modele, reservations
from capabilities.transports import fournisseurs as fr
from capabilities.transports import tools as outils
from coeur import cartes

CAPACITE = Path(__file__).resolve().parent.parent
METHODES = ("resoudre_trajet", "prochains_departs", "perturbations", "etat_vol", "etat_train", "rafraichir", "verifier")


@pytest.fixture()
def foyer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setenv("HOMEOS_NOW", "2026-11-08T19:00:00")  # dimanche soir
    monkeypatch.setenv("HOMEOS_CAPABILITIES", str(CAPACITE.parent))
    donnees = tmp_path / "data"
    donnees.mkdir()
    return SimpleNamespace(donnees=donnees, personne="alice", config={"personnes": [
        {"prenom": "Alice", "linux_user": "alice", "role": "proprietaire"},
        {"prenom": "Bruno", "linux_user": "bruno", "role": "membre"},
        {"prenom": "Zoé", "linux_user": "zoe", "role": "invite"}]})


# --- utilitaires purs ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("entree", "attendu"), [("07:40", "07:40"), ("7h40", "07:40"), ("7h", "07:00"), (" 18H05 ", "18:05"), (460, "07:40")])
def test_lire_heure(entree: Any, attendu: str) -> None:
    assert interface.lire_heure(entree) == attendu


@pytest.mark.parametrize("entree", ["25:00", "7h75", "midi", True])
def test_lire_heure_invalide(entree: Any) -> None:
    with pytest.raises(interface.ErreurTransports, match="Heure invalide"):
        interface.lire_heure(entree)


@pytest.mark.parametrize(("entree", "attendu"), [
    ("lun-ven", ["lun", "mar", "mer", "jeu", "ven"]), ("du lundi au vendredi", ["lun", "mar", "mer", "jeu", "ven"]),
    ("en semaine", ["lun", "mar", "mer", "jeu", "ven"]), ("week-end", ["sam", "dim"]),
    ("Lundi, mercredi et vendredi", ["lun", "mer", "ven"]), (["jeu", "lun"], ["lun", "jeu"]),
    ("ven-lun", ["lun", "ven", "sam", "dim"]), ("tous les jours", list(interface.JOURS)), ("", list(interface.JOURS)),
    (None, list(interface.JOURS)),
])
def test_lire_jours(entree: Any, attendu: list[str]) -> None:
    assert interface.lire_jours(entree) == attendu


def test_lire_date(foyer: SimpleNamespace) -> None:
    assert str(interface.lire_date("demain")) == "2026-11-09"
    assert str(interface.lire_date("9/11")) == "2026-11-09"
    assert str(interface.lire_date("")) == "2026-11-08"
    with pytest.raises(interface.ErreurTransports):
        interface.lire_date("jeudi prochain")


def test_slug_personne() -> None:
    assert outils.slug_personne("Élodie") == "elodie" and outils.slug_personne("Jean Paul") == "jean-paul"
    with pytest.raises(interface.ErreurTransports):
        outils.slug_personne("  ")


# --- fournisseurs standard et interface commune ---------------------------------------------------------------------------


def test_fournisseurs_standard_valides() -> None:
    tous = {f.nom: f for f in fr.decouvrir(None, avec_modeles=True)}
    assert set(tous) == {"cartreize", "sncf", "gtfs_rt", "vols", "manuel"}
    for f in tous.values():
        assert f.valide, (f.nom, f.erreurs)
        assert f.origine == "humain" and f.provenance == "standard"
    assert tous["gtfs_rt"].est_modele and not [f for f in fr.decouvrir(None) if f.est_modele]
    assert (tous["sncf"].portee, tous["cartreize"].portee) == ("nationale", "locale")
    assert tous["vols"].modes == ["avion"] and tous["vols"].source == "manuel"


@pytest.mark.parametrize("nom", ["cartreize", "sncf", "gtfs_rt", "vols"])
def test_interface_commune(nom: str, tmp_path: Path) -> None:
    f = next(x for x in fr.decouvrir(None, avec_modeles=True) if x.nom == nom)
    classe = fr._module_source(f).Source
    assert issubclass(classe, interface.Source)
    for m in METHODES:
        assert callable(getattr(classe, m)), m
    sig = inspect.signature(classe.prochains_departs)
    assert list(sig.parameters)[:3] == ["self", "trajet", "horizon"]
    assert list(inspect.signature(classe.etat_train).parameters) == ["self", "numero", "date"]
    assert list(inspect.signature(classe.etat_vol).parameters) == ["self", "numero", "date"]


def test_non_pris_en_charge(foyer: SimpleNamespace) -> None:
    vols = fr.charger(fr.trouver("vols"), foyer.donnees, foyer)
    with pytest.raises(interface.NonPrisEnCharge, match="ne donne pas l'état des trains"):
        vols.etat_train("6105", "2026-11-09")
    with pytest.raises(interface.NonPrisEnCharge):
        vols.prochains_departs({}, 60)
    assert vols.perturbations({}) == [] and vols.verifier()["ok"] is True


@pytest.mark.parametrize(("brut", "erreur"), [
    ({"nom": "X", "titre": "t", "modes": ["bus"], "source": "manuel"}, "`nom`"),
    ({"nom": "abc", "titre": "t", "modes": ["fusee"], "source": "manuel"}, "`modes`"),
    ({"nom": "abc", "titre": "t", "modes": ["bus"], "source": "scraping"}, "`source`"),
    ({"nom": "abc", "titre": "t", "modes": ["bus"], "source": "api_ouverte"}, "`reseau`"),
    ({"nom": "abc", "titre": "t", "modes": ["bus"], "source": "api_ouverte", "reseau": ["http://x.fr"]}, "hôte réseau invalide"),
    ({"nom": "abc", "titre": "t", "modes": ["bus"], "source": "manuel", "secret": {"cle": "autre/x", "type": "cle_api"}}, "secret.cle"),
    ({"nom": "abc", "titre": "t", "modes": ["bus"], "source": "manuel", "pirate": 1}, "clé inconnue"),
])
def test_manifeste_fournisseur_invalide(brut: dict[str, Any], erreur: str) -> None:
    f = fr.valider_manifeste(brut, "abc")
    assert not f.valide and any(erreur in e for e in f.erreurs), f.erreurs


def test_atelier_doit_etre_origine_assistant() -> None:
    brut = {"nom": "metro_x", "titre": "Métro X", "modes": ["metro"], "source": "page_web", "reseau": ["x.exemple.org"]}
    assert any("origine: assistant" in e for e in fr.valider_manifeste(brut, "metro_x", "atelier").erreurs)
    assert fr.valider_manifeste({**brut, "origine": "assistant"}, "metro_x", "atelier").valide


# --- accès réseau ------------------------------------------------------------------------------------------------------------


def _client(tmp_path: Path, source: str = "api_ouverte", hotes: list[str] | None = None) -> fr.Client:
    f = fr.valider_manifeste({"nom": "demo", "titre": "Démo", "modes": ["bus"], "source": source,
                              "reseau": hotes or ["127.0.0.1"]}, "demo")
    return fr.Client(f, hotes or ["127.0.0.1"], tmp_path / "demo")


def test_client_https_et_hotes(tmp_path: Path) -> None:
    c = _client(tmp_path, hotes=["data.exemple.org", "*.cdn.exemple.org"])
    assert c.verifier_url("https://data.exemple.org/x") == "data.exemple.org"
    assert c.verifier_url("https://a.cdn.exemple.org/x") == "a.cdn.exemple.org"
    with pytest.raises(interface.ErreurTransports, match="https seulement"):
        c.verifier_url("http://data.exemple.org/x")
    with pytest.raises(interface.ErreurTransports, match="n'est pas autorisé"):
        c.verifier_url("https://ailleurs.org/x")
    with pytest.raises(interface.ErreurTransports, match="hors du dossier"):
        c.telecharger("https://data.exemple.org/x.zip", tmp_path / "evasion.zip")


def test_client_robots_page_web(tmp_path: Path, serveur: Any) -> None:
    serveur.routes["/robots.txt"] = (200, b"User-agent: *\nDisallow: /horaires\n", "text/plain")
    serveur.routes["/horaires"] = (200, b"<html>", "text/html")
    serveur.routes["/info"] = (200, b"ok", "text/plain")
    web = _client(tmp_path, "page_web")
    assert web.texte(f"{serveur.url}/info") == "ok"
    with pytest.raises(interface.ErreurTransports, match="demande aux robots"):
        web.texte(f"{serveur.url}/horaires")
    api = _client(tmp_path, "api_ouverte")  # une API ouverte n'est pas une page : robots.txt non consulté
    assert api.octets(f"{serveur.url}/horaires") == b"<html>"


def test_client_erreurs_et_cache(tmp_path: Path, serveur: Any) -> None:
    serveur.routes["/flux"] = (200, b"v1", "text/plain")
    c = _client(tmp_path)
    assert c.octets(f"{serveur.url}/flux", cache_s=60) == b"v1"
    serveur.routes["/flux"] = (200, b"v2", "text/plain")
    assert c.octets(f"{serveur.url}/flux", cache_s=60) == b"v1"  # cache court : un seul appel
    assert c.octets(f"{serveur.url}/flux") == b"v2"
    serveur.routes["/interdit"] = (403, b"", "text/plain")
    with pytest.raises(interface.SourceIndisponible, match="refuse l'accès"):
        c.octets(f"{serveur.url}/interdit")
    c.ecrire_json("etat", {"a": 1})
    assert c.lire_json("etat") == {"a": 1}
    with pytest.raises(interface.ErreurTransports):
        c.lire_json("../coffre")


def test_cle_api_facultative(tmp_path: Path) -> None:
    f = fr.valider_manifeste({"nom": "demo", "titre": "Démo", "modes": ["train"], "source": "api_ouverte",
                              "reseau": ["api.exemple.org"],
                              "secret": {"cle": "transports/demo", "type": "cle_api", "facultatif": True}}, "demo")

    def secret(_cle: str) -> dict[str, Any]:
        raise RuntimeError("Identifiants Transports à renseigner : /identifiants transports")

    assert fr.Client(f, f.reseau, tmp_path, SimpleNamespace(secret=secret)).cle_api() is None
    f.secret = {"cle": "transports/demo", "type": "cle_api"}
    with pytest.raises(interface.ErreurTransports, match="à renseigner"):
        fr.Client(f, f.reseau, tmp_path, SimpleNamespace(secret=secret)).cle_api()
    assert fr.Client(f, f.reseau, tmp_path, SimpleNamespace(secret=lambda c: {"cle": "k"})).cle_api() == "k"


# --- outils ------------------------------------------------------------------------------------------------------------------


def test_reseaux_proposes_sans_supposer(foyer: SimpleNamespace) -> None:
    noms = {f.nom for f in outils.lister_fournisseurs(foyer)}
    assert noms == {"sncf", "vols", "manuel"}  # nationaux seulement : Cartreize n'est pas supposé
    with pytest.raises(interface.ErreurTransports, match="Je ne connais pas encore de réseau de bus"):
        outils.choisir_fournisseur(foyer, "bus")
    assert outils.choisir_fournisseur(foyer, "train").nom == "sncf"


def test_trajet_manuel(foyer: SimpleNamespace) -> None:
    res = outils.ajouter_trajet(foyer, "Bruno", "navette", "Parking", "Bureau", "17h20", "lun-ven", fournisseur="manuel")
    assert res["message"] == "Trajet noté : Navette · Parking → Bureau, 17:20, lun mar mer jeu ven (sans suivi en temps réel)."
    foyer.personne = "bruno"
    (t,) = outils.prochains(foyer)["trajets"]
    assert t["source"] == "theorique" and t["avertissement"].startswith("Pas de source en temps réel")
    assert t["alerte"] is None and t["aujourd_hui"] is False
    assert outils.lister_trajets(foyer)["trajets"]["bruno"][0]["titre"] == "Navette · Parking → Bureau"
    assert outils.supprimer_trajet(foyer, "bruno", "17h20") == {"personne": "bruno", "supprimes": 1, "restants": 0}


def test_aucun_trajet(foyer: SimpleNamespace) -> None:
    res = outils.prochains(foyer)
    assert res["trajets"] == [] and "tu prends quoi, d'où, vers où, quels jours" in res["message"]


def test_mode_inconnu(foyer: SimpleNamespace) -> None:
    assert outils.lire_mode("TGV") == "train" and outils.lire_mode("Tramway") == "tram" and outils.lire_mode("métro") == "metro"
    with pytest.raises(interface.ErreurTransports, match="Mode « fusée » inconnu"):
        outils.lire_mode("fusée")


def test_etat_vol_manuel(foyer: SimpleNamespace) -> None:
    e = outils.etat(foyer, "AF 7664", "demain")
    assert (e["numero"], e["mode"], e["statut"], e["source"], e["date"]) == ("AF7664", "avion", "inconnu", "manuel", "2026-11-09")
    assert "site ou l'appli de Air France" in e["message"] and e["resume"].startswith("Vol AF7664 : Je n'ai pas")
    carte = outils.carte_etat(e)
    assert carte is not None and cartes.erreurs_carte(carte) == [] and carte["titre"] == "Vol AF7664"
    assert "Je n'ai pas de source fiable" in cartes.rendu_texte(carte)


def test_etat_train_par_le_fournisseur(foyer: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    vus: list[tuple[str, str]] = []

    class Faux(interface.Source):
        def etat_train(self, numero: str, date: str) -> dict[str, Any]:
            vus.append((numero, date))
            return interface.etat(numero, "train", "en_retard", date=date, retard_min=12,
                                  depart_={"lieu": "Marseille", "prevu": "08:12", "estime": "08:24"}, arrivee={"lieu": "Paris"})

    monkeypatch.setattr(fr, "charger", lambda f, d, c=None: Faux(None, {}, f))
    e = outils.etat(foyer, "TGV 6105", "9/11")
    assert vus == [("6105", "2026-11-09")]
    assert e["resume"] == "Train 6105 Marseille → Paris, départ 08:24 : retard 12 min." and e["fournisseur"].startswith("SNCF")
    with pytest.raises(interface.ErreurTransports, match="numéro de train"):
        outils.etat(foyer, "bonjour")


# --- réservations dans l'agenda et veille -----------------------------------------------------------------------------------


@pytest.mark.parametrize(("texte", "attendu"), [
    ("TGV 6105 Marseille → Paris", [("train", "6105")]),
    ("Train n° 17512 pour Nice", [("train", "17512")]),
    ("OUIGO 7841", [("train", "7841")]),
    ("Vol AF7664 vers Orly", [("avion", "AF7664")]),
    ("✈ U2 4321 Nice", [("avion", "U24321")]),
    ("AF 1234 Marseille-Paris", [("avion", "AF1234")]),
    ("Réunion TO 1234 salle B", [("avion", "TO1234")]),
    ("Dentiste 14h, salle 1205", []),
    ("RDV ZZ 123", []),
])
def test_detection_reservations(texte: str, attendu: list[tuple[str, str]]) -> None:
    assert reservations.detecter(texte) == attendu


def _ctx_veille(foyer: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, evenements: list[dict[str, Any]],
                etat_train: dict[str, Any] | None) -> list[tuple[Any, ...]]:
    remises: list[tuple[Any, ...]] = []
    foyer.publier = lambda *args: remises.append(args) or {"ok": True}
    monkeypatch.setattr(crons, "lire_agenda", lambda debut, fin: evenements)

    def etat(ctx: Any, numero: str, date: str = "", mode: str = "", fournisseur: str = "") -> dict[str, Any]:
        if mode == "avion":
            return outils.etat.__wrapped__(ctx, numero, date, mode) if hasattr(outils.etat, "__wrapped__") else {}
        if etat_train is None:
            raise interface.NonPrisEnCharge("non")
        return {**etat_train, "numero": numero, "mode": "train", "date": date}

    monkeypatch.setattr(outils, "etat", etat)
    return remises


def test_veille_train_la_veille(foyer: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    ev = [{"agenda": "Famille", "titre": "TGV 6105 Alice Marseille-Paris", "debut": "2026-11-09T08:12:00+01:00", "lieu": None}]
    remises = _ctx_veille(foyer, monkeypatch, ev, interface.etat("6105", "train", "prevu"))
    crons.veille_reservations(foyer)
    (r,) = remises  # la personne nommée dans le titre seulement
    personne, texte, carte, notif, seuil, motif = r
    assert personne == "alice" and motif == "rappel" and seuil is None
    assert texte == "Demain : train 6105 à 08:12. Je surveille les retards et je te préviens demain s'il y a du changement."
    assert notif == {"titre": "Ton voyage de demain", "corps": "Rappel de ton voyage de demain."}
    assert carte["type"] == "transports_etat" and cartes.erreurs_carte(carte) == []
    crons.veille_reservations(foyer)
    assert len(remises) == 1  # une fois


def test_veille_avant_l_heure_rien(foyer: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMEOS_NOW", "2026-11-08T12:00:00")
    ev = [{"titre": "TGV 6105", "debut": "2026-11-09T08:12:00"}]
    remises = _ctx_veille(foyer, monkeypatch, ev, interface.etat("6105", "train", "prevu"))
    crons.veille_reservations(foyer)
    assert remises == []


def test_veille_train_supprime_seuil(foyer: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    ev = [{"titre": "Train 6105", "debut": "2026-11-09T08:12:00"}]
    remises = _ctx_veille(foyer, monkeypatch, ev, interface.etat("6105", "train", "supprime"))
    crons.veille_reservations(foyer)
    assert [r[0] for r in remises] == ["alice", "bruno"]  # adultes seulement (pas l'invitée)
    assert all(r[4] == "reservation" and "annoncé supprimé" in r[1] for r in remises)


def test_jour_meme_retard_par_palier(foyer: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMEOS_NOW", "2026-11-09T07:00:00")
    ev = [{"titre": "TGV 6105 Bruno", "debut": "2026-11-09T08:12:00"}]
    etat_train = interface.etat("6105", "train", "a_l_heure", retard_min=3)
    remises = _ctx_veille(foyer, monkeypatch, ev, etat_train)
    crons.veille_reservations(foyer)
    assert remises == []  # 3 min : sous le seuil
    etat_train.update({"statut": "en_retard", "retard_min": 12})
    crons.veille_reservations(foyer)
    crons.veille_reservations(foyer)
    (r,) = remises
    assert r[0] == "bruno" and r[1] == "Ton train 6105 de 08:12 a 12 min de retard." and r[4] == "reservation"
    etat_train["retard_min"] = 25
    crons.veille_reservations(foyer)
    assert len(remises) == 2 and "25 min" in remises[-1][1]


def test_vol_rappel_porte_jour_meme(foyer: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMEOS_NOW", "2026-11-09T08:30:00")
    ev = [{"titre": "Vol AF7664 Alice", "debut": "2026-11-09T10:25:00"}]
    remises = _ctx_veille(foyer, monkeypatch, ev, None)
    crons.veille_reservations(foyer)
    (r,) = remises
    assert r[0] == "alice" and "vérifie la porte" in r[1] and r[5] == "rappel" and r[2] is None


def test_veille_agenda_injoignable(foyer: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    def boum(_d: Any, _f: Any) -> list[dict[str, Any]]:
        raise OSError("réseau")

    monkeypatch.setattr(crons, "lire_agenda", boum)
    assert crons.veille_reservations(foyer) == []


def test_seuil_minimum_reglage(foyer: SimpleNamespace) -> None:
    d = foyer.donnees / "capacites" / "transports"
    d.mkdir(parents=True)
    (d / "parametres.yaml").write_text("seuil_retard_min: 2\n", encoding="utf-8")
    assert outils.seuil_defaut(foyer) == 5
    (d / "parametres.yaml").write_text("seuil_retard_min: 9\n", encoding="utf-8")
    assert outils.seuil_defaut(foyer) == 9
    assert modele.seuil({"seuil_alerte_min": 0}) == 5 and modele.seuil({"seuil_alerte_min": "x"}) == 5


# --- contrat de la capacité --------------------------------------------------------------------------------------------------


def test_manifeste_conforme_au_contrat() -> None:
    from tools import capacites

    cap = capacites.lire_capacite(CAPACITE)
    assert cap.valide and not [a for a in cap.avertissements if "clé inconnue" in a], cap.avertissements
    m = yaml.safe_load((CAPACITE / "capability.yaml").read_text(encoding="utf-8"))
    assert m["nom"] == CAPACITE.name == "transports" and m["remplace"] == ["bus"]
    assert [c["nom"] for c in m["crons"]] == ["rafraichir_grilles", "alertes_retard", "veille_reservations"]
    assert [s["nom"] for s in m["seuils"]] == ["retard", "reservation"]
    assert m["secrets"] == [] and m["confirmation_toujours"] == ["transports_reseau_ajouter"]
    for f in fr.decouvrir(None):  # chaque hôte d'un fournisseur standard est dans la liste blanche de la capacité
        for h in f.reseau:
            assert h in m["reseau"] or h in fr.HOTES_LOCAUX, (f.nom, h)
    for fichier in ("tools.py", "CLAUDE.md", "onboarding.md", "smoke.py", "crons.py", "README.md", "install.sh"):
        assert (CAPACITE / fichier).is_file(), fichier


def test_sans_donnee_personnelle() -> None:
    """Aucun trajet, arrêt ou prénom du foyer dans le code ni dans les fragments de prompt (noms fictifs des captures
    compris) ; motifs propres au foyer dans HOMEOS_MOTIFS_INTERDITS (hors dépôt)."""
    motifs = ["pont du moulin", "montclair", "saint-aubin", "alice"]
    motifs += [m.strip().casefold() for m in os.environ.get("HOMEOS_MOTIFS_INTERDITS", "").split(",") if m.strip()]
    fichiers = [p for p in CAPACITE.rglob("*") if p.is_file() and p.suffix in (".py", ".md", ".yaml", ".mjs")
                and "tests" not in p.parts and "__pycache__" not in p.parts]
    for fichier in fichiers:
        texte = fichier.read_text(encoding="utf-8").casefold()
        for motif in motifs:
            assert motif not in texte, (fichier, motif)


def test_cartes_valides() -> None:
    depart = interface.depart("16:25", heure_estimee="16:32", dans_min=22)
    carte = outils.carte_prochains({"trajets": [{"titre": "Bus 7 · A → B", "mode": "bus", "prochains": [depart],
                                                 "alerte": "retard 7 min sur le départ de 16:25", "source": "temps_reel"}]})
    assert carte is not None and cartes.erreurs_carte(carte) == []
    assert "• retard 7 min sur le départ de 16:25" in cartes.rendu_texte(carte)
    assert outils.carte_prochains({"trajets": [{"erreur": "x"}]}) is None
