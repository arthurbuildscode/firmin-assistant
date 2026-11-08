"""Faux site de prise de rendez-vous (lot 7b, démonstration de l'atelier) — aucun vrai service n'est touché.

Parcours simulé, proche d'un site de réservation réel :

- `/` accueil (lien de connexion, conditions générales) ; `/cgu` ; `/robots.txt` (interdit `/admin`) ;
- `/connexion` : formulaire (identifiant, mot de passe) → cookie de session → `/creneaux` ;
- `/creneaux` : page « vide » hydratée en JavaScript depuis `/api/creneaux` (JSON) ;
- `/reserver?id=…` : récapitulatif + bouton « Confirmer la réservation » (POST) → « Rendez-vous confirmé » ;
- `/mes-rendez-vous` ; `/bloque` : page de vérification humaine (test « pas de contournement anti-bot »).

Usage : `demarrer()` dans les tests (port libre, fil d'exécution) ; à la main :
`python tests/fixtures/faux_rdv/serveur.py --port 8765` (identifiant demo@example.org, mot de passe dans IDENTIFIANTS).
"""

from __future__ import annotations

import argparse
import html
import json
import secrets
import threading
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

IDENTIFIANTS = {"identifiant": "demo@example.org", "mot_de_passe": "faux-mdp-demo-2026"}

CRENEAUX_INITIAUX = [
    {"id": "c1", "quand": "2026-11-10T10:00", "libelle": "mardi 10 novembre à 10h00", "prestation": "Coupe"},
    {"id": "c2", "quand": "2026-11-10T14:30", "libelle": "mardi 10 novembre à 14h30", "prestation": "Coupe"},
    {"id": "c3", "quand": "2026-11-12T09:00", "libelle": "jeudi 12 novembre à 9h00", "prestation": "Couleur"},
]


@dataclass
class EtatSite:
    sessions: set[str] = field(default_factory=set)
    creneaux: list[dict[str, Any]] = field(default_factory=lambda: [dict(c) for c in CRENEAUX_INITIAUX])
    reservations: list[dict[str, Any]] = field(default_factory=list)
    connexions_refusees: int = 0
    visites: list[str] = field(default_factory=list)


GABARIT = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>{titre} — Salon Démo</title></head>
<body><header><a href="/">Salon Démo</a> <nav><a href="/creneaux">Créneaux</a> <a href="/mes-rendez-vous">Mes rendez-vous</a></nav></header>
<main>{corps}</main><footer><a href="/cgu">Conditions générales d'utilisation</a></footer></body></html>"""

PAGE_CRENEAUX = """<h1>Créneaux disponibles</h1><ul id="liste" aria-busy="true"><li>Chargement…</li></ul>
<script>
fetch('/api/creneaux').then(r => r.json()).then(d => {
  const ul = document.getElementById('liste');
  ul.innerHTML = '';
  for (const c of d.creneaux) {
    const li = document.createElement('li');
    li.className = 'creneau'; li.dataset.id = c.id;
    li.innerHTML = '<span class="quand"></span> — <span class="prestation"></span> <a class="reserver" role="button">Réserver</a>';
    li.querySelector('.quand').textContent = c.libelle;
    li.querySelector('.prestation').textContent = c.prestation;
    li.querySelector('.reserver').href = '/reserver?id=' + encodeURIComponent(c.id);
    ul.appendChild(li);
  }
  ul.setAttribute('aria-busy', 'false');
});
</script>"""


