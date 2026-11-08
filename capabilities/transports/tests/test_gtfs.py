"""Moteur GTFS / GTFS-RT sur un réseau et un flux fictifs servis en local (modèle `gtfs_rt` instancié par le foyer)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from capabilities.transports import fournisseurs as fr
from capabilities.transports import gtfs, interface
from capabilities.transports import tools as outils

TZ = interface.TZ
GRILLE = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,Métro Démo,https://exemple.org,Europe/Paris\n",
    "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\nR1,A,A,Gare ⇄ Hôpital,1\n",
    "stops.txt": (
        "stop_id,stop_name,location_type,parent_station\n"
        "S1,Gare Centrale,1,\nS1a,Gare Centrale,0,S1\nS2,Mairie,0,\nS3,Hôpital Nord,0,\nS4,Mairie Annexe,0,\n"
    ),
    "trips.txt": (
        "route_id,service_id,trip_id,trip_headsign,trip_short_name,direction_id\n"
        "R1,WE,T1,Hôpital Nord,1234,0\nR1,WE,T2,Hôpital Nord,1236,0\nR1,WE,T3,Hôpital Nord,1238,0\n"
        "R1,WE,T5,Hôpital Nord,1240,0\nR1,WE,T4,Gare Centrale,1235,1\nR1,SEM,T9,Hôpital Nord,1300,0\n"
    ),
    "stop_times.txt": (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "T1,16:20:00,16:20:00,S1a,1\nT1,16:25:00,16:25:00,S2,2\nT1,16:32:00,16:32:00,S3,3\n"
        "T2,16:40:00,16:40:00,S1a,1\nT2,16:45:00,16:45:00,S2,2\nT2,16:52:00,16:52:00,S3,3\n"
        "T3,17:00:00,17:00:00,S1a,1\nT3,17:05:00,17:05:00,S2,2\nT3,17:12:00,17:12:00,S3,3\n"
        "T5,17:20:00,17:20:00,S1a,1\nT5,17:32:00,17:32:00,S3,2\n"
        "T4,16:30:00,16:30:00,S3,1\nT4,16:37:00,16:37:00,S2,2\nT4,16:42:00,16:42:00,S1a,3\n"
        "T9,16:22:00,16:22:00,S1a,1\nT9,16:34:00,16:34:00,S3,2\n"
    ),
    "calendar.txt": (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
        "WE,0,0,0,0,0,1,1,20260101,20271231\nSEM,1,1,1,1,1,0,0,20260101,20271231\n"
    ),
    "calendar_dates.txt": "service_id,date,exception_type\nSEM,20261107,1\nWE,20261108,2\n",
}
INSTANT = datetime(2026, 11, 7, 16, 10, tzinfo=TZ)


def _ts(h: int, m: int) -> int:
    return int(datetime(2026, 11, 7, h, m, tzinfo=TZ).timestamp())


FLUX_TU = {
    "header": {"gtfs_realtime_version": "2.0", "timestamp": _ts(16, 9)},
    "entity": [
        {"id": "e1", "trip_update": {"trip": {"trip_id": "T1", "start_date": "20261107"},
                                     "stop_time_update": [{"stop_sequence": 1, "stop_id": "S1a", "departure": {"delay": 360}},
                                                          {"stop_sequence": 3, "stop_id": "S3", "arrival": {"delay": 300}}]}},
        {"id": "e2", "trip_update": {"trip": {"trip_id": "T2", "start_date": "20261107", "schedule_relationship": 3}}},
        {"id": "e3", "trip_update": {"trip": {"trip_id": "T3", "start_date": "20261107"},
                                     "stop_time_update": [{"stop_sequence": 1, "stop_id": "S1a", "schedule_relationship": 1}]}},
        {"id": "e9", "trip_update": {"trip": {"trip_id": "T9", "start_date": "20261107"},
                                     "stop_time_update": [{"stop_id": "S1a", "departure": {"time": _ts(16, 21)}}]}},
    ],
}
FLUX_ALERTES = {
    "entity": [
        {"id": "a1", "alert": {"active_period": [{"start": _ts(6, 0), "end": _ts(23, 0)}],
                               "informed_entity": [{"route_id": "R1"}], "effect": 3,
                               "header_text": {"translation": [{"text": "Trafic perturbé", "language": "fr"},
                                                               {"text": "Disrupted", "language": "en"}]},
                               "description_text": {"translation": [{"text": "<p>Incident technique à Mairie.</p>", "language": "fr"}]}}},
        {"id": "a2", "alert": {"active_period": [{"start": _ts(6, 0), "end": _ts(7, 0)}],
                               "informed_entity": [{"route_id": "R1"}], "header_text": {"translation": [{"text": "Passé"}]}}},
        {"id": "a3", "alert": {"informed_entity": [{"route_id": "AUTRE"}], "header_text": {"translation": [{"text": "Ailleurs"}]}}},
    ]
}


# --- protobuf ---------------------------------------------------------------------------------------------------------------


def test_aller_retour_protobuf() -> None:
    octets = gtfs.encoder_flux(FLUX_TU)
    flux = gtfs.decoder_flux(octets)
    assert flux["header"]["timestamp"] == _ts(16, 9)
    e1 = flux["entity"][0]["trip_update"]
    assert e1["trip"] == {"trip_id": "T1", "start_date": "20261107"}
    assert e1["stop_time_update"][0]["departure"] == {"delay": 360}
    assert flux["entity"][1]["trip_update"]["trip"]["schedule_relationship"] == 3
    # retard négatif (int32 sur 10 octets)
    neg = gtfs.decoder_flux(gtfs.encoder_flux({"entity": [{"id": "x", "trip_update": {"trip": {"trip_id": "T"}, "delay": -90}}]}))
    assert neg["entity"][0]["trip_update"]["delay"] == -90
    alertes = gtfs.decoder_flux(gtfs.encoder_flux(FLUX_ALERTES))
    assert gtfs.texte_traduit(alertes["entity"][0]["alert"]["description_text"]) == "Incident technique à Mairie."


def test_flux_json_camelcase() -> None:
    brut = {"header": {"timestamp": "1794073740"}, "entity": [{"id": "1", "tripUpdate": {
        "trip": {"tripId": "T1", "startDate": "20261107"}, "stopTimeUpdate": [{"stopId": "S1a", "departure": {"delay": 60}}]}}]}
    flux = gtfs.decoder_flux(json.dumps(brut).encode())
    assert flux["entity"][0]["trip_update"]["stop_time_update"][0]["stop_id"] == "S1a"
    assert flux["header"]["timestamp"] == 1794073740


@pytest.mark.parametrize("contenu", [b"", b"\x0a\xff\xff\xff", b"{pas du json"])
def test_flux_invalide(contenu: bytes) -> None:
    with pytest.raises(gtfs.FluxInvalide):
        gtfs.decoder_flux(contenu)


def test_flux_sncf_reel_decodable() -> None:
    """Forme réelle des identifiants SNCF (numéro de train dans le trip_id) : appariement par numéro."""
    from capabilities.transports.fournisseurs import _module_source

    source = _module_source(fr.trouver("sncf")).Source(SimpleNamespace(dossier=Path("/nonexistent")), {}, None)
    assert source.numero_rt("OCESN3970F1187_F:ICN:OCESN-87611483-87547000:87611483:87547000:15:3352:20261107") == "3970"
    assert source.cle_course("x", "03970", "20261107") == source.cle_course("y", "3970", "20261107")


# --- réseau fictif servi en local ----------------------------------------------------------------------------------------------


@pytest.fixture()
def reseau(serveur: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setenv("HOMEOS_NOW", INSTANT.isoformat())
    monkeypatch.setenv("HOMEOS_CAPABILITIES", str(Path(__file__).resolve().parents[2]))
    serveur.routes["/gtfs.zip"] = (200, gtfs.zip_grille(GRILLE), "application/zip")
    serveur.routes["/tu.pb"] = (200, gtfs.encoder_flux(FLUX_TU), "application/x-protobuf")
    serveur.routes["/alertes.pb"] = (200, gtfs.encoder_flux(FLUX_ALERTES), "application/x-protobuf")
    donnees = tmp_path / "data"
    d = donnees / "capacites" / "transports"
    d.mkdir(parents=True)
    (d / "reseaux.yaml").write_text(yaml.safe_dump([{
        "nom": "metro_demo", "titre": "Métro démo", "modele": "gtfs_rt", "modes": ["metro"],
        "url_grille": f"{serveur.url}/gtfs.zip", "url_temps_reel": f"{serveur.url}/tu.pb",
        "url_alertes": f"{serveur.url}/alertes.pb"}]), encoding="utf-8")
    gtfs_cache = fr._CACHE
    gtfs_cache.clear()
    ctx = SimpleNamespace(donnees=donnees, personne="alice", config={
        "personnes": [{"prenom": "Alice", "linux_user": "alice", "role": "proprietaire"}]})
    return SimpleNamespace(ctx=ctx, serveur=serveur)


def _source(r: SimpleNamespace) -> Any:
    return fr.charger(fr.trouver("metro_demo", r.ctx.donnees), r.ctx.donnees, r.ctx)


def test_instance_decouverte(reseau: SimpleNamespace) -> None:
    f = fr.trouver("Métro démo", reseau.ctx.donnees)
    assert (f.provenance, f.modele, f.modes, f.valide) == ("instance", "gtfs_rt", ["metro"], True)
    assert f.reseau == ["127.0.0.1"]
    assert [x.nom for x in outils.lister_fournisseurs(reseau.ctx) if x.provenance == "instance"] == ["metro_demo"]


def test_resoudre_trajet(reseau: SimpleNamespace) -> None:
    t = _source(reseau).resoudre_trajet({"origine": "gare centrale", "destination": "hopital", "mode": "metro"})
    assert (t["origine"], t["destination"], t["ligne"]) == ("Gare Centrale", "Hôpital Nord", "A")
    assert t["details"] == {"origine_ids": ["S1", "S1a"], "destination_ids": ["S3"]}
    with pytest.raises(interface.ErreurTransports, match="Plusieurs arrêts correspondent à « mai » sur Métro démo : Mairie ; Mairie Annexe"):
        _source(reseau).resoudre_trajet({"origine": "mai", "destination": "hopital"})
    with pytest.raises(interface.ErreurTransports, match="Aucune course"):
        _source(reseau).resoudre_trajet({"origine": "Hôpital Nord", "destination": "Mairie Annexe"})


def test_prochains_departs_temps_reel(reseau: SimpleNamespace) -> None:
    res = _source(reseau).prochains_departs({"origine": "Gare Centrale", "destination": "Hôpital"}, 90)
    assert res["temps_reel"] is True and res["avertissement"] is None
    resumes = [(d["numero"], d["heure_theorique"], d["statut"], d["resume"]) for d in res["departs"]]
    assert resumes == [
        ("1234", "16:20", "a_venir", "dans 16 min (16:26, retard 6 min)"),        # retard au départ (delay)
        ("1300", "16:22", "a_venir", "dans 11 min (16:21, en avance de 1 min)"),   # heure absolue, service ajouté ce jour
        ("1236", "16:40", "supprime", "supprimé (prévu à 16:40)"),                 # course supprimée
        ("1238", "17:00", "supprime", "supprimé (prévu à 17:00)"),                 # arrêt sauté
        ("1240", "17:20", "prevu", "à 17:20 (17:20, horaire théorique)"),          # pas dans le flux : théorique
    ]
    assert all(d["ligne"] == "A" for d in res["departs"])


def test_prochains_sans_temps_reel(reseau: SimpleNamespace) -> None:
    reseau.serveur.routes["/tu.pb"] = (500, b"", "text/plain")
    fr._CACHE.clear()
    res = _source(reseau).prochains_departs({"origine": "Gare Centrale", "destination": "Hôpital"}, 30)
    assert res["temps_reel"] is False and "Temps réel indisponible" in res["avertissement"]
    assert [d["resume"] for d in res["departs"]][:2] == ["dans 10 min (16:20, horaire théorique)",
                                                          "dans 12 min (16:22, horaire théorique)"]


def test_perturbations(reseau: SimpleNamespace) -> None:
    (p,) = _source(reseau).perturbations({"origine": "Gare Centrale", "destination": "Hôpital"})
    assert p["titre"] == "Trafic perturbé" and p["texte"] == "Incident technique à Mairie."
    assert p["effet"] == "retards importants"


def test_etat_par_numero(reseau: SimpleNamespace) -> None:
    e = _source(reseau).etat_course("1234", "2026-11-07", "metro")
    assert e["statut"] == "en_retard" and e["retard_min"] == 6 and e["source"] == "temps_reel"
    assert [a["lieu"] for a in e["arrets"]] == ["Gare Centrale", "Mairie", "Hôpital Nord"]
    assert e["depart"] == {"lieu": "Gare Centrale", "prevu": "16:20", "estime": "16:26", "retard_min": 6}
    assert _source(reseau).etat_course("1236", "2026-11-07", "metro")["statut"] == "supprime"
    with pytest.raises(interface.ErreurTransports, match="Je ne trouve pas"):
        _source(reseau).etat_course("1234", "2026-11-08", "metro")  # service retiré ce jour-là (calendar_dates)


def test_trajet_ajoute_puis_alerte(reseau: SimpleNamespace) -> None:
    res = outils.ajouter_trajet(reseau.ctx, "alice", "metro", "Gare Centrale", "Hôpital", "16:20", "sam")
    assert res["trajet"]["fournisseur"] == "metro_demo"
    (t,) = outils.prochains(reseau.ctx)["trajets"]
    assert t["alerte"] == "retard 6 min sur le départ de 16:20"
    assert t["perturbations"][0]["titre"] == "Trafic perturbé"


def test_hote_non_approuve_refuse(reseau: SimpleNamespace) -> None:
    instances = fr.lire_instances(reseau.ctx.donnees)
    instances[0]["url_temps_reel"] = "https://flux.exemple.org/tu.pb"
    fr.ecrire_instances(reseau.ctx.donnees, instances)
    f = fr.trouver("metro_demo", reseau.ctx.donnees)
    assert "flux.exemple.org" in f.reseau and "flux.exemple.org" not in fr.hotes_effectifs(f, reseau.ctx.donnees)
    with pytest.raises(interface.ErreurTransports, match="n'est pas autorisé"):
        fr.charger(f, reseau.ctx.donnees, reseau.ctx).flux("temps_reel")


def test_reseau_ajoute_par_un_proprietaire(reseau: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    resume, payload = outils.preparer_reseau(reseau.ctx, "Tram de Grandport", "https://data.exemple.org/gtfs.zip",
                                             "https://rt.exemple.org/tu.pb", modes="tram")
    assert "Il parlera à data.exemple.org, rt.exemple.org" in resume
    assert payload["nom"] == "tram_de_grandport" and payload["modes"] == ["tram"]
    with pytest.raises(interface.ErreurTransports, match="https seulement"):
        outils.preparer_reseau(reseau.ctx, "Tram X", "http://data.exemple.org/gtfs.zip")
    monkeypatch.setattr(fr.Client, "telecharger", lambda self, *a, **k: (_ for _ in ()).throw(interface.SourceIndisponible("non")))
    message = outils.executer("transports_reseau_ajouter", payload, reseau.ctx)
    assert message.startswith("Réseau « Tram de Grandport » ajouté.") and "cette nuit" in message
    etat = json.loads((reseau.ctx.donnees / "capacites" / "etat.json").read_text(encoding="utf-8"))
    assert etat["transports"]["reseau_approuve"] == ["data.exemple.org", "rt.exemple.org"]
    f = fr.trouver("tram_de_grandport", reseau.ctx.donnees)
    assert fr.hotes_effectifs(f, reseau.ctx.donnees) == ["data.exemple.org", "rt.exemple.org"]


def test_reseau_refuse_a_un_membre(reseau: SimpleNamespace) -> None:
    reseau.ctx.config["personnes"][0]["role"] = "membre"
    from tools import capacites

    with pytest.raises(capacites.ActionRefusee):
        outils.preparer_reseau(reseau.ctx, "Tram", "https://data.exemple.org/gtfs.zip")


def test_grille_invalide_refusee(reseau: SimpleNamespace) -> None:
    reseau.serveur.routes["/gtfs.zip"] = (200, b"pas un zip", "application/zip")
    with pytest.raises(interface.SourceIndisponible, match="zip invalide"):
        _source(reseau).rafraichir(forcer=True)


def test_smoke_source(reseau: SimpleNamespace) -> None:
    f = fr.trouver("metro_demo", reseau.ctx.donnees)
    absente = fr.verifier_source(f, reseau.ctx.donnees, reseau.ctx)
    assert absente["ok"] is False and "grille horaire absente" in absente["message"]
    _source(reseau).rafraichir(forcer=True)
    res = fr.verifier_source(fr.trouver("metro_demo", reseau.ctx.donnees), reseau.ctx.donnees, reseau.ctx)
    assert res["ok"] is True and "grille de 0 j" in res["message"] and "temps réel OK (4 courses suivies)" in res["message"]
