"""Retour d'usage : « quelle météo aujourd'hui ? » → « je n'ai pas accès à la météo ».

1. Outil `meteo_lire` (tools/meteo.py, logique Open-Meteo partagée avec la carte du jour de coeur/brief.py) : commune
   du foyer par défaut, jours relatifs, conditions actuelles, cache, erreurs qui proposent une solution, carte `meteo`.
2. Règle produit « toujours une solution » : section du prompt système rendu, exemples, atelier proposé et joignable
   depuis la conversation d'un propriétaire.

Aucun appel réseau : Open-Meteo est remplacé par un faux téléchargeur.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

RACINE = Path(__file__).resolve().parent.parent
AUJOURDHUI = date(2026, 11, 8)  # un dimanche

CONFIG: dict[str, Any] = {
    "assistant": {"nom": "Testbot"},
    "groupe_telegram_id": -100,
    "personnes": [
        {"telegram_id": 111, "prenom": "Alex", "linux_user": "alex", "role": "proprietaire"},
        {"telegram_id": 222, "prenom": "Camille", "linux_user": "camille", "role": "membre"},
    ],
    "meteo": {"commune": "Villeneuve-les-Pins"},
}


COMMUNES_CONNUES = {"villeneuve-les-pins": (42.5, 2.0), "montagnac": (44.9, 6.6)}


class FauxOpenMeteo:
    """Faux Open-Meteo : géocodage (deux communes connues) et prévisions sur 14 jours à partir d'AUJOURDHUI."""

    def __init__(self) -> None:
        self.appels: list[str] = []
        self.panne = False

    def __call__(self, url: str) -> bytes:
        self.appels.append(url)
        if self.panne:
            raise OSError("réseau coupé")
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        if "geocoding" in url:
            nom = params["name"].lower()
            if nom not in COMMUNES_CONNUES:
                return json.dumps({"results": []}).encode()
            lat, lon = COMMUNES_CONNUES[nom]
            return json.dumps({"results": [{"name": params["name"].title(), "latitude": lat, "longitude": lon}]}).encode()
        assert "daily" in params and "hourly" in params and "current" in params
        jours = [(AUJOURDHUI + timedelta(days=i)).isoformat() for i in range(int(params["forecast_days"]))]
        chaud = float(params["latitude"]) < 44
        heures = [f"{j}T{h:02d}:00" for j in jours for h in range(24)]
        return json.dumps({
            "daily": {
                "time": jours,
                "weather_code": [80 if i == 0 else 0 for i in range(len(jours))],
                "temperature_2m_max": [17.4 if chaud else 6.0] * len(jours),
                "temperature_2m_min": [8.2 if chaud else -2.0] * len(jours),
                "precipitation_probability_max": [70] + [5] * (len(jours) - 1),
                "precipitation_sum": [3.4] + [0.0] * (len(jours) - 1),
                "wind_speed_10m_max": [22.0] * len(jours),
                "wind_gusts_10m_max": [45.0] * len(jours),
            },
            "hourly": {
                "time": heures,
                "temperature_2m": [10.0 + (int(h[11:13]) % 12) for h in heures],
                "precipitation_probability": [70 if h.startswith(jours[0]) and 12 <= int(h[11:13]) <= 17 else 10
                                              for h in heures],
                "weather_code": [80 if h.startswith(jours[0]) and 12 <= int(h[11:13]) <= 17 else 1 for h in heures],
            },
            "current": {"time": f"{jours[0]}T10:15", "temperature_2m": 14.2, "weather_code": 2, "wind_speed_10m": 12.0},
        }).encode()


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from tools import meteo

    data = tmp_path / "data"
    data.mkdir()
    (data / "config.yaml").write_text(yaml.safe_dump(CONFIG, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("HOMEOS_DATA", str(data))
    monkeypatch.setenv("HOMEOS_ROOT", str(RACINE))
    for v in ("HOMEOS_CAPABILITIES", "HOMEOS_CAPABILITIES_ATELIER", "HOMEOS_PERSONNE", "HOMEOS_WHO", "HOMEOS_CANAL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(meteo, "_cache_meteo", {})
    monkeypatch.setattr(meteo, "_cache_lieux", {})
    faux = FauxOpenMeteo()
    monkeypatch.setattr(meteo, "_telecharger", faux)
    monkeypatch.setattr(meteo, "today", lambda: AUJOURDHUI)
    return {"data": data, "faux": faux, "tmp": tmp_path}


# --- 1. météo ---------------------------------------------------------------------------------------------------


def lire(env: dict[str, Any], jour: Any = None, commune: str | None = None) -> dict[str, Any]:
    from tools import meteo

    return meteo.lire(jour, commune, telecharger=env["faux"], aujourdhui=AUJOURDHUI)


def test_meteo_du_jour_pour_la_commune_du_foyer(env: dict[str, Any]) -> None:
    m = lire(env)
    assert m["commune"] == "Villeneuve-les-Pins" and m["commune_du_foyer"] is True
    assert m["date"] == "2026-11-08" and m["quand"] == "aujourd'hui" and m["date_longue"] == "dimanche 8 novembre"
    assert (m["min"], m["max"], m["pluie_pct"], m["libelle"], m["icone"]) == (8.2, 17.4, 70, "Averses", "averse")
    assert m["vent_kmh"] == 22.0 and m["rafales_kmh"] == 45.0 and m["pluie_mm"] == 3.4
    moments = {x["moment"]: x for x in m["moments"]}
    assert set(moments) == {"matin", "après-midi", "soir"}
    assert moments["après-midi"]["pluie_pct"] == 70 and moments["après-midi"]["libelle"] == "Averses"
    assert moments["matin"]["pluie_pct"] == 10
    assert m["actuel"] == {"temperature": 14.2, "libelle": "Partiellement nuageux", "icone": "nuage-soleil",
                           "vent_kmh": 12.0, "heure": "10:15"}
    # Géocodage une seule fois, mis en cache sur disque ; prévisions gardées en mémoire.
    lieu = json.loads((env["data"] / "web" / "meteo-lieu.json").read_text(encoding="utf-8"))
    assert lieu == {"commune": "Villeneuve-les-Pins", "latitude": 42.5, "longitude": 2.0}
    nb = len(env["faux"].appels)
    assert nb == 2
    lire(env, "demain")
    assert len(env["faux"].appels) == nb


@pytest.mark.parametrize(("jour", "attendu", "quand"), [
    (None, "2026-11-08", "aujourd'hui"),
    ("aujourd'hui", "2026-11-08", "aujourd'hui"),
    ("Demain", "2026-11-09", "demain"),
    ("après-demain", "2026-11-10", "après-demain"),
    ("samedi", "2026-11-14", "samedi 14 novembre"),
    ("dimanche", "2026-11-08", "aujourd'hui"),
    ("2026-11-12", "2026-11-12", "jeudi 12 novembre"),
    ("12/11/2026", "2026-11-12", "jeudi 12 novembre"),
])
def test_jours_compris(env: dict[str, Any], jour: Any, attendu: str, quand: str) -> None:
    m = lire(env, jour)
    assert m["date"] == attendu and m["quand"] == quand
    assert ("actuel" in m) == (attendu == "2026-11-08")


def test_abreviation_de_la_commune_du_foyer_et_autre_commune(env: dict[str, Any]) -> None:
    from tools import meteo

    assert meteo.designe_le_foyer(None, CONFIG) and meteo.designe_le_foyer("villeneuve", CONFIG)
    assert meteo.designe_le_foyer("Villeneuve les Pins", CONFIG)
    assert not meteo.designe_le_foyer("Montagnac", CONFIG)
    # « Villeneuve » = la commune du foyer : pas de géocodage d'une autre ville du même nom.
    m = lire(env, None, "Villeneuve")
    assert m["commune"] == "Villeneuve-les-Pins" and m["commune_du_foyer"] is True
    autre = lire(env, "demain", "Montagnac")
    assert autre["commune"] == "Montagnac" and autre["commune_du_foyer"] is False and autre["max"] == 6.0
    assert sum("geocoding" in u for u in env["faux"].appels) == 2


@pytest.mark.parametrize(("jour", "motif"), [
    ("2026-11-01", "jours passés"),
    ("2026-12-25", "trop tôt"),
    ("la semaine des quatre jeudis", "pas compris"),
])
def test_jour_hors_previsions(env: dict[str, Any], jour: str, motif: str) -> None:
    from tools import meteo

    with pytest.raises(meteo.MeteoIndisponible, match=motif):
        lire(env, jour)


def test_erreurs_qui_proposent_une_solution(env: dict[str, Any]) -> None:
    from tools import meteo

    env["faux"].panne = True
    with pytest.raises(meteo.MeteoIndisponible) as exc:
        lire(env)
    assert "réessayer" in str(exc.value) and meteo.LIEN_SECOURS in str(exc.value)
    with pytest.raises(meteo.MeteoIndisponible, match="commune du foyer n'est pas réglée"):
        meteo.lire(None, None, config={"assistant": {"nom": "Testbot"}}, telecharger=env["faux"], aujourdhui=AUJOURDHUI)
    env["faux"].panne = False
    with pytest.raises(meteo.MeteoIndisponible, match="ne répond pas pour Atlantide"):
        lire(env, None, "Atlantide")  # commune introuvable


def test_coordonnees_de_la_configuration_sans_geocodage(env: dict[str, Any]) -> None:
    from tools import meteo

    config = {"meteo": {"commune": "Ici", "latitude": 42.5, "longitude": 2.0}}
    m = meteo.lire("demain", None, config=config, telecharger=env["faux"], aujourdhui=AUJOURDHUI)
    assert m["commune"] == "Ici" and not any("geocoding" in u for u in env["faux"].appels)


def test_carte_du_jour_partage_la_meme_logique(env: dict[str, Any]) -> None:
    from bot.config import parse_config
    from coeur import brief

    cfg = parse_config(CONFIG, env["data"], RACINE)
    m = brief.meteo_jour(cfg, AUJOURDHUI, env["faux"])
    assert m == {"commune": "Villeneuve-les-Pins", "min": 8.2, "max": 17.4, "pluie_pct": 70, "libelle": "Averses",
                 "icone": "averse"}
    assert brief.lieu_meteo(cfg, env["faux"])["latitude"] == 42.5
    assert brief.meteo_jour(cfg, AUJOURDHUI + timedelta(days=30), env["faux"]) is None


def test_outil_mcp_enregistre_et_carte_meteo(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from coeur import cartes as ccartes
    from coeur import etapes
    from tools import cartes, server

    outils = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert "meteo_lire" in outils
    description = " ".join(outils["meteo_lire"].description.split())
    assert "commune du foyer" in description and "linge" in description and "grande ville voisine" in description
    m = server.meteo_lire()
    assert m["commune"] == "Villeneuve-les-Pins"
    from tools import db

    assert any(a["kind"] == "meteo_lire" and a["status"] == "done" for a in db.recent_actions(5))
    # Canal web : carte `meteo` conforme au contrat, avec le bouton de la carte du jour.
    monkeypatch.setenv("HOMEOS_CANAL", "web")
    carte = cartes.produire("meteo_lire", m, {})[0]
    assert carte["type"] == "meteo" and not ccartes.erreurs_carte(carte)
    assert carte["titre"] == "Météo aujourd'hui à Villeneuve-les-Pins"
    assert carte["donnees"]["moments"] and carte["donnees"]["actuel"]["temperature"] == 14.2
    assert carte["actions"] == [{"libelle": "Carte du jour", "action": "ouvrir",
                                 "args": {"ecran": "jour", "date": "2026-11-08"}}]
    assert "Averses 8° / 17°" in ccartes.rendu_texte(carte) and "pluie : 70 %" in ccartes.rendu_texte(carte)
    autre = cartes.produire("meteo_lire", lire(env, None, "Montagnac"), {})[0]
    assert autre["actions"] == []  # la carte du jour ne montre que la commune du foyer
    monkeypatch.setenv("HOMEOS_CANAL", "telegram")
    assert cartes.produire("meteo_lire", m, {}) == []
    assert etapes.libelle("mcp__homeos__meteo_lire") == "Je regarde la météo…"


def test_outil_mcp_erreur_avec_solution(env: dict[str, Any]) -> None:
    from tools import server

    env["faux"].panne = True
    with pytest.raises(server.ErreurOutil, match="réessayer"):
        server.meteo_lire("demain")


def test_hotes_open_meteo_dans_la_liste_blanche() -> None:
    from tools import meteo

    liste = (RACINE / "install" / "reseau-autorise.txt").read_text(encoding="utf-8").split()
    for url in (meteo.URL_PREVISIONS, meteo.URL_GEOCODAGE):
        assert urllib.parse.urlsplit(url).hostname in liste


# --- 2. toujours une solution -----------------------------------------------------------------------------------


def test_prompt_systeme_rendu_contient_toujours_une_solution(env: dict[str, Any]) -> None:
    from bot.claude_runner import preparer_prompt_systeme
    from bot.config import parse_config

    cfg = parse_config(CONFIG, env["data"], RACINE)
    texte = preparer_prompt_systeme(cfg, "alex").read_text(encoding="utf-8")
    debut = texte.index("## Toujours une solution")
    section = texte[debut:texte.index("\n## ", debut + 5)]
    assert "je n'ai pas accès à X" in section and "capacites_lister" in section
    # L'ordre des voies proposées.
    voies = ["capacité existante à activer", "capacité que tu peux te fabriquer", "alternative immédiate",
             "je le note pour l'atelier"]
    positions = [section.index(v) for v in voies]
    assert positions == sorted(positions)
    assert "Je peux m'ajouter ça : je lance la fabrication ?" in section and "`atelier_creer`" in section
    assert "Jamais deux fois la même excuse" in section and "Jamais « c'est fait »" in section
    # Trois formulations types : météo, site inconnu, paiement.
    for exemple in ("**Météo**", "**Un site inconnu**", "**Un paiement**"):
        assert exemple in section
    assert "meteo_lire" in section and "bouton Valider" in section
    # La consigne météo dans la liste des outils, et l'atelier proposé par le prompt (base + fragment de la capacité).
    assert "**`meteo_lire(jour, commune)`**" in texte and "pas de la grande ville voisine" in texte
    atelier = texte[texte.index("## Atelier\n"):]
    assert "atelier_creer" in atelier[:3000] and "Je peux m'ajouter ça" in atelier[:3000]
    assert "## Atelier (fabriquer une nouvelle capacité)" in texte  # consignes de la capacité, chargée
    assert "{{" not in texte and "Testbot" in texte


def test_regle_dans_le_skill_et_le_contrat() -> None:
    skill = (RACINE / "content" / "skills" / "ux-capacites" / "SKILL.md").read_text(encoding="utf-8")
    contrat = (RACINE / "CONTRACT.md").read_text(encoding="utf-8")
    for brut in (skill, contrat):
        texte = " ".join(brut.split())
        assert "Toujours une solution" in texte and "je lance la fabrication ?" in texte
        assert "atelier_creer" in texte and "Jamais deux fois la même excuse" in texte
    assert "`meteo_lire(jour=None, commune=None)`" in contrat and "`meteo` (outil" in contrat


def test_atelier_joignable_depuis_la_conversation_d_un_proprietaire(env: dict[str, Any]) -> None:
    """Serveur d'outils persistant : capacités chargées sous homeos (aucune personne), identité par requête."""
    from tools import capacites, identite

    mcp = capacites.MCPFactice()
    chargees = capacites.charger_actives(mcp, racine=RACINE / "capabilities", donnees=env["data"])
    assert "atelier" in chargees and "atelier_creer" in mcp.outils
    demande = {"demande": "ajoute la médiathèque pour voir mes prêts", "site": "mediatheque.exemple.fr",
               "compte": "moi", "action": "voir les dates de retour", "titre": "Médiathèque"}
    with identite.requete("alex"):
        texte = mcp.outils["atelier_creer"](**demande)
    assert texte.startswith("CONFIRMATION_REQUISE:") and "mediatheque.exemple.fr" in texte
    from tools import db

    action = db.get_action(int(texte.split(":")[1]))
    assert action is not None and action["status"] == "pending" and action["kind"] == "atelier_creer"
    with identite.requete("camille"), pytest.raises(Exception, match="propriétaires"):
        mcp.outils["atelier_creer"](**demande)
