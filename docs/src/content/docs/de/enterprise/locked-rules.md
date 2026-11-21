---
title: Gesperrte Regeln
description: Vorgaben, die Ihr Unternehmen auf seinem Latet-Server festlegt und die keine Person und kein API-Schlüssel unterschreiten kann.
---

Mit Latet Enterprise legt ein Unternehmen auf seinem [Latet-Server](../../getting-started/server/) eine
**Untergrenze** fest: Einstellungen, die keine Person und kein API-Schlüssel unterschreiten kann. Jede Person behält ihre
eigenen Einstellungen und kann jederzeit zusätzlichen Schutz hinzufügen. Eine Änderung, die unter die Untergrenze
fallen würde, wird abgelehnt, mit dem Hinweis, dass die Einstellung vom Unternehmen festgelegt ist; sie wird
abgelehnt und nicht zur Freigabe zurückgehalten, weil die Person sie nicht freigeben kann. Eine Änderung wird nur für
das abgelehnt, was sie selbst senkt: Einstellungen, die schon unter der Untergrenze lagen, hindern nicht daran, etwas
anderes zu ändern.

## Die Vorgabendatei

Die Vorgaben sind eine Regeldatei, im Format, das `latet rules export` schreibt (siehe
[Export und Import](../../concepts/export-import/)), ein Unternehmen kann also von einem Export ausgehen:

```yaml
latet_rules: 1
exceptions: company
profile: max
terms:
  - {value: Project Falcon, type: PROJECT}
  - {value: Northwind Traders, type: CLIENT}
policies: {IBAN: redact}
ocr: auto
```

**Was darin steht, ist gesperrt, und was fehlt, bleibt Sache jeder Person.** Mit dieser Datei kann niemand ein Profil
unterhalb von `max` wählen oder das Lesen des Textes in Bildern abschalten, Project Falcon und Northwind Traders werden
immer maskiert, IBANs werden immer geschwärzt, und es zählen nur die Ausnahmen des Unternehmens (hier legt es keine
fest); jede Person kann weiterhin eigene Begriffe, Erkenner und strengere Werte hinzufügen. Um eine Einstellung auf
dem Standardwert von Latet zu sperren, schreiben Sie sie in die Datei.

- **Das Profil bringt mit, was dazugehört:** Mit `profile: max` sind auch die Identifikationsnummern jedes Landes,
  private IP-Adressen, der strikte Datenmodus und die Regel von max für Bilder gesperrt.
- **Listen sind ein Minimum:** die Begriffe, Erkenner, Feldregeln, Typen personenbezogener Daten, Länder und
  Telefonregionen in den Vorgaben sowie die Dateien und Webadressen, die der strikte Datenmodus abdeckt, gelten
  immer, und die Einträge jeder Person kommen hinzu. Ein Begriff, ein Erkenner oder eine Feldregel eines Typs, den die
  Vorgaben zulassen (`policies: {CLIENT: allow}` und ein Begriff vom Typ `CLIENT`), ist dagegen eine Ausnahme des
  Unternehmens: Er wird niemandes Einstellungen hinzugefügt, und eine Person, die diesen Wert mit einem eigenen Begriff
  oder Erkenner maskiert, maskiert ihn weiterhin.
- **Die Erkenner und Feldregeln des Unternehmens gelten neben denen jeder Person:** ein eigener Erkenner oder eine
  eigene Feldregel einer Person wird durch diese nie außer Betrieb gesetzt. Hat ein Erkenner einer Person den
  Namen oder den Typ eines Erkenners des Unternehmens, wird der des Unternehmens unter einem eigenen Namen oder Typ
  verwendet; hat eine Feldregel einer Person den Namen einer Feldregel des Unternehmens, werden die beiden
  zusammengeführt, und das Feld wird maskiert.
- **Hinweise für den lokalen Prüfer** in den Vorgaben erhält er immer, vor den Hinweisen der Person.
- **Auch in jedem Projekt:** Latet bleibt in jedem Projekt eingeschaltet, und die eigenen Einstellungen eines Projekts
  bringen es nie unter die Untergrenze.
