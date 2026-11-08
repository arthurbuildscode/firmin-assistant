"""Lot 6 : le dépôt est un produit, pas l'instance d'un foyer.

1. Aucun prompt ne garde de variable non rendue : `content/CLAUDE.md`, `content/prompts/*.md`,
   `content/onboarding/*.md` et les fragments des capacités sont rendus avec une configuration fictive
   (`tools.common.rendre_gabarit`) ; le prénom de l'assistant et ceux du foyer viennent de la configuration.
2. Aucune donnée personnelle dans les fichiers du dépôt (hors `docs/interne/`, notes internes retirées du paquet
   produit) :
   - motifs propres au foyer, lus HORS dépôt : `HOMEOS_MOTIFS_INTERDITS` (séparés par des virgules) et/ou
     `HOMEOS_MOTIFS_INTERDITS_FICHIER` (un motif par ligne, `#` = commentaire), insensibles à la casse et aux accents.
     Exemple : HOMEOS_MOTIFS_INTERDITS_FICHIER=~/motifs-foyer.txt pytest tests/test_generique.py
   - motifs génériques, toujours vérifiés : identifiants Telegram numériques en dur (hors valeurs factices),
     noms de machine Tailscale (`tail<hex>.ts.net`), noms d'hôte d'hébergeur, adresses mail de messageries grand
     public, numéros de téléphone, adresses IPv4 publiques ;
   - restes connus du premier foyer, toujours vérifiés (écrits ici en classes de caractères, pour qu'une simple
     recherche texte sur l'archive ne trouve pas ce fichier) : prénoms des deux adultes, ancien nom du produit,
     coordonnées de la commune, ancienne désignation du premier foyer, numéros réels de deux lignes de bus dans
     les exemples, identifiants Telegram à 9 chiffres autres que les valeurs factices 111111111, 222222222 et
     123456789.
"""

from __future__ import annotations

import os
import re
import subprocess
import unicodedata
from pathlib import Path

import pytest

from tools.common import NOM_ASSISTANT_DEFAUT, prenoms_adultes, rendre_gabarit

RACINE = Path(__file__).resolve().parent.parent

# Dossiers et fichiers hors contrôle. Les exclusions « temporaires » sont des fichiers en cours de modification sur
# d'autres branches au moment du lot 6 : à retirer de cette liste dès leur fusion (voir
# docs/revues/2026-11-08-audit-donnees-personnelles.md).
EXCLUS = ("docs/interne/", "tests/test_generique.py")  # ce fichier contient les exemples de son propre contrôle
EXCLUS_TEMPORAIRES = (
    "docs/maquette/",  # lot 4b (front) : maquette de travail et ses captures (hors paquet)
    "docs/spec-lot4b-maquette-v2.md",  # lot 4b
    "tools/coffre.py",  # lot 5c : exemples de clés « planity/<prénom> » dans les docstrings
    "tools/identifiants.py",  # lot 5c
    "coeur/filtre_sensible.py",  # lot 5c
)
IGNORES = {"__pycache__", ".git", ".venv", "node_modules", ".pytest_cache", ".ruff_cache", "data"}
TAILLE_MAX = 2_000_000

CONFIG_FICTIVE = {
    "assistant": {"nom": "Zébulon"},
    "personnes": [
        {"prenom": "Alex", "linux_user": "alex", "telegram_id": 1, "role": "proprietaire"},
        {"prenom": "Camille", "linux_user": "camille", "telegram_id": 2},
        {"prenom": "Sam", "linux_user": "sam", "telegram_id": 3, "role": "invite"},
    ],
}

