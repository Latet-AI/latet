---
title: Roadmap
description: Was für Latet als Nächstes kommt, das Früheste zuerst.
---

Was als Nächstes kommt, das Früheste zuerst. Ideen und Hilfe sind willkommen: Eröffnen Sie ein
[Issue](https://github.com/Latet-AI/latet/issues), oder lesen Sie die Seite [Mitwirken](../contributing/).
Was sich bereits geändert hat, steht im [Changelog](../changelog/).

## Der Auto-Modus von Claude Code, eine klare Ablehnung

Wenn eine Prüfung im Auto-Modus eine Angabe enthielte, die Sie lokal behalten (Ihren Benutzernamen,
Ihre Pfade), stoppt Latet sie heute, und Claude Code meldet seinen Prüfer als nicht verfügbar und
lehnt die Aktion ab. Latet könnte stattdessen mit dem eigenen Urteil des Prüfers antworten,
*blocked*, sodass die Ablehnung als das erscheint, was sie ist. Diese Antwort käme von Latet anstelle
des Prüfers von Anthropic, deshalb fragen wir zuerst: Ein Issue erklärt den Fall, mit dem fertigen
Pull-Request, wir senden es an Anthropic, und der Pull-Request wird nur mit der Zustimmung von
Anthropic gemergt, die dem Issue beigefügt wird.

## Ein signierter Installer für Windows

Virenscanner prüfen die Programme, die sie nicht kennen, bei jedem Start, und die Launcher einer
Python-Installation sind nicht signiert, daher kann unter Windows jeder Befehl Sekunden warten,
bevor er startet (siehe [Fehlerbehebung](../troubleshooting/#befehle-sind-unter-windows-langsam)). Ein
signierter Installer, auch über winget, lässt sie sofort starten.

## Ihr eigenes Namensmodell

Heute verwendet Latet ein einziges Namensmodell, [Privacy Filter](https://huggingface.co/openai/privacy-filter)
von OpenAI, das es auf Anfrage herunterlädt (`latet ner install`) und das viele Sprachen liest. Ein
eigenes Modell würde die Namen und Begriffe Ihres Fachgebiets finden, etwa Medikamente und Diagnosen in einer Klinik oder Parteien und Gerichte in einer Kanzlei:
ein ONNX-Token-Klassifikator auf Ihrem Rechner oder ein Endpunkt, den Sie betreiben, etwa ein
GLiNER- oder Presidio-Dienst. Mehrere Modelle könnten zusammenarbeiten, und jedes fügt hinzu, was es
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

## Latet Enterprise

Latet Enterprise ist für ein Unternehmen, dessen Sicherheitsteam die Regeln für alle festlegt. Zusätzlich
zur Community Edition bringt es Regeln für alle, die niemand lockern kann, Rollen in der
Administration, etwa einen Prüfer mit reinem Lesezugriff, zentrale Freigaben von Änderungen, die den
Schutz verringern, Single Sign-on, Auditberichte und Ereignisse für ein SIEM. Auf der
[Warteliste](https://latet.ai/#waitlist) erfahren Sie, wann es erscheint.

## Weitere Sprachen

Das Dashboard, das Tutorial und die Dokumentation gibt es auf Englisch, Italienisch, Deutsch,
Französisch und Spanisch. Für weitere Sprachen sind Beiträge willkommen: siehe
[Mitwirken](../contributing/#so-fügen-sie-eine-sprache-hinzu).
