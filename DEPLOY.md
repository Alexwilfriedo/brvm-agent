# Déploiement Railway — checklist

> Document opérationnel pour le premier déploiement et les redéploiements.

## 1. Pré-requis (une seule fois)

- [ ] Compte [Railway](https://railway.app) créé, plan Hobby ou supérieur (le plan gratuit s'éteint trop vite pour un scheduler matinal).
- [ ] Compte [Anthropic Console](https://console.anthropic.com) avec crédits chargés (~25-30$ pour le mois).
- [ ] Compte [Brevo](https://www.brevo.com) validé, domaine d'envoi DKIM/SPF configuré (sinon les mails finissent en spam).
- [ ] Compte [Sentry](https://sentry.io) (optionnel mais recommandé — projet "FastAPI").
- [ ] Code poussé sur GitHub.

## 2. Setup Railway (premier déploiement)

### 2.1 Créer le projet
1. Railway → **+ New Project** → **Deploy from GitHub repo** → choisir `brvm-agent`.
2. Railway détecte `railway.json` + Nixpacks Python. Laisser la build auto-démarrer.

### 2.2 Ajouter Postgres
1. Sur le projet → **+ New** → **Database** → **Add PostgreSQL**.
2. Railway injecte automatiquement `DATABASE_URL` dans les variables du service web (via *reference variable*). Vérifier dans **Settings → Variables** du service web : une entrée `DATABASE_URL` doit pointer sur `${{Postgres.DATABASE_URL}}`.

### 2.3 Configurer les variables d'environnement (Service web → Variables)

| Variable | Valeur | Obligatoire |
|---|---|---|
| `ANTHROPIC_API_KEY` | `sk-ant-...` depuis console.anthropic.com | ✅ |
| `MODEL_ENRICHMENT` | `claude-sonnet-4-6` | défaut OK |
| `MODEL_SYNTHESIS` | `claude-opus-4-7` | défaut OK |
| `DATABASE_URL` | auto-injecté par Railway | ✅ |
| `BREVO_SMTP_USER` | login `xxx@smtp-brevo.com` de Brevo → SMTP | ✅ |
| `BREVO_SMTP_PASSWORD` | SMTP key Brevo | ✅ |
| `EMAIL_FROM` | `brief@tondomaine.ci` (domaine DKIM-validé) | ✅ |
| `EMAIL_FROM_NAME` | `BRVM Agent` | défaut OK |
| `EMAIL_TO` | ton email perso | ✅ |
| `BREVO_API_KEY` | vide pour l'instant (WhatsApp phase 2) | optionnel |
| `WHATSAPP_*` | vide pour l'instant | optionnel |
| `TIMEZONE` | `Africa/Abidjan` | défaut OK |
| `DEFAULT_CRON` | `0 8 * * *` (8h Abidjan) | défaut OK |
| `ADMIN_API_TOKEN` | **généré** ci-dessous | ✅ |
| `SENTRY_DSN` | DSN du projet Sentry | recommandé |
| `SENTRY_ENVIRONMENT` | `production` | défaut OK |
| `LOG_LEVEL` | `INFO` | défaut OK |

### 2.4 Générer un admin token
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```
Coller le résultat dans `ADMIN_API_TOKEN`. **Le garder dans un password manager** — il protège toute l'API admin.

### 2.5 Déclencher le déploiement
Push sur `main` → Railway build + deploy. Suivre les logs en temps réel dans l'UI Railway.

## 3. Validation post-deploy

### 3.1 Healthcheck
```bash
BASE=https://<ton-service>.up.railway.app
curl $BASE/health
```
Attendu :
```json
{"status": "ok", "scheduler_running": true, "next_run": "2026-04-21 08:00:00+00:00", "sentry": true}
```

### 3.2 Vérifier les logs de démarrage
Dans Railway → Deployments → View Logs, chercher :
```
=== Démarrage BRVM Agent ===
Sentry activé (env=production)    ← si SENTRY_DSN défini
Seeding des sources par défaut…
Scheduler démarré
Job planifié : cron='0 8 * * *', prochain run = ...
```

### 3.3 Tester le pipeline manuellement
```bash
export TOKEN="<ton ADMIN_API_TOKEN>"
curl -X POST -H "X-Admin-Token: $TOKEN" $BASE/api/schedule/run-now
```
Puis :
```bash
# Suivre l'historique des runs
curl -H "X-Admin-Token: $TOKEN" $BASE/api/runs | jq

# Voir le dernier brief
curl -H "X-Admin-Token: $TOKEN" $BASE/api/briefs | jq '.[0]'
```

**Vérifier** :
- L'email arrive dans la boîte `EMAIL_TO` (regarder aussi les spams)
- `delivery_status: "delivered"` sur le brief
- `status: "success"` sur le run

### 3.4 Valider la charte email en prod
Le endpoint preview n'est pas auth :
```
https://<ton-service>.up.railway.app/preview
https://<ton-service>.up.railway.app/preview/brief?variant=full
https://<ton-service>.up.railway.app/preview/brief/1   ← vrai brief stocké
```

## 4. Plan de rollback

### 4.1 Pipeline qui échoue en boucle
**Couper le cron** (pas besoin de redéployer) :
```bash
# Désactiver complètement
curl -X PATCH -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"enabled": false}' \
  $BASE/api/schedule

# Ou reprogrammer à une date impossible
curl -X PATCH -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"cron_expression": "0 0 31 2 *"}' \
  $BASE/api/schedule
```

### 4.2 Diagnostiquer
```bash
# Les derniers runs avec leur erreur
curl -H "X-Admin-Token: $TOKEN" "$BASE/api/runs?status=failed" | jq

# Détail d'un run
curl -H "X-Admin-Token: $TOKEN" $BASE/api/runs/42 | jq
```

### 4.3 Rollback code
Railway → **Deployments** → identifier le dernier deploy sain → **⋯** → **Rollback to this deployment**.

## 5. Maintenance

### 5.1 Désactiver une source défaillante
```bash
curl -X PATCH -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"enabled": false}' \
  $BASE/api/sources/<id>
```

### 5.2 Ajouter une nouvelle source RSS (zéro code)
```bash
curl -X POST -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"key":"financial_afrik","name":"Financial Afrik","type":"rss",
       "url":"https://financialafrik.com/feed/","config":{"lookback_hours":36}}' \
  $BASE/api/sources
```

### 5.3 Changer l'heure d'envoi
```bash
curl -X PATCH -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"cron_expression": "30 7 * * 1-5"}' \
  $BASE/api/schedule
# → 7h30, lundi-vendredi
```

## 6. Budget mensuel estimé

| Item | Coût |
|---|---|
| Railway (service + Postgres starter) | ~5 $ |
| Claude API (Sonnet + Opus, ~30 runs/mois) | ~15-25 $ |
| Brevo (300 emails/j gratuits) | 0 $ |
| Sentry (plan dev gratuit, 5k erreurs/mois) | 0 $ |
| **Total** | **~20-30 $/mois** |
