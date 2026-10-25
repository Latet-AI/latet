---
title: Profils et politiques
description: Choisissez dans quelle mesure Latet masque (balanced, max, secrets) et ce qu'il advient de chaque type de valeur.
---

## Profils

Un profil décide quels [détecteurs](../detection/) sont actifs. Il y en a trois :

| Masqué | `balanced` (par défaut) | `max` | `secrets` |
| --- | --- | --- | --- |
| Secrets : clés d'API, jetons, mots de passe, clés privées | ✅ | ✅ | ✅ |
| Les valeurs des colonnes et des champs secrets (`password`, `api_key`, `reset_token`...) dans les tableaux et le JSON | ✅ | ✅ | ✅ |
| Vos propres termes | ✅ | ✅ | ✅ |
| Vos propres [reconnaisseurs](../custom-recognizers/) | ✅ | ✅ | ✅ |
| Les valeurs déjà présentes dans le coffre-fort | ✅ | ✅ | ✅ |
| Données personnelles : adresses e-mail, numéros de téléphone, IBAN, numéros de carte, numéros de TVA, adresses IP publiques, adresses Bitcoin | ✅ | ✅ | ❌ |
| Numéros d'identité nationaux et numéros fiscaux ([par pays](../detection/)) | les pays de `id_regions` | tous les pays | ❌ |
| L'identité de cette machine : votre nom d'utilisateur dans les chemins, le nom d'hôte, l'auteur git et le domaine de son entreprise | ✅ | ✅ | ❌ |
| Adresses IP privées (`10.x`, `192.168.x`...) | ❌ | ✅ | ❌ |
| Chaque valeur des tableaux, du JSON et des paires `key=value` qu'affiche un outil ([mode données strict](../production-data/#mode-données-strict)) | ❌ | ✅ | ❌ |
| Noms et adresses postales, avec le [modèle local de noms facultatif](../../getting-started/installation/) | dans vos prompts, vos pièces jointes et le texte des images | partout | ❌ |
| Ce qu'aucun motif ne trouve, avec le [relecteur local facultatif](../llm-review/) | quand il est activé | quand il est activé | quand il est activé |
| Le texte des images, avec la [reconnaissance de texte facultative](../images/) | masqué | non envoyé | masqué |
| PDF, et images sans reconnaissance de texte | envoyés, et signalés | non envoyés | envoyés, et signalés |

- **`balanced`** convient à la plupart des travaux sur du code. C'est le profil par défaut.
- **`max`** ajoute les adresses IP privées et les numéros d'identité de tous les pays, active le
  [mode données strict](../production-data/#mode-données-strict) (chaque valeur d'un tableau qu'affiche
  un outil est masquée, même un ID ou un état), cherche les noms partout quand le modèle local est
  installé, et refuse d'envoyer une requête qui contient une image ou un PDF. L'erreur indique quoi
  faire : retirer l'image de la conversation (dans Claude Code, `/rewind`), ou passer à `balanced`. Un
  de vos projets peut envoyer les images avec leur texte masqué, avec `images: mask`.
- **`secrets`** ne masque que les secrets et vos propres termes. Les données personnelles et votre
  identité passent. Choisissez-le quand les fausses alertes sur les données personnelles vous gênent.

### Choisir un profil

- **Quand vous faites passer vos agents par Latet pour de bon :** `latet global-install --profile max`.
- **Dans le tableau de bord :** la page **Règles**. Le changement s'applique aussitôt aux nouvelles
  requêtes, sans redémarrage.
- **Dans `config.yaml` :** réglez `profile: max`, puis lancez `latet reload` pour l'appliquer sans
  redémarrage.

### Réglage fin

Au-delà du profil, la page **Règles** du tableau de bord et `config.yaml` vous permettent de choisir :

- `pii` : les types de données personnelles à détecter, par exemple `[EMAIL, IBAN]`. La liste
  remplace le choix du profil : un type qu'elle omet n'est pas détecté, y compris les numéros
  d'identité de `id_regions` (et ceux de tous les pays dans `max`). S'il est omis, chaque type
  qu'active le profil est détecté. Dans vos réglages pour un projet, `pii` ajoute des types aux types
  globaux.
- `phone_regions` : les pays dont les numéros de téléphone sont reconnus sans préfixe international
  (par défaut `IT`, `US`, `GB`).
- `include_private_ips: true` : masquer les adresses IP privées en `balanced` aussi.
- `data` : `strict` active le mode données strict en `balanced` et en `secrets` aussi, `normal` le
  désactive en `max`. S'il est omis, c'est le profil qui décide.
- `disabled_secret_rules` : les règles gitleaks à désactiver, comme `generic-api-key`.
- `identity` : désactiver le masquage de l'identité de cette machine, ou en écrire vous-même les
  valeurs.

Chaque clé figure dans la [référence de la configuration](../../reference/configuration/).

## Politiques

Une politique décide de ce qu'il advient d'un type de valeur une fois qu'il est détecté. La politique
par défaut est `mask` :

