#!/usr/bin/env python3
"""Faux sous-agent Claude Code pour les tests de l'atelier (aucun appel à un modèle).

- `HOMEOS_ATELIER_MODE=generer` : copie le modèle (`FAUX_CLAUDE_MODELE`, défaut : salon_demo ; `garder` : laisse le
  gabarit déjà amorcé par l'atelier, cas d'un fournisseur de transports) dans le dossier courant ; `FAUX_CLAUDE_DEFAUT` y injecte un défaut (secret_en_dur, sans_confirmation, paiement, import_reseau) ;
  au tour de correction (CORRECTIONS.md présent), `FAUX_CLAUDE_CORRIGE=1` recopie le modèle propre.
- `HOMEOS_ATELIER_MODE=relire` : verdict JSON (`FAUX_CLAUDE_VERDICT=ok|refus`).
- `FAUX_CLAUDE_JOURNAL` : fichier où chaque appel ajoute une ligne JSON (mode, arguments, cwd).
Sortie : comme `claude -p --output-format json`.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

ICI = Path(__file__).resolve().parent


def copier_modele(cwd: Path) -> None:
    if os.environ.get("FAUX_CLAUDE_MODELE") == "garder":  # fournisseur de transports : le gabarit amorcé tel quel
        return
    modele = Path(os.environ.get("FAUX_CLAUDE_MODELE") or ICI / "salon_demo")
    for f in modele.rglob("*"):
        if f.is_file() and "__pycache__" not in f.parts:
            cible = cwd / f.relative_to(modele)
            cible.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, cible)


def injecter(cwd: Path, defaut: str) -> None:
    manifeste = cwd / "capability.yaml"
    connecteur = cwd / "connecteur.py"
    if defaut == "secret_en_dur":
        connecteur.write_text(connecteur.read_text(encoding="utf-8") + '\nMOT_DE_PASSE = "hunter2-vrai"\n', encoding="utf-8")
    elif defaut == "sans_confirmation":
        texte = manifeste.read_text(encoding="utf-8").replace(
            "confirmation_toujours: [salon_demo_reserver]", "confirmation_toujours: []")
        manifeste.write_text(texte, encoding="utf-8")
    elif defaut == "paiement":
        texte = manifeste.read_text(encoding="utf-8")
        texte = texte.replace("actions_avec_effet: [salon_demo_reserver]",
                              "actions_avec_effet: [salon_demo_reserver, salon_demo_payer]")
        manifeste.write_text(texte, encoding="utf-8")
    elif defaut == "import_reseau":
        connecteur.write_text("import urllib.request\n" + connecteur.read_text(encoding="utf-8"), encoding="utf-8")


def main() -> int:
    cwd = Path.cwd()
    mode = os.environ.get("HOMEOS_ATELIER_MODE", "generer")
    journal = os.environ.get("FAUX_CLAUDE_JOURNAL")
    if journal:
        with open(journal, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"mode": mode, "args": sys.argv[1:], "cwd": str(cwd)}) + "\n")
    if mode == "relire":
        verdict = os.environ.get("FAUX_CLAUDE_VERDICT", "ok")
        corps = {"verdict": verdict, "problemes": [] if verdict == "ok" else ["connecteur.py : effet caché"],
                 "resume": "Relecture factice."}
        resultat = "Voici mon verdict :\n```json\n" + json.dumps(corps, ensure_ascii=False) + "\n```"
    else:
        corrige = (cwd / "CORRECTIONS.md").exists() and os.environ.get("FAUX_CLAUDE_CORRIGE") == "1"
        copier_modele(cwd)
        defaut = os.environ.get("FAUX_CLAUDE_DEFAUT", "")
        if defaut and not corrige:
            injecter(cwd, defaut)
        resultat = "Capacité fabriquée (faux sous-agent)."
    print(json.dumps({"type": "result", "result": resultat, "is_error": False, "total_cost_usd": 0.0}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
