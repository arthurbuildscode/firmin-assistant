# Capacité « Administration » (lot 7a)

Régler le système en parlant à l'assistant, pour les **propriétaires** du foyer (`role: proprietaire` dans
`personnes[]` de `config.yaml`, valeur par défaut d'une personne listée ; `membre` et `invite` ne peuvent pas).
Il s'agit de configuration : aucun code n'est écrit par l'assistant (ce sera le lot 7b « atelier »).

- Outils `admin_*` (préfixe déclaré par `prefixe: admin` dans le manifeste) : `admin_etat`, `admin_capacite_activer`,
  `admin_capacite_desactiver`, `admin_validation`, `admin_cron`, `admin_parametre`, `admin_reconnexion`,
  `admin_pause`, `admin_reprendre`, `admin_journal`, `admin_annuler`. Les changements passent par le bouton Valider.
- Logique : `tools/administration.py` (aussi utilisée par `/admin`) ; écriture de `config.yaml` :
  `tools/config_edition.py` (liste blanche de clés, validation par `bot.config.parse_config`, sauvegarde
  `config.yaml.bak-<horodatage>` ×10, écriture atomique, journal `data/administration/journal.jsonl`).
- Horaires en français → format des crons : `tools/horaires.py` (« mercredi 12h » → `Wed 12:00`).
- Prise en compte : le bot relit `config.yaml` à chaque message ; les timers systemd sont régénérés par
  `homeos-appliquer-config.path` → `scripts/appliquer-config.sh` (root) dès que `config.yaml` ou
  `data/capacites/etat.json` change.
- Validation automatique de l'administration elle-même (`capacites.administration.validation: automatique`) :
  possible mais **déconseillée** (un réglage mal compris serait appliqué sans relecture ; le journal permet
  toujours d'annuler).
- L'administration ne peut pas se désactiver elle-même par la conversation (CLI :
  `python -m tools.capacites disable administration`).
