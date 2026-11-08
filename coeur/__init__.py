"""Cœur de la conversation, partagé par les canaux Telegram (``bot/``) et appli web (``web/``) — lot 4a.

- :mod:`coeur.claude` : commande ``claude -p`` (wrapper, clé API), prompt système par personne, flux ``stream-json`` ;
- :mod:`coeur.conversation` : un échange complet (verrou par personne, session Claude, étapes, cartes,
  confirmations, historique, journal) rendu sous forme d'événements indépendants du canal ;
- :mod:`coeur.confirmations` : motif ``CONFIRMATION_REQUISE``, exécution d'une action validée ;
- :mod:`coeur.cartes` : contrat des cartes génératives (CONTRACT §14) ;
- :mod:`coeur.historique` : conversations et messages (``<etat>/appli.sqlite``) ;
- :mod:`coeur.fichiers`, :mod:`coeur.transcription`, :mod:`coeur.synthese`, :mod:`coeur.brief`.

Aucun module de ce paquet n'importe Telegram.
"""
