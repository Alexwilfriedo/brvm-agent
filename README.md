# BRVM Agent

Agent de veille et d'analyse automatisée de la **BRVM** (Bourse Régionale des Valeurs Mobilières de l'UEMOA). Collecte chaque matin les cotations et l'actualité, les analyse avec Claude (Sonnet 4.6 + Opus 4.7), et envoie un brief structuré par **email** et **WhatsApp**.

> ⚠️ **Aide à la décision uniquement.** Cet outil ne constitue pas un conseil financier et aucune recommandation réglementée. Consulte toujours un conseiller financier agréé avant d'engager des fonds.

## Architecture

```
FastAPI (Railway Web Service)
  ├── APScheduler interne → cron lu depuis PostgreSQL
  ├── Pipeline quotidien : collecte → enrichissement Sonnet → synthèse Opus → livraison
  ├── PostgreSQL (Railway) : sources, cotations, news, briefs, signaux
  └── API admin (/api/sources, /api/schedule, /api/briefs) pour l'UI future
```

Le service tourne **en permanence** sur Railway. À l'heure programmée (8h Abidjan par défaut), le scheduler déclenche le pipeline. Pour changer le cron ou les sources, tu n'as pas à redéployer : il suffit d'appeler l'API admin.

## Structure du projet

```
brvm-agent/
├── src/
│   ├── main.py              # FastAPI + lifespan scheduler
│   ├── config.py            # Pydantic settings
│   ├── database.py          # SQLAlchemy session
│   ├── models.py            # 6 tables : sources, quotes, news, briefs, signals, schedule
│   ├── scheduler.py         # APScheduler wrapper avec reload à chaud
│   ├── pipeline.py          # Orchestrateur du brief quotidien
│   ├── collectors/          # Extensible : BRVM officiel + Sika Finance RSS
│   ├── analysis/            # Sonnet 4.6 (enrichissement) + Opus 4.7 (synthèse)
│   ├── delivery/            # Brevo SMTP + Brevo WhatsApp
│   └── api/                 # Endpoints admin (CRUD sources, cron, briefs)
├── prompts/
│   ├── enrichment.md        # Prompt Sonnet
│   └── synthesis.md         # Prompt Opus
├── requirements.txt
├── railway.json             # Config deploy Railway
├── .env.example
└── README.md
```

## Setup local (test avant deploy)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Remplir .env avec tes clés (voir section ci-dessous)

# DB locale : docker run -d --name brvm-pg -e POSTGRES_PASSWORD=dev -p 5432:5432 postgres:16
# Puis dans .env : DATABASE_URL=postgresql://postgres:dev@localhost:5432/postgres

