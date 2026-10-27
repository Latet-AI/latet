---
title: Référence de l'API pour les applications
description: Le contrat HTTP de l'API pour les applications d'un serveur Latet, ses réponses, ses erreurs et ses limites.
sidebar:
  label: API pour les applications
---

L'[API pour les applications](../../applications/) d'un serveur Latet répond à quatre opérations sous
`https://<votre serveur>/_latet/apps/v1/`. Ceci est la version 1 : un changement qui casserait un
programme écrit pour elle arrive en `v2`, à côté. La [description OpenAPI](../../../apps-api.openapi.json)
dit la même chose pour les outils qui génèrent un client ; les programmes Python peuvent utiliser
`latet-client` ([journaux](../../applications/logs/), [RAG](../../applications/rag/)).

## Authentification

Chaque appel porte le jeton d'une clé d'API dans un seul en-tête :

```http
Authorization: Bearer latet_key_...
```

Un jeton est `latet_key_` suivi de 43 caractères, et c'est l'administrateur qui le crée
(`latet server keys add`, `keys add-token`). Chaque opération demande sa propre permission : `scan`,
`mask` (qui comprend `scan`), `unmask` ou `forget` (qu'aucune autre ne comprend, et que le premier jeton
d'une clé n'a pas). Le serveur refuse :

- avec **401** et `WWW-Authenticate: Bearer`, un appel sans en-tête `Authorization`, avec deux, avec un
  autre schéma que `Bearer`, ou avec un jeton qu'il ne connaît pas (révoqué, renouvelé, d'une clé
  désactivée ou supprimée) ;
- avec **403**, le jeton de la machine d'une personne, un jeton sans la permission de l'opération, et
  toute requête qui porte un en-tête `Origin` ou `Sec-Fetch-Site`, comme celles d'un navigateur : l'API
  est faite pour les programmes, jamais pour une page web.

Les jetons que le serveur refuse comptent contre l'adresse d'où ils viennent, comme sur toutes les
routes du serveur : au-delà de la limite, 429. Appelez le serveur en HTTPS ; sinon le jeton et les
textes circulent en clair.

## Requêtes

| Opération | Chemin | Permission | Corps |
| --- | --- | --- | --- |
| Analyse | `POST /_latet/apps/v1/scan` | `scan` | `{"texts": [...], "kind": "prose"}` |
| Masquage | `POST /_latet/apps/v1/mask` | `mask` | `{"texts": [...], "kind": "prose"}` |
| Restauration | `POST /_latet/apps/v1/unmask` | `unmask` | `{"texts": [...]}` |
| Oubli | `POST /_latet/apps/v1/forget` | `forget` | `{"values": [...]}` |

- `texts` est une liste de 1 à 1 000 chaînes, auxquelles la réponse suit le même ordre. Un texte vide
  revient tel quel, sans résultat et avec `restored: 0`.
- `values` est une liste de 1 à 1 000 chaînes, les valeurs à retirer du coffre-fort de la clé, chacune
  cherchée exactement telle qu'elle est écrite ([oublier des valeurs](../../applications/forget/)).
- `kind` vaut `prose` (du texte écrit par des personnes, par défaut) ou `data` (journaux,
  enregistrements, la sortie d'un programme) ; voir [prose et données](../../applications/#prose-et-données).
  `null` ou un autre mot est refusé. La restauration ne prend pas de `kind`.
- Le corps est un objet JSON d'au plus `apps_request_mb` du serveur (8 Mio si ses réglages ne disent
  pas autre chose, jamais plus que son `max_request_mb`).

