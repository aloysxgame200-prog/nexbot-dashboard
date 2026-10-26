# Nexbot — le bot

Le bot et le Dashboard partagent **la même base de données** (Neon). Ce que tu règles sur le Dashboard est appliqué par le bot en moins de 10 secondes, sans redémarrage.

## 1. Préparer l'application Discord (une seule fois)

Portail développeur → ton application → **Bot** :
- Active **Server Members Intent** et **Message Content Intent** (obligatoires : anti-raid, filtres, logs de messages).
- Le lien d'invitation du Dashboard (`/invite`) demande déjà les bonnes permissions. Après l'invitation, place le rôle **Nexbot le plus haut possible** dans Paramètres du serveur → Rôles : Discord interdit au bot d'agir sur les membres dont le rôle est au-dessus du sien.

## 2. Lancer le bot sur Windows

1. Installe **Python 3.12 ou plus** depuis python.org (coche « Add python.exe to PATH »).
2. Décompresse le dossier du bot, ouvre-le, clique dans la barre d'adresse de l'explorateur, tape `cmd` puis Entrée.
3. Installe les dépendances (une seule fois) :
   ```
   pip install -r requirements-bot.txt
   ```
4. Copie `.env.example` en `.env` et ouvre-le avec le Bloc-notes. Remplis au minimum :
   ```
   NEXBOT_ENV=production
   DISCORD_BOT_TOKEN=ton token (portail → Bot → Reset Token)
   DATABASE_URL=la MÊME adresse Neon que sur Vercel
   DASHBOARD_URL=https://nexbot-dashboard-nexbotsite.vercel.app
   DISCORD_CLIENT_ID=1560347251614679120
   ```
   Le `.env` contient tes secrets : ne l'envoie jamais à personne et ne le mets jamais sur GitHub.
