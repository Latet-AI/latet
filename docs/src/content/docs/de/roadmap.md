---
title: Roadmap
description: Was für Latet als Nächstes kommt, das Früheste zuerst.
---

Was als Nächstes kommt, das Früheste zuerst. Ideen und Hilfe sind willkommen: Eröffnen Sie ein
[Issue](https://github.com/Latet-AI/latet/issues), oder lesen Sie die Seite [Mitwirken](../contributing/).
Was sich bereits geändert hat, steht im [Changelog](../changelog/).

## Der Auto-Modus von Claude Code, eine klare Ablehnung

Wenn eine Prüfung im Auto-Modus eine Angabe enthielte, die Sie lokal behalten (Ihren Benutzernamen,
Ihre Pfade), hält Latet sie derzeit auf, und Claude Code meldet seinen Prüfer als nicht verfügbar und
lehnt die Aktion ab. Latet könnte stattdessen mit dem eigenen Urteil des Prüfers antworten,
*blocked*, sodass die Ablehnung als das erscheint, was sie ist. Diese Antwort käme von Latet anstelle
des Prüfers von Anthropic, deshalb fragen wir zuerst: Ein Issue erklärt den Fall, mit dem fertigen
Pull-Request, wir senden es an Anthropic, und der Pull-Request wird nur mit der Zustimmung von
Anthropic gemergt, die dem Issue beigefügt wird.

## MCP-Tools maskiert, bevor Claude Code sie liest

Das Parameterschema eines MCP-Tools kann einen Wert enthalten, den Latet maskiert, etwa eine
Beispieladresse, und Claude Code sendet es mit seinen Anfragen, wo Latets Plugin es nicht erreicht:
Heute stoppt Latet diese Anfragen (siehe [Fehlerbehebung](../troubleshooting/)). Latet würde sich
stattdessen zwischen Claude Code und die MCP-Server stellen, die Sie einrichten: Es würde die
Beschreibungen und Schemas der Tools in der Liste maskieren, die Claude Code liest, und die echten
Werte in die Aufrufe des Modells zurücksetzen, sodass die Anfragen von Claude Code so bleiben, wie
Claude Code sie gebaut hat. Das gälte für die Server, die Sie selbst hinzufügen, nicht für die
Konnektoren, die von claude.ai kommen.

## Ein signierter Installer für Windows

Virenscanner prüfen die Programme, die sie nicht kennen, bei jedem Start, und die Launcher einer
Python-Installation sind nicht signiert, daher kann unter Windows jeder Befehl Sekunden warten,
bevor er startet (siehe [Fehlerbehebung](../troubleshooting/#befehle-sind-unter-windows-langsam)). Ein
signierter Installer, auch über winget, lässt sie sofort starten.

## Ihr eigenes Namensmodell

Derzeit verwendet Latet ein einziges Namensmodell, [Privacy Filter](https://huggingface.co/openai/privacy-filter)
von OpenAI, das es auf Anfrage herunterlädt (`latet ner install`) und das viele Sprachen liest. Ein
eigenes Modell würde die Namen und Begriffe Ihres Fachgebiets finden, etwa Medikamente und Diagnosen in einer Klinik oder Parteien und Gerichte in einer Kanzlei:
ein ONNX-Token-Klassifikator auf Ihrem Rechner oder ein Endpunkt, den Sie betreiben, etwa ein
GLiNER- oder Presidio-Dienst. Mehrere Modelle könnten zusammenarbeiten, wobei jedes ergänzt, was es
findet.

## Mehr von Googles Gemini API

Den Rest dessen lesen, was die Clients von Gemini senden: die Interactions API, die Live API, die Batch
API, das Hochladen von Dateien, die Bilderzeugung und die Anfragen `:predict` von Vertex (seine
eigenen Embedding- und Bildmodelle). Bis dahin lehnt Latet sie auf seinen Gemini-Routen ab, statt sie
unmaskiert zu senden (siehe [Gemini CLI und Google GenAI](../agents/gemini/)).

## Weitere Agenten

OpenCode, wenn es mit ChatGPT angemeldet ist oder seine Modelle auf Amazon Bedrock oder Azure
nutzt. Die Mantle-API von Amazon Bedrock für Claude Code sowie Bedrock über einen VPC-Endpunkt oder
seine FIPS-Adresse mit Ihrer AWS-Anmeldung (mit einem API-Schlüssel für Bedrock funktionieren beide
schon). Antigravity CLI, mit Latet geprüft.

## pi, oh-my-pi und ThinkRail

Ein Plugin für [pi](https://github.com/earendil-works/pi) und
[oh-my-pi](https://github.com/can1357/oh-my-pi) würde sensible Werte im Agenten selbst maskieren,
bevor jede Anfrage erstellt wird, wie es das Plugin für Claude Code tut. Die Maskierung hinge nicht
vom Anbieter ab: Sie würde neben API-Schlüsseln auch JetBrains AI und Abonnements abdecken, und jede
Anfrage liefe zusätzlich über das Gateway von Latet und erschiene im Dashboard. JetBrains ThinkRail
nutzt pi, also wären auch seine Unterhaltungen geschützt; seine Subagenten und eine Schaltfläche für
das Dashboard warten darauf, dass ThinkRail Erweiterungen von Dritten lädt.

## Dateien mit sensiblen Daten

Dateien, die ein Agent an das Modell sendet, etwa PDFs, Word-Dokumente und Excel-Tabellen, würden
wie Text maskiert, ein Format nach dem anderen. In jeder Datei eines Formats, das es liest,
würde Latet die sensiblen Werte gegen Ersatzwerte austauschen und die maskierte Datei senden; in der
Antwort würde es die Originale wieder einsetzen, auch in Dateien, die das Modell zurückschickt. Eine
Datei in einem Format, das Latet nicht liest, wird abgelehnt, ob sie in einer Unterhaltung steht oder einzeln
über die Datei-API eines Anbieters hochgeladen wird, auf einem Rechner wie auf einem Server (siehe
[Was geschützt ist und was nicht](../concepts/protection-scope/)).

## Latet Enterprise

Latet Enterprise richtet sich an Unternehmen, deren Sicherheitsteam die Regeln für alle festlegt.
Es bringt Regeln, die niemand lockern kann, Single Sign-on mit Gruppen und Rollen wie der eines
Auditors mit reinem Lesezugriff, zentrale Freigaben von Änderungen, die den Schutz verringern,
Audit-Berichte, Ereignisse für ein SIEM und synchronisierte Begriffe (siehe
[Editionen und Lizenz](../enterprise/)). Zwei Erweiterungen sind geplant:

- **Gruppen aus Microsoft AD FS.** Direkt angebunden gibt AD FS Latet den Benutzernamen einer
  Person, aber nicht ihre Gruppen: Es fügt sie nur einer Antwort hinzu, die per Form-Post
  zurückkommt. Latet würde diese Antwort anfordern, damit die Gruppen aus AD FS Personen in die
  Latet-Gruppen aufnehmen und darüber entscheiden, wer ein Konto erhält, wie es die Gruppen anderer
  Anbieter tun, ohne Keycloak dazwischen.
- **Trennen in der Liste Ihre Rechner.** Eine Person würde einen ihrer Rechner, etwa einen, den sie
  nicht mehr hat, im Dashboard in der Liste **Ihre Rechner** selbst trennen, ohne einen
  Administrator darum zu bitten.

## Weitere Sprachen

Das Dashboard, das Tutorial und die Dokumentation gibt es auf Englisch, Italienisch, Deutsch,
Französisch und Spanisch. Für weitere Sprachen sind Beiträge willkommen: siehe
[Mitwirken](../contributing/#so-fügen-sie-eine-sprache-hinzu).