# Valeurs factices tolérées : chiffres tous identiques, suites 123456789…, groupe -100123456789.
RE_TELEGRAM = re.compile(
    r"(?:telegram_id|chat_id|groupe_telegram_id|user_id|groupe_id)\W{0,4}(-?\d{5,})|(?<![\d.])(-100\d{9,})(?!\d)",
    re.IGNORECASE,
)
RE_GENERIQUES = {
    "nom de machine Tailscale": re.compile(r"\btail[0-9a-f]{4,}\.ts\.net\b", re.IGNORECASE),
    "nom d'hôte d'hébergeur": re.compile(r"\b(?:srv\d{4,}\.hs[t]gr\.cloud|vps-?\d{4,}\.[a-z]+\.(?:net|com))\b", re.IGNORECASE),
    "adresse mail personnelle": re.compile(
        r"[\w.+-]+@(?:gmail|googlemail|hotmail|outlook|live|yahoo|icloud|me|orange|free|sfr|laposte|wanadoo|proton(?:mail)?)"
        r"\.[a-z]{2,}",
        re.IGNORECASE,
    ),
    "numéro de téléphone": re.compile(r"(?<![\w.])(?:\+33\s?|0)[1-9](?:[ .]\d{2}){4}(?![\w.])"),
}
# Restes connus du premier foyer (motifs génériques, sans valeur réelle complète), cherchés dans le texte normalisé
# (minuscules, sans accents). Classes de caractères : ce fichier ne doit pas se trouver lui-même par un grep.
RE_FOYER_ORIGINE = {
    "prénom d'un adulte du premier foyer": re.compile(r"\b(?:arth[u]r|b[e]n[e]dicte)\b"),
    "ancien nom du produit": re.compile(r"\b(?:bel[\s_-]?a[i]r|home[\s_-]?b[o]ok)\b"),
    "ancienne désignation du premier foyer (hors notes internes)": re.compile(r"\b(?:foyers?|instance|retours?)\s+pilot[e]s?\b"),
    "coordonnées de la commune du premier foyer": re.compile(
        r"(?<![\d.])4[3][.,]4\d*\D{1,40}?(?<![\d.])5[.,]4\d*(?![\d.])|(?:lat|latitude)\W{0,6}4[3][.,]4\d*(?![\d.])"
        r"|(?:lon|lng|longitude)\W{0,6}5[.,]4\d*(?![\d.])"
    ),
    "ligne de bus réelle dans un exemple": re.compile(
        r"(?:\"(?:ligne|id)\"\s*:\s*\"|\b(?:bus|car|ligne)[\s_]+|/api/(?:ligne|passages)/|/l/)5[13](?![\w.,])"
    ),
}
# Identifiants Telegram à 9 chiffres sur une ligne qui parle de Telegram : seules les valeurs factices passent.
RE_CONTEXTE_TELEGRAM = re.compile(r"telegram|chat_id|user_id|userinfobot|\bid\s*:|autoris", re.IGNORECASE)
RE_NEUF_CHIFFRES = re.compile(r"(?<![\w.,-])(\d{9})(?![\w.,])")
ID_TELEGRAM_FACTICES = {"111111111", "222222222", "123456789"}
LIGNE_MAX_TELEGRAM = 500  # au-delà : code minifié (constantes binaires), hors sujet
RE_IPV4 = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")


def _normaliser(texte: str) -> str:
    t = unicodedata.normalize("NFKD", texte.casefold())
    return "".join(c for c in t if not unicodedata.combining(c))


def motifs_interdits() -> list[str]:
    motifs = [m.strip() for m in os.environ.get("HOMEOS_MOTIFS_INTERDITS", "").split(",") if m.strip()]
    fichier = os.environ.get("HOMEOS_MOTIFS_INTERDITS_FICHIER", "").strip()
    if fichier:
        for ligne in Path(fichier).expanduser().read_text(encoding="utf-8").splitlines():
            ligne = ligne.split("#", 1)[0].strip()
            if ligne:
                motifs.append(ligne)
    return motifs


def _factice(valeur: str) -> bool:
    chiffres = valeur.lstrip("-")
    if len(chiffres) >= 12 and chiffres.startswith("100"):  # groupe Telegram : -100…
        chiffres = chiffres[3:]
    return len(set(chiffres)) == 1 or chiffres.startswith("123456789") or "1234567890".startswith(chiffres)


def _ip_publique(octets: tuple[int, ...]) -> bool:
    a, b = octets[0], octets[1]
    if any(o > 255 for o in octets) or a in (0, 10, 127) or a >= 224:
        return False
    if (a, b) == (192, 168) or (a == 172 and 16 <= b <= 31) or (a == 169 and b == 254) or (a == 100 and 64 <= b <= 127):
        return False
    return octets[:3] not in ((192, 0, 2), (198, 51, 100), (203, 0, 113))  # plages de documentation


