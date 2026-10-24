---
title: Données de production
description: Masquer les résultats de requêtes de base de données, les réponses d'API et les journaux que lit un agent, toutes les valeurs ou champ par champ.
---

Un agent qui enquête sur un bug lit souvent de vraies données : une requête de base de données en
production, la réponse d'une API REST, un journal, une page dans le navigateur. Les détecteurs de
Latet y trouvent ce qui a un format reconnaissable, comme les adresses e-mail, les IBAN, les jetons et
les numéros d'identité. Une note, un salaire ou une clause de contrat n'ont pas de format : dans un
tableau, ils partiraient tels quels.

Latet lit ces sorties d'après leur structure. La sortie d'un outil est souvent un tableau, un
document JSON ou des lignes de paires `key=value`, et le nom de chaque colonne ou clé dit ce que sont
ses valeurs. Trois mécanismes s'en servent :

| | Quand | Ce qu'il masque |
| --- | --- | --- |
| **Colonnes secrètes** | toujours | les valeurs des colonnes et des clés nommées comme un secret : `password_hash`, `api_key`, `reset_token` |
| **Règles de champ** | quand vous les écrivez | les valeurs des champs que vous nommez, sous un type de votre choix |
| **Mode données strict** | avec le profil `max`, ou quand vous l'activez | toutes les autres valeurs de la structure |

## Les structures que Latet lit

- **Tableaux :** psql, CSV, TSV et séparés par `;`, markdown, HTML, et les tableaux de l'instantané
  d'accessibilité d'un navigateur (`columnheader`, `cell`), et les tableaux dessinés avec des caractères de cadre
  (`console.table` de Node, le style unicode de psql).
- **Paires `key=value` :** deux ou plus sur une ligne, comme dans un journal.
- **JSON :** un document entier valeur par valeur, chacune avec son chemin (`items[].sku`) ; un
  fragment `"key": value` dans un autre texte, comme une ligne de journal, d'après sa clé.

## Colonnes secrètes

Elles sont toujours actives. Un CSV d'utilisateurs :

```text
id,email,api_key,password_hash,reset_token,role
7,ops@acme-intra.com,ak_live_9fK2LmQx7Rt3vW8zB1nY5pD,$2b$12$Kq3v0rZQ8xGm1o5t,8f14e45fceea167a,admin
```

part ainsi :

```text
id,email,api_key,password_hash,reset_token,role
7,oliver@consolidated-e55f.example,ak_live_3zF1ObRs6Wa1eH2mU6tO2fM,p5cp22yIa9h4bDH9pBx9p8n,a03c15f272977c30,admin
```

La clé, le hachage et le jeton sont des secrets à cause de leur colonne, l'adresse e-mail à cause de
son format ; `admin` n'est pas touché. Une colonne secrète reste masquée même quand une règle de champ
laisse son champ en clair.

## Mode données strict

Le [profil `max`](../profiles/) l'active. Avec les autres profils, ou pour l'indiquer explicitement :

```yaml
data: strict
```

`data: normal` le désactive même avec `max` ; sans `data`, c'est le profil qui décide. Le désactiver
là où il est activé réduit la protection : depuis un agent ou la ligne de commande, le changement
attend votre approbation.

Chaque valeur d'un tableau, d'un document JSON ou de paires `key=value` dans la sortie d'un outil est
masquée ; les noms des colonnes et des clés restent, sauf une clé qui est elle-même une valeur, comme
une adresse e-mail, masquée comme les valeurs partout. Le résultat d'une requête de base de données :

```text
 id | status  | amount | note
----+---------+--------+-------------------------------
 17 | SHIPPED | 1250.5 | call back on +44 20 7946 0958
```

part ainsi :

```text
 id | status  | amount | note
----+---------+--------+-------------------------------
 17 | QXWRLTE | 2939.3 | wkqp xoaj zd +44 20 3318 4452
```

- **La même valeur reçoit toujours la même valeur factice.** Les comptages, les regroupements et les
  jointures fonctionnent toujours sur ce que lit le modèle.
- **Une requête de base de données écrite avec une valeur factice s'exécute avec la vraie valeur.** Le
  modèle écrit `WHERE status = 'QXWRLTE'` ; l'outil exécute `WHERE status = 'SHIPPED'`.
