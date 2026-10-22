---
title: Points de terminaison d'entreprise
description: Claude Code et Codex via la passerelle de votre entreprise, Amazon Bedrock, Google Vertex, Microsoft Foundry ou Azure OpenAI, avec Latet devant.
---

Au travail, Claude Code et Codex atteignent souvent le modèle par une route choisie par votre entreprise
plutôt que directement chez Anthropic ou OpenAI : sa propre passerelle LLM (LiteLLM, Portkey...), Amazon
Bedrock, Google Vertex, Microsoft Foundry ou Azure OpenAI. Le modèle est le même ; ce qui change, c'est qui
le vend et où vont les requêtes.

Latet lit cette route dans les réglages de l'agent lui-même et se place devant elle. Les requêtes arrivent
au même point de terminaison, avec les mêmes clés, après que Latet les a vérifiées (Claude Code) ou
masquées (Codex). Rien à configurer à la main : `latet claude`, `latet codex` et `latet global-install`
s'en chargent.

## Comment ça marche

- **Latet lit où va l'agent.** Pour Claude Code : les variables de votre shell, votre propre
  `settings.json` et les réglages gérés de votre organisation, tels que Claude Code les fusionne. Pour
  Codex : `config.toml` et les fichiers des profils (`<name>.config.toml`). Les réglages d'un dépôt ne sont
  jamais lus : un projet que vous clonez ne peut pas décider où vont vos requêtes.
