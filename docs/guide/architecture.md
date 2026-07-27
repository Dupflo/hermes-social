# Architecture système — Hermes Social

> Document d'architecture technique — Français  
> Dernière mise à jour : juillet 2026

---

## 1. Vue d'ensemble

```
 ┌─────────────────────────────────────────────────────────────┐
 │                         VPS (Linux)                         │
 │  ┌──────────────────────────────────────────────────────┐   │
 │  │                   Docker Engine                       │   │
 │  │  ┌──────────────┐  ┌──────────────┐  ┌──────────┐  │   │
 │  │  │   hermes-1    │  │ meta-webhook │  │ camofox  │  │   │
 │  │  │               │  │              │  │          │  │   │
 │  │  │  ┌─────────┐  │  │  ┌────────┐  │  │ ┌──────┐ │  │   │
 │  │  │  │ Cron    │  │  │  │Flask   │  │  │ │X11   │ │  │   │
 │  │  │  │ jobs    │  │  │  │server  │  │  │ │VNC   │ │  │   │
 │  │  │  └─────────┘  │  │  └────────┘  │  │ └──────┘ │  │   │
 │  │  │  ┌─────────┐  │  │  ┌────────┐  │  │ ┌──────┐ │  │   │
 │  │  │  │Hermes   │  │  │  │GraphAPI│  │  │ │TikTok│ │  │   │
 │  │  │  │Agent    │  │  │  │client  │  │  │ │API   │ │  │   │
 │  │  │  └─────────┘  │  │  └────────┘  │  │ └──────┘ │  │   │
 │  │  └──────────────┘  └──────────────┘  └──────────┘  │   │
 │  └──────────────────────────────────────────────────────┘   │
 │                                                              │
 │  ┌──────────────────────┐   ┌────────────────────────┐      │
 │  │   Tunnel SSH (Meta) │   │  Git (déploiement)     │      │
 │  │   → webhooks Meta   │   │  ← secrets CI/CD       │      │
 │  └──────────────────────┘   └────────────────────────┘      │
 └─────────────────────────────────────────────────────────────┘
```

---

## 2. Conteneurs

### 2.1 `hermes-1` — Cœur de l'automatisation

| Propriété | Valeur |
|-----------|--------|
| **Base** | Python 3.13 + Hermes Agent |
| **Rôle** | Ordonnancement des tâches, exécution des crons, logique métier |
| **Planificateur** | Cron interne Hermes (configuration YAML) |
| **Dépendances** | `hermes-social` skills, bibliothèques Meta/YouTube |

Contenu :

- **Agent Hermes** — exécute les workflows sociaux (commentaires, modération, publication)
- **Cron jobs** — cycles réguliers pour TikTok, YouTube, fallback et mise à jour
- **Clients API** — Graph API (Meta), YouTube Data API v3

---

### 2.2 `meta-webhook` — Réception des webhooks Meta

| Propriété | Valeur |
|-----------|--------|
| **Base** | Python 3.13 + Flask |
| **Port** | 8443 (interne), exposé via tunnel SSH |
| **Rôle** | Serveur HTTP récepteur des événements Meta (Facebook, Instagram) |

Fonctionnement :

- Point d'entrée unique pour les webhooks envoyés par Meta
- Vérification de signature (`X-Hub-Signature-256`)
- File d'attente des événements entrants (Redis backend)
- Relayage des événements vers `hermes-1` pour traitement

Communication :

```
Meta Platform          Tunnel SSH          meta-webhook        hermes-1
    │                      │                    │                 │
    │── POST /webhook ──►  │── port forwarding ─►│                 │
    │                      │                    │── event ──────► │
    │                      │                    │                 │── traiter
```

---

### 2.3 `camofox` — Interaction TikTok (pilotage navigateur)

| Propriété | Valeur |
|-----------|--------|
| **Base** | Python 3.13 + Playwright + X11 |
| **Rôle** | Automatisation navigateur pour TikTok (API non publique) |
| **Affichage** | X11 virtuel + VNC (debug) |
| **Session** | Headless Chromium piloté par Playwright |

Fonctionnement :

- Connexion TikTok via session persistante (cookies stockés dans `/runtime/sessions/`)
- Navigation, lecture, interaction (commentaires, likes, abonnements)
- X11 headless avec `xvfb` pour le rendu sans écran physique
- Port VNC (5900) accessible en local pour débogage visuel

---

## 3. Couvertures plateformes

