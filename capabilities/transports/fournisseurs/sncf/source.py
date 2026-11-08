"""Fournisseur « sncf » : moteur GTFS / GTFS-RT générique (`capabilities.transports.gtfs.SourceGtfs`), avec les
particularités des données SNCF :

- les identifiants de course du temps réel diffèrent de ceux de la grille (date de fin de validité dans l'un, date de
  circulation dans l'autre) : les courses sont appariées par **numéro de train** et jour de circulation. Le numéro est
  `trip_headsign` dans la grille et se lit dans l'identifiant temps réel (« OCESN6105F… » → 6105) ;
- le temps réel ne couvre que les trains qui partent dans l'heure : avant, l'état d'un train est théorique (avec les
  suppressions et perturbations déjà annoncées dans le flux d'alertes) ;
- la voie n'est pas publiée dans ces flux ouverts.
"""

from __future__ import annotations

import re
from typing import Any

from capabilities.transports.gtfs import SourceGtfs

RE_NUMERO = re.compile(r"OCESN0*(\d+)F")


class Source(SourceGtfs):
    def numero_rt(self, trip_id: str) -> str:
        m = RE_NUMERO.search(trip_id or "")
        return m.group(1) if m else ""

    def cle_course(self, trip_id: str, numero: str, jour: str) -> str:
        numero = str(numero or "").lstrip("0")
        return f"n{numero}|{jour}" if numero else f"{trip_id}|{jour}"

    def etat_train(self, numero: str, date: str) -> dict[str, Any]:
        numero = re.sub(r"\D", "", str(numero))
        return self.etat_course(numero, date, "train")
