"""Liste de courses partagée, avec rayon deviné par un dictionnaire simple."""

from __future__ import annotations

import unicodedata
from typing import Any

from tools import db
from tools.common import iso_now, who

RAYONS: tuple[str, ...] = (
    "fruits-légumes",
    "boulangerie",
    "laitier",
    "viande-poisson",
    "épicerie",
    "surgelés",
    "boissons",
    "hygiène",
    "maison",
    "autre",
)

# Mots-clés (sans accents, minuscules) → rayon. Volontairement court et lisible.
_DICO: dict[str, tuple[str, ...]] = {
    "fruits-légumes": (
        "pomme", "poire", "banane", "orange", "citron", "clementine", "mandarine", "raisin", "fraise", "framboise",
        "kiwi", "peche", "abricot", "cerise", "melon", "pasteque", "ananas", "mangue", "avocat", "tomate",
        "salade", "laitue", "roquette", "epinard", "courgette", "aubergine", "poivron", "concombre", "carotte",
        "pomme de terre", "patate", "oignon", "ail", "echalote", "poireau", "chou", "brocoli", "haricot",
        "petit pois", "champignon", "courge", "potiron", "radis", "navet", "betterave", "celeri", "fenouil",
        "persil", "basilic", "coriandre", "menthe", "ciboulette", "legume", "fruit", "herbes",
    ),
    "boulangerie": ("pain", "baguette", "brioche", "croissant", "viennoiserie", "pain de mie", "biscotte", "tartine"),
    "laitier": (
        "lait", "yaourt", "yogourt", "fromage", "beurre", "creme", "oeuf", "œuf", "camembert", "comte", "emmental",
        "gruyere", "mozzarella", "parmesan", "chevre", "feta", "skyr", "faisselle", "petit suisse", "ricotta",
        "mascarpone", "raclette",
    ),
    "viande-poisson": (
        "poulet", "boeuf", "bœuf", "porc", "veau", "agneau", "steak", "hache", "jambon", "lardon", "saucisse",
        "merguez", "chipolata", "escalope", "rôti", "roti", "cotelette", "dinde", "canard", "saumon", "thon",
        "cabillaud", "crevette", "poisson", "truite", "sardine", "maquereau", "colin", "bacon", "charcuterie",
    ),
    "épicerie": (
        "pates", "pâtes", "riz", "semoule", "quinoa", "lentille", "pois chiche", "farine", "sucre", "sel",
        "poivre", "huile", "vinaigre", "moutarde", "ketchup", "mayonnaise", "sauce", "conserve", "boite",
        "cafe", "the", "tisane", "chocolat", "confiture", "miel", "cereales", "biscuit", "gateau", "chips",
        "noix", "amande", "noisette", "cacahuete", "compote", "epice", "curry", "cumin", "bouillon",
        "levure", "vanille", "cacao", "nutella", "pate a tartiner", "tomate pelee", "coulis", "concentre",
        "mais", "olive", "cornichon", "soupe", "nouille", "couscous", "boulgour", "tortilla", "wrap",
    ),
    "surgelés": ("surgele", "glace", "glaces", "frite", "pizza surgelee", "poisson pane", "batonnet", "sorbet", "congele"),
    "boissons": (
        "eau", "jus", "soda", "coca", "limonade", "sirop", "biere", "vin", "cidre", "champagne", "perrier",
        "boisson", "lait d'amande", "lait d'avoine", "kombucha", "the glace",
    ),
    "hygiène": (
        "savon", "shampoing", "shampooing", "dentifrice", "brosse a dents", "gel douche", "deodorant", "coton",
        "couche", "lingette", "serviette hygienique", "tampon", "rasoir", "mousse a raser", "creme hydratante",
        "papier toilette", "pq", "mouchoir", "coton-tige", "creme solaire", "pharmacie", "doliprane", "pansement",
    ),
    "maison": (
        "lessive", "liquide vaisselle", "eponge", "sac poubelle", "essuie-tout", "sopalin", "produit menager",
        "javel", "nettoyant", "ampoule", "pile", "piles", "aluminium", "film etirable", "papier cuisson",
        "adoucissant", "tablette lave-vaisselle", "croquette", "croquettes", "litiere", "chien", "chat",
        "bougie", "allumette", "filtre a cafe",
    ),
}


def _normaliser(texte: str) -> str:
    sans_accents = unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode("ascii")
    return " ".join(sans_accents.casefold().split())