| Politique | Ce que voit le modèle | Ce que vous recevez en retour |
| --- | --- | --- |
| `mask` (par défaut) | Une valeur factice : `+44 20 3318 4452` | L'original : `+44 20 7946 0958` |
| `redact` | Un marqueur fixe : `[REDACTED:PHONE]` | Le marqueur. Rien n'est remis en place : un fichier que l'agent écrit contient donc lui aussi `[REDACTED:PHONE]` |
| `allow` | L'original, inchangé | L'original |

Définissez une politique par type dans `config.yaml`, ou sur la page **Règles** du tableau de bord :

```yaml
policies:
  PHONE: redact
  IP_ADDRESS: allow
```

Les noms des types sont ceux que le tableau de bord affiche à côté de chaque valeur masquée, comme
`EMAIL`, `PHONE`, `IBAN`, `PASSWORD`, ou le type que vous avez donné à un terme personnalisé. Les
types sans politique sont masqués.

`allow` laisse en clair la valeur de ce type, et rien de plus : une valeur d'un type masqué écrite à
l'intérieur ne passe jamais avec elle. Avec `EMAIL: allow`, `IT60X0542811101000000123456.ops@acme-intra.it`
arrive au modèle avec l'IBAN remplacé par une valeur factice et `.ops@acme-intra.it` tel qu'il est
écrit ; un secret ou un de vos termes écrit dans une adresse est masqué avec l'adresse entière,
comme avant. Un terme ou une règle de champ d'un type laissé passer laisse en clair ce que lui seul
trouve ; ce qu'un détecteur d'un type masqué y trouve est masqué.

`redact` convient aux valeurs dont le modèle n'a jamais besoin, pas même sous forme factice. Cela
fonctionne dans un seul sens : le modèle reçoit le marqueur, et Latet ne remet jamais l'original en
place dans les réponses ou les fichiers. Que Latet conserve l'original ou non dépend de la façon dont
la valeur a été trouvée :

- Une valeur trouvée par les détecteurs, y compris vos termes et vos reconnaisseurs, n'est pas
  conservée.
- Une valeur trouvée par le [relecteur local](../llm-review/), ou par vous dans le
  [Relecteur en direct](../live-review/), est conservée malgré tout : scellée dans le coffre-fort,
  avec une valeur factice qui lui est propre et qui n'est pas envoyée. Latet la retrouve ainsi
  partout où elle apparaît, et la caviarde là aussi.

Une valeur de plus de 16 384 caractères, comme toute une chaîne JSON en
[mode données strict](../production-data/#mode-données-strict), est caviardée quelle que soit la
politique de son type, sauf si ce type est `allow` : elle est trop longue pour que le coffre-fort la
garde, elle n'est donc jamais remise en place, et un fichier que l'agent écrit à partir d'elle
contient le marqueur.

## Listes d'autorisation

Quand Latet masque quelque chose qui n'est pas sensible (un faux positif), dites-lui de ne pas toucher
à cette valeur :

```yaml
allow_values:
  - demo@northwind-legal.com # exact value, any letter case
allow_patterns:
  - "test_[0-9a-f]{16}"      # RE2 regular expression, matched against the whole value
```

Dans le tableau de bord, vous pouvez le faire en un clic : ouvrez une requête, cliquez sur la valeur
masquée et choisissez **Non sensible : toujours autoriser**. L'action inverse, **Toujours masquer**,
ajoute la valeur comme terme personnalisé. Dans une requête d'un [projet](../projects/), choisissez
d'abord si cela s'applique à *Ce projet* ou *Partout*.

Chaque clic de ce genre ajoute une valeur, et la liste grandit. Sur la page **Règles** du tableau de
bord, l'onglet *Valeurs autorisées* la montre en tableau, valeurs et motifs ensemble avec leur
genre : cherchez-y, filtrez-la, ouvrez une ligne pour la modifier (le panneau d'un motif l'essaie
sur une valeur), ou sélectionnez des lignes et supprimez-les. *Exporter* et *Importer* déplacent la
liste en CSV (`kind,value`) ; une importation ajoute ce qui est nouveau et, comme elle laisse passer
des valeurs, ne l'applique que si vous cochez *Les appliquer aussi*.

Le nom de Latet reste toujours en clair : c'est l'outil dont parle l'agent, jamais une de vos
valeurs.

Pour essayer vos règles sur un texte sans rien envoyer, collez-le dans le **Banc d'essai** du tableau
de bord.

## En mode client

En [mode client](../how-it-works/) (Claude Code), le masquage a lieu dans le plugin, avec le même
profil, les mêmes politiques et les mêmes listes d'autorisation. Le daemon ne réécrit jamais une
requête : il peut seulement la laisser passer ou la bloquer. Quand il en bloque une parce qu'une
valeur partirait en clair, ouvrez la requête dans le tableau de bord : cette valeur est encadrée en
rouge. Si elle n'est pas sensible, cliquez dessus et choisissez **Non sensible : toujours autoriser**,
comme plus haut, puis réessayez.
