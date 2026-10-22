---
title: Les agents et vos réglages
description: Laissez votre agent consulter et modifier les réglages de Latet. Ce qui ajoute de la protection s'applique ; ce qui la réduit vous attend.
---

:::caution[Un garde-fou, pas un verrou]
Ce mécanisme permet à un agent de vous aider à configurer Latet et vos projets sans réduire la
protection par erreur. Il ne résiste pas à un agent déterminé à le contourner, ni à un agent trompé
par une injection de prompt : l'agent s'exécute sous votre compte. Une vraie frontière demande
davantage, comme un Latet qui s'exécute sous un autre utilisateur ou sur une autre machine : voir
[ce que cela vaut](#comment-latet-le-sait-et-ce-que-cela-vaut).
:::

Vous pouvez demander à votre agent de configurer Latet pour vous : *"masque partout notre client
Falcon"*, *"active le Relecteur en direct dans ce projet"*, *"pourquoi cette valeur a-t-elle été
masquée ?"*. Indiquez-lui [`llms-config.md`](https://github.com/Latet-AI/latet/blob/main/llms-config.md)
dans le dépôt : il est écrit pour les agents, et leur dit quelles commandes utiliser et quoi vous
demander d'abord.

## Ce qu'un agent peut faire seul

Il consulte avec `latet config get`, `latet project show` et `latet status`, et modifie avec
`latet config set`, `add` et `remove`. Chaque valeur est vérifiée par rapport au schéma des réglages,
et la modification s'applique aussitôt au daemon en cours d'exécution,
**tant qu'elle ajoute de la protection** : un terme, un reconnaisseur, un profil plus strict, le
relecteur local qui lit davantage, le Relecteur en direct activé.

```sh
latet config add terms '{value: Project Falcon, type: PROJECT}'
latet config set live_review true --project .
```

## Ce qui vous attend

Une modification qui **réduit** la protection ne s'applique pas tant que vous ne l'avez pas approuvée,
quel qu'en soit l'auteur :

- une commande `latet` lancée par un agent, ou par un script ;
- `config.yaml` modifié à la main, quand le daemon le recharge ;
- `config.yaml` trouvé modifié au démarrage du daemon : tant que vous n'avez pas décidé, il fonctionne
  avec les derniers réglages que vous avez approuvés.

Réduire la protection, c'est par exemple une valeur à ne jamais masquer, un profil plus bas, un terme
ou un reconnaisseur supprimé, une politique définie sur `allow`, le relecteur local ou le Relecteur en
direct désactivé ou qui lit moins, Latet désactivé dans un projet, le serveur du relecteur local
déplacé vers une autre machine, ou [ses consignes](../llm-review/#vos-consignes) modifiées : un texte
libre pourrait lui dire de ne rien signaler, donc toute modification attend, et le tableau de bord
affiche les consignes avant et après. Il en va de même pour une
[destination](../../agents/company-endpoints/) déplacée vers une autre adresse, ou supprimée : un agent
qui pointe vers elle enverrait ses requêtes, et ses clés, ailleurs, donc le tableau de bord affiche
l'adresse avant et après.

La modification attend sur la page **Approbations** du tableau de bord, dans *Réglages*, avec ce
qu'elle réduit, d'où elle vient (l'agent qui a lancé la commande, quand Latet peut le savoir) et
quels réglages elle touche. **Approuver** l'applique. **L'annuler** remet `config.yaml` tel qu'il
était. Une seule modification attend à la fois ; une plus récente la remplace. Dans un terminal à
vous, c'est la commande qui vous pose la question.

L'agent apprend ce que la modification réduit, jamais une valeur, et qu'elle vous attend : le guide
lui demande de vous dire ce qu'il a changé et pourquoi.

**Un dépôt que Latet ne peut pas lire pour le moment.** Une modification est aussi pesée dans chaque
dépôt doté de son propre `latet.yaml` que Latet a vu. Un processus auxiliaire relit leurs fichiers,
avec 10 secondes pour tous et 2 pour chacun, à un rechargement ou à un démarrage dont la
modification pourrait réduire la protection dans un dépôt, et à une importation dans le tableau de
bord (voir [Les dépôts dont Latet se souvient](../projects/#les-dépôts-dont-latet-se-souvient)).
Quand l'un d'eux ne peut pas être lu dans ce délai, ou ne peut pas être lu du tout pour le moment (un
partage réseau qui ne répond pas, une permission refusée), une modification qui pourrait y réduire
la protection ne s'applique pas et n'attend pas non plus votre approbation : `latet reload` le dit et
ne change rien, de même qu'une importation dans le tableau de bord, jusqu'à ce que le dépôt puisse
de nouveau être lu. Une modification qui ne peut rien réduire dans un dépôt (un terme ajouté, le
port, l'historique) s'applique comme d'habitude. Un démarrage qui trouve `config.yaml` modifié sans
pouvoir le peser ainsi exécute les réglages que vous avez approuvés en dernier.
`latet repositories` indique de quel dépôt il s'agit, et `latet repositories forget` le retire de la
liste, dans un terminal à vous.

## Ce que les agents ne peuvent pas faire

`latet global-uninstall`, `latet agents revert` et `latet service uninstall` retirent la protection à
tous les agents d'un coup : lancées dans un agent, elles refusent, et vous demandent de les lancer dans
un terminal à vous. Il en va de même pour `latet repositories forget`, `latet backup` et
`latet restore` (votre coffre-fort et sa clé), `latet connect` et `latet disconnect` (là où les
requêtes de vos agents sont masquées), `latet dashboard` et `latet identity send` sur une machine
connectée, l'administration `latet server`, et `latet rules suggest --show-values`, qui afficherait de
vraies valeurs.

## Comment Latet le sait, et ce que cela vaut

Un agent marque les shells dans lesquels il lance des commandes avec ses propres variables, que Latet
reconnaît : Claude Code, Codex, OpenCode, Gemini CLI, Cursor, Hermes Agent, Goose, OpenClaw, et tout
agent qui définit `AGENT`. Le daemon nomme aussi l'agent dans la modification qu'il vous présente.

Ce sont des protections contre les erreurs, pas des verrous :

- **La vérification qui compte est celle du daemon.** Il compare chaque nouvelle configuration avec
  la dernière que vous avez approuvée, quel qu'en soit l'auteur. Un agent qui modifie `config.yaml` au
  lieu d'utiliser les commandes reçoit la même réponse.
- **Un agent s'exécute sous votre compte.** Un agent qui le veut vraiment, ou qui est trompé par un
  texte qu'il a lu (une injection de prompt), peut supprimer ses variables, lire les fichiers que lit
  Latet, et approuver sa propre modification par l'interface locale du daemon. Latet ne peut pas
  empêcher ce que votre propre compte utilisateur peut faire : il faudrait pour cela que Latet
  s'exécute sous un autre utilisateur, ou sur une autre machine
  ([Latet sur un serveur](../server/)).

Pour un agent que vous supervisez, cela suffit : il ne peut pas affaiblir Latet par erreur, et vous
voyez chaque modification qui réduit la protection avant qu'elle ne s'applique. Voir
[Sécurité](../../security/) pour le reste du modèle de menaces.

## Sur une machine connectée

Les réglages partagés et les réglages et règles propres à chaque identité résident sur le
serveur. Utilisez `latet dashboard` pour une personne et l'administration `latet server` pour
les réglages partagés ou ceux d'une clé d'API. Pendant la connexion, `config`, `rules`,
`llm`, `project`, `identity`, `repositories` et `reload` refusent de lire les réglages comme de les
modifier ; seuls `config schema`, `rules test` et `identity send` s'exécutent.
`latet identity send` renvoie explicitement les informations d'identité de cette machine.

Métadonnées de connexion, empreinte du certificat, identifiants et liaisons d'agents restent
sur la machine. `LATET_CONNECTION` choisit des métadonnées, pas un fichier de jeton personnel.
L'intégration Claude écrit le jeton sous Windows dans les réglages utilisateur Claude comme
exception d'installation documentée (sous Linux et macOS aucun jeton n'est écrit, puisque Claude Code
passe par un serveur depuis Windows seulement) ; la déconnexion retire la liaison. Points de connexion du
relecteur serveur, clés, certificats et installation OCR/NER relèvent des administrateurs.
Un projet dans le tableau de bord ne permet pas au serveur de parcourir le dépôt de la machine.
Voir [configuration du serveur](../../getting-started/server/) et
[réglages partagés et règles propres](../server/).

## Commandes

| Commande | Ce qu'elle fait |
| --- | --- |
| `latet config get [KEY]` | Un réglage (des points pour ce qui est à l'intérieur : `llm_review.enabled`), ou tous |
| `latet config set KEY VALUE` | Modifie un réglage (la valeur en YAML) ; `--unset` rétablit sa valeur par défaut |
| `latet config add KEY VALUE` / `remove KEY VALUE` | Ajoute un élément à une liste, ou en retire un (les termes par valeur, les reconnaisseurs par nom) |
| `--project FOLDER` | Vos propres réglages pour le projet de ce dossier, au lieu des réglages globaux |
| `--wait` | Quand la modification vous attend, attend votre réponse |
| `latet reload` | Applique un `config.yaml` modifié à la main (ce qui réduit la protection vous attend) |

Codes de sortie : `0` appliquée, ou enregistrée alors que le daemon ne tourne pas (elle s'applique au
démarrage du daemon, et ce qui réduit la protection vous attend alors) ; `3` vous attend ; `1` le daemon
a refusé la modification (enregistrée, non appliquée : `latet reload` dit pourquoi), ou l'élément à
retirer n'y est pas ; `2` non valide (rien n'est enregistré).
