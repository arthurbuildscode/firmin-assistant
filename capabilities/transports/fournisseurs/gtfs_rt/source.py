"""Modèle « gtfs_rt » : un réseau quelconque qui publie GTFS (+ GTFS-RT). Paramètres de l'instance : `url_grille`,
`url_temps_reel` (facultatif), `url_alertes` (facultatif). Appariement des courses par identifiant (`trip_id`), comme
le prévoit la spécification GTFS-RT."""

from __future__ import annotations

from capabilities.transports.gtfs import SourceGtfs


class Source(SourceGtfs):
    pass
