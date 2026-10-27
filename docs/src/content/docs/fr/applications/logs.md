---
title: Masquer les journaux avant qu'ils partent
description: Une application masque ses lignes de journal avec un serveur Latet avant de les expédier, et un outil de support restaure la ligne dont il a besoin.
sidebar:
  label: Journaux
---

Les journaux d'une application nomment ses clients : leurs adresses e-mail, leurs adresses IP, leurs
IBAN, de temps en temps un jeton. Expédiés tels quels, ils restent des mois dans un service de
journaux, lus par tous ceux qui lisent les journaux. Avec l'[API pour les applications](../),
l'application masque chaque lot de lignes avec un serveur Latet avant de l'expédier, et le service de
journaux reçoit des valeurs factices. Comme une valeur reçoit toujours la même valeur factice, les
lignes d'un client peuvent toujours être cherchées, comptées et suivies sur plusieurs jours ; quand le
support doit savoir de qui il s'agissait, un outil muni du bon jeton restaure la ligne.

## Les jetons

Une clé d'API pour les journaux dont vous voulez suivre les clients ensemble, avec un jeton pour
chaque côté :

```sh
latet server --server https://latet.acme.lan keys add "Shop logs" --out shop-logs.key
latet server --server https://latet.acme.lan keys add-token "Shop logs" shipper --mask --out shipper.token
latet server --server https://latet.acme.lan keys add-token "Shop logs" support --unmask --out support.token
```

L'application détient `shipper.token`, qui peut masquer et ne peut voir aucune vraie valeur ; l'outil
de support détient `support.token`, qui peut restaurer et rien d'autre. Le premier jeton,
`shop-logs.key`, reste chez l'administrateur : il a toutes les permissions. Les lignes masquées avec
une autre clé reçoivent d'autres valeurs factices : gardez une seule clé pour les services dont vous
cherchez les journaux ensemble.

## Masquer les lignes

Avec le client Python `latet-client` (`pip install latet-client`, Python 3.10 ou plus récent) :

```python
from pathlib import Path

from latet_client import Latet, LatetError

latet = Latet("https://latet.acme.lan", Path("shipper.token").read_text().strip())


def ship(lines: list[str]) -> None:
    """Mask a batch of log lines, then send them to the log service."""
    try:
        results = latet.mask(lines, kind="data")
    except LatetError:
        keep_for_later(lines)  # never send the lines as they are
        return
    for result in results:
        if result.text is not None:
            send_to_log_service(result.text)
        else:  # blocked: the reason names no value
            send_to_log_service(f"[line withheld by Latet: {result.blocked}]")
```

Les lignes

```text
2026-10-26T09:14:02Z INFO payment accepted customer=mario.rossi@acme.it iban=IT60X0542811101000000123456
2026-10-26T09:14:05Z WARN card declined customer=mario.rossi@acme.it ip=93.184.216.34
```

partent ainsi :

```text
2026-10-26T09:14:02Z INFO payment accepted customer=tommaso.gallo@margie-6e5a.example iban=IT17F3945471087320356678487
2026-10-26T09:14:05Z WARN card declined customer=tommaso.gallo@margie-6e5a.example ip=198.51.100.41
```

