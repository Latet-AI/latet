---
title: Masquer les textes d'un workflow n8n
description: Les nœuds Latet pour n8n analysent, masquent, restaurent et oublient les textes des éléments d'un workflow, par l'API pour les applications.
sidebar:
  label: n8n
---

Les nœuds Latet pour n8n ([`@latet-ai/n8n-nodes-latet`](https://www.npmjs.com/package/@latet-ai/n8n-nodes-latet),
sous licence MIT) appellent l'[API pour les applications](../) d'un serveur Latet depuis un workflow.
Ils analysent, masquent, restaurent et oublient les textes des éléments du workflow : un workflow
masque ce qu'il envoie à un modèle ou à un service, et restaure ce qui revient. Il leur faut un
serveur Latet, 0.10.0 ou plus récent, et le jeton d'une clé d'API ; le jeton de la machine d'une
personne est refusé.

## Installer les nœuds

Dans n8n : **Settings > Community Nodes > Install**, puis saisissez `@latet-ai/n8n-nodes-latet`. Le
[guide d'installation](https://docs.n8n.io/integrations/community-nodes/installation/) de n8n donne
les détails.

## Un jeton pour le workflow

L'administrateur du serveur Latet crée un jeton avec les permissions que le workflow utilise, et
aucune de plus ([clés d'API et jetons](../#clés-dapi-et-jetons)) :

```sh
latet server --server https://latet.acme.lan keys add-token "Support desk" n8n --mask --unmask --out n8n.token
```

| Opération | Permission |
| --- | --- |
| Scan | `scan`, ou `mask` |
| Mask | `mask` |
| Unmask | `unmask` |
| Forget | `forget`, qu'aucune autre permission n'apporte |

Dans n8n, les identifiants **Latet API** contiennent l'adresse du serveur (`https://latet.acme.lan`)
et le jeton, c'est-à-dire le contenu de `n8n.token`. Utilisez une adresse `https://` : par `http://`,
le jeton et les textes circulent en clair. n8n teste les identifiants par l'analyse d'un mot, qui ne
garde rien et demande `scan` ou `mask` : un jeton qui peut seulement restaurer ou oublier échoue au
test, et fonctionne quand même dans le nœud.

## Ce que fait chaque opération

Chaque opération lit le texte de chaque élément dans un champ (**Text Field**, `text` par défaut) et
envoie les éléments par appels de 1 000 textes au plus.

- **Scan** dit où sont les valeurs dans chaque texte, par type, et ne garde rien.
- **Mask** rend chaque texte avec des valeurs factices à la place de ses valeurs, la même valeur
  factice pour la même valeur à chaque appel de la clé. **Kind** dit ce que sont les textes :
  **Prose** pour le texte écrit par des personnes, **Data** pour les journaux, les enregistrements et
  la sortie d'un programme ([prose et données](../#prose-et-données)).
- **Unmask** remet les vraies valeurs à la place des valeurs factices de la clé. Le serveur écrit
  chaque restauration dans son journal d'administration.
- **Forget** retire des valeurs du coffre-fort de la clé, une par élément (**Value Field**, `value`
  par défaut), écrites comme elles ont été masquées ([oublier des valeurs](../forget/)).

Un texte auquel le serveur ne répond pas, parce que son relecteur local l'a arrêté ou que son
garde-fou anti-extraction l'a retenu, sort avec `blocked` et sans texte, pour que le texte en clair
n'aille pas plus loin.

## Un modèle qui ne voit jamais les valeurs

Un workflow qui répond aux messages du support avec un modèle :

1. Un déclencheur reçoit le message.
2. **Latet: Mask**, kind **Prose**, masque le message.
3. Le nœud du modèle (OpenAI, Anthropic, Gemini, un AI Agent) reçoit le message masqué dans son
   prompt.
4. **Latet: Unmask**, avec **Text Field** réglé sur le champ qui contient la réponse du modèle, remet
   les vraies valeurs.
5. Le workflow envoie la réponse.

Le modèle ne voit que des valeurs factices, les mêmes d'un message à l'autre d'un même client, et le
client lit les vraies valeurs. Ne donnez pas l'opération Unmask comme outil à un agent d'IA : elle
remettrait au modèle les valeurs mêmes que le masquage lui cache.

Le [README](https://github.com/Latet-AI/n8n-nodes-latet#readme) du paquet liste chaque champ que les
nœuds écrivent et les erreurs qu'ils renvoient.
