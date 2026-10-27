---
title: Oublier des valeurs
description: Retirer les valeurs d'une personne du coffre-fort d'une clé d'API quand elle demande que ses données disparaissent.
sidebar:
  label: Oublier des valeurs
---

Le coffre-fort d'une clé d'API conserve chaque valeur qu'il a masquée, avec sa valeur factice, tant
que la clé existe : une restauration fonctionne sur une ligne de journal de l'an dernier. Quand une
personne demande que ses données disparaissent de vos journaux ou de l'index de votre application RAG,
le serveur oublie ses valeurs. Chaque valeur sort du coffre-fort de la clé, sa valeur factice ne
restaure plus rien à partir de ce moment, et la valeur reçoit une nouvelle valeur factice si un texte
la contient de nouveau.

Il y a deux façons de le faire : un administrateur lance `latet server keys forget`, ou une application
appelle l'API pour les applications avec un jeton qui a la permission `forget`. Les deux font la même
chose, et les deux sont inscrites dans le journal d'administration avec des nombres, jamais avec une
valeur.

## L'administrateur

Mettez les valeurs dans un fichier, une par ligne, en UTF-8 :

```text
mario.rossi@acme.it
+39 347 123 4567
Mario Rossi
```

```sh
latet server --server https://latet.acme.lan keys forget "Shop" --file erasure.txt
```

La commande dit combien de valeurs elle a lues, demande avant de les envoyer, puis dit combien le
coffre-fort de la clé en contenait et combien non. Sans `--file`, elle vous demande de taper les
valeurs dans votre terminal, une par ligne, et une ligne vide les termine. Elle ne prend jamais une
valeur sur la ligne de commande, où le shell la garderait dans son historique. Elle refuse de tourner
dans un agent d'IA, puisque les valeurs sont les données d'une personne, et dans un script elle demande
`--yes`. Elle envoie les valeurs par appels de 1 000 au plus. Chaque appel est une entrée
`key.forgotten` du journal d'administration, avec le nombre de valeurs demandées, oubliées et absentes
du coffre-fort. Supprimez le fichier quand vous avez fini : il contient les valeurs.

Le premier oubli d'une clé après une mise à jour indexe d'abord ce que le relecteur local de la clé a
mémorisé et ce que son historique a gardé avant la mise à jour : sur une clé avec beaucoup
d'historique, cela prend des minutes. La commande attend jusqu'à cinq minutes chaque appel, et l'appel
d'une application peut durer autant. Si un appel cesse d'attendre, relancez-le : ce que le serveur a
oublié reste oublié, et compte alors comme absent du coffre-fort.

Cela fonctionne que la clé soit activée ou désactivée. Le coffre-fort d'une personne ne s'oublie pas
ainsi : les personnes oublient des valeurs dans leur tableau de bord (Coffre-fort).

## Une application

Donnez à l'application un jeton à elle avec `forget`. Aucune autre permission ne l'inclut, et le
premier jeton de la clé, qui peut analyser, masquer et restaurer, ne peut pas oublier :

```sh
latet server --server https://latet.acme.lan keys add-token "Shop" erasure --forget --out erasure.token
```

L'application envoie jusqu'à 1 000 valeurs par appel :

```sh
printf 'Authorization: Bearer %s' "$(cat erasure.token)" | curl -sS -H @- \
  -H "Content-Type: application/json" \
  -d '{"values": ["mario.rossi@acme.it", "+39 347 123 4567"]}' \
  https://latet.acme.lan/_latet/apps/v1/forget
```

```json
{"forgotten": 1, "unknown": 1}
```

`forgotten` est le nombre de valeurs que le coffre-fort de la clé contenait, `unknown` le nombre de
celles qu'il ne contenait pas, chaque valeur comptée une fois. Chaque appel est une entrée
`apps.forgotten` du journal d'administration (le jeton, le nombre de valeurs demandées, oubliées et
absentes), écrite dans la même transaction que l'oubli : si le journal ne l'accepte pas, rien n'est
oublié et l'appel répond 503. Avec le client Python, `latet.forget([...])` envoie une liste plus longue
en plusieurs appels et renvoie `ForgetResult(forgotten, unknown)`. Un jeton sans la permission reçoit
403.

## Ce qui est retiré

- **Chaque valeur telle qu'elle est écrite.** Une autre casse, d'autres espaces ou une autre forme
  écrite (`mario.rossi%40acme.it` dans une URL) sont une autre valeur. Le coffre-fort garde chaque forme
  qu'il a rencontrée comme une entrée à part : citez donc chaque forme qui doit disparaître.
- **Tout ce qui la nomme dans le coffre-fort :** son entrée et sa valeur factice, scellées avec la clé
  racine de la clé d'API, les hachages que l'index garde de leurs mots, et les réponses mémorisées du
  relecteur local qui ont trouvé la valeur, que le coffre-fort la contienne ou non. Ensuite chaque
  instance la retire de sa mémoire, avec les textes qu'elle garde masqués avec sa valeur factice, dès
  que les appels déjà en cours y sont finis.
