"""Conversations et messages (lot 4a), partagés par Telegram et l'appli web : ``<etat>/appli.sqlite``.

Une personne a une conversation **courante** (la plus récemment active, de moins de 12 h comme la session Claude) :
Telegram y écrit, l'appli web l'affiche et y écrit aussi, avec la même session Claude (``sessions.json``). Une
nouvelle conversation (``/nouveau``, ``POST /api/conversations``, 12 h d'inactivité) repart d'une session vide.

Base distincte de ``data/homeos.sqlite`` : l'état sensible est dans ``/var/lib/homeos`` (700 homeos en mode http),
illisible par les sessions Claude et par l'autre personne (les conversations sont privées), et sauvegardé.
Les tables de l'authentification web (``web/auth.py``) sont dans le même fichier.

Tables :
    conversations(id, linux_user, titre, cree, maj, session_id, session_reponses, archivee)
    messages(id, conversation_id, ts, role [personne|assistant|systeme], canal [telegram|web|routine], auteur,
             texte, cartes (JSON), meta (JSON))
    conversation_actions(action_id, conversation_id)   action à confirmer → conversation où elle est née
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from coeur import filtre_sensible

NOM_FICHIER = "appli.sqlite"
DUREE_CONVERSATION = timedelta(hours=12)  # comme la session Claude (bot/sessions.py)
LONGUEUR_TITRE = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    linux_user TEXT NOT NULL,
    titre TEXT NOT NULL DEFAULT '',
    cree TEXT NOT NULL,
    maj TEXT NOT NULL,
    session_id TEXT,
    session_reponses INTEGER NOT NULL DEFAULT 0,
    archivee INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_conversations_personne ON conversations(linux_user, maj);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    ts TEXT NOT NULL,
    role TEXT NOT NULL,
    canal TEXT NOT NULL,
    auteur TEXT,
    texte TEXT NOT NULL DEFAULT '',
    cartes TEXT,
    meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS conversation_actions (
    action_id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL
);
"""


def _maintenant() -> datetime:
    return datetime.now().astimezone()


def _iso(dt: datetime | None = None) -> str:
    return (dt or _maintenant()).isoformat(timespec="seconds")


def chemin_par_defaut(etat: Path) -> Path:
    return Path(etat) / NOM_FICHIER


