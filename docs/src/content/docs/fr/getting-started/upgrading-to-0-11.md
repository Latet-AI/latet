---
title: Passer à la 0.11
description: Ce que Latet 0.11 refuse que la 0.10 envoyait, ce qu'il faut vérifier avant la mise à jour et comment revenir en arrière.
---

Latet 0.11.0 lit chaque partie d'une requête avant de l'envoyer : le chemin, les paramètres de
requête, les en-têtes et le corps. Une requête dont une partie n'est ni inspectée par Latet ni admise
par une règle ou une exception que vous avez approuvée n'est pas envoyée. Un corps vide ne suffit
plus pour laisser passer une requête. Les identifiants dont votre fournisseur a besoin, et les
parties qu'un fournisseur signe, suivent leurs propres règles. Il en va de même sur votre ordinateur
et sur un serveur Latet.

Un appel que la 0.10 envoyait sans le lire peut donc être refusé maintenant. Cette page indique ce qui
change, ce que `latet doctor` vérifie pour vous avant et après la mise à jour, et que faire dans
chaque cas.

## Ce que la 0.11 refuse que la 0.10 envoyait

**Les appels sans corps.** La liste des modèles et la lecture d'un modèle passent chez chaque
fournisseur que Latet connaît : Anthropic, OpenAI, OpenRouter, ChatGPT (la liste des modèles de
Codex), Gemini et les profils d'inférence de Bedrock. Leurs paramètres de requête sont lus aussi
(`limit`, `after_id`, `pageToken`, `maxResults`...). Tout autre appel est refusé : fichiers et
téléversements, lots, audio, génération d'images, modération, lecture ou suppression d'une réponse
enregistrée, lecture d'un cache de Gemini, les points de terminaison d'un compte. L'agent en est
informé, et rien n'est envoyé :

```text
Latet: request envelope refused (envelope-route): route /{destination}/{unsupported-route}; this version of Latet does not support this call, so change the agent or its settings so that it does not make it. Nothing was sent; run `latet dashboard` to see where it was
```

Une exception nomme un en-tête, un paramètre de requête ou un membre du corps, jamais une route
entière : dans la 0.11.0, aucune exception ne laisse passer l'un de ces appels. Changez les réglages
de l'agent pour qu'il ne fasse pas l'appel, ou faites-le sans Latet, à l'adresse du fournisseur, en
sachant que Latet ne voit pas ce qui est envoyé.

**Les paramètres de requête.** Un paramètre de requête qu'aucun profil ne nomme est refusé, et
l'agent apprend son nom, jamais sa valeur :

```text
Latet: request envelope refused (envelope-query-unknown): query parameter drift. Nothing was sent; run `latet dashboard` to see where it was
```

**Les en-têtes.** Chaque en-tête est lu selon une règle propre : un identifiant est envoyé tel qu'il
est arrivé et jamais journalisé ; un en-tête propre à un agent doit avoir la forme que cet agent lui
donne (un identifiant de session doit être un UUID, par exemple) ; un en-tête qui peut contenir du
texte libre, comme `user-agent`, passe la vérification finale. Un en-tête qu'aucun profil ne nomme est
refusé, et l'agent apprend son nom, jamais sa valeur :

```text
Latet: request envelope refused (envelope-header-unknown): header x-team. Nothing was sent; run `latet dashboard` to see where it was
```