def creer_gestionnaire(etat: EtatSite) -> type[BaseHTTPRequestHandler]:
    class Gestionnaire(BaseHTTPRequestHandler):
        server_version = "SalonDemo/1.0"

        def log_message(self, *_args: Any) -> None:  # silencieux pendant les tests
            return

        # -- utilitaires
        def _session(self) -> str | None:
            cookie = SimpleCookie(self.headers.get("Cookie") or "")
            valeur = cookie.get("session")
            return valeur.value if valeur and valeur.value in etat.sessions else None

        def _envoyer(self, statut: int, corps: str, type_: str = "text/html; charset=utf-8",
                     entetes: dict[str, str] | None = None) -> None:
            donnees = corps.encode("utf-8")
            self.send_response(statut)
            self.send_header("Content-Type", type_)
            self.send_header("Content-Length", str(len(donnees)))
            for cle, val in (entetes or {}).items():
                self.send_header(cle, val)
            self.end_headers()
            self.wfile.write(donnees)

        def _page(self, titre: str, corps: str, statut: int = 200, entetes: dict[str, str] | None = None) -> None:
            self._envoyer(statut, GABARIT.format(titre=html.escape(titre), corps=corps), entetes=entetes)

        def _rediriger(self, cible: str, entetes: dict[str, str] | None = None) -> None:
            self.send_response(303)
            self.send_header("Location", cible)
            for cle, val in (entetes or {}).items():
                self.send_header(cle, val)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _formulaire(self) -> dict[str, str]:
            longueur = int(self.headers.get("Content-Length") or 0)
            brut = self.rfile.read(longueur).decode("utf-8") if longueur else ""
            return {k: v[0] for k, v in parse_qs(brut).items()}

        # -- GET
        def do_GET(self) -> None:
            url = urlsplit(self.path)
            etat.visites.append(url.path)
            if url.path == "/robots.txt":
                self._envoyer(200, "User-agent: *\nDisallow: /admin\n", "text/plain; charset=utf-8")
            elif url.path == "/":
                self._page("Accueil", "<h1>Salon Démo</h1><p>Prenez rendez-vous en ligne.</p>"
                                      "<a href='/connexion'>Se connecter</a>")
            elif url.path == "/cgu":
                self._page("Conditions", "<h1>Conditions générales</h1><p>Vous pouvez gérer vos propres rendez-vous "
                                         "avec l'outil de votre choix, y compris un assistant personnel.</p>")
            elif url.path == "/connexion":
                self._page("Connexion", "<h1>Connexion</h1><form method='post' action='/connexion'>"
                           "<label>Adresse e-mail <input name='identifiant' type='email' autocomplete='username'></label>"
                           "<label>Mot de passe <input name='mot_de_passe' type='password'></label>"
                           "<button type='submit'>Se connecter</button></form>")
            elif url.path == "/creneaux":
                if not self._session():
                    self._rediriger("/connexion")
                    return
                self._page("Créneaux", PAGE_CRENEAUX)
            elif url.path == "/api/creneaux":
                if not self._session():
                    self._envoyer(401, json.dumps({"erreur": "non connecté"}), "application/json")
                    return
                self._envoyer(200, json.dumps({"creneaux": etat.creneaux}, ensure_ascii=False), "application/json")
            elif url.path == "/reserver":
                if not self._session():
                    self._rediriger("/connexion")
                    return
                cid = (parse_qs(url.query).get("id") or [""])[0]
                creneau = next((c for c in etat.creneaux if c["id"] == cid), None)
                if creneau is None:
                    self._page("Indisponible", "<h1>Ce créneau n'est plus disponible</h1>", 404)
                    return
                self._page("Réserver", f"<h1>Récapitulatif</h1><p class='recap'>{html.escape(creneau['prestation'])} — "
                           f"{html.escape(creneau['libelle'])}</p><form method='post' action='/reserver'>"
                           f"<input type='hidden' name='id' value='{html.escape(cid)}'>"
                           "<button type='submit'>Confirmer la réservation</button></form>")
            elif url.path == "/mes-rendez-vous":
                if not self._session():
                    self._rediriger("/connexion")
                    return
                lignes = "".join(f"<li class='rdv'>{html.escape(r['libelle'])} — {html.escape(r['reference'])}</li>"
                                 for r in etat.reservations)
                self._page("Mes rendez-vous", f"<h1>Mes rendez-vous</h1><ul>{lignes or '<li>Aucun</li>'}</ul>")
            elif url.path == "/bloque":
                self._page("Just a moment...", "<h1>Vérification</h1><p>Vérifiez que vous êtes humain.</p>"
                                               "<iframe src='https://challenges.cloudflare.com/x'></iframe>")
            elif url.path == "/admin":
                self._page("Admin", "<h1>Administration</h1>")
            else:
                self._page("Introuvable", "<h1>Page introuvable</h1>", 404)

        # -- POST
        def do_POST(self) -> None:
            url = urlsplit(self.path)
            etat.visites.append("POST " + url.path)
            champs = self._formulaire()
            if url.path == "/connexion":
                if (champs.get("identifiant") == IDENTIFIANTS["identifiant"]
                        and champs.get("mot_de_passe") == IDENTIFIANTS["mot_de_passe"]):
                    jeton = secrets.token_hex(12)
                    etat.sessions.add(jeton)
                    self._rediriger("/creneaux", {"Set-Cookie": f"session={jeton}; Path=/; HttpOnly; SameSite=Lax"})
                else:
                    etat.connexions_refusees += 1
                    self._page("Connexion", "<h1>Connexion</h1><p role='alert'>Identifiants incorrects</p>", 401)
            elif url.path == "/reserver":
                if not self._session():
                    self._rediriger("/connexion")
                    return
                creneau = next((c for c in etat.creneaux if c["id"] == champs.get("id")), None)
                if creneau is None:
                    self._page("Indisponible", "<h1>Ce créneau n'est plus disponible</h1>", 409)
                    return
                etat.creneaux.remove(creneau)
                reference = f"RDV-{len(etat.reservations) + 1:04d}"
                etat.reservations.append({**creneau, "reference": reference})
                self._page("Confirmé", f"<h1>Rendez-vous confirmé</h1><p>{html.escape(creneau['libelle'])}</p>"
                           f"<p>Référence : <strong class='reference'>{reference}</strong></p>")
            else:
                self._page("Introuvable", "<h1>Page introuvable</h1>", 404)

    return Gestionnaire


def demarrer(port: int = 0) -> tuple[ThreadingHTTPServer, str, EtatSite]:
    """Lance le faux site sur 127.0.0.1 dans un fil ; renvoie (serveur, url de base, état)."""
    etat = EtatSite()
    serveur = ThreadingHTTPServer(("127.0.0.1", port), creer_gestionnaire(etat))
    threading.Thread(target=serveur.serve_forever, daemon=True, name="faux-rdv").start()
    return serveur, f"http://127.0.0.1:{serveur.server_address[1]}", etat


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Faux site de rendez-vous (démonstration de l'atelier)")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    serveur, url, _etat = demarrer(args.port)
    print(f"Faux site sur {url} (Ctrl-C pour arrêter)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        serveur.shutdown()