Envoyez les lignes par lots, pas un appel par ligne : un appel prend jusqu'à 1 000 lignes, et le
client envoie une liste plus longue en plusieurs appels, l'un après l'autre. Les lignes d'un appel
sont masquées ensemble : une valeur qu'une ligne de l'appel apprend au coffre-fort est masquée aussi
dans les autres lignes de l'appel. Une valeur que seul un appel ultérieur apprend n'est pas masquée
dans les lignes des appels précédents, qui ont déjà reçu leur réponse. Utilisez `kind="data"` pour
les journaux ([prose et données](../#prose-et-données)).

Quand l'appel échoue, gardez les lignes et réessayez, ou abandonnez-les : ne les envoyez jamais sans
les masquer. Le client réessaie de lui-même un 429 et un 503 (trois fois, après l'attente que demande
le serveur) avant de lever l'erreur. Masquer deux fois les mêmes lignes donne les mêmes valeurs
factices : réessayer est donc sans risque.

Chaque refus du serveur est une `LatetError`, avec son statut et ses mots, et un appel qui n'a pas
abouti est une `ConnectionFailed` (statut 0). `ConnectionFailed` prend la place des erreurs de
`httpx`, y compris celles d'un `httpx.Client` que vous passez en `client=`, et ne contient pas la
requête, pour que le jeton ne figure dans aucune erreur : interceptez les erreurs du client, pas
celles de `httpx`. Une adresse `http://` d'une autre machine donne un `ClearTextWarning`, parce que le
jeton et les lignes circuleraient en clair. Le client en juge par la seule adresse : un `client=` à
vous qui atteint le serveur en TLS par un autre chemin reçoit donc aussi l'avertissement ; le module
`warnings` de Python le fait taire.

## Le format des lignes

Ce que les détecteurs reconnaissent est masqué où qu'il se trouve dans une ligne : adresses e-mail,
IBAN, numéros de carte, adresses IP publiques, jetons et clés, les termes et reconnaisseurs de la clé.
Pour le reste, réglez les règles de la clé
([réglages partagés et règles des clés d'API](../../getting-started/server/#définir-les-réglages-partagés-et-les-règles-des-clés-dapi)) :

- Les **règles de champ** masquent les valeurs d'un champ d'après son nom, comme `phone` ou
  `customer_name`, et le **mode données strict** (`data: strict`) masque chaque valeur d'un
  enregistrement ([données de production](../../concepts/production-data/)). Ils trouvent une valeur
  par sa clé dans du JSON, et dans les lignes `clé=valeur` où une valeur qui contient des espaces est
  entre guillemets : `phone="+39 347 123 4567"`. Une valeur sans guillemets s'arrête au premier
  espace. Écrivez vos lignes de journal en JSON, ou mettez ces valeurs entre guillemets.
- Les **termes** et les **reconnaisseurs** attrapent vos propres noms et formats : le nom d'un client,
  un numéro de commande écrit `ORD-123456` ([reconnaisseurs personnalisés](../../concepts/custom-recognizers/)).
- Les **noms** dans le texte libre d'un message sont trouvés avec le modèle local de noms installé sur
  le serveur, dans les textes `prose` ; dans les textes `data`, avec le profil `max`.

`scan`, avec un jeton qui peut analyser, montre ce que masquerait un échantillon de vos lignes, sans
rien conserver : essayez-le sur de vraies lignes avant d'activer le masquage.

## Restaurer une ligne

Une personne du support copie une ligne masquée depuis le service de journaux ; son outil la restaure :

```python
support = Latet("https://latet.acme.lan", Path("support.token").read_text().strip())
line = "2026-10-26T09:14:05Z WARN card declined customer=tommaso.gallo@margie-6e5a.example ip=198.51.100.41"
print(support.unmask(line).text)
# 2026-10-26T09:14:05Z WARN card declined customer=mario.rossi@acme.it ip=93.184.216.34
```

Pour trouver toutes les lignes d'un client, masquez son adresse avec un jeton qui peut masquer et
cherchez la valeur factice dans le service de journaux. Chaque restauration est inscrite dans le
journal d'administration du serveur, avec le jeton et le nombre de valeurs restaurées ;
`latet server log --action apps.unmasked` les liste.

## À quelle vitesse, et combien d'instances

Sur une instance, le masquage traite environ 190 lignes par seconde pour une clé nouvelle et de 410 à
440 pour une clé dont le coffre-fort contient de 200 000 à 1 000 000 de valeurs ; la restauration de
1 500 à 2 700 ([les mesures](../#à-quelle-vitesse)). Les appels d'une clé s'exécutent l'un après
l'autre sur une instance : un service qui journalise plus vite a donc besoin de plus d'instances
derrière le répartiteur de charge, ou de répartir ses lignes entre des clés dont les clients n'ont pas
besoin d'être suivis ensemble.

Un appel garde les lignes en mémoire jusqu'à sa réponse : expédiez depuis une file d'attente à vous,
pour qu'un serveur lent ou injoignable retarde les journaux au lieu de les perdre ou de les envoyer
sans masque.

## Sans Python

N'importe quel langage peut appeler l'API en HTTPS : un `POST` avec un corps JSON et le jeton dans un
en-tête `Authorization: Bearer`. La [référence HTTP](../../reference/apps-api/) donne le contrat, ses
erreurs et une description OpenAPI à partir de laquelle générer un client.