L'analyse et la restauration avec curl, le jeton passé sur l'entrée standard comme dans
[le premier appel](../../applications/#un-premier-appel), qui masque ; leurs réponses sont plus bas :

```sh
printf 'Authorization: Bearer %s' "$(cat shop.key)" | curl -sS -H @- -H "Content-Type: application/json" \
  -d '{"texts": ["Remboursez la commande de mario.rossi@acme.it sur IT60X0542811101000000123456, merci."]}' \
  https://latet.acme.lan/_latet/apps/v1/scan
printf 'Authorization: Bearer %s' "$(cat shop.key)" | curl -sS -H @- -H "Content-Type: application/json" \
  -d '{"texts": ["Remboursez la commande de tommaso.gallo@margie-6e5a.example sur IT17F3945471087320356678487, merci."]}' \
  https://latet.acme.lan/_latet/apps/v1/unmask
```

## Réponses

Chaque réponse de l'analyse, du masquage et de la restauration est un objet JSON dont les `results`
contiennent une entrée par texte envoyé, dans l'ordre ; un oubli répond par deux nombres.

**L'analyse** dit où se trouvent les valeurs dans le texte envoyé, et ne conserve rien :

```json
{"results": [{"findings": [{"type": "EMAIL", "class": "pii", "start": 26, "end": 45},
                           {"type": "IBAN", "class": "pii", "start": 50, "end": 77}],
              "counts": {"EMAIL": 1, "IBAN": 1}}]}
```

**Le masquage** donne chaque texte avec les valeurs factices de la clé, et leur place dans le texte
renvoyé :

```json
{"results": [{"text": "Remboursez la commande de tommaso.gallo@margie-6e5a.example sur IT17F3945471087320356678487, merci.",
              "replacements": [{"type": "EMAIL", "class": "pii", "start": 26, "end": 59},
                               {"type": "IBAN", "class": "pii", "start": 64, "end": 91}],
              "counts": {"EMAIL": 1, "IBAN": 1}},
             {"blocked": "the local reviewer could not read the user text (it took longer than the 30 seconds an application's call waits for it, which llm_review.budget_ms changes), so it was not sent"}],
 "degraded": ["llm-review"]}
```

- Un résultat avec `blocked` et sans `text` est un texte auquel Latet ne répond pas : le relecteur
  local l'a arrêté, sa relecture a échoué et le `llm_review.on_failure` de la clé vaut `stop`, ou le
  garde-fou anti-extraction l'a retenu. La raison, en anglais, ne nomme aucune valeur.
- `degraded`, présent seulement quand quelque chose a fonctionné en mode réduit pendant l'appel, nomme
  ce qui était concerné : un détecteur qui n'a pas pu lire un texte (comme `ner`) ou `llm-review`.

**La restauration** donne chaque texte où chaque valeur factice du coffre-fort de la clé est remplacée
par sa valeur, et combien de valeurs différentes elle a restaurées :

```json
{"results": [{"text": "Remboursez la commande de mario.rossi@acme.it sur IT60X0542811101000000123456, merci.",
              "restored": 2}]}
```

**L'oubli** dit combien des valeurs le coffre-fort de la clé contenait, parties maintenant avec leurs
valeurs factices, et combien il ne contenait pas, chaque valeur comptée une fois :

```json
{"forgotten": 1, "unknown": 1}
```

### Positions

`start` et `end` comptent des points de code Unicode (`end` exclu) : en Python, `text[start:end]` est
la valeur. JavaScript, Java et C# comptent des unités UTF-16, où un caractère au-delà du plan
multilingue de base, comme la plupart des emoji, en occupe deux. Dans
`Écrivez à 👩‍💻 mario.rossi@acme.it`, l'adresse commence à 14 en points de code et à 16 en unités
UTF-16 ; en JavaScript, `Array.from(text).slice(start, end).join("")` la donne.

### Types et classes

`type` est le type de la valeur : `EMAIL`, `PHONE`, `IBAN`, `CREDIT_CARD`, `IP_ADDRESS` et les autres
des [détecteurs](../detectors/), la règle d'un secret, ou le type d'un des termes, reconnaisseurs ou
règles de champ de la clé. `class` est la famille de ce qui l'a trouvée : `secret`, `custom` (les
termes et reconnaisseurs de la clé), `identity`, `pii`, `ner` (le modèle de noms), `llm` (le relecteur
local) ou `struct` (le mode données strict).

## Erreurs

Une erreur est `{"error": "<mots>"}`. Les mots, en anglais, sont pour les personnes : ils ne citent
jamais un texte envoyé, et les refus de l'entrée du serveur (authentification, limites) commencent par
`latet: `. Décidez d'après le statut.