### 3.1 Meta (Facebook + Instagram)

| Mécanisme | Technologie | Détail |
|-----------|-------------|--------|
| **Webhook entrant** | Meta Webhooks API | Événements comments, messages, mentions |
| **API sortante** | Graph API v21+ | Publication, reply, modération, stats |
| **Authentification** | OAuth 2.0 + Page Access Token | Token long terme, renouvelé automatiquement |

Flux webhook :

```
[Plateforme Meta]
     │
     │ Événement (commentaire, mention, like...)
     ▼
[meta-webhook] ── POST ──► [Tunnel SSH] ──► [hermes-1]
     │                                               │
     │ Vérification HMAC SHA256                      │ Routage skill
     │ Stockage événement                            │ Analyse LLM
     ▼                                               ▼
  Ack 200                                        Réponse via Graph API
```

### 3.2 TikTok (API non publique via navigateur)

| Mécanisme | Technologie | Détail |
|-----------|-------------|--------|
| **Interaction** | Pilotage Chromium (Playwright) | Commentaires, likes, DMs |
| **Session** | Cookies persistants | Stockés dans `/runtime/sessions/tiktok/` |
| **Affichage** | X11 virtuel (Xvfb) + VNC | Debug visuel sur `camofox:5900` |

Flux TikTok :

```
[hermes-1]
     │
     │ Commande (ex. « répondre au commentaire #42 »)
     ▼
[camofox]
     │
     │ Playwright → Chromium headless (Xvfb/X11)
     │ Session persistante → cookies sauvegardés
     ▼
[TikTok.com]
     │ Navigation, interaction, lecture
     ▼
  Retour résultat vers hermes-1
```

### 3.3 YouTube (Data API)

| Mécanisme | Technologie | Détail |
|-----------|-------------|--------|
| **API** | YouTube Data API v3 | Commentaires, vidéos, chaînes, statistiques |
| **Authentification** | OAuth 2.0 (credentials utilisateur) | Refresh token automatique |
| **Quota** | 10 000 unités/jour (projet standard) | Optimisé par pagination et cache |

Flux YouTube :

```
[hermes-1]
     │
     │ YouTube Data API v3 (OAuth 2.0)
     │ Refresh token → access token
     ▼
[Google APIs]
     │ commentThreads.list, comments.list, comments.insert...
     ▼
  Traitement et réponse via Hermes skills
```

---

## 4. Planification (Crons)

| Tâche | Intervalle | Conteneur | Description |
|-------|-----------|-----------|-------------|
| **TikTok — surveillance** | Toutes les **15 min** | `hermes-1` | Vérifier nouvelles notifications, commentaires, messages |
| **YouTube — surveillance** | Toutes les **15 min** | `hermes-1` | Vérifier nouveaux commentaires, mentions, statistiques |
| **Fallback (secours)** | Toutes les **30 min** | `hermes-1` | Rattrapage si une tâche a échoué, logs consolidation |
| **Mise à jour quotidienne** | **1 fois/jour** (03:00 UTC) | `hermes-1` | Refresh tokens OAuth, rotation logs, nettoyage sessions |

Schéma d'exécution :

```
00:00        :15        :30        :45        :00
 │           │           │           │           │
 ├─ TikTok ──┤                                    TikTok (15 min)
 │           ├─ YouTube ─┤                        YouTube (15 min)
 │                       ├── Fallback ────┤       Fallback (30 min)
 │   03:00                                       Update quotidien
```

---

## 5. Sécurité

### 5.1 Aucun secret dans le dépôt

Tous les secrets (tokens API, clés OAuth, mots de passe, jetons de refresh) sont **exclus du dépôt Git** :

```
.gitignore contient obligatoirement :

  runtime/
  .env
  .env.*
  secrets/
  credentials/
  **/sessions/
  **/cookies/
  tokens.json
  *.pem
  *.key
```

### 5.2 Tunnel SSH pour les webhooks Meta

Les webhooks Meta nécessitent une URL publique HTTPS. La solution retenue :

```
[VPS] ── SSH tunnel ──► [Service tunnel public]
   │                          │
   │                          │ URL publique : https://xxx.ngrok.app/webhook
   │                          │ (ou équivalent : bore, Cloudflare Tunnel)
   ▼                          ▼
meta-webhook:8443      Meta Platform envoie ici
```

- Le tunnel ne s'active que pendant les sessions de travail (pas 24/7 inutile)
- Authentification par clé SSH, pas de mot de passe

