---
title: Reconnaisseurs personnalisés
description: Apprenez à Latet un format qui vous est propre, comme un numéro d'employé ou un code de contrat.
---

Latet connaît les secrets, les données personnelles et les numéros d'identité nationaux, mais pas les
formats de votre entreprise : numéros de badge, codes de contrat, identifiants clients. Un
**reconnaisseur** lui en apprend un. C'est un motif, avec éventuellement des chiffres de contrôle et
des mots-clés, et quelques exemples inventés sur lesquels il est vérifié.

Utilisez plutôt un [terme](../detection/#vos-propres-termes) quand il y a une seule valeur exacte à cacher,
comme le nom d'un client. Un reconnaisseur sert pour toutes les valeurs écrites d'une certaine façon.

## Ne collez jamais de vraies valeurs

Un reconnaisseur décrit un format. Ses exemples sont inventés : écrivez-les à partir de la
description, ou prenez une vraie valeur et changez chaque lettre et chaque chiffre, en gardant la
disposition. `latet rules add` refuse un exemple que Latet a déjà vu comme vraie valeur. Quand un
agent IA écrit le reconnaisseur pour vous, décrivez-lui le format avec des mots : deux lettres
majuscules, un tiret, six chiffres, dans la colonne badge de l'export RH.

## Trouver les formats

Vous ne savez pas quels formats contiennent vos fichiers ? Laissez Latet chercher, sur cette machine :

```sh
latet rules suggest ./my-project
```

La commande liste les formats des valeurs qui ressemblent à du code et qu'aucune règle ne couvre
encore, comme `AA-999999` (A pour une lettre, 9 pour un chiffre), avec le nombre de valeurs
différentes vues, dans quels fichiers, les chiffres de contrôle qu'elles valident et les libellés
voisins (*badge*, *invoice_ref*). Elle n'affiche jamais une valeur. Dans un dépôt git, elle ne lit que
les fichiers que git connaît : les fichiers ignorés restent donc à l'écart. `--yaml` ébauche un
reconnaisseur pour chaque format, avec des exemples inventés à partir du format ; décrivez ce que
chacun trouve avant de l'ajouter. `--show-values` affiche trois valeurs de chaque format dans votre
propre terminal, et refuse de s'exécuter dans un agent IA ou dans un pipe.

## Un exemple

```yaml
recognizers:
  - name: employee-id
    description: Badge numbers of the HR system
    pattern: '\b[A-Z]{2}-\d{6}\b'
    context:
      words: [badge, employee]
    examples:
      match: ["badge AB-123456", "employee: QX-904417"]
      no_match: ["AB-123456 alone", "build AB-12345"]
```

Mettez-le dans un fichier à part, comme `rules.yaml`, puis :

```sh
latet rules test rules.yaml     # every example, with the reason when one fails
latet rules add rules.yaml      # saves it in config.yaml and applies it
latet rules list
```

Dès lors, `AB-123456` à côté du mot *badge* est masqué comme `EMPLOYEE_ID`, et le tableau de bord
nomme la règle `recognizer:employee-id`. La valeur factice garde le format : deux lettres, un tiret,
six chiffres.

La page **Règles** du tableau de bord fait la même chose dans son onglet *Reconnaisseurs* : un
tableau de vos reconnaisseurs, chaque ligne avec son nom, son type, ce qu'il trouve (le motif,
raccourci), ce qui arrive à ses valeurs et comment il s'est chargé. *Ajouter un reconnaisseur* ouvre
un panneau avec chaque champ : remplissez-le, puis testez-le dans *Essai* sur un texte à vous (rien
de ce qui y est saisi n'est conservé). Il affiche le résultat de chaque exemple et les
correspondances dans du code ordinaire avant que vous n'enregistriez. Sélectionnez des lignes pour
les activer, les désactiver ou les supprimer ; *Exporter* et *Importer* les déplacent en fichier de
règles. Avec le [relecteur local](../llm-review/#reconnaisseurs-à-partir-dune-description) activé,
*Brouillon à partir d'une description* écrit le reconnaisseur pour vous à partir d'une phrase
(*"numéros de badge : BD, un tiret et six chiffres"*), avec des exemples inventés, et l'ouvre dans
le panneau s'ils sont validés, pour que vous le vérifiiez et l'enregistriez.

## Pour un seul projet

Un reconnaisseur peut appartenir à un [projet](../projects/) plutôt qu'à l'ensemble de Latet :

```sh
latet rules add rules.yaml --repo .          # into the project's latet.yaml, shared with the team
latet rules add rules.yaml --project .       # into your own settings for the project
latet rules list --project .
```

La page **Règles** du tableau de bord fait la même chose une fois que vous avez choisi le projet dans
le sélecteur en haut de la page : son tableau *Reconnaisseurs* montre ceux du projet, de vos réglages
et de `latet.yaml`, avec vos reconnaisseurs globaux, et un nouveau va là où le dit le *Enregistrer
dans* de son panneau.

## Ce que contient un reconnaisseur

| Clé | Ce qu'elle fait |
| --- | --- |
| `name` | Lettres minuscules, chiffres et tirets, unique. |
| `type` | Le type affiché pour ses valeurs, en majuscules. Le nom en majuscules s'il n'est pas défini. Ce ne peut pas être un type que Latet a déjà. |
| `pattern` | Une expression régulière [RE2](https://github.com/google/re2/wiki/Syntax). Un groupe nommé `value` choisit la partie à masquer. |
| `checksum` | Les chiffres de contrôle que la valeur doit valider : `luhn`, `iban`, `mod97-10`, `mod11-10`, `ean`, `verhoeff`, `damm`, ou une somme pondérée. Les valeurs factices les valident aussi. |
| `context` | Des mots-clés (`words`, sans tenir compte de la casse ; `acronyms`, en respectant la casse) qui doivent figurer sur la même ligne, à moins de `window` caractères, ou dans l'en-tête de colonne d'un tableau. |
| `examples` | `match` et `no_match` : des textes inventés, vérifiés à chaque chargement des règles. |
| `class` | `custom`, ou `secret` pour les valeurs qui ne doivent jamais partir en clair : elles sont masquées comme d'habitude, et une requête qui en transporterait une en clair est arrêtée, même dans Claude Code. |
| `policy` | `mask` (par défaut), `redact` ou `allow`. Une entrée dans `policies` l'emporte. |
| `surrogate` | `shape`, ou `{keep: 3}` pour garder un préfixe tel quel. |
| `enabled` | `false` garde la règle dans le fichier, désactivée. |

La [référence de la configuration](../../reference/configuration/) liste toutes les clés, et le
[JSON Schema](https://docs.latet.ai/config.schema.json) permet à votre éditeur de les compléter.

## Les motifs sont en RE2

RE2 s'exécute en temps linéaire : aucun motif ne peut donc bloquer une requête. Il n'a ni assertions
avant ou arrière (`(?=`, `(?<=`) ni références arrière (`\1`). Là où vous utiliseriez une assertion
arrière, incluez le contexte dans le motif et marquez la valeur avec un groupe nommé :

```yaml
recognizers:
  - name: contract-code
    pattern: '(?i)\bcontract[ #:]+(?P<value>[A-Z]{3}\d{5})\b'
    examples:
      match: ["Contract #KLM48213"]
      no_match: ["contract signed"]
```

Seul `KLM48213` est masqué. En YAML, écrivez les motifs entre guillemets simples, pour que les barres
obliques inverses restent telles quelles.

Une valeur a besoin d'une lettre ou d'un chiffre. Une correspondance faite de symboles seuls, comme
le `---` d'un tableau Markdown, reste telle quelle, car sa valeur factice serait l'original
lui-même ; `latet rules test` et *Essai* le signalent.

## Des règles précises

Une règle qui se déclencherait partout dans du code ordinaire masquerait le code lui-même : Latet
vérifie donc chacune sur un échantillon de la bibliothèque standard Python, et au-delà de 50
correspondances, elle est désactivée. Les mots-clés et les chiffres de contrôle sont le remède. Par
exemple, un numéro dont le dernier chiffre est une clé de Luhn :

```yaml
recognizers:
  - name: card-ref
    type: CARD_REF
    pattern: '\bCR(?P<value>\d{8})\b'
    checksum: luhn
    class: secret
    examples:
      match: ["ref CR12345674"]
      no_match: ["ref CR12345678"]
```

`latet rules test rules.yaml --corpus src/` indique aussi combien de valeurs la règle trouverait dans
vos propres fichiers. La commande n'affiche que leurs formats (`AA-999999 x12`), jamais les valeurs.

## Quand quelque chose ne va pas

Un reconnaisseur qui contient une erreur (un motif que RE2 rejette, une somme de contrôle inconnue, une
clé mal orthographiée) est désactivé ; les autres continuent de fonctionner, et le fichier le garde tel
que vous l'avez écrit. `latet reload`, `latet doctor` et la page **Règles** du tableau de bord
indiquent lequel et pourquoi, et `latet rules test` explique chaque exemple. `latet config validate`
vérifie tout le fichier.

Vous écrivez des reconnaisseurs avec un agent IA ? Indiquez-lui
[`llms-rules.md`](https://github.com/Latet-AI/latet/blob/main/llms-rules.md) dans le dépôt : les mêmes
étapes, écrites pour lui.