uvicorn src.main:app --reload
# Swagger UI : http://localhost:8000/docs
```

## Deploy Railway

1. **Push le repo sur GitHub** (Railway déploie depuis Git)
2. **Créer un projet Railway** → "Deploy from GitHub repo"
3. **Ajouter un plugin PostgreSQL** : `+ New` → `Database` → `PostgreSQL`. Railway injecte automatiquement `DATABASE_URL` dans les variables du service
4. **Configurer les variables d'env** (Settings → Variables) — voir section suivante
5. **Deploy** : Railway détecte Nixpacks + `railway.json`, build et lance `uvicorn src.main:app`

Le healthcheck sur `/health` confirme que tout tourne.

## Variables d'environnement

### Claude API (facturation séparée de ton Max)

Crée une clé sur [console.anthropic.com](https://console.anthropic.com) et ajoute des crédits.

```
ANTHROPIC_API_KEY=sk-ant-xxx
MODEL_ENRICHMENT=claude-sonnet-4-6
MODEL_SYNTHESIS=claude-opus-4-7
```

### Brevo (email SMTP)

1. Crée un compte [Brevo](https://www.brevo.com)
2. SMTP & API → **SMTP** : copie login + smtp key
3. Valide un domaine d'envoi (DKIM/SPF) pour ne pas tomber en spam

```
BREVO_SMTP_USER=xxxxx@smtp-brevo.com
BREVO_SMTP_PASSWORD=xxxxx
EMAIL_FROM=brvm-agent@tondomaine.ci
EMAIL_TO=toi@tondomaine.ci
```

### Brevo (WhatsApp Business)

1. Brevo → **Conversations → WhatsApp** : configure ton numéro sender (numéro dédié, validation Meta 1-3 jours)
2. Crée un **template** approuvé par Meta (obligatoire pour les envois sortants) avec une variable `{{brief_text}}`
3. Récupère l'ID du template

```
BREVO_API_KEY=xkeysib-xxxxx
WHATSAPP_SENDER_NUMBER=+2250700000000
WHATSAPP_TO_NUMBER=+2250700000000
WHATSAPP_TEMPLATE_ID=123
```

Si tu ne configures pas WhatsApp, le pipeline continue avec email seul.

### Scheduler et admin

```
TIMEZONE=Africa/Abidjan
DEFAULT_CRON=0 8 * * *
ADMIN_API_TOKEN=<génère une chaîne aléatoire longue>
```

## Utiliser l'API admin

Toutes les routes `/api/*` demandent le header `X-Admin-Token: <ton token>`.

```bash
BASE=https://ton-service.up.railway.app
TOKEN=ton-admin-token

# Lister les sources
curl -H "X-Admin-Token: $TOKEN" $BASE/api/sources

# Ajouter une source RSS
curl -X POST -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"key":"financial_afrik","name":"Financial Afrik","type":"rss","url":"https://financialafrik.com/feed/","config":{"lookback_hours":36}}' \
  $BASE/api/sources

# Désactiver une source
curl -X PATCH -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"enabled":false}' \
  $BASE/api/sources/2

# Changer le cron (à 9h au lieu de 8h)
curl -X PATCH -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"cron_expression":"0 9 * * *"}' \
  $BASE/api/schedule

# Déclencher un brief MAINTENANT (pour tester)
curl -X POST -H "X-Admin-Token: $TOKEN" $BASE/api/schedule/run-now

# Consulter les briefs
curl -H "X-Admin-Token: $TOKEN" $BASE/api/briefs
curl -H "X-Admin-Token: $TOKEN" $BASE/api/briefs/1
```

Swagger UI interactif : `$BASE/docs` (pour auth, clique "Authorize" et colle ton token).

## Personnalisation

**Profil investisseur** (variable `INVESTOR_PROFILE`) : ce texte est injecté dans le prompt Opus. Adapte-le à ta stratégie réelle (long terme, dividendes, secteurs préférés, taille de ticket, etc.). Plus il est précis, plus les suggestions seront pertinentes.

**Ajouter des sources** : crée un nouveau fichier dans `src/collectors/` qui hérite de `Collector`, enregistre-le dans `collectors/registry.py`, puis ajoute la config via l'API. Pour un simple flux RSS, pas de nouveau code : utilise `type="rss"` et donne l'URL via l'API.

**Prompts** : édite `prompts/enrichment.md` et `prompts/synthesis.md`. Changements pris en compte au prochain run (pas besoin de redéployer si tu montes un volume, sinon redéploie).

## Extension : UI d'administration (phase 2)

Tous les endpoints admin sont prêts. Tu peux construire l'UI avec n'importe quelle stack :

- **Quick win** : Retool, Appsmith (no-code, connecte l'API REST en quelques minutes)
- **Custom** : Next.js ou SvelteKit hébergé sur Vercel/Railway, qui consomme les mêmes endpoints

## Limitations connues

1. Le scraping BRVM officiel dépend de la structure HTML de `brvm.org`. Si elle change, adapte `src/collectors/brvm_official.py` (méthode `_parse_quotes`).
2. Certains sites ouest-africains bloquent le scraping — teste chaque source avant de l'activer en prod.
3. La première semaine, l'historique en DB est vide, donc les briefs sont moins contextualisés. C'est normal et ça s'améliore seul.
4. Pas encore de backtest automatique : les prix à T+30j des signaux sont stockés mais l'analyse rétrospective reste à coder (phase 2).

## Coût mensuel estimé

- Railway : ~5$/mois (service + PostgreSQL starter)
- Claude API : ~15-25$/mois avec stratégie Sonnet + Opus pour synthèse
- Brevo : gratuit jusqu'à 300 emails/jour et WhatsApp au démarrage

**Total ~25-35$/mois** en rythme de croisière.