- **Une valeur factice garde le format de la valeur.** Un nombre reste un nombre d'autant de
  chiffres, une date ISO une date valide (à moins de deux ans de la vraie), une heure une heure
  valide. Quand un format n'a plus de valeur factice disponible, comme avec des centaines d'ID à trois
  chiffres dans une même sortie, la valeur factice a un caractère de plus.
- **Un lien reste un lien.** Une adresse web garde son schéma (`https://`, `s3://`...) ; le reste est
  masqué.
- **Ce qui nomme une valeur ou dit à quoi ressemble une page reste en clair :** un espace réservé
  d'un modèle ou d'un shell (`${from}`, `{{ subject }}`, `$MAIL_FROM`), et les attributs `class` et
  `style` du balisage qu'une commande affiche, sauf si le style charge une adresse. Une ligne de
  tableau qui ne correspond pas à ses colonnes est masquée en entier.
- **Une valeur trouvée à l'intérieur d'une autre garde son propre type.** Le numéro de téléphone de la
  note reçoit un numéro de téléphone factice ; le reste de la note est masqué comme la note.
- **Une valeur reste dans sa structure.** `SHIPPED` est masqué dans le tableau, jamais dans votre
  code : le mode données strict n'ajoute pas ses valeurs à celles qui sont masquées partout.
- **Les valeurs d'un ou deux caractères restent en clair,** comme `17` ci-dessus : elles en disent
  peu, et une valeur factice aussi courte ne pourrait pas être distinguée dans une réponse. Les
  valeurs vides et les mots qui tiennent lieu de rien, ou de oui et non, comme `null`, `n/a`, `true`
  et `false`, restent eux aussi en clair.
- **Une valeur de plus de 16 384 caractères est caviardée,** pas remplacée par une valeur factice : le
  modèle reçoit `[REDACTED:DATA]`, et rien n'est remis en place.
