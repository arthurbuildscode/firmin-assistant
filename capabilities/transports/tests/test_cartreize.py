"""Fournisseur Cartreize (ex-capacité bus) : fonctionnellement inchangé, sans réseau.

Le service Node est remplacé par un petit serveur HTTP local qui renvoie des réponses JSON capturées sur le vrai
`node server.mjs` (fixtures/ : ligne 7 vers Grandport, arrêt Pont du Moulin, une capture sans temps réel et une
capture avec un flux SIRI factice). Le test marqué `reseau` lance le vrai service.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from capabilities.transports import crons, interface, modele, smoke
from capabilities.transports import fournisseurs as fr
from capabilities.transports import tools as outils

CAPACITE = Path(__file__).resolve().parent.parent
CARTREIZE = CAPACITE / "fournisseurs" / "cartreize"


def _source(ctx: SimpleNamespace) -> Any:
    return fr.charger(fr.trouver("cartreize", ctx.donnees), ctx.donnees, ctx)


def _port_ferme() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


TRAJET = {"mode": "bus", "fournisseur": "cartreize", "origine": "Pont du Moulin", "destination": "Grandport", "ligne": "7"}


# --- résolution ligne / sens / arrêt ---------------------------------------------------------------------------------


def test_lignes(ctx: SimpleNamespace) -> None:
    lignes = _source(ctx).lignes()
    l7 = next(x for x in lignes if x["ligne"] == "7")
    assert l7["titre"] == "Villeneuve ⇄ Grandport"
    assert l7["temps_reel"] is True and l7["sens"] == ["Vers Grandport", "Vers Villeneuve"]


def test_resoudre_trajet(ctx: SimpleNamespace) -> None:
    t = _source(ctx).resoudre_trajet(dict(TRAJET))
    assert t["origine"] == "Pont du Moulin (Villeneuve)" and t["destination"] == "Grandport"
    assert t["details"] == {"sens": "grandport", "sens_libelle": "Vers Grandport", "arret": "pont-du-moulin-villeneuve",
                            "arret_nom": "Pont du Moulin", "commune": "Villeneuve"}


@pytest.mark.parametrize("destination", ["Grandport", "Vers Grandport", "GRANDPORT", "mairie"])
def test_sens_par_destination(ctx: SimpleNamespace, destination: str) -> None:
    t = _source(ctx).resoudre_trajet({**TRAJET, "destination": destination})
    assert t["details"]["sens"] == "grandport"


def test_ligne_cherchee_sans_numero(ctx: SimpleNamespace) -> None:
    t = _source(ctx).resoudre_trajet({**TRAJET, "ligne": ""})
    assert t["ligne"] == "7"


def test_ligne_inconnue(ctx: SimpleNamespace) -> None:
    with pytest.raises(interface.ErreurTransports, match=r"Ligne « 999 » inconnue\. Lignes disponibles : 15, 16"):
        _source(ctx).resoudre_trajet({**TRAJET, "ligne": "999"})


def test_destination_inconnue(ctx: SimpleNamespace) -> None:
    with pytest.raises(interface.ErreurTransports, match="Je ne trouve pas de car de Pont du Moulin vers Toulon"):
        _source(ctx).resoudre_trajet({**TRAJET, "destination": "Toulon"})


def test_arret_ambigu(ctx: SimpleNamespace) -> None:
    with pytest.raises(interface.ErreurTransports, match="Plusieurs arrêts correspondent à « saint-aubin »"):
        _source(ctx).resoudre_trajet({**TRAJET, "origine": "saint-aubin", "destination": "Mairie"})


# --- prochains départs (mêmes valeurs qu'avec l'ancienne capacité bus) -------------------------------------------------


def test_prochains_temps_reel(ctx: SimpleNamespace) -> None:
    res = _source(ctx).prochains_departs(dict(TRAJET))
    assert res["temps_reel"] is True and res["avertissement"] is None
    premier, deuxieme, troisieme, quatrieme = res["departs"][:4]
    assert {k: premier[k] for k in ("heure_theorique", "heure_estimee", "retard_min", "dans_min", "source", "statut",
                                    "destination", "suivi", "resume", "ligne")} == {
        "heure_theorique": "16:25", "heure_estimee": "16:32", "retard_min": 7, "dans_min": 22, "source": "temps_reel",
        "statut": "a_venir", "destination": "Grandport – Gare Centrale", "suivi": True,
        "resume": "dans 22 min (16:32, retard 7 min)", "ligne": "7",
    }
    assert deuxieme["resume"] == "dans 47 min (16:57, retard 2 min)"
    assert troisieme["resume"] == "à 17:25 (17:25, à l'heure)"
    assert quatrieme["source"] == "theorique" and quatrieme["resume"] == "à 17:55 (17:55, horaire théorique)"
    assert [p["heure_theorique"] for p in res["passes"]] == ["15:55"]
    assert res["passes"][0]["resume"] == "passé il y a 15 min (15:55)"


def test_prochains_theorique(ctx: SimpleNamespace, cartreize: Any) -> None:
    cartreize.mode = "theorique"
    res = _source(ctx).prochains_departs(dict(TRAJET))
    assert res["temps_reel"] is False
    assert res["avertissement"] == "Temps réel indisponible pour l'instant : horaires théoriques."
    assert all(p["source"] == "theorique" and p["retard_min"] is None for p in res["departs"])
    assert res["departs"][0]["resume"] == "dans 16 min (16:25, horaire théorique)"


def test_resume_cas_particuliers() -> None:
    f = fr.decouvrir(None)
    assert f  # fournisseurs présents
    from capabilities.transports.fournisseurs import _module_source

    classe = _module_source(fr.trouver("cartreize")).Source
    assert classe.formater_passage({"statut": "supprime", "prevu": "08:10", "dans": 3})["resume"] == "supprimé (prévu à 08:10)"
    assert classe.formater_passage({"statut": "a_l_arret", "prevu": "08:10", "estime": "08:12", "retard": 2, "dans": 0})["resume"] == "à l'arrêt maintenant"
    avance = {"statut": "a_venir", "prevu": "08:10", "estime": "08:08", "retard": -2, "dans": 3, "deduit": True}
    assert classe.formater_passage(avance)["resume"] == "dans 3 min (08:08, en avance de 2 min, estimé via l'arrêt suivant)"


def test_service_indisponible(ctx: SimpleNamespace) -> None:
    ctx.config = {"transports": {"cartreize": {"url": f"http://127.0.0.1:{_port_ferme()}"}}}
    with pytest.raises(interface.ErreurTransports, match="Service des horaires Cartreize indisponible"):
        _source(ctx).prochains_departs(dict(TRAJET))


def test_service_en_erreur(ctx: SimpleNamespace, cartreize: Any) -> None:
    cartreize.mode = "erreur500"
    with pytest.raises(interface.ErreurTransports, match="indisponible"):
        _source(ctx).lignes()


def test_url_service_compatible_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    from capabilities.transports.fournisseurs import _module_source

    url_service = _module_source(fr.trouver("cartreize")).url_service
    for env in ("HOMEOS_BUS_URL", "HOMEOS_TRANSPORTS_CARTREIZE_URL"):
        monkeypatch.delenv(env, raising=False)
    assert url_service({}) == "http://127.0.0.1:8081"
    assert url_service({"bus": {"url": "http://h:1/"}}) == "http://h:1"  # config.yaml de l'ancienne capacité
    assert url_service({"transports": {"cartreize": {"url": "http://t:3"}}, "bus": {"url": "http://h:1"}}) == "http://t:3"
    monkeypatch.setenv("HOMEOS_BUS_URL", "http://e:2")
    assert url_service({"bus": {"url": "http://h:1/"}}) == "http://e:2"


# --- trajets habituels et alertes (outils de la capacité) -----------------------------------------------------------------


def test_trajet_habituel_alerte_retard(ctx: SimpleNamespace) -> None:
    res = outils.ajouter_trajet(ctx, "Alice", "bus", "pont du moulin", "vers Grandport", "16h25", "week-end", ligne="7")
    assert res["message"] == "Trajet noté : Bus 7 · Pont du Moulin (Villeneuve) → Grandport, 16:25, sam dim."
    res = outils.prochains(ctx)  # personne courante du ctx
    assert res["jour"] == "sam"
    (t,) = res["trajets"]
    assert t["aujourd_hui"] is True and t["part_dans_l_heure"] is True and t["depart_dans_min"] == 15
    assert t["depart_habituel"]["heure_theorique"] == "16:25"
    assert t["alerte"] == "retard 7 min sur le départ de 16:25"
    assert t["source"] == "temps_reel" and len(t["prochains"]) == 4


def test_trajet_sans_alerte_sous_le_seuil(ctx: SimpleNamespace) -> None:
    outils.ajouter_trajet(ctx, "alice", "bus", "pont du moulin", "Grandport", "16:25", "week-end", seuil_alerte_min=10)
    outils.ajouter_trajet(ctx, "alice", "bus", "pont du moulin", "Grandport", "16:55", "week-end")  # retard 2 min
    assert [t["alerte"] for t in outils.prochains(ctx, "Alice")["trajets"]] == [None, None]


def test_seuil_minimum_5(ctx: SimpleNamespace) -> None:
    res = outils.ajouter_trajet(ctx, "alice", "bus", "pont du moulin", "Grandport", "16:25", "sam", seuil_alerte_min=1)
    assert res["trajet"]["seuil_alerte_min"] == 5


def test_trajet_pas_aujourd_hui(ctx: SimpleNamespace) -> None:
    outils.ajouter_trajet(ctx, "alice", "bus", "pont du moulin", "Grandport", "16:25", "lun-ven")
    (t,) = outils.prochains(ctx)["trajets"]
    assert t["aujourd_hui"] is False and t["part_dans_l_heure"] is False and t["alerte"] is None
    assert t["prochains"]


def test_trajet_service_coupe(ctx: SimpleNamespace) -> None:
    outils.ajouter_trajet(ctx, "alice", "bus", "pont du moulin", "Grandport", "16:25", "")
    ctx.config = {"transports": {"cartreize": {"url": f"http://127.0.0.1:{_port_ferme()}"}}}
    (t,) = outils.prochains(ctx)["trajets"]
    assert "indisponible" in t["erreur"] and t["heure_habituelle"] == "16:25"


def test_appel_mcp(ctx: SimpleNamespace) -> None:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:  # SDK mcp 2.x
        from mcp.server.mcpserver import MCPServer as FastMCP  # type: ignore[no-redef]
    mcp = FastMCP("test-transports")
    outils.enregistrer(mcp, ctx)
    noms = {t.name for t in asyncio.run(mcp.list_tools())}
    assert noms == {"transports_prochains", "transports_trajets", "transports_trajet_ajouter",
                    "transports_trajet_supprimer", "transports_etat", "transports_reseau_ajouter"}
    resultat = asyncio.run(mcp.call_tool("transports_prochains", {"origine": "Pont du Moulin", "destination": "Grandport",
                                                                   "ligne": "7"}))
    assert "retard 7 min" in json.dumps(resultat, default=str, ensure_ascii=False)


def test_cron_alertes_retard_une_fois_par_personne(ctx: SimpleNamespace) -> None:
    remises: list[tuple[Any, ...]] = []
    ctx.config["personnes"] = [{"prenom": "Alice", "linux_user": "alice"}, {"prenom": "Bruno", "linux_user": "bruno"}]
    ctx.publier = lambda *args: remises.append(args) or {"ok": True}
    outils.ajouter_trajet(ctx, "alice", "bus", "pont du moulin", "Grandport", "16:25", "week-end")
    outils.ajouter_trajet(ctx, "bruno", "bus", "pont du moulin", "Grandport", "16:25", "lun-ven")  # pas aujourd'hui
    assert crons.executer("alertes_retard", ctx) is None  # rien pour le groupe
    (args,) = remises
    personne, texte, carte, notification, seuil, motif = args
    assert personne == "alice" and seuil == "retard" and motif == "alerte"
    assert texte.startswith("Bus 7 : retard 7 min sur le départ de 16:25")
    assert notification == {"titre": "Bus 7", "corps": "Retard 7 min sur le départ de 16:25."}
    assert carte["type"] == "transports_prochains" and carte["donnees"]["trajets"][0]["departs"]
    from coeur import cartes

    assert cartes.erreurs_carte(carte) == []
    crons.alertes_retard(ctx)  # 10 min plus tard, même alerte : pas de doublon
    assert len(remises) == 1


def test_cron_alertes_silencieux_sans_trajet_ou_service(ctx: SimpleNamespace) -> None:
    remises: list[Any] = []
    ctx.config["personnes"] = [{"prenom": "Alice", "linux_user": "alice"}]
    ctx.publier = lambda *args: remises.append(args)
    assert crons.alertes_retard(ctx) == []
    outils.ajouter_trajet(ctx, "alice", "bus", "pont du moulin", "Grandport", "16:25", "week-end")
    ctx.config["transports"] = {"cartreize": {"url": f"http://127.0.0.1:{_port_ferme()}"}}
    assert crons.alertes_retard(ctx) == [] and remises == []


# --- smoke test et grille --------------------------------------------------------------------------------------------------


def test_smoke_ok(ctx: SimpleNamespace) -> None:
    res = smoke.verifier(ctx)
    assert res["ok"] is True
    assert "Service Cartreize OK (" in res["message"] and "dernière erreur SIRI : fetch failed" in res["message"]


def test_smoke_service_coupe(ctx: SimpleNamespace) -> None:
    ctx.config = {"transports": {"cartreize": {"url": f"http://127.0.0.1:{_port_ferme()}"}}}
    res = smoke.verifier(ctx)
    assert res["ok"] is False and "injoignable" in res["message"]


def test_smoke_grille_trop_vieille(ctx: SimpleNamespace) -> None:
    gtfs = ctx.donnees / "capacites" / "transports" / "cartreize" / "gtfs"
    gtfs.mkdir(parents=True)
    (gtfs / "reseau.json").write_text('{"genere": "2026-10-01T04:00:12", "lignes": []}', encoding="utf-8")
    res = smoke.verifier(ctx)
    assert res["ok"] is False and "37 jours" in res["message"]
    (gtfs / "reseau.json").write_text('{"genere": "2026-11-02T04:00:12", "lignes": []}', encoding="utf-8")
    res = smoke.verifier(ctx)
    assert res["ok"] is True and "grille de 5 j" in res["message"]


def _faux_service(tmp_path: Path, script: str) -> Path:
    d = tmp_path / "service"
    d.mkdir()
    (d / "config.json").write_text("{}", encoding="utf-8")
    (d / "prepare.py").write_text(script, encoding="utf-8")
    return d


PREPARE_OK = """
import json, os
os.makedirs("data", exist_ok=True)
json.dump({"genere": "2026-11-09T04:00:00", "lignes": [{"id": "7"}]}, open("data/reseau.json", "w"))
json.dump({"passages": {"x": [[0, 1]] * 500}}, open("data/horaires.json", "w"))
"""


def test_rafraichir_grille(ctx: SimpleNamespace, tmp_path: Path) -> None:
    src = _source(ctx)
    resume = src.rafraichir(forcer=True, service=_faux_service(tmp_path, PREPARE_OK))
    gtfs = ctx.donnees / "capacites" / "transports" / "cartreize" / "gtfs"
    assert resume["lignes"] == 1 and resume["genere"] == "2026-11-09T04:00:00"
    assert sorted(p.name for p in gtfs.iterdir()) == [".gitignore", ".maj", "horaires.json", "reseau.json"]
    assert (gtfs.parent / ".gitignore").read_text(encoding="utf-8").endswith("*\n")
    assert src.rafraichir(service=_faux_service(tmp_path / "b", PREPARE_OK) if (tmp_path / "b").mkdir() is None else None) is None  # grille de moins de 7 jours


def test_rafraichir_echec_garde_la_grille(ctx: SimpleNamespace, tmp_path: Path) -> None:
    src = _source(ctx)
    gtfs = ctx.donnees / "capacites" / "transports" / "cartreize" / "gtfs"
    gtfs.mkdir(parents=True)
    (gtfs / "reseau.json").write_text("ancienne", encoding="utf-8")
    service = _faux_service(tmp_path, "import sys\nprint('Téléchargement impossible', file=sys.stderr)\nsys.exit(1)\n")
    with pytest.raises(interface.ErreurTransports, match="grille inchangée.*Téléchargement impossible"):
        src.rafraichir(forcer=True, service=service)
    assert (gtfs / "reseau.json").read_text(encoding="utf-8") == "ancienne"
    assert not (gtfs / ".maj").exists()


def test_cron_rafraichir_seulement_reseaux_utilises(ctx: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    appels: list[str] = []
    monkeypatch.setattr(fr, "charger", lambda f, d, c=None: SimpleNamespace(rafraichir=lambda forcer=False: appels.append(f.nom)))
    crons.rafraichir_grilles(ctx)
    assert appels == ["cartreize"]  # ni SNCF (pas utilisé), ni le modèle, ni les sources manuelles


# --- intégration : vrai service Node (optionnel) ---------------------------------------------------------------------------


def _grille_disponible() -> Path | None:
    for candidat in (os.environ.get("BUS_GTFS_DIR"), str(CARTREIZE / "service" / "data"),
                     "/srv/home-os/data/capacites/transports/cartreize/gtfs"):
        if candidat and (Path(candidat) / "horaires.json").is_file():
            return Path(candidat).resolve()
    return None


@pytest.mark.reseau
@pytest.mark.skipif(os.environ.get("HOMEOS_TESTS_RESEAU") != "1", reason="HOMEOS_TESTS_RESEAU=1 pour lancer le vrai service")
def test_integration_vrai_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Lance `node server.mjs` sur un port libre et interroge un arrêt (BUS_TEST_LIGNE/ORIGINE/DESTINATION)."""
    node, grille = shutil.which("node"), _grille_disponible()
    if not node or grille is None:
        pytest.skip("node ou grille GTFS absents (BUS_GTFS_DIR)")
    copie = tmp_path / "service"
    shutil.copytree(CARTREIZE / "service", copie, ignore=shutil.ignore_patterns("data"), symlinks=True)
    (copie / "data").symlink_to(grille)
    port = _port_ferme()
    proc = subprocess.Popen([node, "server.mjs"], cwd=copie, env={**os.environ, "HOST": "127.0.0.1", "PORT": str(port)},
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        monkeypatch.delenv("HOMEOS_NOW", raising=False)
        donnees = tmp_path / "data"
        ctx = SimpleNamespace(donnees=donnees, personne=None, config={"transports": {"cartreize": {"url": f"http://127.0.0.1:{port}"}}})
        src = _source(ctx)
        for _ in range(60):
            if src.verifier()["ok"]:
                break
            time.sleep(0.5)
        assert src.verifier()["ok"] is True
        lignes = src.lignes()
        ligne = os.environ.get("BUS_TEST_LIGNE") or lignes[0]["ligne"]
        detail = src.resoudre_ligne(ligne)
        sens = detail["sens"][0]
        origine = os.environ.get("BUS_TEST_ORIGINE") or sens["arrets"][0]["nom"]
        destination = os.environ.get("BUS_TEST_DESTINATION") or sens["libelle"]
        res = src.prochains_departs({"ligne": ligne, "origine": origine, "destination": destination})
        assert res["departs"] and all(p["heure_theorique"] for p in res["departs"])
        print(json.dumps(res, ensure_ascii=False, indent=1))
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_trajets_ecrits_lisibles(ctx: SimpleNamespace) -> None:
    outils.ajouter_trajet(ctx, "Alice", "bus", "pont du moulin", "Grandport", "16:25", "week-end")
    contenu = yaml.safe_load((ctx.donnees / "capacites" / "transports" / "trajets.yaml").read_text(encoding="utf-8"))
    (t,) = contenu["alice"]
    assert {k: t[k] for k in ("mode", "fournisseur", "ligne", "heure", "jours", "seuil_alerte_min")} == {
        "mode": "bus", "fournisseur": "cartreize", "ligne": "7", "heure": "16:25", "jours": ["sam", "dim"],
        "seuil_alerte_min": 5}
    assert t["id"].startswith("t") and t["details"]["arret"] == "pont-du-moulin-villeneuve"
    assert modele.lire(ctx.donnees)["alice"][0]["id"] == t["id"]
