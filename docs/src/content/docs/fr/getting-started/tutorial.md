---
title: Tutoriel
description: Voyez Latet à l'œuvre sur votre machine en 30 minutes environ, avec un agent fictif, un modèle fictif et des données inventées, avant d'y brancher un vrai agent.
---

`latet tutorial` vous montre Latet à l'œuvre dans votre navigateur, en 30 minutes environ, avant que
vous n'y branchiez un vrai agent. Un agent fictif envoie ses requêtes à un modèle fictif. Latet,
entre les deux, est le vrai : le même masquage, les mêmes vérifications et le même tableau de bord
qu'avec votre propre agent. Les données sont inventées, et rien ne quitte votre machine.

## Le lancer

```sh
latet tutorial
```

La commande ouvre le tutoriel dans votre navigateur, sur le menu des chapitres, et tourne jusqu'à ce
que vous y mettiez fin. Le terminal indique où il se trouve et comment y mettre fin :

```text
Tutorial at http://127.0.0.1:52817 — Ctrl+C ends it
```

| Commande | Ce qu'elle fait |
| --- | --- |
| `latet tutorial` | Ouvre le menu des chapitres. |
| `latet tutorial terms` | Ouvre un chapitre, par son ID : ici, *Vos propres termes*. |
| `latet tutorial --list` | Affiche les chapitres, leur durée en minutes et ceux que vous avez terminés, puis s'arrête. |
| `latet tutorial --print-url` | Affiche le lien de connexion au lieu d'ouvrir le navigateur. Il ne fonctionne qu'une fois, pendant deux minutes. |
| `latet tutorial --keep` | Conserve le dossier du bac à sable quand le tutoriel se termine, et indique où il se trouve. |

Le tutoriel tourne sur votre machine, pas dans le [conteneur](../docker/) de Latet.

## Les chapitres

Chaque chapitre compte quelques étapes. Vous envoyez à l'agent un prompt, saisi pour vous, et le
panneau de Latet montre ce qu'il est advenu de sa requête, avec les vues du tableau de bord lui-même.
Certaines étapes vous demandent d'y agir : cliquer sur une valeur, ajouter un mot à vos termes,
approuver une requête, enregistrer vos consignes.

| Chapitre | Minutes | Ce qui se passe |
| --- | --- | --- |
| `basics` : Les bases | 5 | Une clé dans `.env`, une adresse e-mail et un numéro de téléphone partent sous forme de valeurs factices ; le fichier que l'agent écrit contient la vraie clé. |
| `terms` : Vos propres termes | 3 | Un nom de code part en clair jusqu'à ce que vous l'ajoutiez à vos termes ; dès lors, le modèle reçoit une valeur factice, un nom qu'il n'a jamais vu. |
| `false-positives` : Une valeur masquée pour rien | 3 | La carte de test de Stripe est masquée comme la carte d'un client ; *Non sensible* la laisse passer. |
| `stopped` : Une requête arrêtée | 3 | Une clé collée dans une conversation d'avant l'activation de Latet arrête la requête ; une nouvelle conversation sans elle passe. |
| `live` : Le Relecteur en direct | 3 | Une requête attend votre approbation : vous masquez à la main le nom d'un client, puis vous l'approuvez. |
| `settings` : Un agent qui assouplit vos règles | 3 | L'agent demande à Latet de laisser passer les adresses e-mail, et la modification vous attend : approuvez-la ou annulez-la. |
| `data` : Données de production | 4 | Le mode données strict masque chaque valeur de la table que renvoie une requête de base de données ; vous laissez en clair les ID, les dates et les formules. |
| `llm` : Le relecteur local | 4 | Un ticket contient une adresse e-mail écrite en toutes lettres, un IBAN avec une faute de frappe, un nom et un diagnostic : aucun motif ne les trouve, mais le relecteur local, si. Puis, grâce à vos consignes, il trouve aussi les services de l'hôpital. Le relecteur est fictif : c'est le Tutorial Reviewer. |
| `images` : Captures d'écran | 3 | L'agent envoie une capture d'écran d'un paiement refusé : Latet y lit l'adresse e-mail, l'IBAN et le numéro de téléphone, et recouvre chacune de ces valeurs par sa valeur factice avant le départ de l'image. Ce chapitre a besoin de la reconnaissance de texte : `latet ocr install`. |

