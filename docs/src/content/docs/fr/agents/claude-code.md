---
title: Claude Code
description: Le mode client pour Claude Code, avec les abonnements Pro et Max ou des clés d'API, et les interfaces de Claude qui sont couvertes.
---

Claude Code utilise le **mode client** : le plugin Latet masque le contenu à l'intérieur de Claude Code, et
le daemon local ne fait que vérifier. Cela fonctionne avec les abonnements Pro et Max sans clé d'API, et
aussi avec des clés d'API.

## Terminal Windows connecté

Le processus auxiliaire connecté au serveur prend en charge Claude Code dans un terminal
Windows, avec des réglages utilisateur de confiance et la liaison privée installée par Latet.
Cela n'étend pas la prise en charge connectée à Desktop, VS Code, JetBrains, Linux ni macOS.
Sous Linux et macOS, `latet connect`, `latet claude`, `latet agents apply claude` et
`latet global-install` le disent d'emblée, les réglages de Claude Code ne reçoivent pas le
jeton, et le plugin retient chaque prompt ; `latet codex` et `latet opencode` passent par le
serveur. Suivez le [guide de connexion](../../getting-started/server/) pour `latet connect`,
`latet agents apply claude`, le stockage du jeton et `latet disconnect`.

Sur une machine connectée, `LATET_BRIDGE_TOKEN`, dans les réglages et l'environnement de Claude
Code, contient le jeton de cette machine, et chaque commande que Claude Code exécute peut le lire. De
partout où le serveur est joignable, le jeton ouvre le bridge du plugin et le proxy de votre espace :
qui le détient peut faire masquer des textes dans votre espace, faire restaurer les valeurs factices
qu'il possède et envoyer des requêtes par lui. Il n'ouvre jamais votre tableau de bord, où chaque
original peut être affiché, ne change jamais votre identité et ne réduit jamais votre protection : la
seule modification qu'il peut apporter à vos réglages est l'ajout d'une destination pour une adresse,
qui ne réduit rien. Pour le tableau de bord et l'identité, le serveur demande aussi la clé de la
machine, qui reste dans le magasin d'identifiants (ou dans le fichier privé du jeton) et n'est jamais
donnée à un agent. La clé empêche un jeton qui a fuité (depuis l'environnement, les réglages, un
journal ou un seul `curl`) d'ouvrir le tableau de bord ; elle n'arrête pas une commande écrite pour lire
les deux dans le magasin d'identifiants. Sur une machine connectée, `latet dashboard` et
`latet identity send` refusent de s'exécuter dans un agent. Si le jeton a pu fuiter, `latet disconnect`
le révoque.

Sur une machine connectée, le mode d'impression (`claude -p`) n'est pas pris en charge : le
plugin retient ses requêtes. Utilisez le terminal interactif. Quand le plugin retient une
requête, il dit pourquoi : le mode d'impression ou une autre interface ; une liaison du
processus auxiliaire absente ou périmée (`latet agents apply claude`) ; un serveur qui n'a pas
répondu (`latet status`) ; ou un jeton que le serveur n'accepte plus (`latet disconnect`, puis
`latet connect` avec un nouveau code d'un administrateur).
Le plugin masque les descriptions des outils de Claude Code ; sur un serveur, la vérification
finale lit aussi leurs descriptions, schémas de paramètres et exemples, et une valeur restée en
clair arrête la requête ([périmètre de protection](../../concepts/protection-scope/)). Les tableaux de couverture et
instructions du daemon ci-dessous décrivent **Latet local** ; ils n'élargissent pas
cette prise en charge du processus auxiliaire connecté.

## Couverture

