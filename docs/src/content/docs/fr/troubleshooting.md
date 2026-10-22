---
title: Dépannage et FAQ
description: Problèmes courants et questions fréquentes.
---

## Commencez ici

La plupart des problèmes apparaissent avec une seule commande :

```sh
latet doctor
```

Elle vérifie le daemon, la connexion du plugin au daemon, le démarrage à l'ouverture de session, le
port, un autotest du masquage, et chaque agent qu'elle trouve. Chaque ligne `WARN` ou `FAIL` indique
la commande qui corrige le problème.

Quand il tourne en arrière-plan, le daemon écrit son journal dans `logs/daemon.log`, dans le dossier
de données : `%LOCALAPPDATA%\latet` sous Windows, `~/Library/Application Support/latet` sous macOS,
`~/.local/share/latet` sous Linux. Pour le suivre plutôt en direct, arrêtez le daemon et lancez
`latet serve`, qui écrit son journal dans le terminal.

## Dépannage

### Mon agent dit qu'il ne peut pas se connecter

L'agent est configuré pour utiliser Latet, mais le daemon ne tourne pas. Vérifiez son état, puis
démarrez-le :

```sh
latet status
latet start
```

`latet status` affiche `running: Latet 0.9.2 on 127.0.0.1:5283 (masking), pid 12345` quand le daemon
tourne, ou `not running` sinon. Si vous voulez plutôt cesser d'utiliser Latet, lancez d'abord
`latet global-uninstall` : il remet vos agents comme ils étaient. Voir
[Désinstallation](../getting-started/uninstall/).

### Claude Code dit que Latet a retenu du contenu

Des messages comme ceux-ci signifient que le plugin n'a pas obtenu de réponse du daemon, ou que le
daemon a échoué sur ce contenu, et que le plugin l'a donc retenu au lieu de l'envoyer sans masquage :

- `Latet could not mask this prompt: the local daemon did not answer, or failed on it.`
- `[withheld by Latet: the local daemon did not answer, or failed on it, so this content was not sent]`
- `Latet could not check this request (the local daemon did not answer, or failed on it), so it was not sent.`

Lancez `latet start`, puis `latet doctor`. Si le daemon tourne, son journal (`daemon.log` dans le
dossier `logs` du dossier de données) indique ce qui a échoué : merci d'ouvrir une issue en y
joignant ce journal. Le plugin démarre le daemon de lui-même avec la commande `latet` ; quand vous
exécutez Latet depuis une copie locale du dépôt, cette commande n'est pas dans votre `PATH` :
démarrez-le alors avec `uv run latet start`.

### Latet refuse son dossier de données

Le message nomme le dossier, qui d'autre peut le modifier, et que faire, comme dans :

```text
Integration records unavailable (unsafe folder namespace): C:\Users\you lets PC\other (S-1-5-21-…)
change what is in it (its access list grants full control, (F), mask 0x001f01ff; `icacls "C:\Users\you"`
lists it): remove that entry (`icacls "C:\Users\you" /remove:g *S-1-5-21-…`), trust that account
(LATET_TRUSTED_ACCOUNTS), or use a data folder whose every parent only you, SYSTEM, the Administrators
and the accounts in LATET_TRUSTED_ACCOUNTS can change (LATET_DATA_DIR); with a new data folder,
`latet agents apply` applies your agents again.
```

Latet garde ce qu'il a appliqué à vos agents, et sur une machine connectée à un serveur Latet sa
connexion, dans un dossier de données qu'aucun autre compte de la machine ne peut modifier : un autre
compte pourrait y déposer ce que Latet applique ou envoie ensuite. Le dossier nommé est le dossier de
données ou l'un des dossiers qui le contiennent.

- **Le dossier de données lui-même** est refusé dès qu'un autre compte peut le modifier, de même qu'un
  dossier parent qui appartient à un autre compte.
- **Un dossier parent qu'un autre compte peut modifier** est refusé sur une machine connectée à un
  serveur Latet, où le jeton du serveur est en jeu. Sur une machine qui n'est pas connectée, Latet
  continue et `latet doctor` avertit, avec les mêmes mots : un compte qui peut modifier votre profil
  peut déjà modifier tout ce qui vous appartient à l'intérieur, les données de Latet comprises.

Sous Windows, `icacls "<dossier>"` liste qui peut modifier le dossier : une entrée pour un autre compte
avec `(F)`, `(M)` ou `(W)` le lui permet. Il y a trois solutions :

1. Retirez cette entrée, si la machine vous revient. Une entrée marquée `(I)` vient du dossier
   au-dessus, et se retire là ; une entrée affichée par son seul SID est un compte que Windows ne
   trouve plus, souvent un compte supprimé.
