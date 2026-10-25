# Nexbot

Bot Discord public en Python (discord.py) avec Dashboard web (FastAPI), anti-raid avancé, modération complète, version gratuite et Premium.
Serveur de support : https://discord.gg/BFDZ7577rZ

> **État du projet : Dashboard en ligne + bot complet.**
> Le bot (`nexbot/bot/`) est documenté dans **README-BOT.md** (installation, commandes `*`, fonctionnalités, limites).
> Reste à faire : hébergement 24 h/24 du bot, Stripe (section 5), stratégie de promotion.

## Architecture

```
nexbot/
├── app.py                     # point d'entrée Vercel (FastAPI détecté automatiquement)
├── requirements.txt / .env.example / .python-version
├── nexbot/
│   ├── core/                  # PARTAGÉ par le bot et le Dashboard
│   │   ├── config.py          # configuration centralisée (variables d'environnement)
│   │   ├── security.py        # ID propriétaire (constante), is_owner(), permissions, chiffrement
│   │   ├── db.py / models.py  # SQLAlchemy : SQLite en local, PostgreSQL en production
│   │   ├── migrations.py      # migrations versionnées et idempotentes
│   │   ├── schemas.py         # paramètres de chaque module (+ métadonnées UI / Premium)
│   │   ├── settings_store.py  # lecture/écriture + règles Premium (valeurs conservées à l'expiration)
│   │   ├── premium.py         # droits Premium, limites gratuit / Premium, période de grâce
│   │   ├── billing.py         # Stripe : checkout, portail, webhooks signés et idempotents
│   │   ├── embeds.py          # limites Discord, variables dynamiques, rendu des embeds
│   │   ├── automations.py     # déclencheurs / conditions / actions (liste fermée, aucun code exécuté)
│   │   └── jobs.py            # file d'actions Dashboard → bot, statistiques, état du bot
│   └── dashboard/
│       ├── app.py             # FastAPI, en-têtes de sécurité, gestion des erreurs
│       ├── auth.py            # OAuth2 Discord, sessions, CSRF, contrôle d'accès par serveur
│       ├── discord_api.py     # client API Discord (OAuth + lecture salons/rôles avec le token du bot)
│       ├── routes/pages.py    # pages HTML + /login /callback /logout /invite /support
│       ├── routes/api.py      # API JSON
│       ├── templates/ static/ # interface (HTML, CSS, JS sans framework)
└── tests/                     # pytest (API Discord simulée, aucun appel réseau)
```

**Synchronisation bot ↔ Dashboard** : une seule base de données. Le Dashboard écrit `guild_settings`, `automations`, `scheduled_messages`, `bot_jobs` ; le bot les lit (cache court invalidé par `updated_at`), exécute les `bot_jobs` (envoi d'embed, panneau de tickets, test d'automatisation) et écrit `guilds`, `sanctions`, `security_incidents`, `daily_stats`, `heartbeats`.

## 1. Créer l'application Discord

1. https://discord.com/developers/applications → **New Application** → nom « Nexbot ».
2. **OAuth2** : copiez le *Client ID* et le *Client Secret*. Dans **Redirects**, ajoutez :
   - `http://localhost:8000/callback` (local)
   - `https://VOTRE-PROJET.vercel.app/callback` (Vercel)
