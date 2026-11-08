"""Backend réel de l'appli lancé en local pour le test de bout en bout du front et les captures (lot 4b).

    python -m tests.front.demo --port 8123 --dossier /tmp/demo-appli [--personne alice]

Même assemblage que ``tests/test_web.py`` : FastAPI de ``web.app``, cœur de la conversation, stockage réel dans un
dossier temporaire, et un **faux** ``claude`` (stream-json) qui rejoue des échanges selon les mots du message :
agenda (carte + confirmation), courses, repas (planning + proposition Kitchen Daily), identifiants (carte lot 5c),
documents, comparatif, bus ; le filtre de données sensibles du cœur est réel (événement ``masquage``). Les cartes du
jour (``/api/brief/jour``) sont remplacées par des données de démonstration neutres (aucune donnée du foyer).

Affiche sur une ligne JSON ``{"origine", "invitation", "prete": true}`` quand le serveur écoute.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

# Foyer de démonstration neutre (aucune donnée réelle) : deux adultes, Alex et Camille, deux enfants, un chien.
PERSONNES = [
    {"telegram_id": 101, "prenom": "Alex", "linux_user": "alex"},
    {"telegram_id": 202, "prenom": "Camille", "linux_user": "camille", "role": "membre"},
]

FAUX_CLAUDE = r'''#!{python}
"""Faux claude -p (stream-json) pour le front : scénario choisi selon les mots du message."""
import json, sys, time
args = sys.argv[1:]
prompt = args[args.index("-p") + 1] if "-p" in args else ""
bas = prompt.lower().split("\n")[-1] if "[donnée sensible" not in prompt else prompt.lower()
dernier = next((l for l in reversed(prompt.split("\n")) if l and not l.startswith("[")), "")
actions = json.load(open({actions!r}))
def ev(o): print(json.dumps(o, ensure_ascii=False), flush=True)
def outil(i, nom, cartes, texte="ok"):
    ev({{"type": "assistant", "session_id": "s1", "message": {{"content": [{{"type": "tool_use", "id": i, "name": "mcp__homeos__" + nom, "input": {{}}}}]}}}})
    time.sleep(0.35)
    contenu = texte + "".join("\nCARTE:" + json.dumps(c, ensure_ascii=False, separators=(",", ":")) for c in cartes)
    ev({{"type": "user", "session_id": "s1", "message": {{"content": [{{"type": "tool_result", "tool_use_id": i, "content": [{{"type": "text", "text": contenu}}]}}]}}}})
def dire(texte):
    for mot in texte.split(" "):
        ev({{"type": "stream_event", "session_id": "s1", "event": {{"type": "content_block_delta", "delta": {{"type": "text_delta", "text": mot + " "}}}}}})
        time.sleep(0.02)
def fin(texte):
    ev({{"type": "result", "subtype": "success", "is_error": False, "result": texte, "session_id": "s1", "total_cost_usd": 0.01, "duration_ms": 1200, "usage": {{"input_tokens": 1, "output_tokens": 1}}}})
ev({{"type": "system", "subtype": "init", "session_id": "s1"}})
jour = {jour!r}
if "agenda" in bas or "samedi" in bas or "dentiste" in bas:
    outil("t1", "agenda_lire", [{{"type": "agenda_jour", "id": "c-agenda", "donnees": {{"date": jour, "evenements": [{{"titre": "Piscine", "debut": jour + "T10:00", "agenda": "famille", "lieu": "Centre aquatique"}}, {{"titre": "Dentiste", "debut": jour + "T17:00", "agenda": "alex"}}]}}, "actions": [{{"libelle": "Ouvrir l'agenda", "action": "ouvrir", "args": {{"ecran": "agenda"}}}}]}}])
    outil("t2", "agenda_ajouter", [{{"type": "evenement", "id": "c-ev", "titre": "Proposition d'événement", "donnees": {{"titre": "Dentiste", "debut": jour + "T17:00", "agenda": "famille", "statut": "a_valider"}}, "actions": [{{"libelle": "Valider", "action": "confirmer", "args": {{"decision": "valider"}}, "confirmation": actions["agenda"]}}, {{"libelle": "Modifier", "action": "message", "args": {{"texte": "Je voudrais modifier cette proposition."}}}}, {{"libelle": "Annuler", "action": "confirmer", "args": {{"decision": "annuler"}}, "confirmation": actions["agenda"]}}]}}])
    dire("Samedi : piscine à 10 h. Je propose d'ajouter le dentiste à 17 h, tu valides ?")
    fin("Samedi : piscine à 10 h. Je propose d'ajouter le dentiste à 17 h, tu valides ?\nCONFIRMATION_REQUISE:%d:Ajouter « Dentiste » à 17 h" % actions["agenda"])
elif "course" in bas or "lait" in bas:
    rayons = [{{"rayon": "Fruits et légumes", "articles": [{{"id": 1, "article": "Clémentines", "coche": True, "ajoute_par": "Camille"}}, {{"id": 2, "article": "Poireaux", "coche": False, "ajoute_par": "Alex"}}, {{"id": 3, "article": "Pommes", "coche": False, "ajoute_par": "Camille"}}]}},
              {{"rayon": "Crèmerie", "articles": [{{"id": 4, "article": "Lait demi-écrémé", "coche": False, "ajoute_par": "Alex"}}, {{"id": 5, "article": "Yaourts nature", "coche": False, "ajoute_par": "Camille"}}, {{"id": 6, "article": "Beurre doux", "coche": False, "ajoute_par": "Alex"}}]}},
              {{"rayon": "Boulangerie", "articles": [{{"id": 7, "article": "Pain de mie complet", "coche": False, "ajoute_par": "Alex"}}]}},
              {{"rayon": "Épicerie", "articles": [{{"id": 8, "article": "Huile d'olive", "coche": False, "ajoute_par": "Camille"}}, {{"id": 9, "article": "Café en grains", "coche": True, "ajoute_par": "Alex"}}]}}]
    outil("t1", "courses_ajouter", [{{"type": "courses", "id": "c-courses", "titre": "Liste de courses mise à jour", "donnees": {{"rayons": rayons, "restants": 7, "total": 9, "ajoutes": ["Lait demi-écrémé"]}}, "actions": [{{"libelle": "Ouvrir la liste", "action": "ouvrir", "args": {{"ecran": "courses"}}}}]}}])
    dire("C'est noté, le lait est sur la liste.")
    fin("C'est noté, le lait est sur la liste.")
elif "repas" in bas or "mange" in bas:
    import datetime
    d0 = datetime.date.fromisoformat(jour)
    menus = [("Salade de lentilles", "maison", "Dahl de lentilles corail", "kitchen_daily"), ("Tajine de poulet aux pois chiches", "kitchen_daily", "Tagliatelles carbonara", "kitchen_daily"),
             ("Crêperie après le marché", "restaurant", "Croque-monsieur et salade verte", "maison"), ("Parmentier de patates douces", "kitchen_daily", "Pavé de saumon, riz basmati", "kitchen_daily"),
             ("Restes", "maison", "Pâtes maison", "maison"), ("Quiche aux poireaux", "maison", "Soupe et tartines", "maison"), ("Gigot et gratin dauphinois", "invites", "Dahl de lentilles corail", "kitchen_daily")]
    jours = [{{"date": (d0 + datetime.timedelta(days=i)).isoformat(), "midi": {{"titre": m[0], "source": m[1]}}, "soir": {{"titre": m[2], "source": m[3], "rituel": m[2] == "Pâtes maison"}}}} for i, m in enumerate(menus)]
    plats = [{{"titre": "Parmentier de patates douces", "quantite": 3, "ingredients": ["Patate douce", "Oignons confits", "Cheddar"], "badges": ["Enfants ok", "Favori"]}},
             {{"titre": "Pavé de saumon, riz basmati", "quantite": 2, "ingredients": ["Saumon", "Riz basmati", "Citron confit"]}},
             {{"titre": "Dahl de lentilles corail", "quantite": 3, "ingredients": ["Lentilles corail", "Lait de coco", "Curcuma"], "badges": ["Végé"]}},
             {{"titre": "Tagliatelles carbonara", "quantite": 2, "ingredients": ["Tagliatelles", "Crème", "Parmesan"], "badges": ["Enfants ok"]}}]
    outil("t1", "repas_semaine", [{{"type": "repas_semaine", "id": "c-repas", "titre": "Repas de la semaine", "donnees": {{"jours": jours}}, "actions": []}}])
    outil("t2", "kitchen_daily_menu", [{{"type": "kitchen_daily_proposition", "id": "c-kd", "titre": "Kitchen Daily · prochaine livraison", "donnees": {{"plats": plats, "limite": "À valider avant vendredi", "total": "10 plats"}}, "actions": [{{"libelle": "Valider", "action": "confirmer", "args": {{"decision": "valider"}}, "confirmation": actions["kd"]}}, {{"libelle": "Modifier", "action": "message", "args": {{"texte": "Je voudrais modifier la sélection."}}}}, {{"libelle": "Annuler", "action": "confirmer", "args": {{"decision": "annuler"}}, "confirmation": actions["kd"]}}]}}])
    dire("Ce soir, dahl de lentilles corail. Voici aussi la sélection de la prochaine livraison.")
    fin("Ce soir, dahl de lentilles corail. Voici aussi la sélection de la prochaine livraison.")
elif "identifiant" in bas or "connexion au site" in bas:
    outil("t1", "identifiants_preparer", [{{"type": "identifiants", "id": "c-id", "titre": "Site de la cantine", "donnees": {{"cle": "libre/cantine", "type": "identifiants", "champs": ["identifiant", "mot_de_passe", "url"], "facultatifs": ["url"], "libelle": "Site de la cantine"}}, "actions": [{{"libelle": "Enregistrer", "action": "coffre_ecrire", "args": {{"cle": "libre/cantine"}}, "confirmation": "step-up"}}]}}])
    dire("Saisis les identifiants dans la carte sécurisée : je ne les verrai pas.")
    fin("Saisis les identifiants dans la carte sécurisée : je ne les verrai pas.")
elif "document" in bas or "papier" in bas:
    outil("t1", "fichiers_lister", [{{"type": "fichier_classe", "id": "c-f1", "titre": "Rangé", "donnees": {{"nom": "attestation.pdf", "categorie": "Administratif", "description": "Attestation d'assurance habitation"}}, "actions": []}},
                                    {{"type": "document_sensible", "id": "c-s1", "donnees": {{"nom": "Carnet de santé", "acces": "parents"}}, "actions": [{{"libelle": "Déverrouiller", "action": "deverrouiller", "args": {{"fichier": "carnet"}}}}]}}])
    dire("Voici les documents rangés récemment.")
    fin("Voici les documents rangés récemment.")
elif "compar" in bas or "assurance" in bas:
    outil("t1", "comparer", [{{"type": "comparatif", "id": "c-cmp", "titre": "Assurance habitation", "donnees": {{"offres": [{{"nom": "Offre actuelle", "prix": "412 €/an", "detail": "Franchise 150 €"}}, {{"nom": "Offre B", "prix": "338 €/an", "detail": "Mêmes garanties", "recommande": True}}, {{"nom": "Offre C", "prix": "355 €/an", "detail": "Franchise 300 €"}}], "gain": "74 € d'économie par an à garanties égales."}}, "actions": []}}])
    dire("J'ai comparé trois offres.")
    fin("J'ai comparé trois offres.")
else:
    dire("Bien noté : **" + dernier[:60] + "**.")
    fin("Bien noté : **" + dernier[:60] + "**.\n- Je garde ça en tête\n- Je te le rappelle au brief de demain")
'''


def cartes_demo(cfg: Any, personne: Any, jour: date, *, aujourdhui: date | None = None, meteo: Any = None) -> list[dict[str, Any]]:
    """Cartes du jour de démonstration (données neutres, aucun appel réseau)."""
    from coeur.brief import date_longue
    from coeur.cartes import carte, valides

    aujourdhui = aujourdhui or datetime.now().astimezone().date()
    decalage = (jour - aujourdhui).days
    j = jour.isoformat()
    evenements = [
        {"titre": "Réunion d'équipe", "debut": j + "T09:00", "agenda": personne.linux_user},
        {"titre": "Rendez-vous perso", "debut": j + "T14:00", "agenda": personne.linux_user, "prive": True},
        {"titre": "Sortie d'école", "debut": j + "T16:30", "agenda": "famille"},
    ] if decalage % 2 == 0 else [{"titre": "Marché", "debut": j + "T10:00", "agenda": "famille"}]
    sortie = [carte("bonjour", {"date": j, "date_longue": date_longue(jour), "decalage_jours": decalage, "prenom": personne.prenom,
                                "meteo": {"commune": "Villeneuve", "min": 9, "max": 17, "libelle": "Ciel dégagé", "icone": "soleil"},
                                "evenements": evenements, "evenements_total": len(evenements)},
                    titre=f"Bonjour {personne.prenom}" if decalage == 0 else date_longue(jour).capitalize())]
    if decalage == 0:
        texte = ("Journée calme : réunion à 9 h, ton rendez-vous à 14 h, puis la sortie d'école à 16 h 30.\n"
                 "- Demain : marché à 10 h\n- La sélection de repas attend votre validation avant vendredi")
        sortie.append(carte("brief", {"texte": texte, "date": j}, titre="Brief du jour",
                            actions=[{"libelle": "Écouter", "action": "ecouter", "args": {"texte": texte}}]))
    elements = [
        {"type": "echeance", "titre": "Vermifuge du chien", "date": (jour + timedelta(days=1)).isoformat(), "dans_jours": 1,
         "notes": "À acheter, il n'en reste plus"},
        {"type": "anniversaire", "titre": "Anniversaire de Nina", "date": (jour + timedelta(days=4)).isoformat(), "dans_jours": 4, "age": 9},
        {"type": "echeance", "titre": "Contrôle technique", "date": (jour + timedelta(days=12)).isoformat(), "dans_jours": 12,
         "notes": "Rendez-vous à prendre"},
    ]
    sortie.append(carte("a_noter", {"aujourdhui": 0, "semaine": 2, "elements": elements}, titre="À noter"))
    return valides(sortie)


# --- données des écrans directs (web/ecrans.py) : foyer de démonstration neutre, semé avec les fonctions des outils -


def semer_donnees_demo(donnees: Path) -> None:
    """Courses, échéances, journal, fichiers rangés et mémoire du foyer Alex et Camille (aucune donnée réelle), écrits
    par les mêmes fonctions que les outils : les écrans de l'appli les lisent par les vraies routes."""
    from tools import courses, echeances, memoire

    aujourdhui = datetime.now().astimezone().date()

    def dans(n: int) -> str:
        return (aujourdhui + timedelta(days=n)).isoformat()

    def par(qui: str, fn: Any, *args: Any) -> Any:
        os.environ["HOMEOS_WHO"] = qui
        try:
            return fn(*args)
        finally:
            os.environ.pop("HOMEOS_WHO", None)

    lignes = [
        ("Clémentines", "fruits-légumes", "Camille"), ("Poireaux", "fruits-légumes", "Alex"), ("Pommes", "fruits-légumes", "Camille"),
        ("Avocats", "fruits-légumes", "Firmin"), ("Lait demi-écrémé ×2", "laitier", "Camille"), ("Yaourts nature", "laitier", "Alex"),
        ("Comté", "laitier", "Camille"), ("Beurre doux", "laitier", "Firmin"), ("Pain de mie complet", "boulangerie", "Alex"),
        ("Coquillettes", "épicerie", "Camille"), ("Huile d'olive", "épicerie", "Alex"), ("Céréales des enfants", "épicerie", "Camille"),
        ("Café en grains", "épicerie", "Alex"), ("Lessive", "maison", "Camille"), ("Éponges", "maison", "Alex"),
        ("Croquettes 12 kg", "maison", "Alex"),
    ]
    for article, rayon, qui in lignes:
        par(qui, courses.ajouter, [article], rayon)
    for a in courses.lister():
        if a["article"] in ("Clémentines", "Yaourts nature", "Coquillettes", "Éponges"):
            courses.marquer(a["id"], True)

    for titre, n, notes, qui in [
        ("Vermifuge et anti-puces du chien", 6, "À acheter, il n'en reste plus", "Camille"),
        ("Contrôle technique de la voiture", 12, "Rendez-vous à prendre · Alex s'en occupe", "Alex"),
        ("Inscription cantine janvier–mars", 17, "Dossier à déposer sur le portail de la mairie", "Camille"),
        ("Échéance de l'assurance habitation", 21, "Comparer les offres avant le renouvellement", "Alex"),
        ("Renouvellement de la mutuelle", 51, "Contrat famille, résiliable à échéance", "Alex"),
    ]:
        par(qui, echeances.ajouter, titre, dans(n), [30, 7, 1], None, notes)

    mois = f"{aujourdhui:%Y-%m}"
    entrees = [
        (min(aujourdhui.day, 1), "19:30", "Alex", "Balade au lac : pique-nique au soleil, le chien a fini dans l'eau."),
        (max(1, aujourdhui.day - 4), "20:10", "Camille", "Sam a perdu une dent au goûter."),
        (max(1, aujourdhui.day - 1), "17:45", "Alex", "Grand ramassage des feuilles avec les enfants. Trois sacs, et le chien qui saute dans le tas."),
        (aujourdhui.day, "08:05", "Camille", "Livraison de repas notée 4 étoiles : le dahl a fait l'unanimité, le saumon un peu sec."),
    ]
    contenu = f"# Journal {mois}\n\n" + "".join(f"- **{mois}-{j:02d} {h}** ({qui}) : {t}\n" for j, h, qui, t in sorted(entrees))
    memoire.ecrire(f"foyer/journal/{mois}.md", contenu)

    racine = donnees / "fichiers"
    for cat, docs in {
        "voiture": [("certificat-immatriculation.pdf", "Carte grise de la voiture familiale"),
                    ("controle-technique-2024.pdf", "Rapport favorable, aucune contre-visite"),
                    ("assurance-auto.pdf", "Attestation d'assurance, valable jusqu'au 31 décembre")],
        "administratif": [("quotient-familial.pdf", "Attestation de quotient familial pour la cantine"),
                          ("certificats-scolarite.pdf", "Certificats de scolarité de l'année")],
        "factures": [("electricite-octobre.pdf", "Électricité, octobre : mensualité 84,12 €"),
                     ("box-internet-novembre.pdf", "Box internet, novembre : 34,99 €")],
    }.items():
        dossier = racine / cat / f"{aujourdhui:%Y}"
        dossier.mkdir(parents=True, exist_ok=True)
        index = racine / cat / "index.md"
        texte = f"# Fichiers — {cat}\n\n_Une ligne par fichier rangé : date · nom · description._\n\n"
        for k, (nom, desc) in enumerate(docs):
            (dossier / nom).write_bytes(b"%PDF-1.4\n% document de demonstration\n")
            texte += f"- {dans(-3 * (len(docs) - k))} · {nom} · {desc}\n"
        index.write_text(texte, encoding="utf-8")

    for chemin, contenu in {
        "foyer/qui-on-est.md": "# Qui on est\n\n- Alex et Camille, deux enfants : Nina (9 ans) et Sam (6 ans)\n- Un chien\n- Maison à Villeneuve\n",
        "foyer/regles.md": "# Règles\n\n- Pas d'écran le soir en semaine\n- Pâtes le mardi\n",
        "foyer/poubelles.md": "# Poubelles\n\n- Jaune le mardi soir\n- Verre le premier lundi du mois\n",
        "animaux/chien.md": "# Le chien\n\n- Toilettage toutes les 8 semaines, le samedi matin\n- Vermifuge tous les 3 mois\n",
        "alimentation/foyer.md": "# Repas du foyer\n\n- Végétarien deux soirs par semaine\n- Pas de poivron\n",
        "personnes/alex.md": "# Alex\n\n- Vélo le mardi et le jeudi\n- Brief à 7 h 15, en puces courtes\n",
        "personnes/camille.md": "# Camille\n\n- Yoga le jeudi soir\n",
    }.items():
        memoire.ecrire(chemin, contenu)


