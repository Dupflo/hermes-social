# Guide utilisateur — Hermes Social

Bienvenue dans le guide de configuration complet de **Hermes Social**,
la suite d'automatisation multi-plateforme pour les réseaux sociaux.

## Prérequis

- Un **VPS** (serveur Linux) avec Docker installé
- Un **nom de domaine** (OVH ou autre registrar)
- Un compte **Meta Developer** (Facebook)
- Un compte **Google Cloud** (pour YouTube)
- Environ 30 minutes de configuration initiale

## Par où commencer ?

Si vous partez de zéro, suivez ces étapes dans l'ordre :

### 1. Déploiement du serveur

```bash
git clone https://github.com/Dupflo/hermes-social
cd hermes-social/deploy
cp .env.example .env
vim .env                 # renseignez vos clés
docker compose up -d     # démarre tout le stack
```

### 2. Configuration du domaine

Suivez le guide [Domaine et DNS](setup-domain.md) pour :
- Acheter et configurer un nom de domaine chez OVH
- Pointer le DNS vers votre VPS
- Mettre en place le HTTPS avec Caddy ou Traefik

### 3. Configuration des plateformes

| Guide | Pour | Temps estimé |
|-------|------|-------------|
| [Meta (Facebook/Instagram)](setup-meta.md) | Webhook commentaires, réponses publiques, DM privés | 20 min |
| [TikTok + Camofox](setup-tiktok.md) | Navigation automatisée, DM vérifiés, anti-doublon | 15 min |
| [YouTube](setup-youtube.md) | API + OAuth, réponses publiques avec lien | 10 min |

## Architecture du système

Le guide [Architecture](architecture.md) explique le fonctionnement global :
comment les trois conteneurs Docker interagissent et comment les crons
automatisent le traitement des commentaires.

## Fonctionnement résumé

```text
                     ┌──────────────────┐
  Commentaire ────── │     Webhook      │ ──► Meta (FB/IG)
  entrant            │   (temps réel)   │
                     └──────────────────┘

                     ┌──────────────────┐
  Scan périodique ── │  Camofox (TikTok)│ ──► DM + vérification
  (15 min)           │  navigateur X11  │      anti-doublon
                     └──────────────────┘

                     ┌──────────────────┐
  Scan périodique ── │ YouTube Data API │ ──► Réponse publique
  (15 min)           │  + OAuth 2.0     │      avec lien
                     └──────────────────┘
```

## Crons actifs

| Cron | Intervalle | Action |
|------|-----------|--------|
| TikTok auto-DM | 15 min | Nouveaux commentaires → DM automatisé |
| YouTube reply | 15 min | Nouveaux commentaires → réponse publique |
| TikTok fallback | 30 min | Vérifie réponses/DM sur les fallbacks |
| Mise à jour | quotidien | Vérifie nouvelle version GitHub |

Les crons sont **silencieux** quand il n'y a rien à signaler.
Vous recevez une notification Telegram uniquement en cas d'action.

## Sécurité

- **Aucun secret dans le dépôt** — tout est dans `.env` (gitignoré)
- **Aucun port exposé au public** — SSH tunnel uniquement
- **Aucun lien public posté** sur TikTok (DM uniquement)
- **Vérification anti-doublon** avant chaque envoi DM

Si vous avez des questions, ouvrez une issue sur GitHub.