2. Faites confiance à ce compte : réglez `LATET_TRUSTED_ACCOUNTS` sur son nom ou son SID pour votre
   compte, comme avec `setx LATET_TRUSTED_ACCOUNTS "PC\other"`, ou dans les paramètres système, ou pour
   toute la machine par stratégie de groupe, puis ouvrez un nouveau terminal. Latet la lit là, dans le
   registre, et jamais dans l'environnement d'un terminal : une valeur réglée dans une seule session de
   terminal (`$env:` ou `set`) ne compte pas, afin que les réglages d'un dépôt ou la session d'un agent
   ne puissent pas la modifier. Plusieurs comptes se séparent par `;`, et un SID peut s'écrire
   `*S-1-5-…`, comme l'écrit icacls. Cela vaut pour les dossiers parents seulement, jamais pour le
   dossier de données lui-même, qui contient la clé du coffre, ni pour un dossier qu'il contient.
3. Réglez `LATET_DATA_DIR` sur un dossier dont seuls vous, SYSTEM, les Administrateurs et les comptes
   auxquels vous faites confiance peuvent modifier les dossiers parents, puis relancez
   `latet agents apply`.

Un membre direct du groupe local Administrateurs, comme un groupe du support ajouté par votre service
informatique, reçoit sur les dossiers parents la même confiance que le groupe, sans aucun réglage. La
confiance dans ces comptes et ces membres vaut pour les dossiers parents de chaque dossier que Latet
vérifie ainsi : ceux du dossier de données, ceux au-dessus du dossier de données que vérifie le
processus auxiliaire de Claude Code, ceux de son installation et ceux du dossier des journaux du
service Windows d'un serveur Latet. `latet doctor` liste les comptes auxquels `LATET_TRUSTED_ACCOUNTS`
fait confiance, avec l'endroit où la variable est réglée, et les membres auxquels il a fait confiance.
Il nomme chaque entrée qui ne donne confiance à personne : un nom que Windows ne trouve pas, ou un
compte qui n'est ni de cette machine, ni d'un domaine, ni d'Entra ID, ni d'un service, comme Tout le
monde, Utilisateurs ou Authentification NTLM. Il dit aussi quand la variable n'est réglée que dans son
terminal.

Sous Linux et macOS, `ls -ld <dossier>` montre son propriétaire et ses permissions. Un dossier où
d'autres peuvent écrire est refusé : `chmod go-w <dossier>`, ou `chmod 700` pour le dossier de données
lui-même. Un dossier de données que d'autres peuvent seulement lire (0755, comme le crée `mkdir`)
devient le vôtre seul (0700) la première fois que Latet en a besoin, et Latet le dit une fois :
dans le journal du daemon, ou dans le terminal de la commande qui a fait le changement.
`LATET_TRUSTED_ACCOUNTS` n'y change rien : les vérifications portent sur le propriétaire et les
permissions.

Une machine qui ne s'est jamais connectée à un serveur Latet n'est jamais refusée pour une connexion.

### Claude Code sur une machine connectée retient chaque prompt

Le plugin dit pourquoi, et chaque cause a sa solution :

- **Le mode d'impression ou une autre interface :** `claude -p`, et toute interface autre que le
  terminal interactif, sont retenus sur une machine connectée à un serveur Latet. Utilisez le
  terminal interactif.
- **La liaison du processus auxiliaire est absente ou périmée**, après une mise à jour ou une
  nouvelle connexion : lancez `latet agents apply claude`, ou démarrez Claude Code avec
  `latet claude`. `latet doctor` la vérifie.
- **Le serveur Latet n'a pas répondu :** `latet status` dit s'il répond et accepte le jeton de cette
  machine.
- **Le serveur n'accepte plus le jeton de cette machine :** il a été révoqué, ou votre identité
  désactivée. Lancez `latet disconnect`, puis `latet connect <url>` avec un nouveau code d'un
  administrateur.
- **Linux et macOS :** Claude Code via un serveur nécessite un terminal Windows dans cette version ;
  `latet codex` et `latet opencode` passent par le serveur.

`latet agents apply claude` et `latet claude` peuvent refuser avec `Claude Code's helper binding is
unavailable`. Le message nomme la partie de l'installation en cause (Python, son environnement
`sys.prefix` ou son installation `sys.base_prefix`, le paquet de Latet, ou le dossier de Windows), le
dossier et la raison : son propriétaire, ou un compte que sa liste d'accès autorise à le modifier. Le
processus auxiliaire exécute ce code avec le jeton de cette machine : il ne l'exécute donc que depuis
des dossiers que seuls vous, SYSTEM et les Administrateurs pouvez modifier, et dont seuls ceux-ci, les
membres directs des Administrateurs et les comptes de `LATET_TRUSTED_ACCOUNTS` peuvent modifier les
dossiers parents. Vérifiez le dossier nommé
avec `icacls "<dossier>"` et retirez l'entrée de l'autre compte ; ou installez Python et Latet dans
des dossiers à vous, que choisissent `UV_PYTHON_INSTALL_DIR` et `UV_TOOL_DIR` d'uv.

### Une requête a été bloquée

Dans Claude Code, la vérification finale du plugin répond dans la conversation :

```text
Latet stopped this request before it left your machine: PASSWORD (tool result) would have been
sent in clear. Nothing was sent.
```