3. **Bot** : *Reset Token* → copiez le token. Activez les intents privilégiés **Server Members Intent** et **Message Content Intent** (nécessaires à l'anti-raid, aux filtres et aux logs de messages).
4. Lien d'invitation : il est généré automatiquement par le Dashboard (`/invite`) avec uniquement les permissions nécessaires (pas « Administrateur »).

## 2. Lancer en local (gratuit)

```bash
python -m venv .venv && source .venv/bin/activate   # Windows : .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # puis remplissez DISCORD_* et SECRET_KEY
python -m nexbot.core.migrations
uvicorn nexbot.dashboard.app:app --reload --port 8000
```
Ouvrez http://localhost:8000.

## 3. Déployer le Dashboard sur Vercel (gratuit)

1. **Base PostgreSQL gratuite** : créez un projet sur https://neon.tech et copiez la *connection string* (`postgresql://…?sslmode=require`).
   SQLite ne fonctionne pas sur Vercel (disque non persistant) ; le bot utilisera cette même base.
2. Poussez le dossier sur un dépôt GitHub (le `.gitignore` exclut `.env` et la base locale).
3. https://vercel.com → **Add New Project** → importez le dépôt (preset : *Other*, aucune commande de build).
4. **Settings → Environment Variables** :
   | Variable | Valeur |
   |---|---|
   | `NEXBOT_ENV` | `production` |
   | `SECRET_KEY` | longue chaîne aléatoire |
   | `DATABASE_URL` | URL Neon |
   | `DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET` / `DISCORD_BOT_TOKEN` | depuis le portail Discord |
   | `DASHBOARD_URL` | `https://VOTRE-PROJET.vercel.app` |
5. Déployez, puis ajoutez `https://VOTRE-PROJET.vercel.app/callback` dans les Redirects OAuth2 de Discord.
6. Vérifiez `https://VOTRE-PROJET.vercel.app/healthz`.

Les tables sont créées automatiquement au premier appel.

## 4. Tests

```bash
python -m pytest -q
```
Couvre : connexion OAuth (state), permissions « Gérer le serveur », serveur sans bot, CSRF + origine, isolation des données entre serveurs, validation des champs, options Premium verrouillées, limites gratuites, expiration Premium avec conservation puis réabonnement, impossibilité de simuler le Premium côté client, Premium de test réservé au propriétaire et coupé en production, automatisations (validation, limites, isolation), éditeur de messages, annonces programmées, historique des sanctions, webhooks Stripe (signature, idempotence, paiement échoué, annulation), embeds (limites Discord, URLs, variables), migrations, `is_owner`.

## 5. Premium et Stripe (plus tard)

Sans variables Stripe, le paiement est désactivé proprement et le propriétaire (ID `598080343958093831`) peut activer un **Premium de test** depuis la page Premium d'un serveur (uniquement si `NEXBOT_ENV` ≠ `production`).

Quand vous serez prêt :
1. Stripe → créez un produit « Nexbot Premium » avec un prix récurrent → `STRIPE_PRICE_ID`.
2. Clé secrète → `STRIPE_SECRET_KEY`.
3. Webhook vers `https://VOTRE-PROJET.vercel.app/api/stripe/webhook` avec les événements `checkout.session.completed`, `customer.subscription.created/updated/deleted`, `invoice.paid`, `invoice.payment_failed` → `STRIPE_WEBHOOK_SECRET`.
4. Activez le *Customer portal* dans Stripe (gestion/annulation par l'abonné).

Les droits Premium ne sont accordés que par ces webhooks signés. À l'expiration, les personnalisations restent en base et se réactivent au réabonnement.

## Sécurité

- Aucun secret dans le code ; tokens OAuth chiffrés en base, seul le hash du jeton de session est stocké.
- Chaque route d'un serveur vérifie la permission « Gérer le serveur » via l'API Discord (cache 60 s) et la présence de Nexbot.
- Requêtes de modification : jeton CSRF + contrôle d'origine + limitation de débit.
- En-têtes CSP, `X-Frame-Options: DENY`, HSTS en production.
- L'ID propriétaire est une constante (`nexbot/core/security.py`), jamais lue depuis une variable d'environnement ou une requête. Le Dashboard n'expose **aucun** paramètre global de Nexbot : ceux-ci sont réservés aux commandes `*` du bot.

## Limites connues (honnêtes)

- Sur Vercel, la limitation de débit est par instance serverless (voir `dashboard/ratelimit.py` pour brancher Redis/Upstash).
- Le statut « Nexbot en ligne » et les statistiques n'apparaissent qu'une fois le bot démarré (étape 2).