- **Neue Elemente in Anfragen:** `unknown_members: refuse` lehnt für jede Person und jeden API-Schlüssel ein Element
  einer Anfrage ab, das noch kein Anfrageprofil nennt, wie Latet es vor 0.15.0 tat, wobei ein Text oder eine Liste von
  Texten außerhalb eines Elements des Anbieters bei einer Anfrage, die Latet nur prüft (Claude Code, Bedrock, Vertex),
  weiterhin hinausgeht, sobald die abschließende Prüfung sie gelesen hat ([Neue Elemente in
  Anfragen](../../getting-started/upgrading-to-0-11/#neue-elemente-in-anfragen)); `unknown_members: check` sperrt
  nichts, da jeder sie ablehnen kann.

`latet policy check policy.yaml` liest eine Datei, bevor sie in Kraft gesetzt wird: was sie sperrt, was sie jeder
Person überlässt, wessen Ausnahmen zählen und was darin nicht gelesen werden konnte. Eine Einstellung mit einem Fehler
oder ohne Wert (nur `ocr:`) wird nicht gelesen und sperrt deshalb nichts. Der Befehl baut keine Verbindung auf und
endet mit Exit-Code 0, wenn alles in Ordnung ist, mit 1, wenn etwas in der Datei nicht gelesen wird, und mit 2, wenn
die Datei gar nicht gelesen werden kann.

## Ausnahmen

Werte und Muster, die nie maskiert werden, abgeschaltete Geheimnisregeln und was Werte im Klartext lässt (ein Erkenner
mit der Richtlinie `allow`, `allow` für einen Typ in `policies`, eine Feldregel mit `mask: false`) sind die
Ausnahmen. Der Schlüssel `exceptions` der Vorgaben sagt, wessen Ausnahmen zählen:

- **`personal`** (der Standard): Jede Person kann einen Wert weiterhin für sich als **Nicht sensibel** markieren und
  behält ihre eigenen `allow`-Entscheidungen, außer für die Typen der Begriffe, die das Unternehmen maskiert.
- **`company`**: Es zählen nur die Ausnahmen in den Vorgaben. Eigene Ausnahmen einer Person werden abgelehnt, wenn sie
  hinzugefügt werden, und nicht verwendet, wo sie schon vorhanden waren: ihre Werte und Muster, die nie maskiert
  werden, ihre abgeschalteten Geheimnisregeln, ihre Erkenner, die Gefundenes im Klartext lassen, ihr `allow` für einen
  Typ, den das Unternehmen nicht zulässt, und ihre Feldregeln mit `mask: false`. Auch eine eigene Feldregel eines Typs,
  den das Unternehmen zulässt (`policies: {URL: allow}` in den Vorgaben und eine eigene Feldregel vom Typ `URL`), ist
  eine Ausnahme, denn die Werte des Feldes blieben im Klartext, auch wo der strikte Datenmodus sie maskieren würde: Sie
  wird abgelehnt, wenn sie hinzugefügt wird, nicht verwendet, wo sie schon vorhanden war, und **Regeln** sagt, wie
  viele der eigenen so weggelassen werden. Wo ein solcher Typ in einem ihrer Projekte maskiert wird, bleibt die
  Feldregel erhalten, und ihr Typ wird überall maskiert, wo sie das Feld im Klartext gelassen hätte. Ein eigener
  Begriff eines solchen Typs bleibt erhalten und wird gespeichert, wenn er hinzugefügt wird: Er lässt nie etwas im
  Klartext, das das Unternehmen maskiert (ein Begriff des Unternehmens, was ein Erkenner oder eine Feldregel des
  Unternehmens findet, personenbezogene Daten), und auch nicht die Domain einer Adresse, wo immer die Maskierung sie
  ohne die eigenen Begriffe der Person maskieren würde. Zwischen den eigenen Begriffen gewinnt der längste Treffer, wie ohne Vorgaben.
  Die Identität der Person (ihr Name, ihre Benutzernamen, E-Mail-Adressen und Hostnamen) eines Typs, den das
  Unternehmen zulässt, bleibt auf dieselbe Weise erhalten: Sie lässt nie etwas im Klartext, das die Maskierung ohne sie
  maskieren würde. Wo ihr Konto mit dem Identitätsanbieter des Unternehmens verknüpft ist (SCIM oder Single Sign-on)
  und das Unternehmen Namen (`PERSON`) im Klartext lässt, ist ihr Name der, den der Anbieter liefert, wo auch ihre
  eigenen Einstellungen Namen im Klartext lassen, in jedem ihrer Projekte; sonst bleibt ihr eigener Name in Gebrauch
  und wird überall maskiert, wo Namen maskiert werden. In beiden Fällen wird eine Änderung über ihre Einstellungen
  abgelehnt; ein Rechner trägt ihn nur ein, wo keiner gesetzt ist.

Die Begriffe des Unternehmens eines Typs, den es nicht zulässt, bleiben maskiert, so wie das Unternehmen sie geschrieben
hat. Sie sind in der Maskierung selbst gesperrt: Ein Begriff wird maskiert, gleich welche nie zu maskierenden Werte,
Muster, Erkenner oder `allow`-Richtlinien eine Person festlegt, und niemand kann einen als
**Nicht sensibel** markieren. Hat eine Person einen längeren Text zugelassen, der einen Begriff enthält, bleibt der
Rest so, wie sie ihn zugelassen hat, und der Begriff wird maskiert. Ein Wert von `exceptions`, den Latet nicht lesen
kann, oder gar kein Wert (nur `exceptions:`), zählt als `company`, also als der strengere Wert, und
`latet policy check` sagt das.

Mit `personal` sind nur die Begriffe des Unternehmens auf diese Weise geschützt. Die eigenen Ausnahmen einer Person
können weiterhin im Klartext lassen, was ein Erkenner oder eine Feldregel des Unternehmens findet: einen längeren Text,
den sie zugelassen hat und der es enthält, oder einen eigenen Begriff, Erkenner oder eine eigene Feldregel eines Typs,
den sie zulässt. Um das auszuschließen, verwenden Sie `exceptions: company`.

Anfrageausnahmen (die [genauen
Ausnahmen](../../getting-started/upgrading-to-0-11/#eine-genaue-ausnahme-genehmigen), die einen Header, einen
Abfrageparameter oder ein Element des Bodys durchlassen) folgen demselben Schlüssel. Mit `personal` gehören die einer
Person ihr selbst: Sie genehmigt sie wie ohne Vorgaben, und über die eines API-Schlüssels wird wie über seine anderen
Änderungen entschieden. Mit `company` gehören sie dem Unternehmen: Wer eine anfordert, schlägt sie vor, und sie wartet auf
das Sicherheitsteam unter **Unternehmensfreigaben**, wer auch immer über ihre übrigen Änderungen entscheidet; ihre
eigene Genehmigung, oder die der Personen, die einen API-Schlüssel steuern, gewährt keine. Das gilt auch für die, die
genehmigt wurden, bevor die Vorgaben `company` sagten, dauerhafte eingeschlossen: Sobald der Server die Vorgaben
anwendet, gewähren sie nichts, das Dashboard zeigt jede als *Nicht in Kraft: Über Anfrageausnahmen entscheidet das
Sicherheitsteam Ihres Unternehmens, und diese hat es nicht genehmigt*, und `latet server status` führt sie als
`forbidden` unter den Ausnahmen auf, die jetzt nichts gewähren, sobald ihr Bereich die Vorgaben angewendet hat: Ein
Bereich, der seit der Änderung der Vorgaben auf keiner Instanz geöffnet war, zeigt dort seine Ausnahmen, wie sie waren,
bis er neu aufgebaut wird, während seine Anfragen schon nach den neuen Vorgaben beurteilt werden. Neu angefordert,
wartet eine auf das Sicherheitsteam; sie zählen wieder, sobald das Unternehmen nicht mehr über sie entscheidet: wenn die
Vorgaben zu `personal` zurückkehren oder entfernt werden, wenn die Lizenz entfernt wird oder wenn eine Lizenz
installiert wird, die keine gesperrten Regeln enthält. Ohne zentrale Freigaben in der Lizenz kann niemand eine
genehmigen: Eine angeforderte Ausnahme wartet auf eine Genehmigung, die sie nur ablehnen kann (wer sie angefordert hat,
kann sie zurückziehen, und die eigene Genehmigung wird abgelehnt, mit der Begründung, dass das Sicherheitsteam darüber
entscheidet), und gewährt nichts. Damit Personen unter `company` Anfrageausnahmen haben können, muss die Lizenz
`approvals` enthalten; ohne sie sperrt `company` trotzdem alle anderen Ausnahmen. Das Speichern der Einstellungen eines
API-Schlüssels über die Administration genehmigt die Ausnahmen, die es hinzufügt, weiterhin. Bei beiden Werten gewährt
eine wartende Ausnahme nichts und wird nie abgelehnt, weil sie unter die Vorgaben ginge: Was die Vorgaben beurteilen,
ist ihre Genehmigung, wenn sie kommt.

## Auf einem Latet-Server

Mit [Latet auf einem Server](../../getting-started/server/) legt ein Administrator die Vorgaben von jedem Rechner aus
fest, der das Administrationstoken des Servers hat:

```sh
latet policy check policy.yaml
latet server policy set policy.yaml
```

`latet server policy set` liest die Datei zuerst und ändert nichts, wenn sie nicht gelesen werden kann. Was er in der
Datei nicht lesen kann, sagt er, bevor etwas gesendet wird, und fragt nach: Eine Einstellung mit einem Fehler wird
ganz weggelassen, sodass ein einziger fehlerhafter Begriff alle Begriffe der Datei aus den Vorgaben herausfallen lässt. Der Befehl
fragt auch nach, bevor er Vorgaben festlegt, die weniger Einstellungen sperren als die des Servers, die jeder Person
wieder erlauben, eigene Ausnahmen hinzuzufügen, wo nur die des Unternehmens zählten, oder die Vorgaben ersetzen, die
er hier nicht lesen kann; an einem eigenen Terminal fragt er nach, andernfalls sagt `--yes`, dass Sie einverstanden
sind. Er setzt die Datei an die Stelle der Vorgaben, mit denen er sie verglichen hat: Ändert jemand die Vorgaben des
Servers in der Zwischenzeit, auf der Seite **Vorgaben** oder mit einem weiteren `latet server policy set`, lehnt der
Server das ab; der Befehl liest die Vorgaben dann neu ein, vergleicht sie noch einmal und fragt noch einmal nach. Ein
KI-Agent wird abgelehnt: Vorgaben festzulegen oder zu entfernen ist Sache einer Person. Das `PUT` der
Administrations-API lehnt eine Datei mit einer ganz weggelassenen Einstellung ab (400, mit dem Namen der Einstellung),
es sei denn, die Anfrage enthält auch `"accept_notices": true`. Ein `PUT`, das zusätzlich `"etag"` nennt, also die
Vorgaben, die es ersetzt, so wie das `GET` sie geliefert hat (`null`, wenn keine gespeichert sind), oder ein
`DELETE`, dessen Body ebenso die Vorgaben nennt, die es entfernt, wird mit 409 abgelehnt, wenn diese nicht mehr die
gespeicherten Vorgaben sind.

Jede Person und jeder API-Schlüssel auf dem Server wird auf die Vorgaben angehoben: Jede Instanz hebt jeden Bereich
an, sobald sie informiert ist, und schaut alle 10 Sekunden erneut nach, sodass eine Instanz, die die Nachricht
verpasst hat, die Vorgaben innerhalb von 10 Sekunden hat. Der Server bewahrt sie in seiner Datenbank auf, ihren Text
mit dem Hauptschlüssel versiegelt (die Begriffe darin sind Werte), und protokolliert jede Änderung im
Administrationsprotokoll, mit den Einstellungen, die sie sperrt, und nie mit einem Wert. `latet server policy show` sagt,
wann und von wem sie festgelegt wurden, was sie festlegen und ob sie gelten, und `latet server policy show --file
policy.yaml` schreibt ihren Text zurück, um ihn zu bearbeiten. `latet server policy remove` entfernt sie, nach einer
Rückfrage. Der Befehl entfernt dabei die Vorgaben, die er gelesen hat: Ändert jemand die Vorgaben des Servers in der
Zwischenzeit, lehnt der Server das Entfernen ab, und der Befehl liest die Vorgaben neu ein und fragt noch einmal
nach.

Mit `roles` in der Lizenz legen auch Personen mit der Rolle Sicherheit sie über das Dashboard fest
([Anmeldung, Gruppen und Rollen](../sign-in/)): Auf der Seite **Vorgaben** bearbeiten sie den Text der Vorgaben und
wählen dann **Speichern** oder **Entfernen**. Ein Text, den der Server nicht lesen kann, wird mit der Begründung
abgelehnt, und die geltenden Vorgaben bleiben. Ein Speichern nennt die Vorgaben, die es ersetzt: Hat jemand sie
geändert, seit ihr Text geöffnet wurde, wird es abgelehnt, und die Seite zeigt die Vorgaben, wie sie jetzt sind. Der
Text der Vorgaben enthält die Begriffe und Werte des Unternehmens; solange der Server die Unternehmensanmeldung
verlangt (`company_changes: sso`, der Standard, sobald Single Sign-on eingerichtet ist), erfordern das Lesen und das
Ändern des Textes eine mit dem Unternehmenskonto geöffnete Sitzung. Jede Änderung steht im Administrationsprotokoll, unter der
Person, die sie vorgenommen hat.

**Die Vorgaben gelten auf jeder Instanz, sobald sie aktualisiert ist.** Eine Instanz einer früheren Version oder ohne
die Enterprise Edition wendet keine Vorgaben an. Legen Sie die Vorgaben erst fest, wenn jede Instanz diese Version
ausführt, oder legen Sie sie dann erneut fest: `latet server policy set` und das `PUT` der Administrations-API warnen
und nennen, wie viele laufende Instanzen sie nicht anwenden. Siehe
[Aktualisieren und Instanzen hinzufügen](../../concepts/server-operations/#aktualisieren-und-instanzen-hinzufügen).

Solange die Vorgaben Begriffe sperren (einen Begriff darin von einem Typ, den sie nicht zulassen) oder
`exceptions: company` sagen, liest der Server jeden Text eines Aufrufs der [API für Anwendungen](../../reference/apps-api/) für
sich, wie Latet 0.10 es tat, wo er die Texte eines Aufrufs sonst zusammen liest: Die Maskierung bleibt dieselbe, und
ein Aufruf mit vielen Texten dauert länger. Synchronisierte Begriffe tun dasselbe, solange einer davon maskiert wird
([Begriffssynchronisierung](../term-sync/)).

Die gemeinsamen Einstellungen (`latet server config`, `latet server rules`) bleiben so, wie ein Administrator sie
festlegt: Was darin unter den Vorgaben liegt, wird in jedem Bereich angehoben, wie die eigenen Einstellungen einer
Person. Die Einstellungen eines API-Schlüssels unter die Vorgaben zu setzen wird abgelehnt: `latet server config --key
... set` endet mit Exit-Code 2.

## Was Personen sehen

- **Im Dashboard** zeigt eine Einstellung, die das Unternehmen festlegt, ein Schloss und **Von Ihrem Unternehmen
  festgelegt** in **Regeln** und in den Regeln eines Projekts. Eine Person erfährt, was gesperrt ist und wie viele
  Begriffe, Ausnahmen und Regeln es gibt, zum Beispiel dass 2 Begriffe des Unternehmens maskiert bleiben; nie die
  Begriffe und keinen anderen Wert des Unternehmens. Auf einem Server ist die Seite **Vorgaben** für die Rolle Sicherheit bestimmt, die den
  Text der Vorgaben dort liest und ändert, und für Auditoren, die lesen, was sie sperren; solange die Lizenz kein
  `roles` hat, lesen alle, was sie sperren. Administratoren lesen die Vorgaben mit `latet server policy show`.
- **Eine abgelehnte Änderung nennt die Einstellung:** `Set by your company, not changed: profile: from max to
  balanced`. Das Dashboard antwortet mit einem Konflikt (409) und zeigt die Ablehnung dort, wo die Änderung vorgenommen
  wurde, und im Terminal endet der Befehl mit Exit-Code 2. Eine einzelne Änderung, die die Einstellungen von mehr als
  20 Projektordnern senkt, wird als Ganzes abgelehnt: Ändern Sie weniger Ordner auf einmal.
- **Ihre eigenen Einstellungen bleiben, wie Sie sie festgelegt haben:** Was unter der Untergrenze liegt, wird
  angehoben, solange es verwendet wird, und Ihre Einstellungen behalten, was Sie geschrieben haben, sie gelten also
  wie geschrieben, wenn die Vorgaben entfernt werden.
- **`latet server status`** hat eine Zeile für die Vorgaben: wann sie festgelegt wurden (in UTC), und eine Warnung,
  wenn sie nicht gelten oder nicht gelesen werden können.

## Die Lizenz

Die Vorgaben gelten mit einer Latet-Enterprise-Lizenz, die gesperrte Regeln enthält
([Editionen und Lizenz](../)). Der Schutz sinkt nie: Wenn die Lizenz abläuft, ihre Benutzer überschreitet,
pausiert ist oder eine Testphase endet, bleiben die Vorgaben genau so in Kraft, wie sie sind. Die Vorgaben eines
Servers festzulegen oder zu entfernen erfordert eine Lizenz, die die Enterprise-Einstellungen noch verwalten kann:
Wenn eine Testphase endet, sind die Vorgaben eingefroren, und `latet server policy show` zeigt sie weiterhin an, bis
eine Lizenz installiert wird.

Die Vorgaben gelten nur in zwei Fällen nicht mehr: wenn die Lizenz entfernt wird und wenn eine Lizenz installiert wird,
die gesperrte Regeln nicht enthält. `latet server license remove` und `latet server license install` sagen das, fragen
vorher nach und weisen einen KI-Agenten ab; die Vorgaben des Servers bleiben gespeichert und gelten wieder mit
einer Lizenz, die gesperrte Regeln enthält.

## Die Grenzen

- **Nur auf einem Latet-Server:** Die Enterprise Edition gehört zu einem Server. Latet auf einem Rechner ohne Server
  ist die Community Edition, was auch immer darauf installiert ist, und hat keine Vorgaben.
- **Ein mit einem Latet-Server verbundener Rechner** wird vom Server maskiert, nach den Vorgaben des Servers.
- **Eine Instanz, die die Vorgaben nicht lesen kann:** Gespeicherte, aber unlesbare Vorgaben (von einer neueren
  Version erstellt oder mit einem anderen Hauptschlüssel versiegelt) lassen eine Instanz, die schon Vorgaben gelesen
  hat, die zuletzt gelesenen anwenden: Ihre Bereiche bleiben auf dieser älteren Untergrenze, die niedriger als die neue
  sein kann, und `latet server status` sagt, dass die neuen Vorgaben nicht gelesen werden können. Eine Instanz, die nie
  Vorgaben gelesen hat oder die Lizenz noch nicht gelesen hat, bedient keine Person und keinen API-Schlüssel, bis sie
  es kann: Die Anfrage wird mit einem 503 abgelehnt, der zu einem erneuten Versuch auffordert. `latet server policy remove`
  behebt Vorgaben, die nicht gelesen werden können; sie mit einer Version erneut festzulegen, die jede Instanz
  ausführt, behebt sie ebenfalls.
- **Der Datenbank wird vertraut:** Wer in das PostgreSQL des Servers schreiben kann, kann die Zeile `ee_policy`
  löschen oder eine ältere zurückspielen. Die Untergrenze sinkt dann oder kehrt zu den älteren Vorgaben zurück, auf jeder
  Instanz, und kein Eintrag des Administrationsprotokolls verzeichnet es. Die Einträge `policy.changed` des Protokolls, jeder
  mit dem Etag der festgelegten Vorgaben, sind der Nachweis, mit dem die geltenden Vorgaben zu vergleichen sind
  (`latet server policy show`); überlassen Sie den Schreibzugriff auf diese Datenbank allein dem Server.
- **Der Text in Bildern:** Vorgaben, die `terms` sperren, aber nicht `ocr` und `images`, lassen eine Person das Lesen
  des Textes in Bildern abschalten, und ein Begriff des Unternehmens in einem Bild wird dann nicht gelesen. Sperren Sie
  auch `ocr: auto` und `images`, damit die Begriffe Bilder abdecken.
- **Ein Agent, der direkt auf einen KI-Anbieter zeigt,** wird von niemandem maskiert. Ein Server ist nur
  dann die Kontrollstelle des Unternehmens, wenn sein Netzwerk die Anbieter ausschließlich vom Server aus erreichbar
  macht
  ([Latet auf einem Server](../../concepts/server/)).
