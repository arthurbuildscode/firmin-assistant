"""Fournisseur « {{titre}} » (fabriqué par l'atelier) : prochains départs du réseau, lus en données publiques.

Interface : `capabilities/transports/interface.py` (classe `Source`). Tout accès réseau passe par `self.client`
(hôtes approuvés seulement, https ; pour une page web, robots.txt respecté et navigation via `self.client.navigation()`).
L'analyse des réponses est séparée (fonctions pures `analyser_*`) : c'est elle que les tests couvrent.
"""

from __future__ import annotations

from typing import Any

from capabilities.transports import interface


def analyser_departs(donnees: Any, ligne: str = "") -> list[dict[str, Any]]:
    """Réponse du réseau → départs normalisés (`interface.depart`). À adapter au format réel du réseau."""
    departs = []
    for d in (donnees or {}).get("departs") or []:
        if ligne and str(d.get("ligne") or "") != str(ligne):
            continue
        departs.append(interface.depart(
            d["prevu"], heure_estimee=d.get("estime"),
            statut="supprime" if d.get("supprime") else None,
            ligne=d.get("ligne"), destination=d.get("destination"),
        ))
    return departs


class Source(interface.Source):
    def base(self) -> str:
        return str(self.parametres.get("site") or "").rstrip("/")

    def resoudre_trajet(self, trajet: dict[str, Any]) -> dict[str, Any]:
        """Vérifie l'arrêt de départ auprès du réseau ; lève interface.ErreurTransports (avec les candidats) si
        l'arrêt est inconnu ou ambigu."""
        if not str(trajet.get("origine") or "").strip():
            raise interface.ErreurTransports("À quel arrêt tu montes ?")
        return trajet

    def prochains_departs(self, trajet: dict[str, Any], horizon: int = 60) -> dict[str, Any]:
        donnees = self.client.json(self.base() + "/api/departs", {"arret": trajet.get("origine", "")}, cache_s=30)
        departs = analyser_departs(donnees, str(trajet.get("ligne") or ""))
        temps_reel = any(d["source"] == "temps_reel" for d in departs)
        return interface.departs(
            [d for d in departs if 0 <= (d["dans_min"] or 0) <= horizon or d["statut"] == "supprime"],
            temps_reel=temps_reel,
            avertissement=None if temps_reel else "Pas de temps réel pour l'instant : horaires théoriques.",
        )

    def verifier(self) -> dict[str, Any]:
        """Smoke test : le réseau répond (une seule requête légère)."""
        try:
            self.client.octets(self.base() + "/", max_octets=2_000_000)
        except interface.ErreurTransports as exc:
            return {"ok": False, "message": f"{self.titre} : {exc}"}
        return {"ok": True, "message": f"{self.titre} : le site répond"}