Certains en-têtes sont retirés à la place : ceux de télémétrie et de traçage des SDK, et quelques
en-têtes propres aux agents qui peuvent contenir du texte libre.
[Les en-têtes que Latet retire](#les-en-têtes-que-latet-retire) les liste.

**Les membres du corps.** Chaque membre du corps d'une requête est lu selon son profil : masqué,
vérifié par la vérification finale, ou tenu à la forme que le fournisseur lui donne (l'identifiant
d'un modèle, l'identifiant d'un appel d'outil, un réglage). Un membre qu'aucun profil ne nomme est
refusé, avec l'endroit où il se trouve (`top-level`, `message content block`, `tool definition`...),
jamais son nom ni sa valeur :

```text
Latet: request body refused (unknown member in top-level). Nothing was sent; run `latet dashboard` to see where it was
```

Une [exception exacte](#approuver-une-exception-exacte) peut nommer un tel membre par son pointeur
JSON, sauf là où aucune ne peut l'atteindre : dans un élément propre au fournisseur (`provider item` :
thinking, raisonnement), dans la source d'une image (`inline image`) et dans toute trame WebSocket.
L'agent reçoit les mêmes mots ; le tableau de bord enregistre le refus sous
`body-unknown-member-no-exception` au lieu de `body-unknown-member`, et dit *Son corps contenait un
champ qu'aucun profil de requête ne prévoit, là où aucune exception ne peut l'atteindre (dans un
élément du fournisseur, la source d'une image ou un tour WebSocket).*

Une requête que Latet ne fait que vérifier (Claude Code, Bedrock, Vertex) n'est jamais réécrite, donc
une règle de plus s'y applique : un nouveau membre dont la valeur est un texte ou une liste de textes
passe une fois que la vérification finale a lu son nom et son texte, et le tableau de bord l'affiche
comme *seulement vérifiée*. Un nouveau membre imbriqué, ou d'un autre type, est refusé comme ci-dessus.
`client_metadata` de Codex, qui répète le chemin et les remotes du dépôt, est retiré de la requête
avant qu'elle soit masquée.

**Ce que le fournisseur a renvoyé.** Les signatures du raisonnement du modèle, le raisonnement chiffré,
les compactages et les résultats de recherche web reviennent à chaque tour. Ils passent parce que
Latet les a vus arriver du fournisseur dans cette conversation. Là où il ne les a pas vus (le daemon
d'un ordinateur a redémarré, par exemple), une règle intégrée laisse passer les champs chiffrés du
fournisseur, seulement dans les tours du modèle et seulement vers l'adresse du fournisseur lui-même ;
le tableau de bord les affiche comme *autorisée par une règle*. Une conversation passant par une
[passerelle d'entreprise](#conversations-par-une-passerelle-dentreprise) n'a pas cette règle.

**Images et documents.** Une image n'est envoyée qu'une fois que Latet en a lu le texte. Sans la
reconnaissance de texte, une requête qui porte une image est refusée, avec des mots qui disent
d'exécuter `latet ocr install` et de commencer une nouvelle conversation, puisque l'image reste dans
son historique. `images: allow` n'envoie plus une image que Latet n'a pas lue. Les PDF et autres
documents, l'audio et les images désignées par leur adresse sont refusés, quoi que dise `images`. Sur
une requête que Latet ne fait que vérifier (Claude Code, Bedrock, Vertex), chaque octet d'une image
doit être expliqué, et une photo de téléphone avec une position, les notes du fabricant de
l'appareil, une miniature ou une seconde image à l'intérieur (une carte de profondeur, la vidéo
d'une photo animée) est refusée : voir
[Les photos et leurs métadonnées](../../concepts/images/#les-photos-et-leurs-métadonnées). Une image
WebP d'origine y est refusée aussi : exportez-la en PNG. Les requêtes que Latet réécrit (Codex et les
autres) envoient les images réécrites, sans leurs métadonnées, et ne voient pas ces refus.

**Un projet où Latet est désactivé** n'envoie rien : voir [plus bas](#un-projet-où-latet-est-désactivé).
**`latet serve --passthrough`** n'existe plus : l'option reste cachée pendant toute la 0.11, et
s'arrête aussitôt avec un message, pour qu'un service qui la nomme encore s'arrête là où vous le voyez.
Retirez-la de la commande du service, ou du script qui démarre Latet.

**Un dossier nommé par une requête ne fait qu'ajouter de la protection :** voir
[Les dossiers qu'une requête nomme](#les-dossiers-quune-requête-nomme).

## Les en-têtes que Latet retire

Latet retire ces en-têtes d'une requête avant de l'envoyer, quel que soit l'agent qui les a envoyés :

| Où | En-têtes |
| --- | --- |
| Chaque requête : télémétrie et traçage des SDK | `x-stainless-*`, `x-datadog-*`, `x-b3-*`, `traceparent`, `tracestate`, `baggage` |
| Claude Code, au format d'Anthropic (Anthropic, Bedrock, Vertex, une passerelle d'entreprise) | `x-claude-code-agent-id` et `x-claude-code-parent-agent-id`, les identifiants d'un sous-agent, qui peuvent contenir son nom ; `x-claude-remote-session-id`, `x-claude-remote-container-id`, `x-claude-remote-session-origin` et `x-client-app`, que Claude Code copie de son environnement |
| Bedrock : le SDK d'AWS | `amz-sdk-*`, `x-amz-user-agent` |
| Gemini : la bibliothèque cliente de Google | `x-goog-api-client` |
| Codex, au format d'OpenAI et à celui de ChatGPT | `x-codex-turn-metadata`, qui contient le chemin, les remotes et le commit de votre dépôt ; `x-openai-subagent`, le type ou une étiquette libre d'un sous-agent |

Les en-têtes `x-latet-*` propres à Latet n'atteignent jamais un fournisseur non plus. Un identifiant
n'est jamais retiré. Sur une requête que Latet ne fait que vérifier (Claude Code, Bedrock, Vertex), le
corps part octet pour octet : ces en-têtes sont le seul changement. `client_metadata` de Codex, la
copie de `x-codex-turn-metadata` dans le corps, est retiré du corps d'une requête que Latet réécrit.

Chaque échange du tableau de bord compte les en-têtes et les membres du corps qui lui ont été
retirés. Quand le fournisseur répond `401` ou `403` à une requête dont certains ont été retirés,
l'agent en est informé, et l'échange dit que cet en-tête peut être l'un de ceux dont le fournisseur a
besoin :

```text
Latet: the provider answered 401 after Latet removed 2 SDK/tracing headers
```

## Ce que vérifie `latet doctor`

Lancez `latet doctor` avant la mise à jour, et à nouveau après. Il liste, par type et par nombre,
jamais une valeur :

- les projets où Latet est désactivé (les requêtes de chacun seront refusées) ;
- la reconnaissance de texte non installée, ou désactivée par `ocr: off` (chaque requête portant
  une image sera refusée) ;
- `--passthrough` dans une entrée qui démarre Latet à l'ouverture de session ;
- les appels vus dans votre historique qu'aucun profil ne couvre, par leur méthode et leur modèle de
  route, jamais leur chemin ;
- vos exceptions permanentes ;
- les agents lancés par Latet dont les requêtes sont arrivées sans leur clé de lancement, avec l'agent
  et sa version.

Le daemon dit la même chose dans son journal à son premier démarrage en 0.11.

## Installer la reconnaissance de texte

```sh
latet ocr install   # environ 31 Mo, une seule fois
```

`latet upgrade` ajoute ce sur quoi tournent les modèles à un Latet installé sans. Dans Docker :
`docker exec latet latet ocr install --yes`. Sur un serveur Latet, les administrateurs installent les
modèles sur chaque instance
([Modèles de reconnaissance sur un serveur](../../concepts/server-operations/#modèles-de-reconnaissance-sur-un-serveur)).
Voir [Captures d'écran et images](../../concepts/images/).

## Les en-têtes que vous définissez pour vos agents

Un agent configuré avec des en-têtes à vous les envoie avec chaque requête, et la 0.11 les refuse tant
que vous ne les laissez pas passer : `ANTHROPIC_CUSTOM_HEADERS` de Claude Code, `http_headers` et
`env_http_headers` de Codex, `COPILOT_PROVIDER_HEADERS` de Copilot CLI, `options.headers`
d'OpenCode, `GEMINI_CLI_CUSTOM_HEADERS` de Gemini CLI. Le refus nomme l'en-tête. Laissez passer
chacun par une exception exacte, comme ci-dessous, en nommant la destination où il va ; la clé d'une
passerelle d'entreprise dans un en-tête se nomme comme un identifiant, que Latet envoie tel qu'il est
arrivé et ne lit jamais. La clé de lancement de Latet, que `latet claude` et les autres ajoutent à ces
réglages, ne se nomme pas : Latet la retire avant de transmettre quoi que ce soit.

## Approuver une exception exacte

Une exception nomme exactement ce qu'elle laisse passer, sur une destination et un profil de
requête : un en-tête, un paramètre de requête ou un membre du corps JSON. Par exemple, pour laisser
l'en-tête `x-team` de Claude Code arriver chez Anthropic :

```sh
latet config add request_exceptions '{destination: anthropic, profile: anthropic.messages.1, components: [{kind: header, name: x-team}], reason: custom-header}'
```

La même chose dans `config.yaml` :

```yaml
request_exceptions:
  - destination: anthropic           # la route d'un fournisseur, ou l'une de vos destinations
    profile: anthropic.messages.1    # une méthode, une route et un transport
    components:
      - {kind: header, name: x-team}                  # vérifié : sa valeur passe la vérification finale
    reason: custom-header
  - destination: llm-acme-com
    profile: openai.chat.1
    components:
      - {kind: header, name: x-acme-key, role: credential}   # envoyé tel qu'il est arrivé, jamais lu
    reason: destination-credential
  - destination: openai
    profile: openai.responses.1
    components:
      - {kind: body, name: /metadata_x/team, type: string}  # un pointeur JSON exact et le type de sa valeur
    reason: other
```

- **Elle attend votre approbation.** Une exception réduit la protection, donc elle ne s'applique qu'une
  fois que vous l'approuvez, sur la page **Approbations** du tableau de bord, dans *Réglages*. Latet
  lui donne alors un identifiant (`x1a2b3c4d5e`) et l'heure de son approbation. En retirer une, ou lui
  faire nommer moins, s'applique aussitôt.
- **Elle dure 30 jours** à partir de son approbation, par défaut, et 90 au plus (`days`). Le tableau de
  bord prévient sept jours avant sa fin, et une requête refusée après nomme l'exception et le jour où
  elle a pris fin. La renouveler est une nouvelle approbation.
- **Permanente, exprès.** `permanent: true` en fait une qui n'expire jamais. Elle s'approuve comme
  toute exception, est toujours marquée *permanente* dans le tableau de bord, et listée par
  `latet doctor` et, sur un serveur, par `latet server status` ; en approuver une sur un serveur écrit
  une entrée dans le journal d'administration, avec son identifiant et le type de sa portée seulement.
- **Pour qui.** Une exception sans `project` s'applique à chacune de vos requêtes, quel que
  soit l'agent qui l'envoie, et le tableau de bord le dit avant que vous l'approuviez. Avec
  `project: C:\dev\acme`, elle ne s'applique qu'aux requêtes dont Latet considère le projet comme de confiance :
  un agent qu'il a lancé avec sa clé, ou Claude Code avec le plugin (voir
  [Projets](../../concepts/projects/#dans-quel-projet-se-trouve-une-session)).
- **Jamais dans un dépôt.** Le `latet.yaml` d'un projet ne peut pas contenir d'exceptions. Au plus 128,
  32 pour un projet, chacune nommant au plus 64 composants ; `description` est pour vous seul, jamais
  dans un journal.
- **Pas sur un WebSocket.** Dans la 0.11.0, une exception ne s'applique pas à un WebSocket : Codex
  connecté avec ChatGPT, qui en utilise un, ne peut envoyer aucun de vos propres en-têtes. Le tableau
  de bord n'en propose aucune pour un refus là, et dit *Dans cette version de Latet, un WebSocket ne
  prend aucune exception aux requêtes.*
- **Après une mise à jour.** Une version ultérieure qui change une partie d'un profil nommée par une
  exception suspend cette exception jusqu'à ce que vous l'approuviez à nouveau ; le tableau de bord et
  `latet doctor` disent qu'elle est à approuver de nouveau (*À approuver de nouveau : son profil a
  été révisé*), les permanentes aussi ; sur un serveur, `latet server status` la liste dès que
  l'espace de l'identité a démarré avec la nouvelle version. Une mise à jour n'élargit jamais ce
  qu'une exception laisse passer.

Le profil est le type d'appel. Une exception nomme un seul profil, donc un en-tête qu'un agent envoie
avec chaque appel demande une exception pour chaque type d'appel qu'il fait :

| Appels | Profil |
| --- | --- |
| Anthropic Messages (Claude Code, le SDK d'Anthropic), comptage des tokens, la liste des modèles et un modèle | `anthropic.messages.1`, `anthropic.count_tokens.1`, `anthropic.models.1`, `anthropic.model.1` |
| Le format d'OpenAI (OpenAI, OpenRouter, une passerelle d'entreprise) : Chat Completions, les anciens completions, Responses en HTTP, embeddings, la liste des modèles et un modèle | `openai.chat.1`, `openai.completions.1`, `openai.responses.1`, `openai.embeddings.1`, `openai.models.1`, `openai.model.1` |
| Codex connecté avec ChatGPT, en HTTP : Responses et la liste des modèles | `chatgpt.codex.responses.1`, `chatgpt.codex.models.1` |
| Gemini, par l'API Gemini ou Vertex AI : génération, streaming, comptage, embeddings (un seul ou par lot), la liste des modèles et un modèle, création ou modification d'un cache, le chat et les embeddings compatibles avec OpenAI | `gemini.<version>.generateContent.1`, `gemini.<version>.streamGenerateContent.1`, `gemini.<version>.countTokens.1`, `gemini.<version>.embedContent.1`, `gemini.<version>.batchEmbedContents.1`, `gemini.<version>.models.1`, `gemini.<version>.model.1`, `gemini.<version>.cache-create.1`, `gemini.<version>.cache-patch.1`, `gemini.<version>.openai.completions.1`, `gemini.<version>.openai.embeddings.1` |
| Bedrock : appel, streaming, comptage des tokens, les profils d'inférence et l'un d'eux | `bedrock.invoke.1`, `bedrock.invoke-with-response-stream.1`, `bedrock.count-tokens.1`, `bedrock.inference-profiles.1`, `bedrock.inference-profile.1` |
| Claude sur Vertex AI | `vertex.anthropic.rawPredict.1`, `vertex.anthropic.streamRawPredict.1` |
| Une passerelle d'entreprise devant Bedrock ou devant Claude sur Vertex AI | `company.bedrock.invoke.1`, `company.bedrock.invoke-with-response-stream.1`, `company.bedrock.count-tokens.1`, `company.vertex.rawPredict.1`, `company.vertex.streamRawPredict.1` |
| Destinations Azure OpenAI | `azure-openai.chat.1`, `azure-openai.completions.1`, `azure-openai.responses.1`, `azure-openai.embeddings.1` |
| Destinations Microsoft Foundry | `foundry.chat.1`, `foundry.embeddings.1` |

Dans un identifiant Gemini, `<version>` est celle que nomme le chemin de la requête : `v1` ou
`v1beta` sur l'API Gemini, `v1` ou `v1beta1` sur Vertex AI (`gemini.v1beta.generateContent.1`).

Sur un serveur Latet, les exceptions font partie des réglages propres à chaque personne, qui les
approuve dans le tableau de bord ; celles d'une clé d'API se définissent par l'administration.

## Bedrock et Vertex : ce qu'il faut lister

Un identifiant de modèle écrit sous la forme documentée par un fournisseur passe de lui-même, dans un
chemin comme dans un corps, de même qu'un profil d'inférence de Bedrock défini par Amazon
(`us.anthropic.claude-…`, `eu.`, `apac.`, `global.`). Ce qui appartient à votre compte ou à votre
projet cloud doit être listé, dans `request_resources`, avant qu'une requête qui le nomme soit
envoyée : tout ARN dans un chemin de Bedrock (l'ARN d'un profil d'inférence d'application compris)
et le projet Vertex. Chaque ligne nomme la destination, son adresse, le rôle de la valeur et la
valeur :

```yaml
request_resources:
  - destination: bedrock
    url: https://bedrock-runtime.us-east-1.amazonaws.com    # appels au modèle ; les lectures vont à https://bedrock.us-east-1.amazonaws.com
    role: model                                             # inference-profile, pour en lire un
    value: arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/a1b2c3d4e5f6
  - destination: vertex
    url: https://us-east5-aiplatform.googleapis.com
    role: project
    value: acme-ml-prod
```

`latet config add request_resources '{...}'` en ajoute une. Une ligne laisse passer exactement cette
valeur vers cette destination et rien d'autre, et s'applique aussitôt. Tant qu'elle manque, une telle
requête est refusée avec `envelope-resource`. Un identifiant de modèle hors des formes documentées
(les noms de modèles propres à une passerelle d'entreprise) se liste de la même façon, avec le rôle
`model`.

Sur OpenRouter, un modèle est nommé avec son fournisseur. Ceux d'Anthropic, d'OpenAI et de Google
passent d'eux-mêmes quand la partie après le fournisseur est dans la forme documentée de ce
fournisseur, avec ou sans une variante comme `:free` ou `:thinking` (`anthropic/claude-sonnet-4.5`,
`openai/gpt-4o`, `google/gemini-2.5-pro`), et `openrouter/auto` aussi. Le modèle de tout autre
fournisseur (`meta-llama/…`, `deepseek/…`) se liste avec la destination `openrouter`, l'adresse
`https://openrouter.ai` et le rôle `model`.

## Un projet où Latet est désactivé

Dans la 0.10, un projet où Latet était désactivé envoyait ses requêtes sans les masquer. Dans la 0.11,
Latet n'en envoie aucune, en HTTP ou par un WebSocket, que le projet vienne du lancement de l'agent,
du plugin ou d'un dossier que la requête nomme. L'agent l'apprend :

```text
Latet is off in this project: Latet sends no request from there; turn it on for the project (`latet project on` in its folder, or the project's switch in the dashboard), or reach the provider directly for this folder by starting the agent without Latet's address. Nothing was sent; run `latet dashboard` to see where it was
```

Quand le projet n'est que nommé par la requête (déclaré, plus bas), les mots commencent par
`request claimed a project where Latet is off:` et continuent de la même façon.

`latet project off` et l'interrupteur du tableau de bord le disent avant de désactiver un projet. Pour
travailler dans ce dossier sans Latet, lancez-y l'agent sans l'adresse de Latet : un agent que vous
lancez avec `latet claude`, `latet codex` ou une autre commande `latet` se lance seul (`claude`,
`codex`) ; un agent dont les réglages pointent vers Latet (après `latet global-install`) passe par
Latet où qu'il démarre, donnez-lui donc pour cette session l'adresse de son fournisseur, comme le dit
sa documentation, ou utilisez `latet agents revert`. Latet ne voit alors rien de ce qu'il envoie. Si
vous aviez désactivé Latet pour laisser passer les données personnelles, *Secrets uniquement* fait
exactement cela et continue de protéger les secrets
([Projets](../../concepts/projects/#secrets-uniquement-ou-latet-désactivé-dans-un-projet)).

## Les dossiers qu'une requête nomme

Une requête peut nommer son dossier : l'en-tête `x-latet-project`, ou le dossier de travail que Codex,
OpenCode et Gemini CLI écrivent dans leurs requêtes. Dans la 0.11, un tel dossier est *déclaré* : les
règles de son projet qui ajoutent de la protection s'appliquent, ses assouplissements non (valeurs à
ne jamais masquer, règles de secrets désactivées, un profil plus faible, `data_tools`, les consignes
et les images du relecteur local). Un projet déclaré où Latet est désactivé est refusé, comme
ci-dessus. Un projet est *de confiance* quand Latet le connaît par lui-même : par la clé de lancement d'un
agent qu'il a lancé, ou par le plugin de Claude Code. Le tableau de bord montre pour chaque requête si
son projet était de confiance ou déclaré, et comment son lancement a été jugé. Voir
[Projets](../../concepts/projects/#dans-quel-projet-se-trouve-une-session).

Ce que cela veut dire pour chaque agent :

- **Lancé par Latet** (`latet claude`, `latet codex`, `latet opencode`, `latet gemini`,
  `latet copilot`) : de confiance grâce à sa clé de lancement, là où l'agent la porte. Codex sur ses
  fournisseurs intégrés (connecté avec ChatGPT, ou le fournisseur d'OpenAI), Codex avant la 0.115,
  OpenCode avant la 1.19.5, Gemini CLI en mode ACP, Copilot CLI hors de Chat Completions et les programmes que lance `latet run` n'en portent pas : la
  commande qui les lance le dit en une phrase, et leurs projets sont déclarés.
- **Claude Code avec le plugin**, configuré durablement : de confiance grâce au plugin.
- **Tout autre agent configuré durablement** : déclaré. Vos propres règles et toute exception sans
  `project` s'appliquent toujours.

## Conversations par une passerelle d'entreprise

Claude Code passant par LiteLLM ou une autre passerelle d'entreprise qui relaie les signatures du
raisonnement compte sur le fait que Latet a vu revenir ces signatures. La règle intégrée pour les
champs chiffrés du fournisseur ne vaut que pour l'adresse du fournisseur lui-même, jamais pour une
route d'entreprise. Après un redémarrage du daemon, ou sur un serveur une fois les données de la
conversation disparues, une telle conversation est refusée à son tour suivant : commencez-en une
nouvelle.

## Revenir à la 0.10

```sh
latet upgrade --to 0.10.4
```

Revenir à la version sous laquelle une copie de vos données a été faite propose de remettre cette
copie ([Mettre à jour](../installation/#mettre-à-jour)). Latet 0.10 ignore `request_exceptions` et
`request_resources`, et envoie de nouveau ce que la 0.11 refuse : les parties non lues des requêtes,
les requêtes des projets où Latet est désactivé et les images qu'il ne sait pas lire. Ne revenez en
arrière qu'en le sachant.

Un réglage est inconnu de la 0.10 : `request_profile`, sur une destination de `upstreams`. La 0.11
ne l'écrit que pour une destination dont vous avez choisi le profil, dans `config.yaml` et dans
`config.reviewed.json`, les réglages que vous avez approuvés en dernier, dans le dossier de données de
Latet. La 0.10 ne démarre pas s'il est dans `config.yaml`, et lit un `config.reviewed.json` qui le
contient comme s'il n'y avait aucun réglage approuvé : une modification de `config.yaml`
s'appliquerait alors sans attendre votre approbation. Si vous en avez choisi un, remettez la copie
que la mise à jour vers la 0.11 a faite, qui contient les deux fichiers tels qu'ils étaient avant :
`latet upgrade --to` la propose en revenant en arrière (elle se trouve dans le dossier de données,
dans `backups/before-0.11.0-<heure>`). Sans cette copie (la 0.11 installée par le script
d'installation, qui n'en fait aucune), retirez la clé des deux fichiers avant de revenir en arrière :
les lignes `request_profile:` de `config.yaml`, et chaque membre `"request_profile": "…"` de
`config.reviewed.json`, en le laissant en JSON valide.

Sur un serveur Latet, la 0.11 ajoute des tables à la base de données, et une instance refuse une base
plus récente qu'elle : revenir en arrière demande la sauvegarde de la base faite avant la mise à jour,
toutes les instances arrêtées
([Mettre à jour et ajouter des instances](../../concepts/server-operations/#mettre-à-jour-et-ajouter-des-instances)).