- **Les fichiers ne sont pas touchés.** La sortie des outils qui lisent des fichiers (`Read`, `Grep`,
  `Glob`, `LS`... de Claude Code) est du code et des documents, pas des données, sauf si une règle
  désigne le fichier ([fichiers qui comptent comme données](#fichiers-qui-comptent-comme-données)).
  Les autres détecteurs s'y appliquent toujours.
- **Les résultats web non plus.** Les résultats de recherche et les pages (`WebSearch`, `WebFetch`)
  sont du contenu public, pas vos données, et ils arrivent en JSON : le mode données strict
  transformerait chaque titre et chaque adresse en lettres aléatoires, et ne laisserait au modèle rien
  à lire ni à citer. Les autres détecteurs s'y appliquent toujours : une clé ou une adresse e-mail
  dans une page est donc masquée comme partout ailleurs. Pour couvrir malgré tout une page web,
  nommez son adresse dans `data_hosts`
  ([adresses web qui comptent comme données](#adresses-web-qui-comptent-comme-données)). Pour couvrir
  un outil web, ajoutez-le à `data_tools` avec tous les autres outils que le mode données strict doit
  couvrir : dès que `data_tools` est défini, le mode données strict ne couvre que les outils qu'il
  nomme, et `"*"` nomme tous les outils sauf ceux qui lisent des fichiers.

### Quels outils

Le mode données strict couvre la sortie de tous les outils, sauf ceux qui lisent des fichiers et les
outils web. Pour n'en couvrir que certains, nommez-les, avec `*` pour n'importe quels caractères :

```yaml
data: strict
data_tools: [mcp__postgres__*, Bash]
```

Un outil d'un serveur MCP porte le nom du serveur : `mcp__crm__*` désigne chaque outil du serveur
`crm`.

Une commande que vous lancez avec `!` dans Claude Code compte comme la sortie de l'outil `Bash`.

Codex lit les fichiers avec des commandes shell : dans Codex, le mode données strict couvre donc aussi
les fichiers qu'il lit. Nommez son outil de base de données dans `data_tools` pour ne couvrir que
celui-ci.

### Fichiers qui comptent comme données

```yaml
data: strict
data_files: ["exports/**", "data/*.csv", ".env*"]
```

Un fichier qu'un motif désigne compte comme données que le mode données strict soit activé ou non : le désigner suffit. Un fichier que lit l'agent et dont le chemin correspond à un motif compte comme données : chacune de
ses valeurs est masquée, comme dans la sortie d'un outil. Un motif sans barre oblique désigne un
fichier par son nom (`*.json`, `.env*`), un motif avec barre oblique la fin de son chemin
(`exports/**`, `**/*.csv`) ; `*` correspond à n'importe quels caractères, et la casse ne compte pas.
Les valeurs sont trouvées selon le format du fichier :

- **JSON** (`.json`, `.jsonl`, ou un fichier d'un autre nom qui contient du JSON) : chaque chaîne et
  chaque nombre, selon le chemin de sa clé, comme dans la sortie JSON d'un outil. Les clés restent.
- **Tableaux** (`.csv`, `.tsv`) : chaque cellule, selon sa colonne.
- **Fichiers `.env`, properties et ini :** la valeur de chaque ligne `KEY=value`.
- **YAML :** chaque valeur, selon son chemin (`servers[].host`) ; les clés et la structure restent.
- **Tout autre fichier :** les tableaux, les paires `key=value` et les fragments JSON qu'il contient.

Chaque valeur est remplacée là où elle se trouve : le fichier garde ses lignes, leurs numéros et ses
clés, l'agent peut donc toujours le modifier d'après le texte qu'il a lu, et une modification écrite
avec une valeur factice écrit la vraie valeur. Seuls les outils qui renvoient le texte d'un fichier
en font des données (`Read` et `NotebookRead` de Claude Code) : les lignes d'une recherche (`Grep`)
et une liste (`Glob`, `LS`) restent telles quelles. Quand `Edit`, `MultiEdit` ou `Write` de Claude
Code modifie un fichier qu'un motif désigne, les lignes du fichier qu'il affiche, que l'agent n'a
peut-être jamais lues, sont masquées comme le fichier, même quand `data_tools` nomme d'autres outils.

- **Un motif large couvre plus que des données.** `*.json` couvre aussi `package.json` et
  `tsconfig.json`, dont les valeurs seraient alors masquées partout dans la conversation : désignez les
  dossiers qui contiennent des données (`exports/**`).
- **Le markdown ne compte que si un motif le désigne** (`*.md`, `docs/**/*.md`) : les agents lisent
  leurs instructions et leurs documents en markdown, et un motif pour tous les fichiers (`*`) ne les
  touche pas. Dans un fichier markdown qu'un motif désigne, seuls ses tableaux sont masqués, jamais
  son texte.
- **Les fichiers PDF ne sont pas couverts :** Latet ne lit pas le texte d'un PDF. `images: mask` ou
  `block` empêche tout simplement l'envoi des documents ([images](../images/)).

### Adresses web qui comptent comme données

```yaml
data: strict
data_hosts: [api.internal.example.com, "*.salesforce.com"]
```

Une adresse qu'un motif désigne compte comme données que le mode données strict soit activé ou non. Une page qu'un outil web (`WebFetch`) récupère sur un hôte qui correspond à un motif compte comme
données, comme la sortie d'un outil : chaque valeur de ses tableaux, de son JSON et de ses paires
`key=value` est masquée. `*` correspond à n'importe quels caractères ; la casse et le port ne
comptent pas. Un motif pour tous les sous-domaines désigne aussi le domaine lui-même :
`*.salesforce.com` désigne aussi `salesforce.com`. Un hôte écrit avec un point final
(`example.com.`) est le même hôte que sans ce point. Une recherche (`WebSearch`) n'a pas d'adresse
propre. Pour couvrir les recherches, ajoutez `WebSearch` à `data_tools` avec les autres outils que
vous voulez couvrir, ou écrivez `data_tools: ["*"]` pour tous les outils sauf ceux qui lisent des
fichiers : un `data_tools` qui nomme `WebSearch` seul ne couvre que les recherches, et laisse de
côté vos outils de base de données et de shell. Quand le mode données strict est activé, définir
`data_tools` là où il était vide le restreint : le changement attend donc votre approbation.

Ajouter un motif ou une adresse s'applique tout de suite ; en retirer un couvre moins, donc réduit la
protection : depuis un agent ou la ligne de commande, le changement attend votre approbation. Dans
le tableau de bord, ils se règlent dans **Règles** → **Protection** → **Données de production**, à
côté des outils.

### Dans un échange

Le mode données strict se règle dans **Règles** → **Protection** → **Données de production**, ou
dans vos réglages pour un projet ; un échange n'a pas d'interrupteur pour lui. Tant qu'il est
désactivé pour un échange (dans le projet de l'échange, ou dans vos règles globales pour un échange
sans projet), le tableau de bord demande à Latet si l'une des dix sorties d'outil les plus récentes
de la requête contient un tableau, du JSON ou des paires `key=value`, lus comme pour
[choisir les colonnes à laisser en clair](#choisir-les-colonnes-à-laisser-en-clair), et vient d'un
outil que couvre le mode données strict ([quels outils](#quels-outils) : pas un outil qui lit des
fichiers, ni un outil web, sauf si une règle désigne le fichier, l'adresse ou l'outil). Si l'une d'elles en contient, une ligne
au-dessus des blocs de la requête le signale, avec un lien vers l'endroit où le mode données strict
se règle pour cet échange : vos réglages pour son projet quand ils règlent `data`, sinon **Règles**
→ **Protection** → **Données de production**. Latet lit les sorties dans les textes que conserve
l'historique : avec `history: metadata` ou `off`, il n'y a pas de ligne.

## Règles de champ

Une règle de champ nomme une colonne, une clé JSON ou un chemin JSON (`items[].sku`) ; `*` correspond
à n'importe quels caractères, et la casse ne compte pas. Tout autre caractère est pris tel qu'il est
écrit, `?` et crochets compris : `orders[].items[].sku` nomme donc ce chemin. Ses valeurs sont
masquées sous le type que vous indiquez, avec ou sans mode données strict :

```yaml
fields:
  - {name: note_*, type: NOTE}
  - {name: ral, type: SALARY}
  - {name: iban_fornitore, type: IBAN, secret: true}
```

- `secret: true` fait de ses valeurs des secrets : une valeur qui arrive en clair dans une requête la
  bloque.
- `mask: false` laisse un champ en clair en mode données strict. Sa place est dans vos propres
  réglages, pas dans le `latet.yaml` d'un projet, qui ne peut qu'ajouter de la protection.

Pour un champ, votre première règle qui lui correspond (d'abord vos réglages pour un projet, puis
vos règles globales) et la première du `latet.yaml` du projet qui lui correspond sont comparées : la
plus stricte s'applique (caviardé, puis masqué, puis en clair), la vôtre à égalité.

## Choisir les colonnes à laisser en clair

Le mode données strict masque toutes les valeurs : le modèle ne peut donc pas non plus lire un état
ou une date. `SHIPPED` devient `QXWRLTE`, et un agent à qui l'on demande quelles commandes ont été
expédiées en retard n'a rien sur quoi s'appuyer. La plupart des tableaux ont des colonnes qui ne
disent rien d'une personne, et le tableau de bord les propose.

Dans un échange, sous une sortie d'outil que le mode données strict a masquée, ouvrez *Choisir les
colonnes à laisser en clair*. Chaque colonne de la sortie est accompagnée d'une proposition et de sa
raison :

| Proposition | Pour |
| --- | --- |
| **Laisser en clair** | un nom qui indique des ID ou des codes, des dates d'événements (`created_at`, `order_date`), des états (`status`, `state`), oui ou non (`active`), des comptages (`qty`) |
| **Laisser masquée** | des données personnelles ou un secret trouvés dans ses valeurs ; un nom qui concerne des personnes (`customer`, `created_by`, `doctor`), des secrets (`api_key`), la santé, les convictions ou la vie privée (`hiv_status`, `marital_status`) ; du texte libre ; des dates de naissance ou de décès |
| **À vous de voir** | les montants ; des dates, oui ou non, ou quelques valeurs qui se répètent, quand le nom ne dit pas ce qu'elles sont ; les colonnes sur lesquelles rien ne renseigne |

Une colonne n'est proposée en clair que si son nom dit ce qu'elle contient et que rien dans ses
valeurs n'est une donnée personnelle ou un secret : les mêmes détecteurs les vérifient, comme tout le
reste. Le format des valeurs ne suffit jamais à mettre une colonne en clair, car les noms
d'utilisateur se répètent comme les états. Les propositions sont un point de départ : regardez les
valeurs affichées à côté de chaque colonne avant d'appliquer.

Les colonnes proposées en clair sont déjà cochées. *Laisser 2 colonnes en clair* ajoute une règle pour
chacune, comme `{name: status, mask: false}`, en tête de vos réglages pour le projet (*Ce projet*) ou
de vos règles globales (*Partout*) : parmi vos règles, la première qui correspond l'emporte, donc une
règle plus large de vous comme `note_*` ne la masque pas à nouveau ; une règle du `latet.yaml` du
projet qui masque la colonne continue de s'y appliquer, quoi que vous choisissiez. Les sorties
suivantes montrent ces colonnes telles quelles. Rien ne s'applique tant que vous n'avez pas cliqué.

- **Calculée à l'ouverture de la liste.** Les colonnes sont lues dans la sortie que conserve
  l'historique, sur votre machine, et rien de nouveau n'est stocké. Avec `history: metadata` ou `off`,
  il n'y a pas de liste.
- **L'avis du relecteur local, si vous le demandez.** Avec le [relecteur local](../llm-review/)
  activé, *Demander au relecteur local* lui envoie les noms des colonnes et quelques valeurs de
  chacune, et affiche son avis à côté des propositions. Il n'est jamais appliqué tout seul.
- **Les règles propres à un projet tiennent.** Une colonne que vos réglages pour un projet masquent y
  reste masquée, quoi qu'ajoute *Partout* : la liste l'indique, et *Ce projet* la change. Une colonne
  que le `latet.yaml` du projet masque y reste masquée quoi que vous choisissiez, et la liste dit
  laquelle.
- **Après un redémarrage,** les réglages d'un projet se chargent à sa session suivante : d'ici là, le
  choix ne peut aller que dans vos règles globales.
- **Codex et OpenCode** envoient toute la conversation avec chaque requête, et Latet la masque de
  nouveau à chaque fois : dès la requête suivante, les sorties qu'elle contient déjà partent aussi avec
  ces colonnes en clair. Dans Claude Code, une sortie reste telle que le plugin l'a masquée à son
  arrivée.

Pour une seule colonne, cliquez sur l'une de ses valeurs : le panneau qui s'ouvre propose
*Ne pas masquer `status` dans ce projet*, et indique quand le nom d'une colonne ressemble à un ID, une
date, un état ou un comptage.

## Le garde-fou anti-extraction

```yaml
dump_guard: 50
```

Une sortie d'outil qui contient plus de valeurs personnelles que ce nombre (noms, adresses e-mail,
numéros de téléphone, numéros d'identité...) est retenue : le modèle reçoit à la place une courte note
qui lui demande de réclamer moins de lignes ou des comptages. Le tableau de bord marque l'échange
*Sortie retenue*. Non défini, ou 0 : aucune limite.

Dans le profil `max`, avec le [modèle local de noms](../../getting-started/installation/) installé,
une sortie d'outil est aussi lue à la recherche de noms et d'adresses : les textes et les clés d'une
sortie JSON sont lus d'un coup, et une sortie que le modèle ne peut pas lire en cinq secondes est
retenue de la même façon, avec une note qui demande au modèle de réclamer moins de lignes ou de
champs. Rien de ce qu'il y aurait trouvé ne part en clair.

## Le navigateur

Un agent qui pilote un navigateur lit les pages comme des sorties d'outils : instantanés
d'accessibilité, HTML, résultats de scripts, réponses réseau. Le mode données strict s'y applique
comme au reste, tableaux compris.

Ses captures d'écran sont des images : avec la [reconnaissance de texte](../images/) installée, leur
texte est lu et masqué, et chaque valeur est recouverte par sa valeur factice. Pour n'envoyer dans un
projet que les images que Latet a lues et masquées, ou aucune :

```yaml
images: mask    # or block: no image or PDF is sent
```

## Dans un projet

Ces clés fonctionnent dans [config.yaml](../../reference/configuration/), dans le
[`latet.yaml`](../projects/) d'un projet et dans vos propres réglages pour un projet. Le fichier du
projet ne peut qu'ajouter de la protection :

| Clé | Dans `latet.yaml` | Dans vos réglages pour le projet |
| --- | --- | --- |
| `data` | seulement `strict` | `strict` ou `normal` |
| `data_tools` | ignoré | ajouté aux vôtres |
| `data_files` | ajouté | ajouté aux vôtres |
| `data_hosts` | ajouté | ajouté aux vôtres |
| `fields` | seulement les règles qui masquent, d'un type masqué là | toute règle, `mask: false` compris |
| `dump_guard` | seulement plus bas | toute valeur ; 0 le désactive |
| `images` | seulement moins d'images : `mask`, puis `block` | `allow`, `mask` ou `block` |
| `ocr` | seulement `auto` | `auto` ou `off` |

Un projet dont le profil est `max`, dans son fichier ou dans vos réglages pour lui, a le mode données
strict activé, sauf si `data` en dispose autrement à cet endroit ou dans vos règles globales.