def fichiers_du_depot() -> list[Path]:
    """Fichiers suivis par git (ou, hors dépôt git — archive produit —, tout l'arbre), texte seulement."""
    try:
        sortie = subprocess.run(
            ["git", "-C", str(RACINE), "ls-files", "-z"], capture_output=True, check=True, timeout=30
        ).stdout.decode()
        relatifs = [r for r in sortie.split("\0") if r]
    except (OSError, subprocess.SubprocessError):
        relatifs = []
    if not relatifs:
        relatifs = [
            p.relative_to(RACINE).as_posix()
            for p in RACINE.rglob("*")
            if p.is_file() and not any(part in IGNORES for part in p.relative_to(RACINE).parts)
        ]
    fichiers = []
    for rel in sorted(relatifs):
        if rel.startswith(EXCLUS + EXCLUS_TEMPORAIRES) or any(part in IGNORES for part in Path(rel).parts):
            continue
        chemin = RACINE / rel
        if not chemin.is_file() or chemin.stat().st_size > TAILLE_MAX:
            continue
        fichiers.append(chemin)
    return fichiers


def _lire(chemin: Path) -> str | None:
    try:
        return chemin.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None  # binaire


def problemes(fichiers: list[Path], motifs: list[str]) -> list[str]:
    normalises = [(m, _normaliser(m)) for m in motifs]
    trouves: list[str] = []
    for f in fichiers:
        texte = _lire(f)
        if texte is None:
            continue
        rel = f.relative_to(RACINE) if f.is_relative_to(RACINE) else f
        normal = _normaliser(texte)
        trouves += [f"{rel} : motif du foyer « {brut} »" for brut, motif in normalises if motif in normal]
        for m in RE_TELEGRAM.finditer(texte):
            valeur = m.group(1) or m.group(2)
            if not _factice(valeur):
                trouves.append(f"{rel} : identifiant Telegram en dur « {m.group(0)} »")
        for nom, regex in RE_GENERIQUES.items():
            trouves += [
                f"{rel} : {nom} « {m.group(0)} »"
                for m in regex.finditer(texte)
                if not (nom == "numéro de téléphone" and re.sub(r"\D", "", m.group(0)).endswith("12345678"))
            ]  # « 06 12 34 56 78 » : numéro factice conventionnel (tests du filtre de données sensibles)
        for nom, regex in RE_FOYER_ORIGINE.items():
            trouves += [f"{rel} : {nom} « {m.group(0)} »" for m in regex.finditer(normal)]
        for ligne in texte.splitlines():
            if len(ligne) <= LIGNE_MAX_TELEGRAM and RE_CONTEXTE_TELEGRAM.search(ligne):
                trouves += [
                    f"{rel} : identifiant Telegram à 9 chiffres « {m.group(1)} »"
                    for m in RE_NEUF_CHIFFRES.finditer(ligne)
                    if m.group(1) not in ID_TELEGRAM_FACTICES
                ]
        for m in RE_IPV4.finditer(texte):
            octets = tuple(int(o) for o in m.groups())
            if _ip_publique(octets):
                trouves.append(f"{rel} : adresse IPv4 publique « {m.group(0)} »")
    return trouves


# --- 1. prompts rendus depuis la configuration ------------------------------------------------------------------


def prompts() -> list[Path]:
    content = RACINE / "content"
    liste = [content / "CLAUDE.md", *sorted((content / "prompts").glob("*.md")), *sorted((content / "onboarding").glob("*.md"))]
    for cap in sorted((RACINE / "capabilities").glob("*/")):
        liste += [f for f in (cap / "CLAUDE.md", cap / "onboarding.md") if f.is_file()]
    return liste


@pytest.mark.parametrize("chemin", prompts(), ids=lambda p: p.relative_to(RACINE).as_posix())
def test_prompt_entierement_rendu(chemin: Path) -> None:
    rendu = rendre_gabarit(chemin.read_text(encoding="utf-8"), CONFIG_FICTIVE)
    restes = [v for v in re.findall(r"\{\{[^}]*\}\}", rendu) if v != "{{donnees}}"]
    assert not restes, f"variables non rendues : {restes}"
    assert NOM_ASSISTANT_DEFAUT not in rendu, "prénom de l'assistant écrit en dur : utiliser {{assistant.nom}}"