| Interface | Plugin | Daemon en vérification seule | Remarques |
| --- | --- | --- | --- |
| CLI de Claude Code (terminal) | ✅ | ✅ | Mode client complet |
| Extensions VS Code et JetBrains | ✅ | ✅ | Le même Claude Code, les mêmes plugins et réglages |
| Onglet Code de Claude Desktop (sessions locales), connecté à un compte Claude | ✅ | ❌ | Plugin seulement : l'application ignore `ANTHROPIC_BASE_URL`, donc aucun daemon ne vérifie derrière le plugin et rien n'est enregistré dans le tableau de bord |
| Chat et Cowork de Claude Desktop, connecté à un compte Claude | ❌ | ❌ | Non couverts : ils ne chargent pas les plugins de Claude Code et ne passent pas par Latet |
| Claude Desktop sur une passerelle tierce : Chat, Cowork et Code | Onglet Code | ✅ | Chaque requête passe par Latet et est vérifiée ; seul l'onglet Code masque |
| Sessions dans le cloud | ❌ | ❌ | Non couvertes : elles s'exécutent sur l'infrastructure d'Anthropic, pas sur votre machine |

**Claude Desktop, deux configurations.** Quand Claude Desktop est connecté à un compte Claude, avec un
abonnement Pro ou Max, l'onglet Code charge le plugin de Latet, qui masque le contenu et fait sa
propre vérification finale avant chaque requête. L'application ignore toutefois `ANTHROPIC_BASE_URL` :
aucun daemon ne vérifie donc la requête derrière le plugin, et rien n'est enregistré dans le tableau
de bord. Chat et Cowork ne chargent pas les plugins de Claude Code et ne passent pas par Latet : ils
ne sont pas couverts.