- **Ce que les agents de la clé ont laissé dans Latet.** Quand le premier jeton de la clé sert des
  agents par la passerelle, l'entrée de la valeur quitte aussi leurs conversations, et l'historique de
  la clé perd les textes, les réponses du modèle et les constats du relecteur local qui la nommaient,
  avec son ancienne valeur factice. Un message antérieur qu'un agent envoie à nouveau est masqué à
  nouveau, avec la nouvelle valeur factice.
- **Son ancienne valeur factice ne restaure plus rien, et aucune autre valeur ne la reçoit jamais.**
  Une ligne de journal masquée avant garde l'ancienne valeur factice, et la restauration la laisse
  telle quelle. La valeur factice est retirée : le coffre-fort n'en garde que le hachage à clé, lié à
  aucune entrée, aussi longtemps que le coffre-fort existe, si bien qu'elle n'est jamais tirée de
  nouveau pour une autre valeur et qu'une ancienne ligne n'est jamais restaurée avec la valeur de
  quelqu'un d'autre. Une valeur factice retirée ne prend pas de place, puisque la limite du
  coffre-fort compte les valeurs, mais elle reste : l'état du serveur et `latet_vault_near_limit`
  pèsent les valeurs factices retirées d'une clé avec ses valeurs, si bien qu'un jeton qui masque et
  oublie en boucle s'y voit. Ne donnez `forget` qu'au processus qui traite les demandes d'effacement.
- **La valeur reçoit une nouvelle valeur factice si elle revient.** Les valeurs factices et les
  entrées sont tirées de la valeur ; après un oubli, le coffre-fort de la clé les tire donc dans une
  nouvelle génération : la valeur reçoit une nouvelle valeur factice et une nouvelle entrée, et
  l'ancienne valeur factice ne revient jamais. Les textes masqués qui ont quitté Latet avant ne
  peuvent pas être reliés aux nouveaux par leurs valeurs factices.
- **Une adresse garde à part le domaine de son entreprise.** Une adresse au domaine d'une entreprise
  (`mario.rossi@acme.it`) garde le domaine comme une valeur à part, et le domaine de la valeur factice
  est la valeur factice de ce domaine. Oublier l'adresse laisse le domaine, donc la partie domaine de
  l'ancienne valeur factice se restaure encore en `acme.it` ; oubliez aussi `acme.it` s'il doit
  disparaître. Le domaine d'un fournisseur de messagerie, comme `gmail.com`, n'est jamais gardé.

Ce qui reste :

- les nombres dans le journal d'administration, et les échanges de l'historique dont les textes sont
  partis, sans ces textes ;
- les textes masqués qui ont déjà quitté Latet, dans votre service de journaux ou votre index, qui
  contiennent l'ancienne valeur factice et que vous supprimez là-bas s'ils doivent disparaître ;
- dans Valkey, scellés, pendant une semaine après leur dernière utilisation : les textes des
  conversations des agents de la clé tels qu'ils sont partis et les réponses du modèle telles qu'il
  les a écrites, qui contiennent l'ancienne valeur factice, jamais la valeur. Un message antérieur
  envoyé à nouveau est masqué à nouveau ; une réponse du modèle lui revient telle qu'il l'a écrite ;
- pendant une semaine au plus après la dernière utilisation d'une conversation, l'identifiant de
  l'ancienne entrée dans cette conversation des agents de la clé (un hachage à clé de la valeur, jamais
  la valeur), quand Valkey n'a pas répondu juste après l'oubli, ou quand une instance a manqué le
  message qui l'annonce (Valkey ne garde pas les messages) et a réécrit l'identifiant depuis une
  conversation qu'elle garde ;
- ce que PostgreSQL lui-même garde des lignes supprimées, scellé, dans ses fichiers jusqu'à ce qu'il
  réutilise leur place, dans son journal d'écriture anticipée (WAL) et dans les sauvegardes de la base.

Un appel en cours pendant l'oubli peut encore utiliser l'ancienne valeur factice, et ce qu'il garde
à ce moment peut encore nommer la valeur : une réponse du relecteur local, qu'un second oubli un
instant plus tard retire, et un échange que l'historique enregistre, qui reste tant que l'historique
le garde (`retention_days`). Les instances retirent la valeur dès que les appels déjà en cours y
sont finis.

## Qui peut apprendre quoi

La réponse dit combien de valeurs le coffre-fort contenait : un jeton qui peut oublier apprend donc si
une valeur est à la clé, comme un jeton qui peut analyser ou masquer
([qui peut apprendre quoi](../#qui-peut-apprendre-quoi)). Donnez `forget` seulement au processus qui
traite les demandes d'effacement, et lisez le journal avec `latet server log --action apps.forgotten`.
