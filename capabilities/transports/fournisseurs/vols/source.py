"""Fournisseur « vols » (source manuelle) : aucun appel réseau. `etat_vol` dit honnêtement ce qui est connu (le
numéro, la compagnie quand le préfixe est courant) et où vérifier ; il ne prétend jamais connaître un retard."""

from __future__ import annotations

import re
from typing import Any

from capabilities.transports import interface

COMPAGNIES = interface.COMPAGNIES
RE_VOL = re.compile(r"^\s*([A-Z0-9]{2})\s?(\d{1,4})[A-Z]?\s*$", re.IGNORECASE)


class Source(interface.Source):
    def etat_vol(self, numero: str, date: str) -> dict[str, Any]:
        m = RE_VOL.match(str(numero))
        if not m:
            raise interface.ErreurTransports(f"« {numero} » ne ressemble pas à un numéro de vol (exemple : AF7664).")
        code, chiffres = m.group(1).upper(), m.group(2)
        propre = f"{code}{chiffres}"
        jour = interface.lire_date(date)
        compagnie = COMPAGNIES.get(code)
        ou = f"sur le site ou l'appli de {compagnie}" if compagnie else "sur le site ou l'appli de la compagnie"
        return interface.etat(
            propre, "avion", "inconnu", date=jour.isoformat(), source="manuel",
            message=f"Je n'ai pas de source fiable pour l'état des vols : retard et porte se vérifient {ou}.",
        )

    def verifier(self) -> dict[str, Any]:
        return {"ok": True, "message": "Vols : source manuelle (aucun appel réseau)"}