Le message indique le type de la valeur qui aurait été envoyée en clair, et la partie de la
conversation où elle se trouvait ; rien n'a été envoyé. Un tel blocage signifie qu'un contenu a
atteint la requête sans être masqué. Lancez `latet doctor`, et vérifiez avec `latet agents status`
que Claude Code affiche `applied` ; sinon, lancez `latet agents apply claude` et redémarrez Claude
Code.

Quand c'est le daemon qui bloque une requête, l'agent affiche une erreur avec le message
`Latet: sensitive values would leave in clear` (des valeurs sensibles partiraient en clair), suivi
du type et de la longueur de chaque valeur, comme `PASSWORD[13]`. Lancez `latet dashboard` : la
requête bloquée s'y trouve, telle qu'elle serait partie, avec les valeurs qui l'ont arrêtée
encadrées en rouge. Pour continuer, retirez la valeur : commencez une nouvelle conversation (dans
Claude Code, `/clear`) et indiquez à l'agent le fichier ou la variable qui la contient, au lieu de
la coller. Uniquement si une valeur n'est pas du tout un secret, comme une valeur de test ou
d'exemple, cliquez dessus et choisissez **Non sensible : toujours autoriser**, puis relancez le tour.

En mode passerelle, une erreur avec `Latet: masking failed` ou `Latet: unreadable request body`
signifie que le daemon n'a pas pu masquer la requête, et qu'il l'a donc refusée.
Le journal du daemon dit pourquoi.

### Le mode auto dit que son relecteur est temporairement indisponible

En mode auto, Claude Code demande à un modèle relecteur si chaque action est sûre. Quand Latet
arrête cette relecture, le mode auto ne peut pas approuver l'action : Claude Code la refuse, en
disant que le relecteur est temporairement indisponible, et ne vous demande rien. Exécutez l'action
vous-même, passez Claude Code dans un mode de permission qui vous demande votre accord, ou laissez
passer les détails comme expliqué ci-dessous. Dans le tableau de bord, ces requêtes reçoivent la
réponse `400` et portent l'indicateur *Relecture du mode auto* ; le motif indique ce que
transportait la relecture. Deux choses l'arrêtent :

