"""Markdown (sortie de Claude) → HTML Telegram simple, et découpage des longs messages.

Telegram (parse_mode=HTML) n'accepte qu'un petit sous-ensemble : <b>, <i>, <code>, <pre>, <a>,
<s>, <u>. Tout le reste doit être échappé. On convertit :

- ``**gras**`` / ``__gras__`` → <b>, ``*italique*`` / ``_italique_`` → <i>
- `` `code` `` → <code>, blocs ``` → <pre>
- ``# Titre`` → <b>Titre</b>
- listes ``- `` / ``* `` / ``+ `` → « • », listes numérotées conservées
- ``[texte](url)`` → <a href="url">texte</a> (http/https uniquement)
- ``~~barré~~`` → <s>
"""

from __future__ import annotations

import html
import re

LIMITE_TELEGRAM = 4000

_RE_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_RE_CODE = re.compile(r"`([^`\n]+)`")
_RE_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_RE_ITAL_STAR = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_RE_ITAL_UND = re.compile(r"(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])")
_RE_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_RE_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_RE_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_RE_BULLET = re.compile(r"^(\s*)[-*+•]\s+(.*)$")
_RE_NUMBERED = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_RE_HR = re.compile(r"^\s*([-*_])\s*(\1\s*){2,}$")


def _inline(texte: str) -> str:
    """Convertit le Markdown en ligne d'un fragment déjà hors bloc de code."""
    # 1. Code inline : on extrait d'abord pour ne pas le reformater.
    codes: list[str] = []

    def _stash_code(m: re.Match[str]) -> str:
        codes.append(f"<code>{html.escape(m.group(1), quote=False)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    texte = _RE_CODE.sub(_stash_code, texte)

    # 2. Liens : on extrait aussi (l'URL ne doit pas être touchée).
    liens: list[str] = []

    def _stash_link(m: re.Match[str]) -> str:
        label = html.escape(m.group(1), quote=False)
        url = html.escape(m.group(2), quote=True)
        liens.append(f'<a href="{url}">{label}</a>')
        return f"\x01{len(liens) - 1}\x01"

    texte = _RE_LINK.sub(_stash_link, texte)

    # 3. Échappement du reste, puis balises simples.
    texte = html.escape(texte, quote=False)
    texte = _RE_BOLD.sub(lambda m: f"<b>{m.group(2)}</b>", texte)
    texte = _RE_STRIKE.sub(lambda m: f"<s>{m.group(1)}</s>", texte)
    texte = _RE_ITAL_STAR.sub(lambda m: f"<i>{m.group(1)}</i>", texte)
    texte = _RE_ITAL_UND.sub(lambda m: f"<i>{m.group(1)}</i>", texte)

    # 4. Réinjection.
    texte = re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], texte)
    texte = re.sub(r"\x01(\d+)\x01", lambda m: liens[int(m.group(1))], texte)
    return texte


def _bloc(texte: str) -> str:
    lignes_out: list[str] = []
    for ligne in texte.split("\n"):
        if _RE_HR.match(ligne) and len(ligne.strip()) >= 3:
            lignes_out.append("———")
            continue
        m = _RE_HEADING.match(ligne)
        if m:
            lignes_out.append(f"<b>{_inline(m.group(1))}</b>")
            continue
        m = _RE_BULLET.match(ligne)
        if m:
            indent = "  " if len(m.group(1)) >= 2 else ""
            lignes_out.append(f"{indent}• {_inline(m.group(2))}")
            continue
        m = _RE_NUMBERED.match(ligne)
        if m:
            indent = "  " if len(m.group(1)) >= 2 else ""
            lignes_out.append(f"{indent}{m.group(2)}. {_inline(m.group(3))}")
            continue
        # Tableaux Markdown : on retire les barres pour garder quelque chose de lisible.
        if ligne.strip().startswith("|") and ligne.strip().endswith("|"):
            cellules = [c.strip() for c in ligne.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cellules if c):
                continue
            lignes_out.append(" — ".join(_inline(c) for c in cellules if c))
            continue
        lignes_out.append(_inline(ligne))
    return "\n".join(lignes_out)


def md_vers_html(texte: str) -> str:
    """Convertit le Markdown de Claude en HTML Telegram sûr (tout est échappé)."""
    if not texte:
        return ""
    texte = texte.replace("\r\n", "\n")
    parts: list[str] = []
    pos = 0
    for m in _RE_FENCE.finditer(texte):
        parts.append(_bloc(texte[pos : m.start()]))
        code = m.group(1).rstrip("\n")
        parts.append(f"<pre>{html.escape(code, quote=False)}</pre>")
        pos = m.end()
    reste = texte[pos:]
    # Bloc ``` non fermé : on l'affiche en <pre>.
    if reste.count("```") == 1:
        avant, _, code = reste.partition("```")
        code = code.split("\n", 1)[1] if "\n" in code else code
        parts.append(_bloc(avant))
        parts.append(f"<pre>{html.escape(code.strip('\n'), quote=False)}</pre>")
    else:
        parts.append(_bloc(reste))
    out = "".join(parts)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def texte_brut(texte: str) -> str:
    """Version sans HTML (pour le streaming, où l'on envoie en texte brut)."""
    return texte


def decouper(texte: str, limite: int = LIMITE_TELEGRAM) -> list[str]:
    """Découpe un message en morceaux ≤ ``limite``, de préférence aux sauts de ligne.

    Les balises HTML ouvertes dans un morceau sont fermées et rouvertes dans le suivant
    pour que chaque morceau reste un HTML Telegram valide.
    """
    if len(texte) <= limite:
        return [texte] if texte else []
    morceaux: list[str] = []
    reste = texte
    while len(reste) > limite:
        coupe = reste.rfind("\n\n", 0, limite)
        if coupe < limite // 3:
            coupe = reste.rfind("\n", 0, limite)
        if coupe < limite // 3:
            coupe = reste.rfind(" ", 0, limite)
        if coupe < limite // 3:
            coupe = limite
        morceaux.append(reste[:coupe].rstrip())
        reste = reste[coupe:].lstrip("\n ")
    if reste:
        morceaux.append(reste)
    return _reequilibrer_balises([m for m in morceaux if m])


_RE_TAG = re.compile(r"<(/?)(b|i|code|pre|s|u|a)(\s[^>]*)?>")


def _reequilibrer_balises(morceaux: list[str]) -> list[str]:
    ouvertes: list[tuple[str, str]] = []  # (nom, balise ouvrante complète)
    resultat: list[str] = []
    for m in morceaux:
        prefixe = "".join(tag for _, tag in ouvertes)
        for t in _RE_TAG.finditer(m):
            ferme, nom, attrs = t.group(1), t.group(2), t.group(3) or ""
            if ferme:
                for i in range(len(ouvertes) - 1, -1, -1):
                    if ouvertes[i][0] == nom:
                        del ouvertes[i]
                        break
            else:
                ouvertes.append((nom, f"<{nom}{attrs}>"))
        suffixe = "".join(f"</{nom}>" for nom, _ in reversed(ouvertes))
        resultat.append(prefixe + m + suffixe)
    return resultat
