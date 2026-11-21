---
title: Secrets dans les sessions enregistrées
description: latet leak lit les sessions que vos agents ont enregistrées sur cette machine, liste les secrets qu'elles contiennent sans montrer aucune valeur, et indique lesquels ont pu arriver chez le fournisseur du modèle.
---

Vos agents de code gardent chaque conversation sur le disque, en entier : les prompts que vous avez
tapés, les fichiers qu'ils ont lus, la sortie des commandes qu'ils ont lancées. Un secret entré dans
une conversation avant que vous installiez Latet, ou dans une session qui n'est pas passée par lui,
se trouve dans ces fichiers. Le [tableau de bord](../../dashboard/) ne peut pas le montrer : il ne
voit que les requêtes passées par Latet, et seulement tant que son historique les conserve (14 jours
par défaut). `latet leak` lit les sessions enregistrées avec les détecteurs de Latet et liste les
secrets qu'elles contiennent, pour que vous sachiez quelles clés remplacer et quelles conversations
ne pas reprendre.

```sh
latet leak
```

La commande lit et affiche, et ne montre jamais une valeur. Elle ne modifie aucun de vos fichiers,
n'a besoin ni du réseau ni d'un daemon en marche, et le coffre-fort de Latet n'en apprend rien : les
valeurs trouvées restent dans sa mémoire le temps de la lecture, pas au-delà. Comme elle n'affiche
aucune valeur, un agent peut la lancer lui aussi.

## Où vos agents enregistrent leurs sessions

`~` est votre dossier personnel (`C:\Users\you` sous Windows). Quand la variable d'un agent est
définie, ses sessions sont dans le dossier qu'elle indique, et `latet leak` les lit là.

