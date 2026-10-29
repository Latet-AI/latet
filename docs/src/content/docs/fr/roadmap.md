---
title: Feuille de route
description: Les prochaines étapes de Latet, la plus proche d'abord.
---

Les prochaines étapes, la plus proche d'abord. Les idées et l'aide sont les bienvenues : ouvrez une
[issue](https://github.com/Latet-AI/latet/issues), ou consultez [Contribuer](../contributing/). Ce
qui a déjà changé se trouve dans le [journal des modifications](../changelog/).

## Le mode auto de Claude Code, un refus explicite

Quand une relecture du mode auto transporterait un détail que vous gardez en local (votre nom
d'utilisateur, vos chemins), Latet l'arrête aujourd'hui, et Claude Code signale son relecteur comme
indisponible et refuse l'action. Latet pourrait plutôt répondre avec le verdict propre du relecteur,
*blocked*, pour que le refus apparaisse pour ce qu'il est. Cette réponse viendrait de Latet à la
place du relecteur d'Anthropic, alors nous demandons d'abord : une issue expose le cas avec la pull
request prête, nous l'envoyons à Anthropic, et la pull request n'est fusionnée qu'avec l'accord
d'Anthropic, joint à l'issue.

## Un programme d'installation signé pour Windows

Les antivirus vérifient un programme qu'ils ne connaissent pas chaque fois qu'il démarre, et les
lanceurs d'une installation Python ne sont pas signés : sous Windows, chaque commande peut donc
attendre plusieurs secondes avant de démarrer (voir
[Dépannage](../troubleshooting/#les-commandes-sont-lentes-sous-windows)). Un programme d'installation signé,
proposé aussi via winget, permet aux commandes de démarrer tout de suite.

## Votre propre modèle de noms

Aujourd'hui, Latet utilise un seul modèle de noms, [Privacy Filter](https://huggingface.co/openai/privacy-filter)
d'OpenAI, qu'il télécharge à la demande (`latet ner install`) et qui lit de nombreuses langues. Un
modèle à vous trouverait les noms et les termes de votre domaine, comme les médicaments et les diagnostics dans une clinique, ou les parties et les tribunaux
dans un cabinet d'avocats : un classificateur de jetons ONNX sur votre machine, ou un point de
terminaison que vous hébergez, comme un service GLiNER ou Presidio. Plusieurs modèles pourraient
travailler ensemble, chacun ajoutant ce qu'il trouve.

## Davantage de la Gemini API de Google

Lire le reste de ce qu'envoient les clients de Gemini : l'Interactions API, la Live API, la Batch API,
l'envoi de fichiers, la génération d'images, et les requêtes `:predict` de Vertex (ses propres modèles
d'embeddings et d'images). D'ici là, Latet les refuse sur ses routes Gemini plutôt que de les envoyer
sans les masquer (voir [Gemini CLI et Google GenAI](../agents/gemini/)).

## D'autres agents

OpenCode connecté avec ChatGPT, ou avec ses modèles sur Amazon Bedrock ou Azure. L'API Mantle
d'Amazon Bedrock pour Claude Code, et Bedrock via un point de terminaison VPC ou son adresse FIPS avec
votre connexion AWS (avec une clé d'API Bedrock, les deux fonctionnent déjà). Antigravity CLI, vérifié
avec Latet.

## pi, oh-my-pi et ThinkRail

Un plugin pour [pi](https://github.com/earendil-works/pi) et
[oh-my-pi](https://github.com/can1357/oh-my-pi) masquerait les valeurs sensibles dans l'agent
lui-même, avant que chaque requête soit construite, comme le fait le plugin pour Claude Code. Le
masquage ne dépendrait pas du fournisseur : il couvrirait JetBrains AI et les abonnements autant que
les clés d'API, et chaque requête passerait aussi par la passerelle de Latet et apparaîtrait dans le
tableau de bord. JetBrains ThinkRail utilise pi, donc ses conversations seraient protégées aussi ;
ses sous-agents et un bouton vers le tableau de bord attendent que ThinkRail charge des extensions
écrites par d'autres.

## Latet Enterprise

Latet Enterprise s'adresse à une entreprise dont l'équipe de sécurité fixe les règles pour tout le
monde. En plus de l'édition Community, il ajoute des règles pour tout le monde que personne ne peut
assouplir, des rôles d'administration comme celui d'auditeur en lecture seule, des approbations
centralisées des changements qui réduisent la protection, l'authentification unique, des rapports
d'audit et des événements pour un SIEM. [Inscrivez-vous à la liste d'attente](https://latet.ai/#waitlist)
pour savoir quand il sortira.

## D'autres langues

Le tableau de bord, le tutoriel et la documentation existent en anglais, en italien, en allemand, en
français et en espagnol. D'autres langues sont ouvertes aux contributions : voir
[Contribuer](../contributing/#comment-ajouter-une-langue).
