"""Tables de l'authentification de l'appli web (lot 4a), dans ``<etat>/appli.sqlite`` (600, homeos) avec l'historique.

Aucun jeton n'est stocké en clair : invitations, sessions, step-up, codes à usage unique et cérémonies WebAuthn
sont rangés par empreinte SHA-256 (:func:`empreinte`). Les passkeys gardent leur clé publique (pas un secret).

Tables :
    web_passkeys(id, linux_user, credential_id, cle_publique, compteur, transports, nom, cree, utilise)
    web_utilisateurs(linux_user, handle)                 identifiant WebAuthn stable (aléatoire) de la personne
    web_invitations(empreinte, linux_user, cree, expire, utilisee)
    web_sessions(id, empreinte, linux_user, cree, expire, vu, appareil, methode, step_up_empreinte, step_up_expire,
                 tailscale_login)                       methode « code » = niveau « secours » (24 h, jamais prolongée)
    web_codes(linux_user, empreinte, expire, essais, cree)
    web_ceremonies(empreinte, type, linux_user, challenge, expire, invitation)
    web_pieces(id, linux_user, chemin, nom, taille, cree)   fichiers envoyés, cités ensuite dans un message
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from web import config as configweb

SCHEMA = """
CREATE TABLE IF NOT EXISTS web_utilisateurs (
    linux_user TEXT PRIMARY KEY,
    handle BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS web_passkeys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    linux_user TEXT NOT NULL,
    credential_id BLOB NOT NULL UNIQUE,
    cle_publique BLOB NOT NULL,
    compteur INTEGER NOT NULL DEFAULT 0,
    transports TEXT,
    nom TEXT NOT NULL DEFAULT '',
    cree TEXT NOT NULL,
    utilise TEXT
);
CREATE TABLE IF NOT EXISTS web_invitations (
    empreinte TEXT PRIMARY KEY,
    linux_user TEXT NOT NULL,
    cree TEXT NOT NULL,
    expire TEXT NOT NULL,
    utilisee TEXT
);
CREATE TABLE IF NOT EXISTS web_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    empreinte TEXT NOT NULL UNIQUE,
    linux_user TEXT NOT NULL,
    cree TEXT NOT NULL,
    expire TEXT NOT NULL,
    vu TEXT NOT NULL,
    appareil TEXT NOT NULL DEFAULT '',
    methode TEXT NOT NULL DEFAULT 'passkey',
    step_up_empreinte TEXT,
    step_up_expire TEXT,
    tailscale_login TEXT
);
CREATE TABLE IF NOT EXISTS web_codes (
    linux_user TEXT PRIMARY KEY,
    empreinte TEXT NOT NULL,
    expire TEXT NOT NULL,
    essais INTEGER NOT NULL DEFAULT 0,
    cree TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS web_ceremonies (
    empreinte TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    linux_user TEXT,
    challenge BLOB NOT NULL,
    expire TEXT NOT NULL,
    invitation TEXT
);
CREATE TABLE IF NOT EXISTS web_pieces (
    id TEXT PRIMARY KEY,
    linux_user TEXT NOT NULL,
    chemin TEXT NOT NULL,
    nom TEXT NOT NULL,
    taille INTEGER NOT NULL,
    cree TEXT NOT NULL
);
"""


def maintenant() -> datetime:
    return datetime.now().astimezone()


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def empreinte(jeton: str) -> str:
    return hashlib.sha256(jeton.encode("utf-8")).hexdigest()


def nouveau_jeton(octets: int = 32) -> str:
    return secrets.token_urlsafe(octets)


def _migrer(conn: sqlite3.Connection) -> None:
    """Colonnes ajoutées après la création de la base (``CREATE TABLE IF NOT EXISTS`` ne les ajoute pas)."""
    colonnes = {r[1] for r in conn.execute("PRAGMA table_info(web_sessions)")}
    if "tailscale_login" not in colonnes:
        try:
            conn.execute("ALTER TABLE web_sessions ADD COLUMN tailscale_login TEXT")
        except sqlite3.OperationalError as exc:  # ajoutée entre-temps par l'autre processus (bot, appli)
            if "duplicate column" not in str(exc):
                raise


def secours(session: dict[str, Any]) -> bool:
    """Session ouverte par le code Telegram : niveau « secours » (converser, lire, enrôler une passkey)."""
    return session.get("methode") == configweb.METHODE_SECOURS


def _secours_echue(session: dict[str, Any]) -> bool:
    """Une session de secours vit 24 h depuis sa création, quelle que soit son expiration enregistrée (sessions
    ouvertes avant ce durcissement, avec 30 jours glissants)."""
    if not secours(session):
        return False
    try:
        cree = datetime.fromisoformat(str(session.get("cree")))
    except ValueError:
        return True
    return cree + timedelta(hours=configweb.DUREE_SESSION_SECOURS_H) <= maintenant()


def _expire(valeur: str | None) -> bool:
    if not valeur:
        return True
    try:
        return datetime.fromisoformat(valeur) <= maintenant()
    except ValueError:
        return True


class StockageAuth:
    def __init__(self, chemin: Path) -> None:
        self.chemin = Path(chemin)
        self._pret = False

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
            _migrer(conn)
            conn.commit()
        finally:
            conn.close()
        if neuf:
            try:
                os.chmod(self.chemin, 0o600)
            except OSError:
                pass
        self._pret = True

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

    # --- utilisateurs et passkeys --------------------------------------------------------------------------------

    def handle(self, linux_user: str) -> bytes:
        """Identifiant WebAuthn de la personne (32 octets aléatoires, jamais le nom) : créé au premier enrôlement."""
        with self.connexion() as c:
            row = c.execute("SELECT handle FROM web_utilisateurs WHERE linux_user = ?", (linux_user,)).fetchone()
            if row:
                return bytes(row[0])
            h = secrets.token_bytes(32)
            c.execute("INSERT INTO web_utilisateurs (linux_user, handle) VALUES (?, ?)", (linux_user, h))
            return h

    def passkeys(self, linux_user: str) -> list[dict[str, Any]]:
        with self.connexion() as c:
            rows = c.execute(
                "SELECT * FROM web_passkeys WHERE linux_user = ? ORDER BY id", (linux_user,)
            ).fetchall()
        return [dict(r) for r in rows]

    def passkey_par_credential(self, credential_id: bytes) -> dict[str, Any] | None:
        with self.connexion() as c:
            row = c.execute("SELECT * FROM web_passkeys WHERE credential_id = ?", (credential_id,)).fetchone()
        return dict(row) if row else None

    def ajouter_passkey(
        self, linux_user: str, credential_id: bytes, cle_publique: bytes, compteur: int, transports: list[str], nom: str
    ) -> int:
        with self.connexion() as c:
            cur = c.execute(
                "INSERT INTO web_passkeys (linux_user, credential_id, cle_publique, compteur, transports, nom, cree) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (linux_user, credential_id, cle_publique, compteur, ",".join(transports), nom[:60], iso(maintenant())),
            )
            return int(cur.lastrowid or 0)

    def maj_compteur(self, passkey_id: int, compteur: int) -> None:
        with self.connexion() as c:
            c.execute(
                "UPDATE web_passkeys SET compteur = ?, utilise = ? WHERE id = ?", (compteur, iso(maintenant()), passkey_id)
            )

    def supprimer_passkey(self, linux_user: str, passkey_id: int) -> bool:
        with self.connexion() as c:
            cur = c.execute("DELETE FROM web_passkeys WHERE id = ? AND linux_user = ?", (passkey_id, linux_user))
            return cur.rowcount > 0

    # --- invitations ---------------------------------------------------------------------------------------------

    def creer_invitation(self, linux_user: str, duree: timedelta) -> str:
        jeton = nouveau_jeton()
        quand = maintenant()
        with self.connexion() as c:
            # Les invitations expirées ou déjà servies depuis plus d'un jour ne servent plus à rien.
            c.execute("DELETE FROM web_invitations WHERE expire < ?", (iso(quand - timedelta(days=1)),))
            c.execute(
                "INSERT INTO web_invitations (empreinte, linux_user, cree, expire) VALUES (?, ?, ?, ?)",
                (empreinte(jeton), linux_user, iso(quand), iso(quand + duree)),
            )
        return jeton

    def invitation_valide(self, jeton: str) -> str | None:
        """linux_user de l'invitation si elle existe, n'a pas servi et n'a pas expiré."""
        with self.connexion() as c:
            row = c.execute("SELECT * FROM web_invitations WHERE empreinte = ?", (empreinte(jeton or ""),)).fetchone()
        if row is None or row["utilisee"] or _expire(row["expire"]):
            return None
        return str(row["linux_user"])

    def consommer_invitation(self, jeton_empreinte: str) -> bool:
        """Marque l'invitation servie (une seule fois, atomiquement) si elle n'a pas expiré entre-temps."""
        quand = iso(maintenant())
        with self.connexion() as c:
            cur = c.execute(
                "UPDATE web_invitations SET utilisee = ? WHERE empreinte = ? AND utilisee IS NULL AND expire > ?",
                (quand, jeton_empreinte, quand),
            )
            return cur.rowcount > 0

    # --- cérémonies WebAuthn (challenge à usage unique) ----------------------------------------------------------

    def creer_ceremonie(
        self, type_: str, challenge: bytes, duree: timedelta, linux_user: str | None = None, invitation: str | None = None
    ) -> str:
        jeton = nouveau_jeton(24)
        with self.connexion() as c:
            c.execute("DELETE FROM web_ceremonies WHERE expire < ?", (iso(maintenant()),))
            c.execute(
                "INSERT INTO web_ceremonies (empreinte, type, linux_user, challenge, expire, invitation) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (empreinte(jeton), type_, linux_user, challenge, iso(maintenant() + duree), invitation),
            )
        return jeton

    def prendre_ceremonie(self, jeton: str, type_: str) -> dict[str, Any] | None:
        """La cérémonie (supprimée : un challenge ne sert qu'une fois), None si inconnue, expirée ou d'un autre type."""
        with self.connexion() as c:
            row = c.execute("SELECT * FROM web_ceremonies WHERE empreinte = ?", (empreinte(jeton or ""),)).fetchone()
            if row is None:
                return None
            c.execute("DELETE FROM web_ceremonies WHERE empreinte = ?", (row["empreinte"],))
        if row["type"] != type_ or _expire(row["expire"]):
            return None
        return dict(row)

    # --- sessions ------------------------------------------------------------------------------------------------

    def creer_session(
        self, linux_user: str, duree: timedelta, appareil: str, methode: str, tailscale_login: str | None = None
    ) -> str:
        jeton = nouveau_jeton()
        quand = maintenant()
        with self.connexion() as c:
            c.execute("DELETE FROM web_sessions WHERE expire < ?", (iso(quand),))
            c.execute(
                "INSERT INTO web_sessions (empreinte, linux_user, cree, expire, vu, appareil, methode, tailscale_login) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (empreinte(jeton), linux_user, iso(quand), iso(quand + duree), iso(quand), appareil[:120], methode,
                 (tailscale_login or None) and tailscale_login[:200]),
            )
        return jeton

    def session(self, jeton: str) -> dict[str, Any] | None:
        if not jeton:
            return None
        with self.connexion() as c:
            row = c.execute("SELECT * FROM web_sessions WHERE empreinte = ?", (empreinte(jeton),)).fetchone()
        if row is None or _expire(row["expire"]) or _secours_echue(dict(row)):
            return None
        return dict(row)

    def renouveler_session(self, session_id: int, duree: timedelta) -> None:
        """Session glissante : 30 jours après la dernière visite. Une session de secours n'est jamais prolongée
        (seule sa dernière visite est notée)."""
        quand = maintenant()
        with self.connexion() as c:
            c.execute(
                "UPDATE web_sessions SET vu = ?, expire = CASE WHEN methode = ? THEN expire ELSE ? END WHERE id = ?",
                (iso(quand), configweb.METHODE_SECOURS, iso(quand + duree), session_id),
            )

    def lier_tailscale(self, session_id: int, login: str) -> None:
        """Session ouverte sans identité Tailscale (accès local, ou avant ce durcissement) : liée au premier login
        vu ; jamais remplacé ensuite."""
        with self.connexion() as c:
            c.execute(
                "UPDATE web_sessions SET tailscale_login = ? WHERE id = ? AND tailscale_login IS NULL",
                (login[:200], session_id),
            )

    def sessions_de(self, linux_user: str) -> list[dict[str, Any]]:
        with self.connexion() as c:
            rows = c.execute(
                "SELECT id, linux_user, cree, expire, vu, appareil, methode, tailscale_login FROM web_sessions "
                "WHERE linux_user = ? AND expire > ? ORDER BY vu DESC",
                (linux_user, iso(maintenant())),
            ).fetchall()
        return [dict(r) for r in rows if not _secours_echue(dict(r))]

    def revoquer_session(self, linux_user: str, session_id: int) -> bool:
        with self.connexion() as c:
            cur = c.execute("DELETE FROM web_sessions WHERE id = ? AND linux_user = ?", (session_id, linux_user))
            return cur.rowcount > 0

    def revoquer_tout(self, linux_user: str) -> int:
        with self.connexion() as c:
            return c.execute("DELETE FROM web_sessions WHERE linux_user = ?", (linux_user,)).rowcount

    def accorder_step_up(self, session_id: int, duree: timedelta) -> tuple[str, str]:
        jeton = nouveau_jeton()
        expire = iso(maintenant() + duree)
        with self.connexion() as c:
            c.execute(
                "UPDATE web_sessions SET step_up_empreinte = ?, step_up_expire = ? WHERE id = ?",
                (empreinte(jeton), expire, session_id),
            )
        return jeton, expire

    def step_up_valide(self, session: dict[str, Any], jeton: str | None) -> bool:
        """Jeton de step-up de CETTE session, non expiré (comparaison à temps constant des empreintes)."""
        attendu = session.get("step_up_empreinte")
        if not jeton or not attendu or _expire(session.get("step_up_expire")):
            return False
        return secrets.compare_digest(empreinte(jeton), str(attendu))

    def consommer_step_up(self, session_id: int, jeton: str | None) -> tuple[str, str] | None:
        """Step-up à usage unique (écritures du coffre) : retire le jeton de la session, atomiquement — de deux
        requêtes concurrentes avec le même jeton, une seule l'obtient. Renvoie ``(empreinte, expiration)`` pour
        :meth:`restituer_step_up`, ou None si le jeton n'est pas (ou plus) celui de la session."""
        if not jeton:
            return None
        attendu = empreinte(jeton)
        with self.connexion() as c:
            row = c.execute(
                "SELECT step_up_empreinte, step_up_expire FROM web_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None or not row["step_up_empreinte"] or _expire(row["step_up_expire"]) \
                    or not secrets.compare_digest(attendu, str(row["step_up_empreinte"])):
                return None
            cur = c.execute(
                "UPDATE web_sessions SET step_up_empreinte = NULL, step_up_expire = NULL "
                "WHERE id = ? AND step_up_empreinte = ?",
                (session_id, attendu),
            )
            if cur.rowcount != 1:
                return None
        return attendu, str(row["step_up_expire"])

    def restituer_step_up(self, session_id: int, consomme: tuple[str, str]) -> None:
        """Rend le jeton consommé quand rien n'a été écrit (saisie refusée par la validation) ; jamais par-dessus
        un step-up plus récent."""
        with self.connexion() as c:
            c.execute(
                "UPDATE web_sessions SET step_up_empreinte = ?, step_up_expire = ? "
                "WHERE id = ? AND step_up_empreinte IS NULL",
                (consomme[0], consomme[1], session_id),
            )

    # --- codes à usage unique (repli par Telegram) ---------------------------------------------------------------

    def creer_code(self, linux_user: str, duree: timedelta) -> str | None:
        """Code à 6 chiffres ; None si un code a été demandé il y a moins d'une minute (anti-rafale)."""
        quand = maintenant()
        with self.connexion() as c:
            row = c.execute("SELECT cree FROM web_codes WHERE linux_user = ?", (linux_user,)).fetchone()
            if row is not None:
                try:
                    if quand - datetime.fromisoformat(row["cree"]) < timedelta(minutes=1):
                        return None
                except ValueError:
                    pass
            code = f"{secrets.randbelow(1_000_000):06d}"
            c.execute(
                "INSERT OR REPLACE INTO web_codes (linux_user, empreinte, expire, essais, cree) VALUES (?, ?, ?, 0, ?)",
                (linux_user, empreinte(f"{linux_user}:{code}"), iso(quand + duree), iso(quand)),
            )
        return code

    def verifier_code(self, linux_user: str, code: str, essais_max: int) -> bool:
        with self.connexion() as c:
            row = c.execute("SELECT * FROM web_codes WHERE linux_user = ?", (linux_user,)).fetchone()
            if row is None or _expire(row["expire"]) or int(row["essais"]) >= essais_max:
                return False
            if secrets.compare_digest(empreinte(f"{linux_user}:{(code or '').strip()}"), str(row["empreinte"])):
                c.execute("DELETE FROM web_codes WHERE linux_user = ?", (linux_user,))
                return True
            c.execute("UPDATE web_codes SET essais = essais + 1 WHERE linux_user = ?", (linux_user,))
            return False

    # --- pièces jointes ------------------------------------------------------------------------------------------

    def ajouter_piece(self, linux_user: str, chemin: Path, nom: str, taille: int) -> str:
        pid = f"f-{secrets.token_hex(8)}"
        with self.connexion() as c:
            c.execute(
                "INSERT INTO web_pieces (id, linux_user, chemin, nom, taille, cree) VALUES (?, ?, ?, ?, ?, ?)",
                (pid, linux_user, str(chemin), nom[:120], taille, iso(maintenant())),
            )
        return pid

    def pieces(self, linux_user: str, ids: list[str]) -> list[dict[str, Any]]:
        """Pièces de CETTE personne, dans l'ordre demandé (les ids inconnus ou d'une autre personne sont ignorés)."""
        if not ids:
            return []
        with self.connexion() as c:
            rows = {
                r["id"]: dict(r)
                for r in c.execute(
                    f"SELECT * FROM web_pieces WHERE linux_user = ? AND id IN ({','.join('?' * len(ids))})",
                    (linux_user, *ids),
                ).fetchall()
            }
        return [rows[i] for i in ids if i in rows]