Un chapitre se termine par ce que vous avez appris et l'endroit où le retrouver dans le tableau de
bord. Le dernier se termine par la façon d'utiliser Latet avec votre propre agent. Le menu marque
les chapitres que vous avez terminés, d'une exécution à l'autre, et *Recommencer* remet un chapitre
dans son état de départ.

## Un bac à sable

Les chapitres masquent des valeurs, arrêtent des requêtes et changent des réglages : ils tournent
donc sur un Latet à eux, dans un bac à sable :

- **Ses propres données, réglages et historique**, dans un dossier temporaire, et son propre serveur
  sur `127.0.0.1`, sur un port libre. Votre propre daemon n'est pas touché, et n'a pas besoin de
  tourner.
- **Rien de ce qui vous appartient ne change :** vos réglages, votre coffre-fort et votre historique,
  et la connexion à votre tableau de bord (la page du tutoriel se connecte avec son propre cookie).
- **Rien ne quitte votre machine.** La seule route du bac à sable mène au modèle fictif, qui tourne
  dans la commande `latet tutorial` : aucune requête ne peut atteindre un fournisseur, et aucune clé
  d'API ni connexion n'est nécessaire.
- **Les mêmes valeurs factices à chaque exécution :** le bac à sable les crée à partir d'une clé
  fixe, pas de la vôtre.
- **Un seul fichier hors du bac à sable :** `tutorial.json` dans votre
  [dossier de données](../uninstall/#3-supprimer-les-données-locales-facultatif), avec les ID des chapitres que
  vous avez terminés.

Le bac à sable n'a ni détection des noms ni rien de votre identité : les chapitres montrent ce que
trouvent les détecteurs, et ce que vous masquez à la main. Dans le chapitre sur le relecteur local,
le relecteur est le Tutorial Reviewer : fictif, il trouve ce que dit le chapitre, et aucune requête
n'atteint de serveur. Dans le chapitre sur les captures d'écran, le bac à sable lit le texte des
images avec vos propres modèles de reconnaissance de texte, une fois qu'ils sont installés
(`latet ocr install`). Toutes les autres pages de son tableau de bord sont là pour être explorées,
avec une barre en haut qui vous ramène au chapitre.

## Y mettre fin

Cliquez sur *Terminer le tutoriel* dans la page, ou appuyez sur Ctrl+C dans le terminal : le bac à
sable s'arrête, et son dossier est supprimé. Une exécution interrompue autrement, en fermant son
terminal par exemple, laisse le dossier dans votre dossier temporaire, et le `latet tutorial`
suivant le supprime. Une exécution qui s'arrête sur une erreur affiche les dernières lignes de son
journal, et conserve le dossier, avec le journal complet, jusqu'à l'exécution suivante.

## Dans votre propre tableau de bord

Ce que montrent les chapitres, dans le tableau de bord de votre propre Latet (`latet dashboard`) :

| Quoi | Où |
| --- | --- |
| Chaque requête : le texte de votre machine, et ce que le modèle a reçu | **En direct**, puis la requête |
| Chaque valeur que Latet a remplacée, et sa valeur factice | **Coffre-fort** |
| Vos termes | **Règles** → **Termes** |
| Les valeurs que vous laissez passer | **Règles** → **Valeurs autorisées** |
| Les requêtes que Latet a arrêtées, et pourquoi | **En direct**, filtré par *Bloquée*, puis la requête |
| Ce qui attend votre approbation : des requêtes, et des modifications de vos réglages | **Approbations** |
| Le Relecteur en direct, pour tous les projets ou pour un seul | **Règles** → **Relecteurs** → **Relecteur en direct** |
| Le mode données strict, et les colonnes que vous laissez en clair | **Règles** → **Protection** → **Données de production** |
| Ce que le relecteur local a trouvé dans une requête | **En direct**, puis la requête : *Relecteur local* |
| Le relecteur local, et vos consignes | **Règles** → **Relecteurs** → **Relecteur local** |
| Une image que contenait une requête, telle qu'elle a été envoyée, et le texte qui y a été lu | **En direct**, puis la requête : *Images* |
| Ce qui part avec une image ou un PDF, et si le texte des images est lu | **Règles** → **Protection** → **Données de production** : *Images et PDF* |

Essayez-le ensuite sur votre propre agent, le temps d'une session, avec une clé inventée : voir le
[Démarrage rapide](../quickstart/).