5. Double-clique sur **`run_bot.bat`**. Quand tu vois « Connecté en tant que Nexbot… », le bot est en ligne.
6. Sur Discord, tape **`*sync`** : les commandes slash sont publiées (jusqu'à une heure pour apparaître partout ; `*sync here` les affiche tout de suite sur le serveur actuel).

Le bot reste en ligne tant que la fenêtre est ouverte et le PC allumé. Pour un fonctionnement 24 h/24, il faut un serveur qui reste allumé (VPS, hébergeur de bots…) : on pourra s'en occuper ensuite, le code ne change pas.

## 2 bis. Héberger le bot 24 h/24 (recommandé : depuis GitHub)

Branche ton hébergeur sur le dépôt GitHub `aloysxgame200-prog/nexbot-dashboard` plutôt que d'envoyer des fichiers à la main : chaque mise à jour fusionnée sur GitHub (dont celles du lundi) arrive alors sur le bot au prochain redéploiement.

| Réglage de l'hébergeur | Valeur |
|---|---|
| Dépôt | `aloysxgame200-prog/nexbot-dashboard`, branche `main` |
| Dossier racine | `nexbot-site (3)` |
| Installation | `pip install -r requirements.txt` (souvent automatique) |
| Démarrage | `python start_bot.py` |
| Variables | `DISCORD_BOT_TOKEN`, `DATABASE_URL` (la même que sur Vercel), `NEXBOT_ENV=production`, `DASHBOARD_URL`, `DISCORD_CLIENT_ID`, `SECRET_KEY` |

Active le redéploiement automatique à chaque push si l'hébergeur le propose ; sinon, clique sur « Redeploy » après avoir fusionné une mise à jour.

## 3. Commandes propriétaire `*` (uniquement l'ID 598080343958093831)

Pour toute autre personne, ces commandes n'existent pas : aucune réponse, aucune réaction, aucun message d'erreur. Le filtrage se fait à trois endroits indépendants dans le code.

| Commande | Rôle |
|---|---|
| `*help` | Liste complète |
| **`*stats`** | **Combien de personnes utilisent Nexbot** : serveurs, membres, personnes uniques, utilisateurs actifs jour / 7 j / 30 j, commandes utilisées, croissance, Premium, connectés au Dashboard, top commandes |
| `*growth [jours]` | Arrivées / départs de Nexbot sur des serveurs, jour par jour |
| `*cmdstats [jours]` | Classement complet des commandes |
| `*globalinfo` · `*guilds [page]` · `*guildinfo <id>` | Informations générales, liste des serveurs, détail d'un serveur |
| `*status [online/idle/dnd/invisible]` | Statut |
| `*activity` · `*playing` · `*watching` · `*listening` · `*competing <texte>` | Activité |
| `*streaming <url twitch/youtube> <texte>` · `*presence reset` | Activité « En direct », présence par défaut |
| `*maintenance on [message]` / `off` | Suspend les commandes publiques (la protection continue de tourner) |
| `*module <nom> on/off` · `*modules` | Couper un module sur tous les serveurs |
| `*premium grant <serveur> <jours>` · `revoke` · `list` · `info` | Offrir / retirer le Premium à la main |
| `*blacklist user/guild <id> [raison]` · `remove` · `list` | Interdire Nexbot à un utilisateur ou un serveur |
| `*leave <id>` | Quitter un serveur (avec confirmation) |
| `*sync [here/global/clear]` · `*reloadconfig` · `*reload [module]` | Technique |
| `*jobs` · `*db` · `*errors` · `*ping` · `*uptime` · `*invite` | Diagnostic |
| `*shutdown` · `*restart` | Arrêt propre / redémarrage (avec `run_bot.bat`) |

Un **utilisateur actif** = une personne qui a utilisé une commande, un bouton, un ticket ou un signalement de Nexbot. Seul l'identifiant et le jour sont enregistrés (90 jours maximum), jamais le contenu des messages.

## 4. Ce que fait le bot

- **Modération** : `/ban` (définitif ou temporaire), `/unban`, `/kick`, `/timeout`, `/untimeout`, `/warn`, `/unwarn`, `/clearwarns` (retire tous les avertissements actifs d'un membre, l'historique reste visible), `/warnings`, `/history`, `/case`, `/clear`, `/slowmode`, `/note add|list`. La hiérarchie des rôles est vérifiée, le membre est prévenu en MP, tout est enregistré et loggé. L'escalade est automatique (X avertissements → timeout / expulsion / ban).
- **Filtre automatique** : invitations, liens (domaines autorisés), mots interdits (accents, leet speak et espaces contournés), mentions de masse, spam, messages répétés, majuscules (Premium), exclusions par rôle et par salon. Les **messages modifiés** sont aussi revérifiés (option gratuite, activée par défaut) : impossible d'envoyer un message propre puis de le modifier pour contourner le filtre.
- **Anti-raid** : arrivées massives → mode protection renforcée (quarantaine, expulsion ou verrouillage), comptes trop récents, bots ajoutés sans autorisation.
- **Anti-nuke**, via le journal d'audit en temps réel : suppressions ou créations de salons et de rôles en rafale, bans ou expulsions en masse, webhooks, permissions dangereuses. Réaction graduée : alerte → retrait des rôles → ban (seulement si tu l'as confirmé dans le Dashboard). Utilisateurs de confiance exemptés.
  - Premium : récidives, alertes en MP au propriétaire, spam de mentions coordonné, **recréation automatique** des salons et rôles supprimés.
  - Commandes : `/raidmode`, `/lockdown`, `/unlock`, `/quarantine`, `/unquarantine`, `/security`.
- **Logs** par catégorie avec l'auteur de chaque action, couleurs personnalisables (Premium), durée de conservation réglable.
- **Bienvenue / départ**, avec embed et MP en Premium ; **rôles automatiques** (délai en Premium, attente du dépistage d'adhésion Discord).
- **Tickets** : panneau à bouton ou menu de catégories (Premium), salon privé, transcription `.txt` à la fermeture, fermeture automatique après inactivité (Premium). Si un salon de ticket est supprimé à la main, le ticket est clôturé automatiquement : le membre n'est plus bloqué par la limite de tickets ouverts. Un double clic sur « Fermer » ne produit plus deux transcriptions.
- **Suggestions** (votes + validation du staff avec commentaire) et **signalements** (`/report` ou clic droit sur un message).
- **Automatisations** du Dashboard, avec un bouton « Tester » qui simule sans agir, et les **annonces programmées** (Premium).

Limites honnêtes :
- aucun bot ne peut empêcher une action avant que Discord l'exécute ;
- aucun bot ne peut agir sur le propriétaire du serveur ou au-dessus de son propre rôle ;
- aucun bot ne peut restaurer des messages supprimés ;
- les actions d'automatisation avec un délai sont perdues si le bot redémarre pendant l'attente.

## 5. Tests

```
pip install -r requirements-dev.txt
python -m pytest -q
```