def deviner_rayon(article: str) -> str:
    """Devine le rayon d'un article ; « autre » si aucun mot-clé ne correspond."""
    norme = _normaliser(article)
    mots = set(norme.split())
    meilleur: tuple[int, str] | None = None
    for rayon, cles in _DICO.items():
        for cle in cles:
            cle_n = _normaliser(cle)
            if " " in cle_n:
                trouve = cle_n in norme
            else:
                trouve = cle_n in mots or any(m.startswith(cle_n) and len(cle_n) >= 4 for m in mots)
            if trouve and (meilleur is None or len(cle_n) > meilleur[0]):
                meilleur = (len(cle_n), rayon)
    return meilleur[1] if meilleur else "autre"


def normaliser_rayon(rayon: str | None) -> str | None:
    if rayon is None:
        return None
    cible = _normaliser(rayon)
    for r in RAYONS:
        if _normaliser(r) == cible or _normaliser(r).startswith(cible):
            return r
    return "autre"


def lister(inclure_coches: bool = True) -> list[dict[str, Any]]:
    """Liste courante, groupée par rayon (ordre des rayons puis alphabétique)."""
    with db.cursor() as conn:
        sql = "SELECT id, article, rayon, coche, ajoute_par, ts FROM courses"
        if not inclure_coches:
            sql += " WHERE coche = 0"
        rows = conn.execute(sql).fetchall()
    articles = db.rows_to_dicts(rows)
    ordre = {r: i for i, r in enumerate(RAYONS)}
    articles.sort(key=lambda a: (ordre.get(a["rayon"], len(RAYONS)), a["article"].casefold()))
    for a in articles:
        a["coche"] = bool(a["coche"])
    return articles


def ajouter(articles: list[str], rayon: str | None = None) -> list[dict[str, Any]]:
    """Ajoute des articles (ignore les doublons non cochés, décoche un doublon coché)."""
    rayon_force = normaliser_rayon(rayon)
    ajoutes: list[dict[str, Any]] = []
    with db.cursor() as conn:
        for brut in articles:
            article = " ".join(str(brut).strip().split())
            if not article:
                continue
            existant = conn.execute(
                "SELECT id, coche FROM courses WHERE article = ? COLLATE NOCASE", (article,)
            ).fetchone()
            r = rayon_force or deviner_rayon(article)
            if existant is not None:
                if existant["coche"]:
                    conn.execute(
                        "UPDATE courses SET coche = 0, rayon = ?, ajoute_par = ?, ts = ? WHERE id = ?",
                        (r, who(), iso_now(), existant["id"]),
                    )
                    ajoutes.append({"id": existant["id"], "article": article, "rayon": r, "statut": "réactivé"})
                else:
                    ajoutes.append({"id": existant["id"], "article": article, "rayon": r, "statut": "déjà présent"})
                continue
            cur = conn.execute(
                "INSERT INTO courses (article, rayon, coche, ajoute_par, ts) VALUES (?,?,0,?,?)",
                (article, r, who(), iso_now()),
            )
            ajoutes.append({"id": cur.lastrowid, "article": article, "rayon": r, "statut": "ajouté"})
    return ajoutes


def cocher(articles: list[str]) -> dict[str, list[str]]:
    """Coche les articles trouvés (par nom, insensible à la casse, correspondance partielle acceptée)."""
    coches: list[str] = []
    introuvables: list[str] = []
    with db.cursor() as conn:
        for brut in articles:
            cible = str(brut).strip()
            if not cible:
                continue
            row = conn.execute(
                "SELECT id, article FROM courses WHERE coche = 0 AND article = ? COLLATE NOCASE", (cible,)
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT id, article FROM courses WHERE coche = 0 AND article LIKE ? ORDER BY length(article) LIMIT 1",
                    (f"%{cible}%",),
                ).fetchone()
            if row is None:
                introuvables.append(cible)
                continue
            conn.execute("UPDATE courses SET coche = 1, ts = ? WHERE id = ?", (iso_now(), row["id"]))
            coches.append(row["article"])
    return {"coches": coches, "introuvables": introuvables}


def marquer(article_id: int, coche: bool) -> dict[str, Any] | None:
    """Coche ou décoche un article par son identifiant, sur place (l'ordre de la liste ne change pas) ;
    None si l'article n'existe pas. Utilisé par l'écran Courses de l'appli (``PATCH /api/courses/{id}``)."""
    with db.cursor() as conn:
        row = conn.execute("SELECT id FROM courses WHERE id = ?", (int(article_id),)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE courses SET coche = ?, ts = ? WHERE id = ?", (1 if coche else 0, iso_now(), row["id"]))
        maj = conn.execute("SELECT id, article, rayon, coche, ajoute_par, ts FROM courses WHERE id = ?",
                           (row["id"],)).fetchone()
    article = dict(maj)
    article["coche"] = bool(article["coche"])
    return article


def vider_coches() -> int:
    """Supprime les articles cochés ; renvoie le nombre supprimé."""
    with db.cursor() as conn:
        cur = conn.execute("DELETE FROM courses WHERE coche = 1")
        return int(cur.rowcount or 0)