| Agent | Ses sessions enregistrées | La variable qui les déplace |
| --- | --- | --- |
| [Claude Code](../../agents/claude-code/) | `~/.claude/projects/` : un fichier `.jsonl` par session et par sous-agent | `CLAUDE_CONFIG_DIR`, à la place de `~/.claude` |
| [Codex](../../agents/codex/) | `~/.codex/sessions/` et `~/.codex/archived_sessions/` | `CODEX_HOME`, à la place de `~/.codex` |
| [OpenCode](../../agents/opencode/) | `~/.local/share/opencode/opencode.db`, et l'ancien dossier `storage` à côté | `XDG_DATA_HOME`, à la place de `~/.local/share` |
| [Copilot CLI](../../agents/other-tools/#github-copilot-cli) | `~/.copilot/session-state/` : un fichier `events.jsonl` par session | `COPILOT_HOME`, à la place de `~/.copilot` |
| [Gemini CLI](../../agents/gemini/) | `~/.gemini/tmp/` : le dossier `chats` de chaque projet | `GEMINI_CLI_HOME`, à la place de `~` |
| [Qwen Code](../../agents/other-tools/#qwen-code) | `~/.qwen/projects/` : le dossier `chats` de chaque projet ; et l'ancien `~/.qwen/tmp/` | `QWEN_HOME`, à la place de `~/.qwen` |
| [Hermes Agent](../../agents/autonomous-agents/#hermes-agent) | `state.db` dans `~/.hermes` et, sous Windows, dans `%LOCALAPPDATA%\hermes` | `HERMES_HOME`, le dossier qui contient `state.db` |

`--agent` ne lit que les agents que vous nommez, sous ces noms : `claude`, `codex`, `opencode`,
`copilot`, `gemini`, `qwen` et `hermes` ; répétez l'option pour en lire d'autres. `--path` lit en plus
un fichier, ou les fichiers `.json`, `.jsonl` et `.ndjson` d'un dossier et de ses sous-dossiers, de
la même façon : par exemple les sessions d'un agent que ce tableau ne cite pas, ou un dossier de
sessions copié depuis une autre machine.

**Ce qui est lu.** Dans chaque enregistrement, chaque texte : vos prompts, les réponses du modèle,
les appels d'outils et leurs résultats, y compris un texte qui est lui-même du JSON. Pas les
identifiants, les horodatages ni les signatures, qui ne contiennent rien de la conversation. Chaque
texte est lu par vos propres détecteurs avec vos réglages globaux, comme Latet lit la sortie d'un
outil : les règles intégrées, vos termes et vos reconnaisseurs, chaque type avec sa politique. Une
valeur que vos réglages laissent passer n'est pas listée. Le modèle local de noms et le relecteur
local ne servent pas, et le `latet.yaml` d'un projet n'est pas lu.

Par défaut, la commande ne cherche que les secrets : clés, jetons, mots de passe, clés privées,
identifiants dans une adresse web. `--personal` compte en plus les données personnelles, comme les
adresses e-mail et les numéros de téléphone, par type : combien de sessions contiennent chaque type,
jamais une valeur.

## Ce que la commande affiche

Un exemple, avec des dossiers et des nombres inventés :

```text
$ latet leak
Latet read 242 Claude Code sessions, 38 Codex sessions and 5 Copilot CLI sessions.

2 secrets in clear (no sign that Latet replaced them: they may have reached the provider):
  By type: 1 Github pat and 1 Password.
  Github pat  ghp_… (40 characters)  Claude Code, 4 sessions in C:\Users\you\dev\shop
      2026-09-02 to 2026-09-09
  Password (17 characters)  Codex, 1 session in C:\Users\you\dev\billing
      2026-09-20
Latet can tell only from 2026-10-10 on, the earliest date its vault and history keep on this computer: an emptied vault, or a history past the days it keeps (14 by default), keeps nothing older. A session listed in clear from before then may have gone through Latet all the same.

2 secrets Latet replaced, or may have (its fake left instead where the session went through Latet):
  By type: 1 Anthropic api key and 1 Github pat.
  Anthropic api key  sk-ant-api03-… (108 characters)  Claude Code and Codex, 6 sessions in C:\Users\you\dev\billing, C:\Users\you\dev\shop
      2026-10-12 to 2026-11-14; 5 confirmed by Latet's history, 1 if the session went through Latet
  Github pat  ghp_… (40 characters)  Claude Code, 2 sessions in C:\Users\you\dev\shop
      2026-10-12 to 2026-11-14; 1 confirmed by Latet's history, 1 if the session went through Latet

Rotate each secret in clear at its vendor: what reached a model provider may stay in its logs. Do not resume those sessions (a resumed conversation sends them again): start new ones. The secrets Latet replaced need nothing where the session went through Latet.
```

Le même secret collé dans plusieurs sessions tient sur une ligne, avec le nombre de sessions. De
même pour un secret que les détecteurs ont trouvé sous deux types, comme un jeton GitHub dans
l'en-tête d'une commande `curl` et affecté à une variable : la ligne montre le type du fournisseur.
Un secret peut figurer dans deux listes, comme le jeton GitHub ci-dessus : collé avant que Latet
soit là, puis de nouveau après.

### En clair

Rien n'indique que Latet les ait remplacés : ils ont pu arriver chez le fournisseur du modèle (*no
sign that Latet replaced them: they may have reached the provider*). Une session compte ici quand
elle contenait la valeur avant que le coffre-fort de Latet la garde pour la première fois, ou quand
le coffre-fort ne connaît pas du tout la valeur. Deux autres cas comptent ici : une session dont les
enregistrements n'ont pas d'heure, qu'on ne peut placer ni avant ni après cette date, et une session
dont l'historique de Latet montre qu'elle a d'abord envoyé la valeur en clair : l'historique garde
encore les premières requêtes de la session, et ne montre la valeur masquée que dans des requêtes
plus tardives. Ce sont les secrets à remplacer. Sous la liste, la commande indique à partir de
quelle date Latet peut le dire : la date la plus ancienne que son coffre-fort et son historique
gardent sur cette machine.

### Remplacés par Latet

Le coffre-fort connaissait déjà la valeur quand la session la contenait : là où la session est
passée par Latet, le fournisseur du modèle a donc reçu la valeur factice de Latet à la place. La
valeur est quand même dans le fichier de la session, parce que l'agent enregistre ce qu'il avait
avant que Latet la remplace à la sortie. Il n'y a rien à remplacer vis-à-vis du fournisseur du
modèle, mais la valeur se trouve dans un fichier sur cette machine.

Chaque secret indique combien de ses sessions sont *confirmed by Latet's history*, confirmées par
l'historique de Latet : l'historique garde encore une requête de la session où la valeur a été
masquée. Il faut pour cela un historique conservé en entier (*Tout*, le réglage par défaut) qui
contienne encore la session (14 jours par défaut), et un agent qui nomme sa session dans ses
requêtes, comme le font Claude Code, Codex et Hermes Agent. Toute autre session compte *if the
session went through Latet*, seulement si elle est passée par Latet : un agent qui n'était pas
configuré pour Latet à ce moment-là a envoyé la valeur en clair, et seul l'historique peut le dire.
Quand un secret a une telle session, l'en-tête dit *Latet replaced, or may have*.

### Caviardés

Les secrets que vos réglages caviardent, ou que Latet caviarde parce qu'ils sont trop longs à garder
(*your settings redact, or Latet redacts as too long to keep*) : un secret d'un type que vos
[politiques](../profiles/#politiques) règlent sur `redact`, ou un secret trop long pour le
coffre-fort (plus de 16 384 caractères). Latet remplace une telle valeur par un marqueur et ne la
garde nulle part, si bien que son coffre-fort n'a pas de date pour elle. Les sessions à partir de la
date la plus ancienne que Latet a sur cette machine sont listées ici, celles d'avant en clair. Vos
réglages ont pu être différents à ce moment-là, et la session n'est peut-être pas passée par Latet :
c'est pourquoi l'en-tête dit que Latet a pu envoyer un marqueur à sa place (*may have sent a
marker*).

### Chaque liste

Sous son en-tête, une liste compte ses secrets par type : les six types qui en ont le plus, puis les
autres ensemble. Viennent ensuite les secrets, d'abord les clés d'un fournisseur (un jeton GitHub,
une clé Anthropic, une clé AWS : celles à remplacer en premier), puis ceux qui sont dans le plus de
sessions. Une liste montre les 20 premiers et dit combien il en reste ; `--all` les liste tous.

Un secret montre son type, un indice et, pour les sessions de cette liste, les agents, le nombre de
sessions, les dossiers de projet (trois au plus, puis combien d'autres) et les dates où il a été vu
pour la première et la dernière fois, à l'heure de cette machine, comme `--since` lit une date.
L'indice ne garde que le préfixe public que le fournisseur met devant chaque clé de ce type
(`ghp_…`, `sk-ant-api03-…`, `AKIA…`), le même que garde une valeur factice, et la longueur ; un
secret sans tel préfixe montre son type et sa longueur (`Password (17 characters)`).

À la fin viennent les données personnelles avec `--personal`, ce qu'il faut faire, et ce qui n'a pas
pu être lu, avec la raison, jamais son contenu : un fichier qui ne s'ouvre pas, un fichier JSON de
plus de 32 Mo, un seul texte de plus de 8 Mo, une base de données qui a changé à chaque copie.

**Sans les réponses de Latet.** Sur une machine connectée à un [serveur Latet](../server/), le
coffre-fort et l'historique sont ceux du serveur. La commande dit alors qu'elle n'a pas pu savoir
quels secrets Latet a remplacés, et liste chaque secret en clair, sauf ceux qu'elle liste comme
caviardés. Elle fait de même, en disant pourquoi, quand le coffre-fort de Latet sur cette machine,
ou sa clé, n'a pas pu être lu, ou qu'aucune clé trouvée ici ne l'ouvre. Sur une machine sans
coffre-fort Latet ni serveur Latet, Latet n'a rien remplacé : la commande le dit, et liste chaque
secret en clair, ceux des types caviardés aussi.

## Options

| Option | Ce qu'elle fait |
| --- | --- |
| `--agent NAME` | Ne lire que les sessions de cet agent : `claude`, `codex`, `opencode`, `copilot`, `gemini`, `qwen` ou `hermes`. À répéter pour d'autres. |
| `--since DATE` | Lire les enregistrements à partir de cette date (`2026-10-01`), d'après l'heure de chaque enregistrement, sinon d'après la dernière modification de son fichier. |
| `--personal` | Compter en plus les données personnelles, par type : combien de sessions contiennent chaque type. |
| `--path PATH` | Lire en plus ce fichier, ou les fichiers JSON et JSON Lines de ce dossier. À répéter pour d'autres. |
| `--json` | Afficher un document JSON au lieu du texte, avec chaque secret. |
| `--all` | Lister chaque secret de chaque liste, pas seulement les 20 premiers. |

`--json` donne un enregistrement par secret : son `type` (celui que montre le texte), ses `types`
(chaque type sous lequel il a été trouvé), sa `class`, son `hint`, ses `agents` et ses `folders`, et
ses sessions `in_clear`, `replaced` et `redacted`, chacune avec leur nombre, la première et la
dernière date, combien sont confirmées, et les `agents` et les `folders` de ces sessions. À côté :
les sessions lues par agent, les données personnelles par type, ce qui n'a pas pu être lu
(`skipped`) et combien de choses cela fait (`unread`), `latet_answers` (si le coffre-fort et
l'historique de Latet ont pu être interrogés) et `latet_since` (la date la plus ancienne qu'ils
gardent, donnée quand un secret est en clair).

Le code de sortie est `0` quand rien n'est trouvé, `1` quand un secret est trouvé, dans n'importe
quelle liste, ou des données personnelles avec `--personal`, et `2` quand aucune session n'a pu être
lue, ou qu'une option est fausse. Un script peut lancer la commande et agir en conséquence.

## Combien de temps cela prend

`latet leak` lit chaque texte de chaque session enregistrée, ce qui prend du temps quand il y en a
beaucoup : environ 11 minutes pour 10 Go de sessions sur un PC de bureau. Dans une console, la
commande affiche sur une ligne l'agent qu'elle lit et combien de ses sessions elle a déjà lues, puis
le temps que cela a pris ; au-delà d'une minute, elle indique que `--agent` et `--since` lisent
moins :

```sh
latet leak --agent claude --since 2026-10-01
```

Une session est jugée sur les enregistrements lus : avec `--since`, un secret qu'un enregistrement
plus ancien de la même session a envoyé en clair peut être listé comme remplacé.

Ces lignes vont sur la sortie d'erreur standard, et seulement vers une console : seul le rapport va
sur la sortie standard, et avec `--json` il y reste un unique document JSON.

## Ce que la commande ne peut pas savoir

- **Avant la date de Latet.** Latet connaît une valeur à partir du jour où son coffre-fort l'a
  gardée pour la première fois, et une session qui contenait la valeur avant ce jour est listée en
  clair. Un coffre-fort vidé, une valeur retirée avec *Oublier* (le coffre-fort la connaît de
  nouveau à partir du moment où elle revient) ou un historique au-delà des jours qu'il conserve ne
  garde rien de plus ancien : une session listée en clair, antérieure à la date indiquée sous la
  liste, a pu passer par Latet malgré tout.
- **Une session qui a contourné Latet.** Un secret listé comme remplacé, dans une session que
  l'historique ne confirme pas, a pu partir sans passer par Latet : seul l'historique peut le dire,
  pour les jours qu'il conserve.
- **Ce que Claude Code garde pour son propre affichage.** Claude Code enregistre une deuxième copie
  du résultat d'un outil pour son propre affichage (`toolUseResult`) : un fichier qu'il a lu, le
  fichier entier d'une modification, la sortie d'une commande. Cette copie n'arrive jamais chez le
  fournisseur du modèle, et `latet leak` ne la lit pas. La copie que portait la requête est lue.
- **Un secret coupé en deux.** Chaque texte est lu à part : un secret coupé entre deux
  enregistrements n'est pas trouvé, et une valeur qu'un texte fait reconnaître par le mot qui la
  précède (`password:`) n'est pas trouvée là où un autre texte l'écrit seule.
- **Ce que les détecteurs ne reconnaissent pas,** comme pour le masquage de Latet
  ([Non protégé](../protection-scope/#non-protégé)). Une valeur que Latet ne connaît que par son
  coffre-fort, parce que le relecteur local ou vous l'avez trouvée, n'est pas trouvée non plus :
  l'analyse part d'un coffre-fort à elle, vide.

Une base de données en mode write-ahead, à côté de laquelle une simple lecture ajouterait des
fichiers, est lue depuis une copie privée dans un dossier temporaire, supprimée ensuite ; la copie
n'est faite que là où il reste au moins 1 Go libre sur le disque après elle. Cela vaut pour les
bases d'OpenCode et de Hermes Agent, et pour le coffre-fort et l'historique de Latet lui-même.

## Que faire d'un secret trouvé

- **Remplacez chaque secret en clair chez le fournisseur de la clé,** d'abord les clés d'un
  fournisseur, dans l'ordre de la liste : créez une nouvelle clé là où le fournisseur les délivre
  (GitHub, Anthropic, AWS...), utilisez-la à la place de l'ancienne, et révoquez l'ancienne ;
  changez un mot de passe. Ce qui est arrivé chez un fournisseur de modèle peut rester dans ses
  journaux, quoi que vous supprimiez sur votre machine. Le type, le préfixe, les dossiers et les
  dates vous disent de quelle clé il s'agit et où elle a servi.
- **Ne reprenez pas ces conversations.** Une conversation reprise renvoie tous ses messages, et le
  secret avec eux : commencez-en une nouvelle.
- **Les secrets que Latet a remplacés** n'appellent rien chez le fournisseur de la clé là où la
  session est passée par Latet : le fournisseur du modèle a reçu une valeur factice. **Les secrets
  caviardés n'appellent peut-être rien :** si la session est passée par Latet, le fournisseur du
  modèle a reçu un marqueur à leur place.
- **Les fichiers sur le disque sont ceux de votre agent.** Latet n'en supprime ni n'en déplace
  aucun. Pour retirer une valeur de cette machine, supprimez ou déplacez les fichiers de session qui
  la contiennent, agent fermé : l'agent, le dossier du projet et les dates de la liste vous disent
  où chercher. Une conversation dont vous supprimez le fichier ne peut plus être reprise.

Un secret que vous avez remplacé reste listé tant que ses fichiers de session sont là : la liste dit
ce que contiennent les fichiers, pas si une clé est encore valable.