class Historique:
    def __init__(self, chemin: Path) -> None:
        self.chemin = Path(chemin)
        self._pret = False

    @contextmanager
    def connexion(self) -> Iterator[sqlite3.Connection]:
        if not self._pret:
            self.init()
        conn = sqlite3.connect(self.chemin, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> None:
        self.chemin.parent.mkdir(parents=True, exist_ok=True)
        neuf = not self.chemin.exists()
        if neuf:
            # Créé en 600 AVANT SQLite : les fichiers -wal et -shm reprennent les droits de la base (jamais lisibles
            # par le groupe foyer, même en mode stdio où /var/lib/homeos est 750).
            try:
                os.close(os.open(self.chemin, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            except FileExistsError:  # créé entre-temps par l'autre processus (bot, appli)
                pass
        conn = sqlite3.connect(self.chemin, timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            colonnes = {r[1] for r in conn.execute("PRAGMA table_info(conversations)").fetchall()}
            if "session_reponses" not in colonnes:  # base créée avant le renouvellement des sessions
                conn.execute("ALTER TABLE conversations ADD COLUMN session_reponses INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        finally:
            conn.close()
        if neuf:
            try:
                os.chmod(self.chemin, 0o600)
            except OSError:
                pass
        self._pret = True

    # --- conversations -------------------------------------------------------------------------------------------

    @staticmethod
    def _conv(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def conversation(self, conversation_id: int) -> dict[str, Any] | None:
        with self.connexion() as c:
            return self._conv(c.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone())

    def conversation_de(self, linux_user: str, conversation_id: int) -> dict[str, Any] | None:
        """La conversation si elle appartient à cette personne (sinon None : jamais celle de l'autre)."""
        conv = self.conversation(conversation_id)
        return conv if conv is not None and conv["linux_user"] == linux_user else None

    def derniere(self, linux_user: str) -> dict[str, Any] | None:
        with self.connexion() as c:
            return self._conv(
                c.execute(
                    "SELECT * FROM conversations WHERE linux_user = ? AND archivee = 0 ORDER BY maj DESC, id DESC LIMIT 1",
                    (linux_user,),
                ).fetchone()
            )

    def courante(self, linux_user: str, maintenant: datetime | None = None) -> dict[str, Any] | None:
        """Conversation courante : la plus récente, si son dernier message date de moins de 12 h."""
        conv = self.derniere(linux_user)
        if conv is None:
            return None
        try:
            maj = datetime.fromisoformat(conv["maj"])
        except ValueError:
            return None
        if (maintenant or _maintenant()) - maj > DUREE_CONVERSATION:
            return None
        return conv

    def creer(self, linux_user: str, titre: str = "") -> dict[str, Any]:
        quand = _iso()
        with self.connexion() as c:
            cur = c.execute(
                "INSERT INTO conversations (linux_user, titre, cree, maj) VALUES (?, ?, ?, ?)",
                (linux_user, titre[:LONGUEUR_TITRE], quand, quand),
            )
            cid = int(cur.lastrowid or 0)
        conv = self.conversation(cid)
        assert conv is not None
        return conv

    def lister(self, linux_user: str, limite: int = 50) -> list[dict[str, Any]]:
        with self.connexion() as c:
            rows = c.execute(
                "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS nb_messages "
                "FROM conversations c WHERE linux_user = ? AND archivee = 0 ORDER BY maj DESC, id DESC LIMIT ?",
                (linux_user, limite),
            ).fetchall()
        return [dict(r) for r in rows]

    def maj_session(self, conversation_id: int, session_id: str | None) -> None:
        """Session Claude de la conversation après une réponse : compteur de réponses + 1 si c'est la même session,
        sinon remis à 1 (renouvellement après ``ia.session_max_reponses``, ``coeur.conversation``)."""
        with self.connexion() as c:
            c.execute(
                "UPDATE conversations SET session_reponses = CASE WHEN session_id IS ? THEN session_reponses + 1 "
                "ELSE 1 END, session_id = ? WHERE id = ?",
                (session_id, session_id, conversation_id),
            )

    # --- messages ------------------------------------------------------------------------------------------------

    def ajouter_message(
        self,
        conversation_id: int,
        role: str,
        canal: str,
        texte: str,
        *,
        auteur: str | None = None,
        cartes: list[dict[str, Any]] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> int:
        """Ajoute un message. Le texte d'une personne est stocké masqué (lot 5c, ``coeur.filtre_sensible``) : ce
        qu'on relit plus tard est exactement ce que l'assistant a reçu, jamais une carte ou un mot de passe."""
        if role == "personne" and texte:
            texte = filtre_sensible.masquer(texte)
        quand = _iso()
        with self.connexion() as c:
            cur = c.execute(
                "INSERT INTO messages (conversation_id, ts, role, canal, auteur, texte, cartes, meta) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    conversation_id,
                    quand,
                    role,
                    canal,
                    auteur,
                    texte or "",
                    json.dumps(cartes, ensure_ascii=False) if cartes else None,
                    json.dumps(meta, ensure_ascii=False, default=str) if meta else None,
                ),
            )
            mid = int(cur.lastrowid or 0)
            c.execute("UPDATE conversations SET maj = ? WHERE id = ?", (quand, conversation_id))
            if role == "personne" and texte:
                c.execute(
                    "UPDATE conversations SET titre = ? WHERE id = ? AND titre = ''",
                    (" ".join(texte.split())[:LONGUEUR_TITRE], conversation_id),
                )
        return mid

    @staticmethod
    def _message(row: sqlite3.Row) -> dict[str, Any]:
        m = dict(row)
        for cle in ("cartes", "meta"):
            try:
                m[cle] = json.loads(m[cle]) if m.get(cle) else ([] if cle == "cartes" else {})
            except ValueError:
                m[cle] = [] if cle == "cartes" else {}
        return m

    def messages(self, conversation_id: int, apres_id: int = 0, limite: int = 200) -> list[dict[str, Any]]:
        with self.connexion() as c:
            rows = c.execute(
                "SELECT * FROM messages WHERE conversation_id = ? AND id > ? ORDER BY id LIMIT ?",
                (conversation_id, apres_id, limite),
            ).fetchall()
        return [self._message(r) for r in rows]

    def messages_avant(self, conversation_id: int, avant_id: int, limite: int = 12) -> list[dict[str, Any]]:
        """Les ``limite`` derniers messages avant ``avant_id``, dans l'ordre chronologique."""
        with self.connexion() as c:
            rows = c.execute(
                "SELECT * FROM messages WHERE conversation_id = ? AND id < ? ORDER BY id DESC LIMIT ?",
                (conversation_id, avant_id, limite),
            ).fetchall()
        return [self._message(r) for r in reversed(rows)]

    def dernier_id(self, conversation_id: int) -> int:
        with self.connexion() as c:
            row = c.execute("SELECT MAX(id) FROM messages WHERE conversation_id = ?", (conversation_id,)).fetchone()
        return int(row[0] or 0)

    def premier_message_du_jour(self, linux_user: str, jour: date | None = None) -> bool:
        """Vrai si la personne n'a encore écrit aucun message aujourd'hui (tous canaux)."""
        jour = jour or _maintenant().date()
        with self.connexion() as c:
            row = c.execute(
                "SELECT COUNT(*) FROM messages m JOIN conversations c ON c.id = m.conversation_id "
                "WHERE c.linux_user = ? AND m.role = 'personne' AND substr(m.ts, 1, 10) = ?",
                (linux_user, jour.isoformat()),
            ).fetchone()
        return int(row[0] or 0) == 0

    # --- messages spontanés (coeur.spontane) -----------------------------------------------------------------------

    def spontanes_du_jour(self, linux_user: str, jour: date | None = None) -> list[dict[str, Any]]:
        """Messages spontanés (``canal = 'routine'``) écrits aujourd'hui pour cette personne, avec leur ``meta``."""
        jour = jour or _maintenant().date()
        with self.connexion() as c:
            rows = c.execute(
                "SELECT m.* FROM messages m JOIN conversations c ON c.id = m.conversation_id "
                "WHERE c.linux_user = ? AND m.canal = 'routine' AND substr(m.ts, 1, 10) = ? ORDER BY m.id",
                (linux_user, jour.isoformat()),
            ).fetchall()
        return [self._message(r) for r in rows]

    # --- actions à confirmer ---------------------------------------------------------------------------------------

    def associer_action(self, action_id: int, conversation_id: int) -> bool:
        """Rattache une action à la conversation où elle est née ; la première association gagne (une ligne
        ``CONFIRMATION_REQUISE:<id>`` injectée dans une autre conversation ne déplace jamais une action).
        Renvoie vrai si l'action est maintenant rattachée à ``conversation_id``."""
        with self.connexion() as c:
            c.execute(
                "INSERT OR IGNORE INTO conversation_actions (action_id, conversation_id) VALUES (?, ?)",
                (action_id, conversation_id),
            )
            row = c.execute(
                "SELECT conversation_id FROM conversation_actions WHERE action_id = ?", (action_id,)
            ).fetchone()
        return bool(row) and int(row[0]) == conversation_id

    def action_de(self, linux_user: str, action_id: int) -> bool:
        """Vrai si l'action est rattachée à une conversation de cette personne."""
        with self.connexion() as c:
            row = c.execute(
                "SELECT 1 FROM conversation_actions a JOIN conversations c ON c.id = a.conversation_id "
                "WHERE a.action_id = ? AND c.linux_user = ?",
                (action_id, linux_user),
            ).fetchone()
        return row is not None

    def conversation_de_action(self, action_id: int) -> int | None:
        with self.connexion() as c:
            row = c.execute(
                "SELECT conversation_id FROM conversation_actions WHERE action_id = ?", (action_id,)
            ).fetchone()
        return int(row[0]) if row else None


def depuis(etat: Path) -> Historique:
    return Historique(chemin_par_defaut(etat))


def jour_de(ts: str) -> date | None:
    try:
        return datetime.fromisoformat(ts).date()
    except ValueError:
        return None
