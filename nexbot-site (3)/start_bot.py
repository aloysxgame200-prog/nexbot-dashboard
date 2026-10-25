"""Lance le bot Nexbot : `python start_bot.py`.

Pour un hébergeur branché sur GitHub : dossier racine « nexbot-site (3) »,
commande de démarrage « python start_bot.py ». Chaque mise à jour fusionnée
sur GitHub est alors récupérée au prochain redéploiement.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nexbot.bot.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