def sans_points_acces_ecrans(appli: Any) -> Any:
    """Démo « backend d'avant » : les routes des écrans directs répondent 404 (le front se replie sur les cartes du
    fil) ; le reste de l'appli (dont l'envoi de fichiers, même chemin en POST) est inchangé."""
    import json as json_mod

    from web import ecrans

    routes = [(r.path_regex, set(r.methods or ())) for r in ecrans.routeur.routes]

    async def application(scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and any(rx.match(scope.get("path", "")) and scope.get("method") in m for rx, m in routes):
            corps = json_mod.dumps({"detail": "Not Found"}).encode()
            await send({"type": "http.response.start", "status": 404, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": corps})
            return
        await appli(scope, receive, send)

    return application


def preparer(dossier: Path, port: int) -> dict[str, Any]:
    dossier.mkdir(parents=True, exist_ok=True)
    os.environ.pop("HOMEOS_ETAT", None)
    os.environ.pop("ANTHROPIC_API_KEY", None)
    os.environ["HOMEOS_NO_SUDO"] = "1"
    os.environ["HOMEOS_SECRETS"] = str(dossier / "secrets")
    os.environ["HOMEOS_FICHIER_WEB"] = str(dossier / "absent")
    os.environ["COFFRE_CLE"] = "c" * 64
    donnees = dossier / "data"
    donnees.mkdir(exist_ok=True)
    os.environ["HOMEOS_DATA"] = str(donnees)
    racine = dossier / "repo"
    (racine / "content").mkdir(parents=True, exist_ok=True)
    (racine / "tools").mkdir(exist_ok=True)
    (racine / "capabilities").mkdir(exist_ok=True)
    os.environ["HOMEOS_ROOT"] = str(racine)
    (donnees / "config.yaml").write_text(json.dumps({"assistant": {"nom": "Firmin"}, "personnes": PERSONNES}), encoding="utf-8")
    return {"donnees": donnees, "racine": racine}


def lancer(dossier: Path, port: int) -> dict[str, Any]:
    chemins = preparer(dossier, port)
    import coeur.brief
    from bot.config import empreinte_fichier, parse_config
    from bot.db import STATUS_PENDING, Db
    from bot.sessions import Sessions
    from coeur.claude import ClaudeRunner
    from coeur.historique import Historique
    from web import config as configweb
    from web import contexte as contexte_web
    from web.contexte import ContexteWeb
    from web.stockage import StockageAuth

    coeur.brief.cartes_du_jour = cartes_demo  # GET /api/brief/jour
    contexte_web.cartes_du_jour = cartes_demo  # premier message du jour

    cfg = parse_config({"assistant": {"nom": "Firmin"}, "personnes": PERSONNES}, chemins["donnees"], chemins["racine"])
    cfg.empreinte = empreinte_fichier(cfg.config_path)
    db = Db(cfg.db_path)
    db.init()
    actions = {
        "agenda": db.log("Alex", "agenda_ajouter", "Ajouter « Dentiste »", {"titre": "Dentiste"}, STATUS_PENDING),
        "kd": db.log("Alex", "kitchen_daily_proposer", "Valider la sélection", {}, STATUS_PENDING),
    }
    fichier_actions = dossier / "actions.json"
    fichier_actions.write_text(json.dumps(actions), encoding="utf-8")
    binaires = dossier / "bin"
    binaires.mkdir(exist_ok=True)
    faux = binaires / "claude"
    faux.write_text(FAUX_CLAUDE.format(python=sys.executable, actions=str(fichier_actions), jour=datetime.now().astimezone().date().isoformat()),
                    encoding="utf-8")
    faux.chmod(0o755)
    os.environ["PATH"] = f"{binaires}:/usr/bin:/bin"

    try:  # entrée du coffre préparée (sans valeur) pour la carte identifiants
        from tools import coffre

        coffre.preparer_entree("libre/cantine", {"type": "identifiants", "libelle": "Site de la cantine", "proprietaire": "alex"})
    except Exception as exc:  # noqa: BLE001 - la démonstration reste utilisable sans coffre
        print(f"coffre indisponible : {exc}", file=sys.stderr)

    try:
        semer_donnees_demo(chemins["donnees"])
    except Exception as exc:  # noqa: BLE001 - la démonstration reste utilisable sans données d'écran
        print(f"données des écrans non semées : {exc!r}", file=sys.stderr)

    async def executer(cfg: Any, action_id: int) -> tuple[bool, str]:
        return True, "C'est fait : ajouté à l'agenda."

    async def classer(texte: str) -> str:
        return "standard"

    origine = f"http://localhost:{port}"
    appli = cfg.etat_dir / "appli.sqlite"
    ctx = ContexteWeb(cfg=cfg, reglages=configweb.ReglagesWeb(origine=origine, port=port), db=db,
                      sessions=Sessions(cfg.sessions_path), runner=ClaudeRunner(cfg), historique=Historique(appli),
                      auth=StockageAuth(appli), executer=executer, envoyer_telegram=lambda chat_id, texte: True,
                      classifieur=classer)
    return {"ctx": ctx, "origine": origine}


def main(argv: list[str] | None = None) -> int:
    analyseur = argparse.ArgumentParser(description=__doc__)
    analyseur.add_argument("--port", type=int, default=8123)
    analyseur.add_argument("--dossier", type=Path, required=True)
    analyseur.add_argument("--personne", default="alex")
    args = analyseur.parse_args(argv)
    import uvicorn

    from web.app import creer_app

    infos = lancer(args.dossier, args.port)
    ctx = infos["ctx"]
    appli = creer_app(ctx)
    if os.environ.get("HOMEOS_DEMO_SANS_POINTS_ACCES"):  # repli du front sur les cartes du fil
        appli = sans_points_acces_ecrans(appli)
    jeton = ctx.auth.creer_invitation(args.personne, timedelta(minutes=30))
    serveur = uvicorn.Server(uvicorn.Config(appli, host="127.0.0.1", port=args.port, log_level="warning",
                                            access_log=False))
    fil = threading.Thread(target=serveur.run, daemon=True)
    fil.start()
    while not serveur.started:
        time.sleep(0.05)
    print(json.dumps({"origine": infos["origine"], "invitation": f"{infos['origine']}/inscription#{jeton}", "prete": True}),
          flush=True)
    try:
        while fil.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        serveur.should_exit = True
    return 0


if __name__ == "__main__":
    sys.exit(main())