Sur une passerelle tierce, Claude Desktop n'est pas connecté à un compte Claude, il n'y a donc pas
d'abonnement Pro ou Max : les requêtes sont facturées à l'usage, avec une clé d'API Anthropic ou par le
fournisseur de votre entreprise. Avec Latet comme passerelle, Chat, Cowork et Code envoient chaque
requête par lui. Le moteur de Claude Code construit ces requêtes, si bien que Latet vérifie chacune
d'elles et arrête celle qui transporterait en clair un secret ou une donnée personnelle, mais ne la
masque pas : le [guide des passerelles](https://code.claude.com/docs/en/llm-gateway-protocol)
d'Anthropic demande à une passerelle d'inspecter les requêtes de Claude Code, pas de les modifier.
L'onglet Code bénéficie à la fois du masquage du plugin et de la vérification du daemon. Pour la
configuration, voir [Claude Desktop sur une passerelle](../other-tools/#claude-desktop-sur-une-passerelle).

Au travail, Claude Code passe souvent par la passerelle de votre entreprise, par Amazon Bedrock, Google
Vertex ou Microsoft Foundry : Latet lit cette route dans vos réglages et se place devant elle, clés
comprises. Voir [Points de terminaison d'entreprise](../company-endpoints/).

Testé en conditions réelles : la CLI avec un abonnement. Les extensions pour IDE et l'onglet Code de Claude
Desktop utilisent le même plugin et les mêmes réglages ; si quelque chose n'y fonctionne pas, merci
d'[ouvrir un ticket](https://github.com/Latet-AI/latet/issues). Cursor n'est pas couvert : ses requêtes au
modèle partent des serveurs de Cursor, pas de votre machine.

## Abonnements Pro et Max

Le mode client fonctionne quand Claude Code est connecté avec un abonnement Pro ou Max, sans clé d'API, et
aussi avec une clé d'API. La page [Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)
d'Anthropic fixe la façon dont un abonnement peut être utilisé. Point par point :

| Les conditions d'Anthropic | Ce que fait Latet |
| --- | --- |
| La connexion avec un abonnement est destinée à l'usage ordinaire de Claude Code et des propres applications d'Anthropic. | Votre abonnement n'est utilisé que dans Claude Code. Codex, OpenCode, Hermes Agent et tous les autres agents passent par la passerelle avec leur propre connexion : une clé d'API, ou la connexion ChatGPT de Codex. |
| Le binaire de Claude Code ne doit pas être modifié. | Claude Code s'exécute tel qu'Anthropic le publie. Latet ajoute un plugin, au moyen des commandes de plugin de Claude Code lui-même, et quelques variables d'environnement dans `settings.json`. |
| La connexion doit passer par la procédure propre à Anthropic ; les développeurs ne peuvent pas collecter, stocker ni servir d'intermédiaire pour les identifiants Claude.ai. | Vous vous connectez par la procédure de connexion de Claude Code lui-même. Latet ne vous demande jamais vos identifiants, ne les lit jamais pour son propre compte et ne les stocke jamais. |
| Les règles ne vous empêchent pas de vous connecter au binaire non modifié de Claude Code avec votre propre abonnement. | C'est ainsi que Latet s'utilise : votre Claude Code, votre connexion. |

**Le chemin d'une requête.** Quand `ANTHROPIC_BASE_URL` pointe vers le daemon local, Claude Code fait passer
ses requêtes par lui, comme par n'importe quelle passerelle : Anthropic documente l'utilisation d'une
[passerelle](https://code.claude.com/docs/en/llm-gateway) avec une connexion par abonnement, et son
[guide des passerelles](https://code.claude.com/docs/en/llm-gateway-protocol) demande à une passerelle de
transmettre les requêtes inchangées et de les inspecter sans les modifier. C'est ce que fait le daemon :
chaque requête arrive chez Anthropic octet pour octet, en-têtes compris, avec votre jeton tel que Claude
Code l'y a placé. Le jeton n'est jamais enregistré, journalisé ni affiché dans le tableau de bord.

**Pourquoi rien n'a besoin d'être réécrit.** Claude Code ajoute au prompt système de chaque requête un bloc
d'attribution avec une empreinte dérivée de la conversation. Un proxy qui réécrirait les requêtes
changerait ce que Claude Code a envoyé. Le plugin masque plutôt à l'intérieur de Claude Code, avant que la
requête ne soit construite, si bien que le daemon ne fait que la vérifier : il peut laisser passer une
requête ou la bloquer, jamais la modifier. Voir [Comment ça marche](../../concepts/how-it-works/) pour un
exemple étape par étape.

Ceci décrit le fonctionnement de Latet ; ce n'est pas un avis juridique, et les conditions d'Anthropic font
foi.

## Ce que fait le plugin

Le plugin demande au daemon local de détecter et de masquer ; rien n'est détecté dans Claude Code lui-même.

- **Masque le contenu là où il naît**, avant qu'il ne rejoigne la conversation : les prompts que vous
  tapez, le prompt système, `CLAUDE.md` et les autres éléments de contexte qu'ajoute Claude Code, les
  pièces jointes, les prompts des skills, les descriptions des outils, et chaque résultat d'outil (contenu
  des fichiers, sortie des commandes, sortie des hooks).
- **Remet les originaux en place dans les arguments des appels d'outils** avant qu'un outil ne s'exécute,
  pour que Read, Edit, Write, Bash et vos serveurs MCP locaux travaillent sur les vraies valeurs et que les
  fichiers sur le disque soient corrects.
- **Garde les valeurs factices là où les arguments quittent votre machine** : WebFetch, WebSearch et les
  connecteurs claude.ai reçoivent les valeurs factices que le modèle a écrites. Les sous-agents les
  reçoivent aussi, et travaillent dessus comme le modèle.
- **Arrête un outil hors de votre machine qui travaillerait sur une valeur factice** : un appel à
  WebSearch, à WebFetch ou à un connecteur claude.ai contenant une valeur factice chercherait quelqu'un
  d'autre, ouvrirait la mauvaise page ou écrirait à une adresse qui n'existe pas. Il ne s'exécute pas ; le
  modèle apprend pourquoi, et vous demande si la valeur peut sortir en clair. Le tableau de bord affiche
  l'appel, avec *Non sensible* pour chaque valeur.
- **Remet les vraies valeurs dans ce que vous voyez** : vos messages, les réponses du modèle, les commandes
  et les appels d'outils affichés à l'écran (y compris un groupe replié de ceux-ci, pendant qu'il
  s'exécute) et leur sortie contiennent les vraies valeurs. Ce que lit le modèle ne change pas.
- **Vérifie chaque requête avant son envoi.** Si une valeur sensible est sur le point de partir en clair,
  la requête n'est pas envoyée et vous voyez un message à la place :

  ```text
  Latet stopped this request before it left your machine: PASSWORD (tool result) would have been
  sent in clear. Nothing was sent.
  ```

  Le tableau de bord affiche la requête, avec la valeur qui l'a arrêtée encadrée en rouge : si elle n'est
  pas sensible, cliquez dessus et choisissez *Non sensible*, puis relancez le tour.

  Quand les valeurs en clair se trouvent dans une conversation commencée avant que Latet ne soit activé,
  le message le dit, et vous demande de commencer une nouvelle conversation avec `/clear` (voir
  [Dépannage](../../troubleshooting/#chaque-requête-dune-conversation-est-bloquée)).

Si le daemon ne tourne pas, le plugin le démarre. Si cela échoue, le plugin retient le contenu plutôt que
de l'envoyer non masqué : votre prompt n'est pas envoyé, les résultats d'outils sont remplacés par un
avis, et les appels d'outils sont refusés, jusqu'au retour du daemon. `latet doctor` vous dit ce qui ne va
pas.

## Ce que fait le daemon

Quand `ANTHROPIC_BASE_URL` pointe vers lui, chaque requête passe par le daemon, qui :

- cherche dans la requête sortante des valeurs sensibles en clair, et la bloque s'il en trouve une : Claude
  Code affiche alors une erreur d'API avec le message `Latet: sensitive values would leave in clear` ;
- sinon, transmet les octets inchangés, en-têtes compris ;
- enregistre l'échange pour le [tableau de bord](../../dashboard/), sans ses identifiants.

Il ne réécrit jamais une requête venant de Claude Code, ni une requête qui porte un jeton d'abonnement
Claude, quel que soit son expéditeur.

## Toutes les sessions, ou une seule

Il y a deux façons de faire passer Claude Code par Latet :

| | `latet global-install`, `latet agents apply claude` | `latet claude` |
| --- | --- | --- |
| Ce qui est couvert | Toutes les sessions de Claude Code : le terminal, VS Code, JetBrains, et l'onglet Code de Desktop (plugin seulement) | Cette seule session, dans le terminal |
| Ce qui change | Installe le plugin et ajoute les réglages ci-dessous à `settings.json`, sauvegardé au préalable | Rien : le plugin et les réglages ne sont donnés qu'à cette session |
| Annuler | `latet agents revert claude`, ou `latet global-uninstall` | Terminer la session |
| `latet doctor` | Vérifie le plugin et les réglages | Signale Claude Code comme non appliqué, ce qui est normal |

`latet agents apply claude` fait passer Claude Code seul par Latet : lancez cette commande quand vous
installez Claude Code après `latet global-install`, ou après avoir changé le port. `latet claude` démarre le
daemon si besoin et transmet tous les arguments à `claude` :

```sh
latet claude
latet claude --continue
```

`claude` lancé seul, sans `latet global-install`, ne passe pas par Latet ; dans un shell où `latet env` a
défini `ANTHROPIC_BASE_URL`, il atteint Latet sans le plugin, et ses requêtes sont seulement vérifiées.
`latet run claude` revient au
même que `latet claude` : Claude Code a besoin du plugin en plus de l'adresse, et `latet run` seul ne lui
donnerait que l'adresse.

## Ce que modifie global-install

`latet global-install`, ou `latet agents apply claude`, fait deux choses :

1. Installe le plugin au moyen des commandes de plugin de Claude Code lui-même, depuis une marketplace
   locale dans le dossier de données de Latet.
2. Ajoute un bloc `env` à `~/.claude/settings.json` (ou `$CLAUDE_CONFIG_DIR/settings.json`), après avoir
   sauvegardé le fichier :

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:5283/anthropic",
    "LATET_BRIDGE_URL": "http://127.0.0.1:5283",
    "LATET_BRIDGE_TOKEN": "<a random token, created when first needed>",
    "ENABLE_TOOL_SEARCH": "true",
    "CLAUDE_CODE_AUTO_MODE_SERVER": "0"
  }
}
```

- `ANTHROPIC_BASE_URL` fait passer chaque requête par le daemon. Avec la passerelle de votre entreprise,
  Amazon Bedrock, Google Vertex ou Microsoft Foundry, Latet écrit à sa place la variable de cette route
  (voir [Points de terminaison d'entreprise](../company-endpoints/)) ; avec une adresse sur cet
  ordinateur, il laisse la vôtre telle quelle.
- `LATET_BRIDGE_URL` et `LATET_BRIDGE_TOKEN` indiquent au plugin où se trouve le daemon, et prouvent au
  daemon que le plugin est le vôtre.
- `ENABLE_TOOL_SEARCH` garde activée la recherche d'outils MCP : Claude Code la désactive quand l'URL de
  base est personnalisée.
- `CLAUDE_CODE_AUTO_MODE_SERVER` déplace la relecture du mode auto dans des requêtes que Latet peut
  vérifier. Sans cette ligne, la relecture voyage comme un champ supplémentaire de chaque requête, hors de
  portée du plugin comme du daemon. `latet agents apply claude --keep-classifier-context` omet cette ligne.

Redémarrez ensuite Claude Code. `latet agents status` indique si la configuration est appliquée, et
`latet agents revert claude` annule les deux étapes.

### Le relecteur du mode auto

En mode auto, Claude Code demande à un modèle relecteur si chaque action est sûre. Le relecteur reçoit
votre CLAUDE.md, l'action, votre nom d'utilisateur du système (sous l'intitulé *User identity*) et les
chemins de vos répertoires personnel et de travail, dans un texte que le plugin ne peut pas atteindre. Il
s'en sert pour distinguer ce qui est à vous de ce qui est aux autres : un push vers une branche
`yourname/...` est à vous, un push vers `someone-else/...` ne l'est pas. Le daemon vérifie ces requêtes
comme toutes les autres, et `auto_mode_review` décide, information par information, ce que le relecteur
peut recevoir de votre identité :

| Information | Ce que c'est |
| --- | --- |
| `user_name` | Votre nom d'utilisateur du système, la *User identity* du relecteur. |
| `paths` | Le nom de votre dossier utilisateur dans les chemins de vos répertoires personnel et de travail. |
| `host_name` | Le nom d'hôte de cette machine, quand votre CLAUDE.md ou l'action le contient. |
| `git_author` | Votre nom et votre adresse e-mail tels qu'ils figurent dans votre auteur git, et le domaine de cette adresse quand c'est celui de votre entreprise, quand votre CLAUDE.md ou l'action les contient. |

Chaque information vaut `block` (par défaut) ou `allow`. Une relecture qui contiendrait une information
bloquée est arrêtée, si bien que l'information reste sur votre machine, et le mode auto ne peut pas
approuver l'action. Claude Code la refuse alors, en indiquant que son relecteur est temporairement
indisponible, et ne vous demande rien : exécutez l'action vous-même, ou passez Claude Code dans un mode de
permission qui vous pose la question. Une information autorisée parvient au relecteur, et à lui seul.
`auto_mode_review: block` et `auto_mode_review: allow-identity` règlent toutes les informations d'un
coup ; une par une, cela s'écrit :

```yaml
auto_mode_review:
  user_name: allow
  paths: allow
  host_name: block
  git_author: block
```

Les secrets et les données des autres ne passent jamais : une clé ou l'adresse e-mail d'un client dans
l'action arrête la relecture, quel que soit votre choix. Votre propre adresse e-mail est considérée comme
faisant partie de votre auteur git.

**Gardez les secrets hors de votre CLAUDE.md.** Claude Code donne au relecteur votre CLAUDE.md tel qu'il
est sur le disque, dans une requête qu'il construit lui-même : le plugin ne peut donc pas le masquer à cet
endroit, et le daemon ne modifie jamais les requêtes de Claude Code. Un mot de passe ou l'adresse e-mail
d'un client dans le fichier arrête alors chaque relecture, et le mode auto refuse toute action qui en a
besoin. Le modèle qui fait le travail n'est pas concerné : il reçoit votre CLAUDE.md masqué. Déplacez ces
valeurs dans `.env` et nommez plutôt la variable dans le fichier (*la base de données de préproduction est
dans `DATABASE_URL`*). Le plugin le signale au début d'une session, et `latet doctor` liste chaque valeur
par ligne et par type, jamais la valeur elle-même.

Par défaut, chaque information reste sur votre machine ;
`latet global-install --auto-mode-review user_name,paths` les laisse passer une par une. La page **Règles**
du tableau de bord et `config.yaml` permettent de changer cela ensuite. Quand une relecture est arrêtée, le
tableau de bord affiche les informations qu'elle contenait et le réglage qui les laisse passer.

**Un nom d'utilisateur factice.** Avec `user_name: surrogate`, Latet donne à Claude Code la valeur factice
de votre nom d'utilisateur dans `USERNAME` et `USER`. Claude Code communique ce nom au relecteur, le même
nom factice que celui que le modèle voit partout ailleurs, et une relecture qui contient encore votre vrai
nom d'utilisateur est arrêtée. Lancez `latet agents apply claude` après l'avoir choisi, puis redémarrez
Claude Code. Les commandes que lance Claude Code voient aussi le nom factice, dans `%USERNAME%`, `$USER` et
le `getpass.getuser()` de Python ; `whoami`, votre dossier personnel et l'auteur de git restent tels quels.
Combiné avec `paths: allow`, c'est utile quand votre nom d'utilisateur et votre dossier utilisateur
diffèrent, ce qui est courant sous Windows : le relecteur reçoit alors le nom de votre dossier, mais jamais
votre nom d'utilisateur. Quand ils sont identiques, les chemins contiennent de toute façon le vrai nom.

## Limites

- **Le mode print affiche les valeurs factices.** Avec `claude -p`, la réponse affichée contient des
  valeurs factices, car seule l'interface interactive remet les vraies valeurs à l'écran. Les fichiers
  écrits par les outils reçoivent quand même les vraies valeurs.
- **Claude Desktop connecté à un compte Claude** : l'onglet Code a le plugin et sa vérification
  finale, mais pas le daemon derrière lui, et rien n'est enregistré dans le tableau de bord ; Chat et
  Cowork ne sont pas couverts. Sur une passerelle tierce, chaque requête est vérifiée et seul l'onglet
  Code masque : voir [Couverture](#couverture).
- **Remote Control est désactivé** tant que Latet est sur le chemin : Claude Code le désactive quand l'URL
  de base est personnalisée.
- **Les outils web et les connecteurs claude.ai ne peuvent pas utiliser une valeur masquée.** Leurs
  arguments quittent votre machine, ils recevraient donc la valeur factice : un tel appel est arrêté, et
  vous choisissez si la valeur peut sortir en clair (*Non sensible*). Les sous-agents restent sur votre
  machine et travaillent sur des valeurs factices, comme le modèle. Les résultats des connecteurs
  parviennent à Claude Code par les serveurs d'Anthropic : leur contenu s'y trouve donc avant que Latet ne
  le voie. Voir [Ce qui est protégé et ce qui ne l'est pas](../../concepts/protection-scope/).
- **Images**, avec la [reconnaissance de texte](../../concepts/images/) facultative : celles que renvoient
  les outils sont remplacées par leur copie masquée ; une capture d'écran que vous collez et qui contient
  des valeurs est retirée du prompt, et Claude reçoit le chemin d'une copie masquée à ouvrir avec Read.
  Les **PDF** ne sont pas lus et sont envoyés tels quels.
