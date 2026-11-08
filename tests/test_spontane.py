"""Messages spontanés (coeur.spontane) : routines → fil de l'appli + notification courte sans donnée sensible,
une notification par personne et par jour hors seuils déclarés, destinataires, intégration aux crons (briefs,
capacités, Kitchen Daily) et à web.push."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

RACINE = Path(__file__).resolve().parent.parent

CONFIG: dict[str, Any] = {
    "assistant": {"nom": "Testbot"},
    "personnes": [
        {"telegram_id": 1, "prenom": "Alex", "linux_user": "alex"},
        {"telegram_id": 2, "prenom": "Camille", "linux_user": "camille", "role": "membre"},
        {"telegram_id": 3, "prenom": "Invité", "linux_user": "invite1", "role": "invite"},
    ],
}

CRONS_SEUIL = '''\
def executer(nom_cron, ctx):
    if nom_cron == "texte":
        return "Résultat simple de la routine"
    if nom_cron == "riche":
        return {"texte": "Total 54,90 € pour la semaine", "seuil": "urgent",
                "notification": {"titre": "Démo", "corps": "Total 54,90 €"}}
    if nom_cron == "perso":
        ctx.publier("camille", "Pour Camille seulement", notification={"titre": "Démo", "corps": "Rien que pour toi."},
                    seuil="urgent")
        return None
    return None
'''


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    data = tmp_path / "data"
    data.mkdir()
    caps = tmp_path / "capabilities"
    caps.mkdir()
    monkeypatch.setenv("HOMEOS_DATA", str(data))
    monkeypatch.setenv("HOMEOS_ROOT", str(tmp_path))
    monkeypatch.setenv("HOMEOS_CAPABILITIES", str(caps))
    monkeypatch.setenv("HOMEOS_WHO", "test")
    monkeypatch.delenv("HOMEOS_ETAT", raising=False)
    monkeypatch.delenv("HOMEOS_PERSONNE", raising=False)
    (data / "config.yaml").write_text(yaml.safe_dump(CONFIG, allow_unicode=True), encoding="utf-8")
    return {"data": data, "caps": caps}


def creer_demo(caps: Path, seuils: list[dict[str, str]] | None = None, personnes: list[str] | None = None) -> None:
    d = caps / "demo"
    d.mkdir()
    manifeste = {
        "nom": "demo", "titre": "Démo", "description": "Capacité de test.", "version": 1, "statut": "actif",
        "origine": "humain", "personnes": personnes or [],
        "crons": [{"nom": n, "quand": "Mon 04:00"} for n in ("texte", "riche", "perso")],
        "seuils": seuils if seuils is not None else [{"nom": "urgent", "description": "quelque chose d'urgent"}],
    }
    (d / "capability.yaml").write_text(yaml.safe_dump(manifeste, allow_unicode=True), encoding="utf-8")
    (d / "tools.py").write_text("def enregistrer(mcp, ctx):\n    pass\n", encoding="utf-8")
    (d / "CLAUDE.md").write_text("Démo.\n", encoding="utf-8")
    (d / "smoke.py").write_text("def verifier(ctx):\n    return {'ok': True, 'message': 'ok'}\n", encoding="utf-8")
    (d / "crons.py").write_text(CRONS_SEUIL, encoding="utf-8")


def historique(env: dict[str, Path]) -> Any:
    from coeur.historique import Historique

    return Historique(env["data"] / "appli.sqlite")


def messages_de(env: dict[str, Path], linux_user: str) -> list[dict[str, Any]]:
    return historique(env).spontanes_du_jour(linux_user)


# --- notification -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "corps",
    [
        "Total 54,90 € cette semaine",
        "Prélèvement de 120 euros",
        "Code 482913 pour le colis",
        "Carte 4970 1012 3456 7890",
        "**Important** : réunion",
        "Facture : € 30",
    ],
)
def test_notification_sans_donnee_sensible(corps: str) -> None:
    from coeur import spontane

    sure = spontane.notification_sure({"titre": "Brief", "corps": corps}, "Testbot")
    assert sure == {"titre": "Brief", "corps": spontane.CORPS_GENERIQUE}


def test_notification_conservee_si_sure() -> None:
    from coeur import spontane

    assert spontane.notification_sure({"titre": "Bus 7", "corps": "Retard 7 min sur le bus de 16:25."}, "X") == {
        "titre": "Bus 7", "corps": "Retard 7 min sur le bus de 16:25."}
    # Titre vide ou dangereux : prénom de l'assistant ; corps absent : générique ; tout tient sur une ligne, court.
    sure = spontane.notification_sure({"titre": "Solde 1 200 €", "corps": "x" * 500}, "Testbot")
    assert sure["titre"] == "Testbot" and len(sure["corps"]) == spontane.LONGUEUR_CORPS
    assert spontane.notification_sure(None, "Testbot") == {"titre": "Testbot", "corps": spontane.CORPS_GENERIQUE}


# --- publication, quota, seuils -------------------------------------------------------------------------------------


def test_publier_ecrit_dans_le_fil_et_applique_le_quota(env: dict[str, Path]) -> None:
    from coeur import spontane

    creer_demo(env["caps"])
    hist = historique(env)
    p1 = spontane.publier("alex", "Brief du jour : rien de spécial.", spontane.carte_brief("Brief du jour"),
                          notification={"titre": "Brief du matin", "corps": "Ton brief du jour est prêt."},
                          motif="brief", historique=hist)
    assert p1.notifier is True and p1.cartes[0]["type"] == "brief"
    conv = hist.conversation_de("alex", p1.conversation_id)
    assert conv is not None and conv["titre"] == "Brief du matin"
    (m,) = hist.messages(p1.conversation_id)
    assert m["canal"] == "routine" and m["role"] == "assistant" and m["auteur"] == "Testbot"
    assert m["meta"]["spontane"] is True and m["meta"]["notification"]["corps"] == "Ton brief du jour est prêt."
    # Deuxième message du jour sans seuil : écrit en silence, dans la même conversation (courante).
    p2 = spontane.publier("alex", "Autre routine", historique=hist)
    assert p2.notifier is False and p2.conversation_id == p1.conversation_id
    # Seuil non déclaré par la capacité : compté dans le quota (silence).
    p3 = spontane.publier("alex", "Faux urgent", seuil="inconnu", capacite="demo", historique=hist)
    assert p3.notifier is False and "seuil" not in hist.messages(p3.conversation_id)[-1]["meta"]
    # Seuil déclaré dans `seuils:` du manifeste : notifié malgré le quota.
    p4 = spontane.publier("alex", "Vrai urgent", seuil="urgent", capacite="demo", historique=hist)
    assert p4.notifier is True and hist.messages(p4.conversation_id)[-1]["meta"]["seuil"] == "urgent"
    # Quota par personne : Camille n'a encore rien reçu aujourd'hui.
    assert spontane.publier("camille", "Pour Camille", historique=hist).notifier is True
    with pytest.raises(ValueError):
        spontane.publier("alex", "  ", historique=hist)


def test_publier_foyer_destinataires_et_cloisonnement(env: dict[str, Path]) -> None:
    from coeur import spontane

    pubs = spontane.publier_foyer("Le programme de la semaine", spontane.carte_brief("x", periode="semaine"),
                                  {"titre": "Brief de la semaine", "corps": "Le programme de la semaine est prêt."},
                                  motif="brief")
    # Adultes seulement (propriétaire, membre) : jamais l'invité.
    assert sorted(p.personne for p in pubs) == ["alex", "camille"]
    hist = historique(env)
    conv_alex, conv_camille = (hist.courante(u) for u in ("alex", "camille"))
    assert conv_alex["id"] != conv_camille["id"]
    assert hist.conversation_de("camille", conv_alex["id"]) is None
    # Une carte par personne, avec des identifiants distincts.
    ids = {hist.messages(c["id"])[0]["cartes"][0]["id"] for c in (conv_alex, conv_camille)}
    assert len(ids) == 2
    assert hist.courante("invite1") is None


def test_capacite_reservee_ne_parle_qu_a_ses_personnes(env: dict[str, Path]) -> None:
    from coeur import spontane

    creer_demo(env["caps"], personnes=["camille"])
    assert spontane.destinataires(capacite="demo") == ["camille"]
    assert [p.personne for p in spontane.publier_foyer("x", capacite="demo")] == ["camille"]


def test_brief_publie_retire_la_copie_des_cartes_du_jour(env: dict[str, Path]) -> None:
    from coeur import spontane

    hist = historique(env)
    assert spontane.brief_publie_aujourdhui(hist, "alex") is False
    spontane.publier("alex", "Brief hebdo", spontane.carte_brief("Brief hebdo", periode="semaine"), motif="brief",
                     historique=hist)
    assert spontane.brief_publie_aujourdhui(hist, "alex") is False
    spontane.publier("alex", "Brief", spontane.carte_brief("Brief"), motif="brief", historique=hist)
    assert spontane.brief_publie_aujourdhui(hist, "alex") is True
    assert spontane.brief_publie_aujourdhui(hist, "camille") is False


# --- intégration web.push ----------------------------------------------------------------------------------------


def test_notification_web_push_n_utilise_jamais_le_texte(env: dict[str, Path]) -> None:
    from coeur import spontane
    from web import push
    from web.flux import EvenementBus

    hist = historique(env)
    avant = push._dernier_id(hist.chemin)
    spontane.publier("alex", "Courses : 54,90 € chez le primeur, rendez-vous médical à 9 h",
                     notification={"titre": "Brief du matin", "corps": "Ton brief du jour est prêt."}, historique=hist)
    spontane.publier("alex", "Deuxième", historique=hist)
    premier, second = push._messages_spontanes(hist.chemin, avant)
    assert premier["linux_user"] == "alex"
    notif = push.resume_evenement(EvenementBus("message", premier, premier["conversation_id"]))
    assert notif == {"titre": "Brief du matin", "texte": "Ton brief du jour est prêt.", "url": "/"}
    assert "54" not in json.dumps(notif) and "médical" not in json.dumps(notif)
    # Au-delà du quota : affiché dans l'appli, jamais notifié.
    assert push.resume_evenement(EvenementBus("message", second, second["conversation_id"])) is None


# --- intégration aux crons ------------------------------------------------------------------------------------------


def test_brief_du_runner_publie_dans_l_appli(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import runner

    texte = "**Mardi** : piscine 10 h, facture EDF 84,20 €."
    monkeypatch.setattr(runner, "appeler_claude", lambda prompt, modele=None: texte)
    donnees = {"aujourdhui": "2026-11-10"}
    # Sans --publier (/brief dans Telegram) : rien dans l'appli.
    assert runner.generer("brief-jour", donnees, False, "brief_jour") == 0
    assert messages_de(env, "alex") == []
    assert runner.generer("brief-jour", donnees, False, "brief_jour", publier=True) == 0
    for u in ("alex", "camille"):
        (m,) = messages_de(env, u)
        assert m["texte"] == texte and m["meta"]["motif"] == "brief"
        assert m["cartes"][0]["type"] == "brief" and m["cartes"][0]["donnees"]["periode"] == "jour"
        assert m["cartes"][0]["donnees"]["date"] == "2026-11-10"
        assert m["meta"]["notification"] == runner.NOTIFICATIONS["brief_jour"]
    assert messages_de(env, "invite1") == []


def test_cron_de_capacite_publie_avec_carte_et_seuil(env: dict[str, Path]) -> None:
    from coeur import spontane

    creer_demo(env["caps"])
    hist = historique(env)
    spontane.publier("alex", "Brief", historique=hist)  # quota d'Alex consommé
    proc = subprocess.run([sys.executable, "-m", "tools.capacites", "cron", "demo", "texte", "--publier"],
                          cwd=str(RACINE), capture_output=True, text=True, check=False,
                          env={**__import__("os").environ, "PYTHONPATH": str(RACINE)})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "Résultat simple de la routine"  # posté dans le groupe par capacite-cron.sh
    m = messages_de(env, "alex")[-1]
    assert m["cartes"][0]["type"] == "routine_resultat" and m["cartes"][0]["donnees"]["capacite"] == "demo"
    assert m["meta"]["notifier"] is False  # quota d'Alex atteint, pas de seuil
    assert m["meta"]["notification"] == {"titre": "Démo", "corps": "Nouveau résultat de ta routine."}
    assert messages_de(env, "camille")[-1]["meta"]["notifier"] is True

    from tools import capacites

    resultat = capacites.executer_cron_detaille("demo", "riche")
    assert resultat is not None and resultat.seuil == "urgent"
    assert capacites.publier_resultat_cron("demo", "riche", resultat) == 2
    m = messages_de(env, "alex")[-1]
    assert m["meta"]["notifier"] is True and m["meta"]["seuil"] == "urgent"  # seuil déclaré : notifié
    assert m["meta"]["notification"]["corps"] == spontane.CORPS_GENERIQUE  # montant refusé dans la notification

    # ctx.publier : une seule personne, rien pour le groupe.
    assert capacites.executer_cron("demo", "perso") is None
    assert messages_de(env, "camille")[-1]["texte"] == "Pour Camille seulement"
    assert all(m["texte"] != "Pour Camille seulement" for m in messages_de(env, "alex"))


def test_capacite_reservee_refuse_ctx_publier_pour_un_autre(env: dict[str, Path]) -> None:
    from tools import capacites

    creer_demo(env["caps"], personnes=["alex"])
    with pytest.raises(capacites.ErreurCapacite):
        capacites.executer_cron("demo", "perso")  # Camille n'est pas autorisée


def test_kitchen_daily_rappel_seuil_date_limite(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from coeur import spontane
    from tools import capacites, runner

    monkeypatch.delenv("HOMEOS_CAPABILITIES")
    cap = capacites.trouver("kitchen_daily")
    assert [s.nom for s in cap.manifeste.seuils] == ["date_limite"]
    monkeypatch.setattr(runner, "appeler_claude", lambda prompt, modele=None: "1. Dahl\n2. Lasagnes (12,50 €)")
    monkeypatch.setattr(runner, "donnees_rappel_kitchen", lambda ref=None: {"menu": {}})
    hist = historique(env)
    spontane.publier("alex", "Brief", historique=hist)
    resultat = capacites.executer_cron_detaille("kitchen_daily", "rappel")
    assert resultat is not None and resultat.texte.startswith("1. Dahl") and resultat.seuil == "date_limite"
    assert capacites.publier_resultat_cron("kitchen_daily", "rappel", resultat) == 2
    m = messages_de(env, "alex")[-1]
    assert m["meta"]["notifier"] is True and m["meta"]["seuil"] == "date_limite"
    assert m["meta"]["notification"] == {"titre": "Kitchen Daily", "corps": "Une sélection de plats t'attend, à valider."}
    assert "Dahl" not in json.dumps(m["meta"]["notification"], ensure_ascii=False)


# --- manifeste : seuils et crons périodiques -------------------------------------------------------------------------


def test_seuils_et_cron_periodique_dans_le_manifeste() -> None:
    from tools import capacites

    assert capacites.oncalendar("06:00..21:00/10") == "*-*-* 06..20:00/10:00"
    assert capacites.oncalendar("Mon..Fri 07:00..09:00/15") == "Mon..Fri *-*-* 07..08:00/15:00"
    for invalide in ("06:30..21:00/10", "06:00..21:00/7", "21:00..06:00/10", "06:00..25:00/10"):
        with pytest.raises(ValueError):
            capacites.oncalendar(invalide)
    base = {"nom": "demo", "titre": "Démo", "description": "d", "version": 1, "statut": "actif", "origine": "humain"}
    m, erreurs, _ = capacites.valider_manifeste({**base, "seuils": [{"nom": "retard", "description": "≥ 5 min"}]})
    assert not erreurs and m is not None and m.seuils[0].nom == "retard"
    _, erreurs, _ = capacites.valider_manifeste({**base, "seuils": [{"nom": "retard"}]})
    assert any("description" in e for e in erreurs)
    _, erreurs, _ = capacites.valider_manifeste({**base, "seuils": "retard"})
    assert erreurs
