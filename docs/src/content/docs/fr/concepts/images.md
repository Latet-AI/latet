---
title: Captures d'écran et images
description: Le texte d'une image lu sur votre machine, masqué comme n'importe quel autre texte, et recouvert sur l'image par sa valeur factice.
---

Une capture d'écran d'un terminal, d'un formulaire ou d'une facture contient les mêmes valeurs que le
texte qui l'entoure : une adresse e-mail, un IBAN, le nom d'un client. Avec la reconnaissance de texte
installée, Latet lit sur votre machine le texte de chaque image que transporte une requête, le masque
comme n'importe quel autre texte, et recouvre chaque valeur trouvée en peignant sa valeur factice
par-dessus. Le modèle lit la valeur factice dans l'image ; quand il la réécrit, Latet remet en place
la vraie valeur, comme il le fait pour le texte.

## L'activer

```sh
latet ocr install   # downloads the models once (about 31 MB)
```

- Les modèles fonctionnent avec le composant optionnel `ocr`, ONNX Runtime et Pillow, que les
  programmes d'installation fournissent avec Latet ; `latet upgrade` l'ajoute à un Latet installé
  sans lui.
- Les modèles sont [PP-OCRv6 small](https://huggingface.co/PaddlePaddle/PP-OCRv6_small_rec_onnx) de
  PaddleOCR, l'un qui trouve les lignes de texte et l'autre qui les lit, sous licence Apache-2.0. Ils
  proviennent de huggingface.co, dans une révision figée, et chaque fichier est vérifié par rapport à
  un SHA-256 figé avant utilisation.
- Ils tournent sur votre processeur, en une seconde environ pour une capture d'écran Full HD, en
  quelques secondes pour celle d'un écran 4K ou 5K, et en plusieurs secondes pour la capture d'une
  page entière. Rien n'est envoyé nulle part, et Latet ne se connecte à huggingface.co que lorsque
  vous lancez `latet ocr install`.
- Un daemon en cours d'exécution commence à les utiliser aussitôt. `latet ocr status` indique s'ils
  sont utilisés ; `latet ocr remove` les supprime. Pour les garder installés sans les utiliser,
  définissez `ocr: off`.
- `latet global-install --ocr` les télécharge avec le reste de l'installation. Dans
  [Docker](../../getting-started/docker/) : `docker exec latet latet ocr install --yes`.

## Ce qui arrive à une image

1. Latet lit les lignes de texte qu'elle contient, et l'emplacement de chaque caractère. Une image de
   plus de 3 000 pixels de long environ, comme la capture d'un écran 4K ou 5K, d'une page entière
   bien plus haute qu'un écran ou de plusieurs écrans côte à côte, est lue en parties de 2 000 à
   3 000 pixels qui se chevauchent, à une taille où son texte se lit : si on la réduisait pour la
   lire d'un coup, son petit texte serait mal lu, voire pas trouvé du tout.
2. Le texte lu devient un texte de plus de la requête, *texte dans une image* : les détecteurs, vos
   termes, le coffre-fort, le [relecteur local](../llm-review/) et le
   [Relecteur en direct](../live-review/) le voient comme n'importe quel autre.
3. Chaque valeur trouvée est recouverte sur l'image : un rectangle de la couleur du fond, avec la
   valeur factice écrite par-dessus en lettres d'à peu près la même taille. Un type dont la
   [politique](../profiles/#politiques) est `redact` reçoit une simple barre. L'image garde son format,
   sans ses métadonnées ; une image qui dépasserait la taille qu'acceptent les fournisseurs (5 Mo) est
   envoyée en JPEG, ou retenue si elle reste trop grande même ainsi.
4. Renvoyée, puisque chaque agent renvoie la conversation, la même image part à l'identique, octet
   pour octet, même après un redémarrage : le cache de prompt du fournisseur continue de fonctionner.

Une image sans rien à masquer part telle quelle. Une image que Latet ne peut pas lire est retenue : le
modèle reçoit à sa place une courte note, *[image withheld by Latet: ...]*, avec le motif. Cela arrive
à une image animée, à une image de plus de 20 Mo ou de 40 mégapixels, à une image qui n'est pas
valide, à une image si longue pour sa largeur, ou si large pour sa hauteur, qu'il faudrait la lire en
plus de 16 parties (la capture d'une page de plus de 29 000 pixels de long environ), et à toute image
pendant que les modèles sont encore en cours de chargement (une requête les attend jusqu'à
20 secondes).

## Quelles images

| Quoi | Lues et masquées |
| --- | --- |
| Images dans les messages : les images base64 de Claude, les URL `data:` d'OpenAI Chat et Responses, Codex en HTTP et sur son WebSocket, les images en ligne de Gemini (`inlineData`) | oui |
| Captures d'écran renvoyées par les outils : Read de Claude Code sur une image, les serveurs MCP comme celui d'un navigateur, le computer use d'OpenAI | oui |
| Images désignées par leur adresse (une URL `https://`, un ID de fichier), PDF et autres documents, audio | non : envoyés tels quels, avec l'indicateur *Non inspecté*, ou non envoyés (voir plus bas) |

## Les réglages

`images` indique ce qui peut partir, `ocr` si les images sont lues. Les deux se trouvent dans
**Règles** → **Protection** → **Données de production**, dans la
[configuration](../../reference/configuration/), et dans un projet.

| `images` | Avec la reconnaissance de texte | Sans elle |
| --- | --- | --- |
| `allow` (par défaut) | les images sont lues et masquées | les images sont envoyées telles quelles, avec l'indicateur *Non inspecté* |
| `mask` | seules les images lues et masquées sont envoyées : une requête qui en transporte une que Latet ne peut pas lire (un PDF, une image désignée par son adresse) n'est pas envoyée | aucune requête contenant une image n'est envoyée |
| `block` | aucune requête qui transporte une image ou un document n'est envoyée | de même |

- Le [profil `max`](../profiles/) n'envoie pas les images, comme `block`. Un de vos projets peut
  définir `images: mask` pour les envoyer masquées.
- Le `latet.yaml` d'un dépôt ne peut que réduire les images envoyées. En envoyer davantage, depuis vos
  réglages ou l'un de vos projets, et `ocr: off`, attendent votre approbation quand c'est un agent qui
  les demande ([Les agents et vos réglages](../agents-and-settings/)).
- Un agent arrêté reçoit une erreur qui dit pourquoi et quoi faire.

## Dans Claude Code

En [mode client](../../agents/claude-code/), le plugin masque les images avant que Claude Code ne les
envoie :

- **Les images que renvoient les outils** (Read sur un PNG, la capture d'écran d'un serveur MCP) sont
  remplacées par leur copie masquée avant que Claude ne les voie. Votre écran affiche l'original.
- **Une capture d'écran que vous collez** ne peut pas être modifiée, seulement retirée du prompt. Quand
  Latet y trouve des valeurs à masquer, le plugin la retire et ajoute à votre prompt une note avec le
  chemin d'une copie masquée, `.latet/masked-<id>.png` dans votre projet, que Claude ouvre avec Read.
  Rien n'y est écrit : le plugin répond lui-même à ce Read.
- La vérification avant chaque requête couvre aussi les images : une image avec une valeur en clair
  arrivée dans la requête par un autre chemin l'arrête.

## Relire les images

- **Dans le [Relecteur en direct](../live-review/),** le texte d'une image attend comme n'importe quel
  texte, avec l'image au-dessus : un cadre là où chaque valeur reçoit sa valeur factice, les valeurs
  que vous masquez dans son texte recouvertes elles aussi, et vous pouvez tracer un rectangle sur
  n'importe quelle partie de l'image pour la recouvrir d'une barre, ou ne pas envoyer l'image du tout.
  Votre décision est conservée, scellée, pour cette image : renvoyée, elle part de la même façon.
- **Le [relecteur local](../llm-review/#images)** lit de toute façon le texte de chaque image. On peut
  aussi lui montrer l'image elle-même, uniquement si vous l'activez, pour trouver ce que la
  reconnaissance de texte a manqué.

## Dans le tableau de bord

- Un échange affiche ses images telles qu'elles ont été envoyées (avec `history: full`), chacune avec
  un lien vers le texte qui y a été lu, et les indicateurs *Image : masquée* et *Image : retenue*.
- **Règles** indique si la reconnaissance de texte est installée et utilisée, et la commande qui
  l'installe.

## Ce que la reconnaissance de texte ne peut pas faire

- **La lecture se fait au mieux, comme pour les noms.** Le texte qui n'est pas lu (très petit, pâle,
  manuscrit, pivoté, dans une police stylisée) part tel quel. Pour les captures d'écran qui ne doivent
  jamais partir, utilisez `images: block`, les rectangles du Relecteur en direct, ou le relecteur local
  à qui l'on montre les images.
- **Une valeur mal lue est masquée telle qu'elle a été lue.** Un `O` lu à la place d'un `0` dans un
  IBAN ou un code fiscal italien est corrigé grâce à leurs chiffres de contrôle ; une autre valeur mal
  lue peut ne pas être reconnue.
- **Le modèle lit la valeur factice.** S'il la lit mal, sa réponse contient la valeur factice mal lue,
  pour laquelle Latet ne peut pas remettre en place la vraie valeur.
- **Les PDF et autres documents ne sont pas lus**, ni les images désignées par leur adresse : voir le
  tableau ci-dessus.
- **Seulement du texte.** Les visages, les signatures, les logos et l'écriture manuscrite ne sont pas
  recouverts.