| Statut | Quand | Que faire |
| --- | --- | --- |
| 400 | Le corps n'est pas un objet JSON avec `texts` une liste de 1 à 1 000 chaînes et, pour l'analyse et le masquage, un `kind` qui vaut `prose` ou `data` ; pour l'oubli, avec `values` une liste de 1 à 1 000 chaînes. | Corriger l'appel. |
| 401 | Pas de jeton, deux en-têtes `Authorization`, un autre schéma, ou un jeton que le serveur ne connaît pas. | Vérifier le jeton ; en demander un nouveau. |
| 403 | Il manque la permission au jeton (`this token lacks the mask permission`), c'est le jeton d'une personne, ou une page web a fait la requête. | Utiliser un jeton qui a la permission. |
| 404 | Un autre chemin sous `/_latet/apps/v1/`. | |
| 405 | Une autre méthode que `POST` (`Allow: POST`). | |
| 413 | Le corps dépasse `apps_request_mb`. | Envoyer moins de textes par appel. |
| 422 | Analyse, masquage et restauration : les textes nomment plus de 100 000 valeurs ou valeurs factices du coffre-fort, plus qu'un appel ne peut en examiner. Rien n'a été masqué ni restauré. | Envoyer moins de textes par appel. |
| 429 | Trop de jetons refusés depuis cette adresse, ou la clé a `requests_in_flight` requêtes d'applications sur cette instance (16 si les réglages ne disent pas autre chose). | Attendre `Retry-After`, puis réessayer. |
| 500 | Une autre erreur ; ses mots ne nomment aucune valeur. | Réessayer ; prévenir l'administrateur si cela dure. |
| 503 | Un stockage du serveur n'a pas répondu, l'espace de la clé n'est pas disponible sur cette instance pour le moment, ou le journal d'administration n'a pas pris l'entrée : pour la restauration rien n'a été restauré, pour l'oubli rien n'a été oublié. | Attendre (`Retry-After` s'il est donné), puis réessayer. |
| 507 | Masquage seulement : le coffre-fort de la clé contient le plus de valeurs qu'il peut. Rien de l'appel n'a été renvoyé, et rien n'est sorti en clair. | Un administrateur relève la limite avec `latet server keys limit`. |

Un masquage ou une restauration peut être répété sans risque : une valeur que le coffre-fort a
conservée reçoit de nouveau la même valeur factice, et une valeur qu'un appel qui a échoué n'a pas
conservée en reçoit de nouveau une. Un oubli peut aussi être répété : une valeur déjà partie est
comptée dans `unknown`. Un appel `mask` qui a échoué peut avoir conservé certaines de ses
nouvelles valeurs ([le coffre-fort d'une clé](../../applications/#le-coffre-fort-dune-clé) dit
quand).

## En-têtes

- Chaque réponse dit `Cache-Control: no-store` : aucun cache ne garde un texte ou une valeur.
- `Retry-After`, en secondes, accompagne chaque 429, et certains 503.

## Limites

| Limite | Valeur | Fixée par |
| --- | --- | --- |
| Textes par appel, ou valeurs par oubli | 1 000 | Latet |
| Corps d'un appel | 8 Mio | `apps_request_mb` du serveur |
| Requêtes d'applications simultanées d'une clé sur une instance | 16 | `requests_in_flight` du serveur |
| Valeurs ou valeurs factices du coffre-fort que les textes d'un appel peuvent nommer | 100 000 | Latet |
| Valeurs que contient le coffre-fort d'une clé | 200 000 | `vault_limit` du serveur, ou la limite propre de la clé : `latet server keys limit` |
| Valeurs du coffre-fort d'une clé qu'une instance garde en mémoire | 5 000 | `key_working_values` du serveur |

## Ce que le serveur enregistre

- **Le journal d'administration** reçoit une entrée `apps.unmasked` par appel de restauration, avant
  l'envoi de sa réponse : l'identifiant du jeton, le nombre de textes et de valeurs restaurés
  (`{"token": "t_...", "texts": 3, "restored": 5}`), et l'adresse du client. Jamais un texte ni une
  valeur. L'analyse et le masquage n'y figurent pas. Lisez ces entrées avec
  `latet server log --action apps.unmasked`. Chaque appel d'oubli est une entrée `apps.forgotten`,
  écrite dans la même transaction que l'oubli : l'identifiant du jeton, le nombre de valeurs demandées,
  oubliées et absentes (`{"token": "t_...", "values": 2, "forgotten": 1, "unknown": 1}`), et l'adresse
  du client.
- **Les métriques** comptent les requêtes de l'API comme `latet_http_requests_total{kind="apps"}`, par
  statut, et ses textes et valeurs par opération : `latet_apps_texts_total{operation}` et
  `latet_apps_values_total{operation}` (les valeurs que l'analyse a trouvées, que le masquage a
  remplacées et que la restauration a remises ; pour l'oubli, les valeurs demandées et celles oubliées).
  Elles n'ont pas d'étiquette pour la clé : qui a fait
  quoi se trouve dans le journal d'administration.
- **Le journal du serveur** a une ligne par appel : la méthode, le chemin, le statut et la durée,
  jamais un en-tête, le corps ni une valeur. Les refus (401, 403, 404, 405, 413, 429, et ceux du
  serveur lui-même) sont résumés une fois par minute, avec leur nombre, au lieu d'une ligne chacun.