- **L'adresse d'une entreprise devient une destination.** Elle est enregistrée dans `config.yaml` sous
  [`upstreams`](../../reference/configuration/#upstreams-items), avec un nom tiré de l'adresse
  (`https://llm.acme.com` devient `llm-acme-com`), et l'agent envoie ses requêtes à
  `http://127.0.0.1:5283/llm-acme-com` à la place. La même adresse reçoit toujours la même destination.
  Certains noms sont ceux des routes de Latet, et une destination ne peut pas les prendre :
  `anthropic`, `openai`, `chatgpt`, `openrouter`, `gemini`, `bedrock`, `vertex` et `backend-api`.
- **Bedrock et Vertex ont leurs propres routes**, `/bedrock/<region>/` et `/vertex/`, qui n'atteignent que
  les adresses d'Amazon et de Google.
- **Quand Latet ne peut pas se placer devant, il le dit.** L'agent ne démarre pas, et Latet dit pourquoi
  et quoi faire. Il ne laisse jamais un agent le contourner en silence.

Pour une session, `latet claude` passe ses réglages avec l'option `--settings` de Claude Code, qui
l'emporte sur vos réglages et sur ceux du projet ; `latet codex` ajoute des options `-c`. Pour de bon,
`latet global-install` écrit la même chose dans les fichiers des agents, et `latet global-uninstall`
rétablit chaque adresse qu'il a remplacée, même si vous avez modifié le fichier depuis.

## La passerelle de votre entreprise

Claude Code configuré avec la passerelle de votre entreprise :

```json title="~/.claude/settings.json"
{ "env": { "ANTHROPIC_BASE_URL": "https://llm.acme.com", "ANTHROPIC_AUTH_TOKEN": "sk-acme-..." } }
```

Avec `latet claude`, Claude Code envoie ses requêtes à `http://127.0.0.1:5283/llm-acme-com`, et Latet
transmet chaque requête à `https://llm.acme.com`, avec la clé de votre entreprise telle que Claude Code l'a
envoyée. Codex fonctionne de la même façon, avec `openai_base_url` ou un fournisseur à lui :

```toml title="~/.codex/config.toml"
model_provider = "acme"

[model_providers.acme]
name = "Acme gateway"
base_url = "https://llm.acme.com/v1"
env_key = "ACME_API_KEY"
```

Seule l'adresse de ce fournisseur est redirigée ; sa clé, et tout le reste, restent tels que vous les avez
écrits. Un fournisseur écrit dans le fichier d'un profil (`~/.codex/work.config.toml`) est redirigé dans ce
fichier, et `latet codex --profile work` fait passer par Latet les fournisseurs de ce profil et son
`openai_base_url`.

## Amazon Bedrock

Claude Code sur Bedrock (`CLAUDE_CODE_USE_BEDROCK=1`) prouve son identité de l'une des deux façons
suivantes, et Latet gère les deux.

- **Une clé d'API Bedrock** (`AWS_BEARER_TOKEN_BEDROCK`) voyage avec chaque requête comme un mot de passe :
  elle traverse Latet telle quelle.
- **Votre connexion AWS** (un profil AWS, une connexion SSO, des clés dans `~/.aws`) : Claude Code signe
  chaque requête pour l'adresse à laquelle il l'envoie. Une signature faite pour l'adresse de Latet ne
  serait pas valable chez Amazon, comme une lettre recommandée dont on aurait changé l'adresse. Claude Code
  envoie donc à Latet des requêtes non signées (`CLAUDE_CODE_SKIP_BEDROCK_AUTH=1`), et Latet signe chacune
  d'elles pour Amazon, après l'avoir vérifiée, avec votre profil AWS.

Pour signer, Latet lit votre profil comme le font les propres outils d'Amazon : avec l'AWS CLI
(`aws configure export-credentials`) quand elle est installée, ce qui couvre les connexions SSO et les
rôles, ou à partir des clés écrites dans `~/.aws`. Latet cherche `aws` dans son propre PATH, puis dans
`/usr/local/bin`, `/opt/homebrew/bin`, `/usr/bin`, `~/.local/bin` et `/snap/bin` (dans
`C:\Program Files\Amazon\AWSCLIV2` sous Windows) : le Latet qui démarre à l'ouverture de session ne voit
pas le PATH de votre shell. Si votre AWS CLI est ailleurs, démarrez Latet depuis un shell où `aws`
fonctionne. Les identifiants restent dans la mémoire de Latet, ne sont jamais écrits nulle part, et ne
signent que les requêtes vers Amazon Bedrock. Quand votre connexion expire, Claude Code le dit en termes
simples : `run aws sso login --profile work` ; une fois cette commande lancée, tout refonctionne sans rien
redémarrer.
Les réglages `awsAuthRefresh` et `awsCredentialExport` de Claude Code ne sont pas utilisés tant que Latet
signe : Latet lit lui-même votre profil AWS, lancez donc vous-même la connexion qu'ils auraient lancée.
`latet doctor` avertit quand ils sont définis.

La route qui signe porte une clé que seul votre Latet peut fabriquer
(`/bedrock/eu-west-1/profile/work/<key>`) : aucun autre programme ni aucun autre utilisateur de votre
machine ne peut donc faire signer des requêtes en votre nom. Les identifiants qui n'existent que dans les
variables de votre shell (aws-vault, par exemple) sont remis à Latet par `latet claude` pour cette
session. Latet les garde en mémoire, et seulement tant que cette session dure : `latet claude` maintient
une connexion ouverte avec Latet, et quand la commande se termine (même si vous la tuez) Latet oublie les
identifiants en quelques secondes. Il les oublie aussi à leur propre expiration, quand votre outil en fixe
une (`AWS_SESSION_EXPIRATION` ou `AWS_CREDENTIAL_EXPIRATION`). Ensuite, Claude Code dit en termes simples
que les identifiants ont disparu : renouvelez-les et relancez `latet claude`. `global-install` a besoin
d'un profil AWS, que Latet peut lire tout seul.

Avec votre connexion AWS, écrire en toutes lettres l'adresse publique de Bedrock dans vos réglages
(`ANTHROPIC_BEDROCK_BASE_URL` valant `https://bedrock-runtime.eu-west-1.amazonaws.com`) revient à n'en
écrire aucune : Latet signe pour elle. C'est la région de l'adresse qui est utilisée, même quand
`AWS_REGION` en indique une autre : l'adresse est l'endroit où vont les requêtes. L'adresse FIPS de Bedrock
et un point de terminaison VPC sont d'autres cas : voir
[Là où Latet ne peut pas se placer devant](#là-où-latet-ne-peut-pas-se-placer-devant).

Les requêtes vers Bedrock sont vérifiées, jamais réécrites : elles viennent de Claude Code, dont le plugin
les a déjà masquées. Les réponses de Bedrock parviennent à Claude Code exactement comme Amazon les a
envoyées ; Latet ne les lit que pour les afficher dans le tableau de bord. Les ID de modèle et les ARN des
profils d'inférence arrivent chez Amazon exactement tels que Claude Code les a écrits.

## Google Vertex

Claude Code sur Vertex (`CLAUDE_CODE_USE_VERTEX=1`) envoie votre connexion Google sous forme de jeton, qui
traverse Latet tel quel : rien à signer. Chaque requête indique son emplacement, et Latet l'envoie à
l'adresse de Google qui lui correspond : le point de terminaison global, les multirégions `eu` et `us`, ou
une région comme `us-east5`. Des modèles différents dans des régions différentes fonctionnent aussi.

## Microsoft Foundry et Azure OpenAI

Claude Code sur Microsoft Foundry (`CLAUDE_CODE_USE_FOUNDRY=1`, avec `ANTHROPIC_FOUNDRY_RESOURCE` ou
`ANTHROPIC_FOUNDRY_BASE_URL`) et Codex sur Azure OpenAI (un fournisseur dont le `base_url` est
`https://<resource>.openai.azure.com/openai/v1`) sont des destinations, comme une passerelle : l'adresse de
la ressource est enregistrée (`foundry-acme`, `azure-acme`) et la clé passe telle quelle.

## Un modèle sur cette machine

Une adresse sur cette machine (`http://localhost:11434` pour Ollama, par exemple) est laissée de côté : un
modèle qui tourne ici n'envoie rien ailleurs, et Latet ne peut pas le distinguer d'un proxy qui transmet
vers le cloud. S'il s'agit d'un tel proxy, LiteLLM par exemple, ajoutez-le comme destination, et Latet se
place devant lui :

```sh
latet config add upstreams "{name: litellm, url: http://localhost:4000}"
```

Lancez ensuite une nouvelle session `latet claude` ou `latet codex` ; si vous avez configuré vos agents
avec `latet global-install`, relancez `latet agents apply` : d'ici là, leurs réglages envoient encore
directement vers le proxy.

Latet vous le rappelle chaque fois qu'il laisse une telle adresse de côté. Les adresses sur votre réseau
local et sur internet passent toujours par Latet.

## Là où Latet ne peut pas se placer devant

- **Des réglages gérés qui imposent une adresse.** Quand les réglages gérés de votre organisation fixent
  `ANTHROPIC_BASE_URL` (ou l'adresse d'un cloud) à autre chose, rien ne peut passer outre. Latet le dit et
  ne lance pas Claude Code. Votre service informatique peut régler cette adresse sur Latet, comme
  ci-dessous.
- **L'API Mantle d'Amazon Bedrock** (`CLAUDE_CODE_USE_MANTLE`) : Latet ne la lit pas encore. Utilisez
  l'API Invoke de Bedrock (`CLAUDE_CODE_USE_BEDROCK`) ou la passerelle de votre entreprise.
- **Amazon Bedrock via un point de terminaison VPC** (un `ANTHROPIC_BEDROCK_BASE_URL` sur
  `vpce-….vpce.amazonaws.com`) : pas encore pris en charge. Latet devrait signer pour ce point de
  terminaison, et Amazon refuse une signature faite pour l'adresse de Latet. Latet le dit et ne lance pas
  Claude Code : utilisez l'adresse publique de Bedrock, ou la passerelle de votre entreprise. Avec une clé
  d'API Bedrock (`AWS_BEARER_TOKEN_BEDROCK`), rien n'a à être signé, et le point de terminaison est une
  destination comme toute adresse d'entreprise.
- **L'adresse FIPS d'Amazon Bedrock** (un `ANTHROPIC_BEDROCK_BASE_URL` sur
  `bedrock-runtime-fips.us-east-1.amazonaws.com`) : pas encore prise en charge. Latet ne peut envoyer qu'à
  l'adresse standard, qui n'est pas un point de terminaison FIPS : il le dit donc et ne lance pas Claude
  Code, plutôt que d'abandonner votre exigence FIPS. Utilisez la passerelle de votre entreprise, ou
  l'adresse standard si vous n'avez pas besoin de FIPS. Avec une clé d'API Bedrock
  (`AWS_BEARER_TOKEN_BEDROCK`), rien n'a à être signé, et l'adresse FIPS reste la destination, comme toute
  adresse d'entreprise.
- **Plus d'un cloud à la fois** (`CLAUDE_CODE_USE_BEDROCK` et `CLAUDE_CODE_USE_VERTEX`, par exemple) :
  Latet ne peut pas savoir lequel Claude Code utilise, il le dit donc et ne le lance pas. N'en gardez
  qu'un.
- **Une adresse que deux programmes pourraient lire différemment** (contenant une barre oblique inverse, un
  espace, un nom d'utilisateur ou un mot de passe), ou un serveur hors de votre réseau local joint en
  `http` : Latet ne la prend pas en charge, et dit pourquoi.
- **L'onglet Code de Claude Desktop** ignore ces variables : le plugin y masque, mais le daemon ne voit pas
  les requêtes (voir [Claude Code](../claude-code/#couverture)).
- **OpenCode** passe par Latet avec Anthropic, OpenAI et OpenRouter ; ses autres fournisseurs, non.

## Latet pour tout le monde dans une entreprise

Là où des réglages gérés décident de l'adresse de Claude Code, le service informatique peut la faire
pointer vers le Latet de chaque machine, et donner à chaque Latet le point de terminaison de l'entreprise
comme destination :

```json title="managed-settings.json"
{ "env": { "ANTHROPIC_BASE_URL": "http://127.0.0.1:5283/llm-acme-com" } }
```

```sh
latet config add upstreams "{name: llm-acme-com, url: https://llm.acme.com}"
```

Chaque ordinateur a encore besoin du plugin de Latet pour Claude Code : lancez-y `latet global-install`,
ou démarrez Claude Code avec `latet claude`. Sans le plugin, les requêtes de Claude Code arrivent chez
Latet sans être masquées : Latet ne fait alors que les vérifier, et arrête chacune de celles qui portent
une valeur sensible en clair. L'adresse nomme le port 5283 : sur un ordinateur dont le Latet tourne sur un
autre port, Latet dit que les réglages gérés envoient Claude Code ailleurs, et ne le démarre pas.

## Vérifications en chemin

- **Le plugin vérifie chaque session.** Quand Claude Code démarre, le plugin regarde où vont réellement ses
  requêtes, réglages compris. Si le `.claude/settings.json` d'un projet fixe où elles vont (une adresse, un
  cloud, un proxy), le plugin arrête ses prompts et nomme le fichier, même quand celui-ci pointe vers
  Latet : mettez plutôt la ligne dans vos propres réglages. Un projet ne peut pas non plus faire pointer le
  plugin vers un autre Latet. Quand la vérification elle-même échoue, les prompts sont aussi arrêtés.
- **`latet agents status`** affiche la route de chaque agent. **`latet doctor`** l'affiche aussi, avec une
  variable de votre shell que les réglages de Claude Code écrasent (pas l'adresse de votre entreprise
  quand Latet a placé la sienne devant : c'est normal), et si l'AWS CLI est présente quand Latet signe
  pour Bedrock.
- **Le tableau de bord** liste vos destinations sous Agents protégés → Où vont les requêtes, avec, pour
  chacune, l'agent qui l'a nommée et sa dernière utilisation, et affiche sur chaque échange où il est allé
  et si Latet l'a signé. En supprimer une à cet endroit retire cette seule destination et aucune autre. Une
  destination encore utilisée ne peut pas être supprimée : les réglages de Claude Code ou de Codex, ou les
  réglages gérés de votre organisation, pointent vers elle, ou elle a été enregistrée depuis le démarrage
  de Latet, par une session `latet claude` ou `latet codex`, par `latet agents apply` ou par
  `latet global-install`. Latet ne voit pas une destination que seul un autre agent utilise (OpenCode,
  Hermes, un programme lancé avec `latet run`) : vérifiez qu'aucun ne l'utilise avant de la supprimer.
- **Une destination ne se déplace pas sans vous.** Changer l'adresse d'une destination, ou la supprimer,
  hors du tableau de bord (la commande d'un agent, `config.yaml` modifié à la main) attend votre
  approbation dans Approbations, avec l'adresse avant et après : cela enverrait vos requêtes, et vos clés,
  ailleurs. En ajouter une, non : seul un agent qui pointe vers elle l'utilise. Si une modification est en
  attente quand un agent en ajoute une, Approbations affiche de nouveau la modification, nouvelle
  destination comprise, et ce qu'elle déplacerait désormais ; une modification qui ne réduit plus rien s'applique
  aussitôt.

## Avec quoi cela a été testé

Les vrais Claude Code (2.1.294) et Codex (0.161.0), à travers Latet, face à des fournisseurs qui répondent
comme chaque cloud, sur une seule machine : la passerelle d'une entreprise, Bedrock avec une clé d'API,
avec un profil pour lequel Latet a signé et avec l'ARN d'un modèle, Vertex, et un fournisseur Codex au
format d'Azure. Les signatures de Latet correspondent à celles de la bibliothèque d'AWS elle-même, requête
par requête. Si vous utilisez l'un de ces clouds au travail,
[dites-nous comment ça se passe](https://github.com/Latet-AI/latet/issues).