- **Votre identité :** votre nom d'utilisateur du système et votre dossier utilisateur dans les
  chemins, par défaut. Pour les laisser passer vers le relecteur, et vers lui seul, définissez
  `auto_mode_review: {user_name: allow, paths: allow}` ou utilisez *Relectures du mode auto de
  Claude Code* sous **Règles** → **Avancé** → **Identité** dans le tableau de bord. Voir
  [Claude Code](../agents/claude-code/#le-relecteur-du-mode-auto).
- **Un secret ou les données de quelqu'un d'autre,** qui ne vont jamais au relecteur. On les trouve
  souvent dans votre CLAUDE.md : Claude Code donne ce fichier au relecteur sans le masquer. Déplacez
  ces valeurs dans `.env` et nommez la variable dans le fichier. Quand une session démarre, le plugin
  indique le fichier et les lignes, et `latet doctor` les liste.

  **S'il s'agit d'espaces réservés,** écrivez-les sous une
  [forme que Latet connaît](../concepts/detection/#espaces-réservés) et laisse intacte :
  `postgres://app:<password>@host/db`, `${DB_PASSWORD}`, `YOUR_PASSWORD`, `changeme` ou `***`. Un
  espace réservé d'une autre forme va parmi les valeurs du projet à ne jamais masquer (**Règles**,
  choisissez le projet, *Valeurs autorisées*), dont `latet doctor` tient compte aussi quand
  il est lancé dans le dossier du projet.

Gardez les demandes de permission plutôt que de passer en mode bypass permissions : dans les
commandes qu'il exécute, l'agent travaille avec les vraies valeurs.

### Chaque requête d'une conversation est bloquée

Dans Claude Code, un message comme celui-ci arrête chaque requête d'une conversation :

```text
Latet stopped this request before it left your machine: this conversation already held values in
clear before Latet checked it (PASSWORD (user text), EMAIL (tool result)). It began before Latet
was on, or was resumed from then, and every request sends that history again. Start a new
conversation with /clear: from there everything is masked as it comes in. Nothing was sent.
```

La conversation a commencé avant que Latet soit activé, ou vous en avez repris une d'avant
(`claude --resume`) : ses premiers messages, votre CLAUDE.md et la sortie de ses outils sont entrés
en clair dans l'historique. Claude Code envoie tout l'historique avec chaque requête : chacune
transporte donc de nouveau ces valeurs, et Latet ne peut pas savoir qu'elles étaient déjà parties.
Dans le tableau de bord, ces requêtes portent l'indicateur *Dans l'historique*.

Commencez une nouvelle conversation avec `/clear`, ou une nouvelle session, avec Latet activé.
À partir de là, tout est masqué au fur et à mesure.

### Le relecteur local a arrêté une requête

Quand le [relecteur local](../concepts/llm-review/) est activé, une requête attend sa relecture, et
n'est pas envoyée quand le relecteur ne peut pas la faire. L'agent, ou Claude Code, dit pourquoi :

- `the local reviewer could not read the user text (its server did not answer: start it, or run
  latet doctor), so it was not sent` : le serveur du modèle n'a pas répondu. Démarrez-le, puis
  vérifiez-le avec `latet llm status`. Un serveur qui répond `HTTP 401` demande une clé d'API :
  enregistrez-la sous *Clé d'API* dans le tableau de bord, ou avec `latet llm key` ;
- `... (it took longer than llm_review.budget_ms)` : la relecture a dépassé le temps imparti.
  Augmentez `budget_ms`, ou supprimez-le pour attendre aussi longtemps que dure la relecture ;
- `the local reviewer could not trust its review of the tool result: the text gives it
  instructions` : le texte donne des instructions au relecteur, qui n'y a rien trouvé mais ne peut
  pas se fier à sa propre relecture. Examinez-le dans le tableau de bord avant de l'envoyer de
  nouveau ;
- `the local reviewer stopped this tool result: it must not leave even masked` : avec `block: true`,
  le relecteur a jugé le texte entier trop sensible pour être envoyé, même masqué, comme un dossier
  médical.

Dans Claude Code, un prompt arrêté n'est pas envoyé, et une sortie d'outil arrêtée parvient au
modèle sous la forme `[withheld by Latet: ...]`. Pour envoyer ce que les détecteurs ont masqué quand
le relecteur ne peut pas répondre, définissez `on_failure: send`.

### Une sortie d'outil a été retenue

`[withheld by Latet: this tool output holds 214 personal values ...]` dans la conversation, et
l'indicateur *Sortie retenue* dans le tableau de bord, viennent du
[garde-fou anti-extraction](../concepts/production-data/#le-garde-fou-anti-extraction) : la sortie contenait plus
de valeurs concernant des personnes que `dump_guard` n'en autorise dans une seule sortie, donc rien
n'en a été envoyé. Demandez à l'agent moins de lignes, ou des comptages et des agrégats ; augmentez
`dump_guard`, ou réglez-le sur `0` pour désactiver le garde-fou.

### Mon latet.yaml n'a pas d'effet

- `latet project show` affiche ce qui s'applique dans le projet, et chaque clé du fichier qui est
  ignorée, avec la raison : un fichier de projet peut seulement ajouter de la protection, donc les
  valeurs à ne jamais masquer, les règles désactivées et Latet désactivé y sont ignorés. Leur place
  est dans vos propres réglages pour le projet.
- `latet project check` vérifie le fichier, et se termine avec le code 1 quand quelque chose est
  ignoré ou qu'un exemple échoue.
- Seul le `latet.yaml` le plus proche s'applique, du dossier de l'agent jusqu'à la racine du dépôt.
  Voir [Projets](../concepts/projects/).

### Rien n'est masqué dans un projet

Latet y est peut-être désactivé : le tableau de bord signale ces échanges par l'indicateur
*Latet désactivé*. `latet project show` le dit, et `latet project on` le réactive. Un projet réglé
sur *Secrets uniquement* masque les secrets et vos termes, et laisse passer les données personnelles.

### Une valeur est masquée alors qu'elle ne devrait pas l'être

C'est un faux positif. Dans le tableau de bord, ouvrez la requête, cliquez sur la valeur et
choisissez **Non sensible : toujours autoriser**, ou ajoutez la valeur à `allow_values` dans
`config.yaml`. Pour désactiver toute une règle de secrets, ajoutez son identifiant à
`disabled_secret_rules` : pour une valeur que le tableau de bord affiche comme trouvée par
`gitleaks:generic-api-key`, l'identifiant est `generic-api-key`. Voir
[Listes d'autorisation](../concepts/profiles/#listes-dautorisation).

### Une valeur n'est pas masquée

- Vérifiez le [profil](../concepts/profiles/) : `secrets` ne masque pas les données personnelles.
- Les noms dans du texte libre nécessitent le
  [modèle local de noms](../getting-started/installation/#détection-des-noms) facultatif ou le
  [relecteur local](../concepts/llm-review/), et aucun des deux n'est infaillible : ajoutez les noms
  qui comptent comme [termes personnalisés](../concepts/detection/#vos-propres-termes).
- Collez le texte dans le **Banc d'essai** du tableau de bord pour voir ce que trouvent les règles
  actuelles.

### La réponse de `claude -p` montre des valeurs factices

C'est normal : en mode print, Claude Code ne remet pas les vraies valeurs en place dans ce qu'il
affiche. Les fichiers écrits par les outils contiennent les vraies valeurs, et l'interface
interactive les affiche aussi. Voir [Claude Code](../agents/claude-code/#limites). Sur une machine
connectée à un serveur Latet, le mode d'impression est retenu : voir
[Claude Code sur une machine connectée retient chaque prompt](#claude-code-sur-une-machine-connectée-retient-chaque-prompt).

### Les commandes sont lentes sous Windows

Chaque commande `latet` met plusieurs secondes à démarrer, `latet version` aussi, alors que vos
agents travaillent aussi vite que d'habitude. C'est votre antivirus. Il vérifie les programmes qu'il
ne connaît pas chaque fois qu'ils s'exécutent, et le petit lanceur qu'un outil d'installation crée
pour la commande `latet` (celui de uv comme celui de pip) en fait partie : non signé, et différent
sur chaque machine. Avec Windows Defender, cela coûte environ une demi-seconde ; certains antivirus,
Avira par exemple, prennent plusieurs secondes pour chaque lanceur. Latet lui-même démarre en une
fraction de seconde, et vos agents parlent au daemon qui tourne déjà : seul le démarrage d'une
commande attend.

Deux solutions :

- **Ajoutez Latet aux exclusions de votre antivirus.** Avec une installation faite par uv, les
  dossiers sont `%APPDATA%\uv\tools\latet` et `%USERPROFILE%\.local\bin` (dans le second,
  `latet.exe` et `latet-daemon.exe` suffisent).
- **Ou lancez-le avec Python lui-même,** qui est signé et démarre tout de suite : installez le
  fichier wheel de la [dernière version](https://github.com/Latet-AI/latet/releases/latest) avec
  `py -m pip install "latet[ner,ocr] @ https://github.com/Latet-AI/latet/releases/download/vX.Y.Z/latet-X.Y.Z-py3-none-any.whl"`
  (son numéro de version à la place de `X.Y.Z`) et lancez chaque commande sous la forme
  `py -m latet`, comme `py -m latet claude`. Le `latet.exe` que crée pip est aussi lent que les
  autres : laissez-le de côté. Latet partage alors ce Python avec vos autres programmes au lieu
  d'avoir le sien. Pour le mettre à niveau, lancez le même `py -m pip install` avec le wheel de la
  nouvelle version : `latet upgrade` installe avec uv, ce qui fait une seconde copie. Pour le
  supprimer, lancez `py -m latet global-uninstall`, puis `py -m pip uninstall latet`.

Avec un antivirus lent, le plugin Claude Code peut ne pas réussir à démarrer le daemon de lui-même à
temps, et il retient le contenu en attendant. Faites démarrer le daemon à l'ouverture de session
(`latet global-install` s'en charge), ou lancez `latet start` avant d'ouvrir Claude Code.

### Le port 5283 est déjà utilisé

Latet écoute sur `127.0.0.1:5283`, un port qu'aucun logiciel courant n'utilise. Si un autre programme
l'a pris, ou si Windows l'a réservé (Hyper-V, WSL et Docker réservent des plages de ports qui peuvent
changer à chaque démarrage), `latet start` le dit tout de suite :

```text
Latet cannot listen on 127.0.0.1:5283: in use by another program.
Run `latet global-install` to move it to a free port (your agents are updated too).
```

`latet global-install` choisit le premier port libre de 5284 à 5335, l'enregistre dans `config.yaml`
et fait pointer vers le nouveau port chaque agent qu'il a configuré. Si aucun n'est libre, choisissez-en
un vous-même : `latet global-install --port 6655`. `latet serve --port <port>` ne change le port que pour une seule
exécution au premier plan.

### Latet tourne dans Docker et les agents ne peuvent pas le joindre

Lancez `docker exec latet latet status`, puis vérifiez que le port est publié sur `127.0.0.1` et
qu'il est le même à l'intérieur et à l'extérieur (`LATET_PORT` définit les deux dans `compose.yaml`).
Après un changement de port, relancez les commandes `agents merge` que donne
`docker exec latet latet agents print`. Claude Code avec `LATET_AUTOSTART=0` ne démarre jamais de
daemon à lui : il attend le conteneur. Derrière un proxy, voir
[Docker](../getting-started/docker/#derrière-un-proxy).

### "browser requests are not accepted on proxy routes"

Vous avez ouvert une URL du proxy dans un navigateur web, ou une page web a essayé de la joindre.
C'est voulu : le daemon refuse les requêtes qui viennent des navigateurs. Les agents ne sont pas
concernés. Pour examiner le trafic, utilisez `latet dashboard`.

### "host not allowed"

La requête utilisait un nom d'hôte autre que `127.0.0.1`, `localhost` ou `[::1]`, ce que le daemon
n'autorise pas. Utilisez l'un d'eux dans l'URL de base.

### Codex ne passe pas par Latet

- Un Codex antérieur à la version 0.115 ignore `openai_base_url` : mettez-le à jour. `latet doctor`
  le signale, et `latet agents apply codex` laisse un tel Codex inchangé. En attendant, démarrez-le
  avec `latet codex`, qui lui passe l'adresse dans `OPENAI_BASE_URL`, la seule qu'il lit. Si Codex a
  son propre fournisseur (Azure, la passerelle de votre entreprise), `latet codex` ne le démarre pas,
  et indique de mettre Codex à jour.
- Lancez `latet agents status` : Codex devrait afficher `applied`, et sa route.
- Si vous avez changé de mode de connexion entre ChatGPT et une clé d'API, ou ajouté un fournisseur,
  relancez `latet agents apply codex`.

### Claude Code dit que ses requêtes contournent Latet

Le plugin a trouvé Claude Code configuré pour envoyer ses requêtes ailleurs, et a arrêté ses
prompts : rien ne vérifierait ce qui part. Le message indique le fichier qui l'a configuré ainsi.

- **Le `.claude/settings.json` de ce projet** (ou `.claude/settings.local.json`) définit
  `ANTHROPIC_BASE_URL`, l'adresse d'un cloud ou l'option qui l'active, ou un proxy (`HTTPS_PROXY`) :
  un dépôt ne décide jamais où vont les requêtes, même à travers Latet. Déplacez la ligne dans vos
  propres réglages et lancez `latet agents apply claude`, ou démarrez Claude Code avec
  `latet claude`, dont les réglages l'emportent sur l'adresse du projet. Un proxy défini par le
  projet arrête les prompts jusqu'à ce qu'il en soit retiré.
- **Plusieurs clouds sont activés** (`CLAUDE_CODE_USE_BEDROCK` et `CLAUDE_CODE_USE_VERTEX`, par
  exemple) : n'en gardez qu'un.
- **Aucune adresse**, ou un cloud joint directement (`CLAUDE_CODE_USE_BEDROCK` sans son adresse) :
  démarrez Claude Code avec `latet claude`, ou lancez une fois `latet agents apply claude` pour toutes
  les sessions.
- **Les réglages gérés de votre organisation** imposent une adresse : voir
  [Points de terminaison d'entreprise](../agents/company-endpoints/#là-où-latet-ne-peut-pas-se-placer-devant).

### Bedrock dit que la connexion AWS a expiré

Latet signe les requêtes vers Bedrock avec votre profil AWS, et la connexion derrière ce profil a
pris fin. Lancez la commande indiquée dans le message (`aws sso login --profile work`), puis
renvoyez : rien n'a besoin de redémarrer. Sans l'AWS CLI, Latet ne lit que les clés écrites dans
`~/.aws` ; une connexion SSO nécessite l'AWS CLI, et `latet doctor` le signale. Latet cherche `aws`
dans son propre PATH, puis dans `/usr/local/bin`, `/opt/homebrew/bin`, `/usr/bin`, `~/.local/bin` et
`/snap/bin` (dans `C:\Program Files\Amazon\AWSCLIV2` sous Windows). Si la vôtre se trouve ailleurs,
démarrez Latet depuis un shell où `aws` fonctionne.

### Un point de terminaison d'entreprise est injoignable

La requête a atteint Latet, et la destination vers laquelle il la transmet n'a pas répondu. Vérifiez
l'adresse sous Agents protégés → Où vont les requêtes dans le tableau de bord, ou avec
`latet config get upstreams` ; Claude Code affiche l'hôte de la destination dans son erreur.

### Claude Desktop ne passe pas par le daemon

C'est normal : l'application de bureau ignore `ANTHROPIC_BASE_URL`. Les sessions locales de son
onglet Code ont le plugin et sa vérification finale, mais pas la vérification du daemon, et elles ne
sont pas enregistrées dans le tableau de bord. Voir [Claude Code](../agents/claude-code/).

### Remote Control a disparu de Claude Code

C'est normal tant que Latet est sur le chemin des requêtes : Claude Code désactive Remote Control
quand son URL de base est personnalisée. `latet agents revert claude` le rétablit, et sort Claude
Code de Latet.

### Le tableau de bord dit que ses fichiers ne font pas partie de cette installation

Vous exécutez Latet depuis une copie locale du dépôt. Compilez le tableau de bord : `npm ci`, puis
`npm run build`, dans `dashboard/`. Les versions publiées l'incluent. La page du tutoriel en a
besoin aussi : d'ici là, `latet tutorial` avertit que sa page ne s'affichera pas.

### Le tutoriel ne s'ouvre pas, ou demande à être relancé

- **Aucun navigateur ne s'est ouvert :** lancez `latet tutorial --print-url`, et ouvrez le lien qu'il
  affiche dans le navigateur de votre choix. Le lien fonctionne une seule fois, pendant deux minutes.
- **La page demande de relancer le tutoriel :** son lien a déjà servi, ou plus de deux minutes se
  sont écoulées. Appuyez sur Ctrl+C là où tourne le tutoriel, et relancez-le : les chapitres que
  vous avez terminés le restent.
- **Il s'est arrêté sur une erreur :** il a affiché les dernières lignes de son journal, et garde le
  journal complet dans le dossier du bac à sable jusqu'à ce que vous le relanciez. Merci d'ouvrir
  une issue en y joignant ce journal.

### Une mise à jour échoue sous Windows car des fichiers sont utilisés

Utilisez `latet upgrade` : il arrête le daemon, installe la nouvelle version dans une fenêtre à part
une fois la commande terminée (Windows ne remplace aucun fichier d'un programme en cours
d'exécution), et redémarre Latet.

Un Latet 0.9.0 ou 0.9.1 mis à jour sous Windows avec son propre `latet upgrade` peut rester incapable
de démarrer, `latet` répondant alors `No module named 'latet'` : relancez le script d'installation,
qui installe la dernière version en entier.

### Impossible de trouver la dernière version

Les scripts d'installation et `latet upgrade` demandent à l'API de GitHub quelle est la dernière
version, et installent son fichier wheel. Quand ils n'y arrivent pas, ils disent pourquoi et ne
changent rien :

- **L'API de GitHub a répondu 403** (ou 429) : elle accepte 60 requêtes par heure depuis une même
  adresse, et un bureau derrière une seule adresse peut les épuiser. Tous deux lisent alors la page
  de la dernière version, qui n'est pas décomptée ; si cela échoue aussi, attendez une heure, ou
  installez à la main (ci-dessous).
- **GitHub est injoignable :** il n'y a pas de connexion, ou un proxy fait obstacle.
- **La dernière version n'a pas de fichier wheel,** ou il n'y a aucune version : installez
  [depuis les sources](../getting-started/installation/#exécuter-depuis-les-sources).

Pour installer une version à la main, prenez l'adresse de son fichier wheel sur la page des
[versions](https://github.com/Latet-AI/latet/releases), et donnez-la au script d'installation dans
`LATET_SOURCE` (il met aussi à jour un Latet déjà installé ; lancez ensuite `latet start`, et
`latet agents apply claude` si Claude Code passe par Latet pour de bon) :

```sh
curl -LsSf https://raw.githubusercontent.com/Latet-AI/latet/main/scripts/install.sh | LATET_SOURCE="latet[ner,ocr] @ https://github.com/Latet-AI/latet/releases/download/vX.Y.Z/latet-X.Y.Z-py3-none-any.whl" sh
```

Sous Windows (PowerShell) :

```powershell
$env:LATET_SOURCE = "latet[ner,ocr] @ https://github.com/Latet-AI/latet/releases/download/vX.Y.Z/latet-X.Y.Z-py3-none-any.whl"
irm https://raw.githubusercontent.com/Latet-AI/latet/main/scripts/install.ps1 | iex
```

Un fichier wheel que vous avez téléchargé fonctionne aussi : son chemin à la place de l'adresse.

## FAQ

### Latet fonctionne-t-il avec un abonnement Claude Pro ou Max ?

Oui, sans clé d'API : c'est à cela que sert le mode client. Claude Code tourne sans modification,
vous utilisez sa propre connexion, le plugin masque à l'intérieur de Claude Code, et le daemon
transmet chaque requête octet pour octet. Voir
[Claude Code](../agents/claude-code/#abonnements-pro-et-max).

### Hermes Agent ou OpenClaw peuvent-ils utiliser mon abonnement Claude à travers Latet ?

Non. Anthropic autorise un abonnement dans Claude Code et dans ses propres applications, et Latet ne
le prête à rien d'autre. Les agents autonomes passent par la passerelle avec une clé d'API : une clé
OpenAI ou OpenRouter (OpenRouter sert aussi les modèles Claude), ou une clé d'API Anthropic. Voir
[Hermes Agent, OpenClaw et autres agents](../agents/autonomous-agents/).

### Pourquoi Latet ne réécrit-il pas simplement le trafic de Claude Code ?

Claude Code ajoute à chaque requête un bloc d'attribution avec une empreinte de la conversation, et
le guide d'Anthropic sur les passerelles demande à une passerelle d'inspecter les requêtes sans les
modifier. Le masquage a donc lieu dans Claude Code, avant que la requête soit construite, et le
daemon se contente de la vérifier. Voir
[Comment ça marche](../concepts/how-it-works/#mode-client--claude-code).

### Le modèle peut-il encore faire son travail avec des valeurs factices ?

En général, oui. Les valeurs factices gardent le format de l'original, donc le code qui manipule une
clé, une adresse e-mail ou un IBAN garde tout son sens pour le modèle. Il ne connaît simplement
jamais la vraie valeur. Seule la valeur factice elle-même redevient la vraie valeur, cependant : si
le modèle écrit *Dear Laura* parce que l'adresse factice commence par `laura.`, cette salutation
reste.

### Le masquage casse-t-il le cache de prompts du fournisseur ?

Non. Une même valeur reçoit toujours la même valeur factice, et les tours précédents sont renvoyés
exactement comme avant : le fournisseur peut donc toujours les lire depuis son cache. La page
**Statistiques** du tableau de bord affiche quelle part de chaque prompt venait du cache.

### Où puis-je voir ce qui a réellement été envoyé ?

Lancez `latet dashboard`. Voir le [guide du tableau de bord](../dashboard/).

### Comment le désactiver ?

`latet global-uninstall` remet chaque agent comme il était, supprime le démarrage à l'ouverture de
session et arrête le daemon. Les outils que vous avez pointés vers Latet dans leurs propres réglages
gardent son adresse : remettez la leur. Relancer `latet global-install` le réactive.

### Latet envoie-t-il quelque chose quelque part ?

Seulement le trafic propre de votre agent, vers le fournisseur (ou le point de terminaison
d'entreprise) que votre agent utilise déjà. Les exceptions ont toutes lieu à votre demande :
`latet ner install` et `latet ocr install` téléchargent leurs modèles depuis huggingface.co une
fois ; les scripts d'installation et `latet upgrade` demandent à GitHub quelle est la dernière
version, et y téléchargent son fichier wheel ; et le relecteur local est interrogé sur votre propre
serveur. Il n'y a ni service cloud Latet, ni télémétrie, ni statistiques d'utilisation.

### Mes données sont-elles en sécurité une fois Latet installé ?

Plus en sécurité, pas en sécurité absolue. Latet ne masque que ce que ses détecteurs reconnaissent,
et certaines surfaces et certains types de fichiers sont hors de sa portée. Lisez
[Ce qui est protégé et ce qui ne l'est pas](../concepts/protection-scope/).

### Et les images et les PDF ?

Téléchargez les modèles de reconnaissance de texte avec `latet ocr install` (un Latet installé sans
le composant optionnel `ocr` sur lequel ils tournent l'obtient avec `latet upgrade`) : le texte des
captures d'écran et des images est alors lu sur votre machine et masqué, et chaque valeur est
recouverte sur l'image par sa valeur factice. Voir [Captures d'écran et images](../concepts/images/).

Les PDF et les autres documents ne sont pas lus : ils sont envoyés tels quels, et le tableau de bord
signale ces requêtes par l'indicateur *Non inspecté*. Évitez de joindre des fichiers qui contiennent
des données sensibles, ou définissez `images: block` (le profil `max` fait de même) pour qu'une
requête qui en transporte un ne soit pas envoyée.

### Une image a été retenue

Le modèle a reçu *[image withheld by Latet: ...]* à la place d'une image, avec la raison :
- *the text recognition models were still loading* : les modèles de reconnaissance de texte étaient
  encore en cours de chargement, car le daemon venait de démarrer. Renvoyez-la.
- *it could not be read* : elle n'a pas pu être lue. Il s'agit d'une image animée, d'une image de
  plus de 20 Mo ou de 40 mégapixels, ou d'un fichier qui n'est pas une image valide.
- *its text could not be read (OcrTooManyTiles)* : l'image est si longue pour sa largeur, ou si
  large pour sa hauteur, qu'il faudrait la lire en plus de 16 parties, comme la capture d'une page de
  plus de 29 000 pixels de long environ. Capturez la page en plusieurs fois, ou recadrez-la sur ce
  dont l'agent a besoin.
- *its text could not be read (OcrTooWide)* : une ligne de texte y est trop longue pour sa hauteur
  pour être lue, comme une seule ligne qui s'étend sur plusieurs écrans. Recadrez l'image sur ce dont
  l'agent a besoin.
- *its text could not be read (OcrTooLong)* : sa lecture a pris plus de temps qu'une lecture n'en peut
  prendre. Renvoyez-la, ou envoyez une image plus petite.
- *you withheld it in the Live Reviewer* : vous l'avez retenue dans le Relecteur en direct, en
  cochant *Ne pas envoyer cette image*.
- *the local reviewer saw a value in it ...* : le [relecteur local](../concepts/llm-review/#images),
  à qui l'image a été montrée, y a vu une valeur que Latet n'a pas trouvée dans le texte lu et n'a
  donc pas pu recouvrir. Masquez-la à la main dans le Relecteur en direct, ou recadrez l'image pour
  l'en retirer.

### Puis-je exécuter Latet dans un bac à sable avec mon agent ?

Oui. Latet n'est pas lui-même un bac à sable, et il fonctionne à l'intérieur d'un bac à sable :
installez-le là où tourne l'agent, pour que l'agent le joigne à `127.0.0.1`.

### Mon entreprise peut-elle utiliser Latet gratuitement ?

Oui, quelle que soit sa taille, pour son propre travail, sur autant d'ordinateurs et de serveurs
qu'elle veut. L'usage personnel et l'usage non commercial sont gratuits aussi. Pour l'édition
Community, une licence commerciale n'est nécessaire que pour vendre Latet, l'offrir à d'autres comme
service, l'intégrer dans un produit que vous vendez, ou pour un fork commercial : écrivez à
[info@latet.ai](mailto:info@latet.ai). L'Enterprise Edition, les fichiers des dossiers `ee`, ajoute ce
dont une entreprise a besoin pour gérer Latet de façon centralisée, et demande une licence commerciale
en production ; la développer et la tester est gratuit. L'édition Community ne vérifie jamais de
licence : il n'y a pas de clé. [LICENSING.md](https://github.com/Latet-AI/latet/blob/main/LICENSING.md) donne les
détails et des exemples.

### D'où vient le nom ?

*Latet* signifie "il se cache" en latin, comme dans *latet anguis in herba* : "un serpent se cache
dans l'herbe". Latet cache vos secrets à la vue de tous : le modèle voit une valeur en tout point
semblable à la vraie.