def test_variables_du_foyer() -> None:
    assert prenoms_adultes(CONFIG_FICTIVE) == "Alex et Camille"  # l'invité n'est pas un adulte du foyer
    assert prenoms_adultes({}) == "les adultes du foyer"
    rendu = rendre_gabarit("{{assistant.nom}} aide {{foyer.adultes}}.", CONFIG_FICTIVE)
    assert rendu == "Zébulon aide Alex et Camille."
    assert rendre_gabarit("{{assistant.nom}}", {}) == NOM_ASSISTANT_DEFAUT
    claude = rendre_gabarit((RACINE / "content" / "CLAUDE.md").read_text(encoding="utf-8"), CONFIG_FICTIVE)
    assert "Tu t'appelles Zébulon" in claude and "assistant du foyer (Alex et Camille)" in claude


# --- 2. aucune donnée personnelle -----------------------------------------------------------------------------------


def test_depot_sans_donnee_personnelle() -> None:
    trouves = problemes(fichiers_du_depot(), motifs_interdits())
    assert not trouves, "Données personnelles dans le dépôt produit :\n" + "\n".join(trouves[:50])


def test_detection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Le contrôle lui-même : motifs du foyer (fichier hors dépôt), identifiants, adresses."""
    liste = tmp_path / "motifs.txt"
    liste.write_text("# foyer\nRue des Lilas\n\nDupont  # nom de famille\n", encoding="utf-8")
    monkeypatch.setenv("HOMEOS_MOTIFS_INTERDITS", "Médor")
    monkeypatch.setenv("HOMEOS_MOTIFS_INTERDITS_FICHIER", str(liste))
    assert motifs_interdits() == ["Médor", "Rue des Lilas", "Dupont"]
    a = tmp_path / "a.md"
    a.write_text(
        "medor et DUPONT\ntelegram_id: 987654321\ngroupe_telegram_id: -1001234567890\ntelegram_id: 111111111\n"
        "mon-vps.tail1a2b3c.ts.net srv1234567." + "hst" + "gr.cloud jean.dupont@gmail.com 06 24 35 46 57 06 12 34 56 78\n"
        "8.8.8.8 127.0.0.1 100.101.102.103 192.168.1.10 version 1.64.1\n",
        encoding="utf-8",
    )
    (tmp_path / "b.bin").write_bytes(b"\xff\xfe\x00Dupont")
    trouves = problemes([a, tmp_path / "b.bin"], motifs_interdits())
    texte = "\n".join(trouves)
    for attendu in ("« Médor »", "« Dupont »", "987654321", "tail1a2b3c", "hst" + "gr", "gmail", "06 24 35 46 57", "8.8.8.8"):
        assert attendu in texte, attendu
    for absent in ("06 12 34 56 78", "111111111", "-1001234567890", "127.0.0.1", "100.101", "192.168", "b.bin", "Rue des Lilas"):
        assert absent not in texte, absent


def test_detection_restes_du_premier_foyer(tmp_path: Path) -> None:
    """Motifs génériques des restes connus : écrits en morceaux pour qu'un grep sur l'archive ne trouve rien ici."""
    a = tmp_path / "a.md"
    a.write_text(
        "Merci " + "Arth" + "ur et " + "Bén" + "édicte\n"
        "# " + "Home " + "Book " + "Bel " + "Air OS\n"
        "retour du foyer " + "pil" + "ote\n"
        "latitude: " + "43" + ".45\n"
        "(" + "43" + ".45, 5" + ".41)\n"
        '{"ligne": "' + "5" + '1"} Bus ' + "5" + "3\n"
        "Identifiant Telegram : " + "5849" + "30211 · chat_id " + "7391" + "04826\n",
        encoding="utf-8",
    )
    texte = "\n".join(problemes([a], []))
    for attendu in ("prénom d'un adulte", "ancien nom du produit", "ancienne désignation", "coordonnées", "ligne de bus réelle",
                    "identifiant Telegram à 9 chiffres"):
        assert attendu in texte, attendu
    assert texte.count("prénom d'un adulte") == 2 and texte.count("ligne de bus réelle") == 2
    assert texte.count("identifiant Telegram à 9 chiffres") == 2

    b = tmp_path / "b.md"
    b.write_text(
        "telegram_id: 111111111 · Telegram 222222222 · est_autorise(123456789)\n"
        "latitude: 45.00 · longitude: 2.00 · version 5.4.1 · 43,40 € · Bel-Abbès · Bus 7 · ligne 12 · Bus 510\n"
        "Livraison confirmée - 110000002\n",
        encoding="utf-8",
    )
    assert problemes([b], []) == []