### 5.3 Runtime gitignoré

Le dossier `/runtime/` est le seul endroit où les données volatiles persistent :

```
runtime/
├── sessions/          # Cookies navigateur persistants
├── tokens/            # Access & refresh tokens (chiffrés)
├── cache/             # Cache API (réponses, quotas)
├── logs/              # Logs applicatifs (rotation auto)
└── tmp/               # Fichiers temporaires (nettoyés au redémarrage)
```

- **Jamais versionné** — `.gitignore` le bloque à la racine
- **Backup optionnel** — crypté, hors dépôt
- **Rotation** — logs et cache purgés automatiquement (max 7 jours)

### 5.4 Principes généraux

| Principe | Application |
|----------|-------------|
| **Séparation des secrets** | Fichiers `.env` par environnement, jamais commités |
| **Moindre privilège** | Chaque conteneur a ses propres tokens, pas de partage |
| **Chiffrement au repos** | Tokens stockés chiffrés (AES-256-GCM) |
| **Rotation automatique** | OAuth refresh tokens renouvelés avant expiration |
| **Signature HMAC** | Webhooks Meta validés par signature avant traitement |
| **Isolation réseau** | Docker bridge network, seuls les ports nécessaires sont exposés |

---

## 6. Réseau Docker

```
┌──────────────────────────────────────────────────┐
│                    docker network                 │
│                    hermes-net (bridge)             │
│                                                    │
│    hermes-1          meta-webhook       camofox    │
│   ┌────────┐        ┌──────────┐      ┌────────┐  │
│   │:9090   │◄──────►│:8443     │◄────►│:5900   │  │
│   │(health)│        │(webhook) │      │(VNC)   │  │
│   └────────┘        └──────────┘      └────────┘  │
│        ▲                                  ▲        │
│        │ tunnel SSH                       │        │
│        ▼                                  ▼        │
│    Internet  ◄────────────────────────  Internet   │
│    (Meta)                                (TikTok)  │
└────────────────────────────────────────────────────┘
```

- Les conteneurs communiquent via DNS interne Docker (`hermes-1`, `meta-webhook`, `camofox`)
- Aucun port exposé sur l'interface publique du VPS sauf le tunnel SSH
- Le pare-feu (iptables/ufw) bloque tout port entrant sauf SSH (22)

---

## 7. Arborescence du projet

```
hermes-social/
├── .gitignore                  # Secrets, runtime, tokens exclus
├── docker-compose.yml          # Orchestration des 3 conteneurs
├── hermes-1/
│   ├── Dockerfile
│   ├── config.yaml             # Crons, skills, providers
│   └── skills/                 # Skills Hermes (Meta, TikTok, YouTube)
├── meta-webhook/
│   ├── Dockerfile
│   ├── app.py                  # Flask server pour webhooks
│   └── requirements.txt
├── camofox/
│   ├── Dockerfile
│   ├── entrypoint.sh           # Lance Xvfb + Chromium + app
│   └── requirements.txt
├── docker/
│   └── nginx/                  # (optionnel) reverse proxy interne
└── docs/
    └── guide/
        └── architecture.md     # Ce document
```

---

## 8. Déploiement

```bash
# 1. Cloner le dépôt
git clone git@github.com:org/hermes-social.git
cd hermes-social

# 2. Configurer les secrets (hors dépôt)
cp .env.example .env
#   → Éditer .env avec les tokens réels

# 3. Lancer les conteneurs
docker compose up -d --build

# 4. Vérifier l'état
docker compose ps
docker compose logs -f

# 5. Démarrer le tunnel SSH (webhooks Meta)
ssh -R 8443:meta-webhook:8443 user@tunnel.example.com
```

---

## 9. Logs et monitoring

| Type | Destination | Rétention |
|------|-------------|-----------|
| Logs applicatifs | `runtime/logs/` | 7 jours (rotation journalière) |
| Logs Docker | `docker compose logs` | Config Docker (max 10 MB) |
| Métriques health | `/health` sur chaque conteneur | Temps réel, prometheus compatible |

Points de terminaison health :

- `hermes-1` → `http://hermes-1:9090/health`
- `meta-webhook` → `http://meta-webhook:8443/health`
- `camofox` → `http://camofox:5900/health` (via VNC)

---

> **Document d'architecture** — Hermes Social, juillet 2026  
> Sujet à évolution — maintenir à jour avec les décisions techniques.
