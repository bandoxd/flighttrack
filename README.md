# Indonesia Flight Deal Monitor

Automatischer Flugpreis-Monitor für **FRA → Surabaya / Denpasar → FRA** (März–Mai 2027, ca. 30 Tage, 1 Erw., Economy, Aufgabegepäck).
Er läuft zweimal täglich kostenlos auf GitHub Actions, speichert alle Preise, vergleicht sie mit früheren Ergebnissen und meldet sich **nur bei wirklich guten Angeboten** per Discord (optional Telegram/E-Mail).

**Laufende Kosten: 0 €.** Keine KI-Aufrufe, kein Server, keine Buchungen.

---

## So funktioniert es

| Baustein | Lösung | Warum |
|---|---|---|
| Preisdaten | **SerpApi Google Flights API** (Free Plan: 250 Suchen/Monat) | Offizielle, legale API mit Google-Flights-Daten aller Airlines, auch Gabelflüge. Amadeus Self-Service wurde im Juli 2026 abgeschaltet; Kiwi/Duffel kosten Geld oder setzen ein Unternehmen voraus. |
| Zeitsteuerung | **GitHub Actions** (cron 2× täglich) | kostenlos, läuft ohne geöffneten Browser/PC |
| Speicher | JSON-Dateien in `data/` (werden automatisch committet) | kein Datenbankserver nötig, Verlauf nachvollziehbar |
| Dashboard | statische Seite `docs/index.html` über **GitHub Pages**: Deals und Verbindungen als aufklappbare Karten, Preisverlauf, Fehlerprotokoll | kostenlos, mobil abrufbar |
| Benachrichtigung | **Discord** (Webhook oder eigener Bot), optional Telegram/E-Mail | kostenlos und zuverlässig |

### Ablauf pro Lauf
1. Freies Suchbudget beim SerpApi-Konto abfragen (kostet keine Suche) und auf die verbleibenden Läufe des Monats verteilen (≈ 3–4 Suchen pro Lauf, 30 Suchen als Reserve für Detailprüfungen).
2. Nächste Termine aus dem Rotationsplan abfragen: 19 Abflugtage (1.3.–15.5.2027, alle 4 Tage) × Reisedauer 27/30/33 Tage × 4 Varianten. Gewichtung: V1 Gabelflug SUB/DPS ×3, V2 DPS hin/zurück ×2, V3 CGK/DPS ×2, V4 DPS/SUB umgekehrt ×1. Jeder V1-Termin wird etwa alle 7–8 Tage geprüft, V2/V3 alle ~10 Tage, V4 alle ~3 Wochen; danach beginnt der Plan von vorn. Der bisher günstigste Termin wird zusätzlich täglich nachgeprüft.
3. Nur Verbindungen mit **max. 1 Umstieg pro Richtung** (Google-Flights-Filter) und durchgehendem Ticket. Self-Transfer/getrennte Tickets werden erkannt und **nie gemeldet**.
4. **Gesamtpreis** = Ticketpreis + Gepäck. Gepäck gilt als inklusive, wenn die Buchungsoption es bestätigt. Bei Airlines, die es üblicherweise enthalten (Qatar, Emirates, Singapore, Turkish, Garuda …), wird es angenommen und so gekennzeichnet. Sonst werden pauschal 120 € (hin und zurück) aufgeschlagen.
5. Bewertung (siehe unten). Für Kandidaten werden Rückflug und Buchungsoptionen nachgeladen (2 Suchen), um Flugzeiten, Gepäck und Anbieter zu verifizieren.
6. Meldung nur wenn neu oder deutlich besser (siehe *Doppelte Meldungen*), alle Deals eines Laufs in **einer** Nachricht.
7. Dashboard neu erzeugen, Daten committen.

### Bewertung
| Effektiver Gesamtpreis | Stufe | Meldung |
|---|---|---|
| < 750 € | 🔥 EXTREM GUT | sofort |
| 750–850 € | ✅ GUT | wenn Verbindung gut: ≤ 1 Umstieg, ≤ 26 h pro Richtung, Umstieg ≤ 8 h, kein Umstieg über Nacht |
| 850–950 € | NORMAL | nein |
| > 950 € | ZU TEUER | nein |
| ≥ 15 % unter beobachtetem Niveau (Median der letzten 21 Tage, ab 6 Messwerten) | 📉 | ja, solange < 950 € |

„Effektiv“ = Gesamtpreis + Varianten-Aufschlag für weniger bevorzugte Routen (V2 +40 €, V3 +40 €, V4 +30 €), z. B. für die Anreise Bali→Java. In der Meldung steht immer der echte Gesamtpreis. Die Aufschläge lassen sich in `config.toml` auf 0 setzen.

### Doppelte Meldungen
- Derselbe Deal (gleiche Daten + Flüge) wird erst wieder gemeldet, wenn er **≥ 25 € günstiger** ist oder in eine bessere Stufe wechselt.
- Andere Termine derselben Variante werden nur gemeldet, wenn sie ≥ 25 € unter der besten Meldung der letzten 7 Tage liegen oder in eine bessere Stufe fallen. So gibt es keine Flut, wenn das ganze Preisniveau sinkt.
- Nach 3 fehlgeschlagenen Läufen in Folge kommt **eine** Systemwarnung.

### Inhalt jeder Meldung
Stufe und Gesamtpreis (Ticket + Gepäck), Route und Daten, Airline(s), Hin- und Rückflug mit Abflug- und Ankunftszeiten, Dauer, Umstiege mit Wartezeit und Flugnummern, Gepäckstatus (verifiziert oder angenommen), Grund der Meldung, Warnungen, buchender Anbieter und ein **Link zu Google Flights** mit genau dieser Suche (von dort direkt zur Airline/zum Anbieter). Ist das Budget knapp, steht ausdrücklich in der Meldung, welche Details nicht geprüft wurden.

---

## Einrichtung (ca. 15 Minuten, einmalig)

### 1. SerpApi-Schlüssel (kostenlos)
1. Auf <https://serpapi.com/users/sign_up> registrieren und E-Mail sowie Telefon bestätigen. Free Plan, keine Kreditkarte.
   SerpApi ist ein etablierter Anbieter aus Austin, USA. Die Telefonnummer dient dem Schutz des Gratis-Kontingents vor Mehrfachkonten. Wegwerf- oder Online-SMS-Nummern sind nicht empfehlenswert: Sie werden oft abgelehnt, verstoßen gegen die Nutzungsbedingungen, und eine gesperrte Nummer kann das Konto und damit den Monitor stilllegen. Eine Zweit- oder Prepaid-SIM ist ein sauberer Mittelweg.
2. Unter *Dashboard → Your Private API Key* den Schlüssel kopieren.

### 2. Discord (empfohlen)
Es gibt zwei Wege. Für beide muss nichts dauerhaft laufen: Der Monitor schickt die Nachricht direkt über die Discord-Schnittstelle, auch wenn dein Bot offline ist.

**Variante A – Webhook (am einfachsten, 2 Minuten)**
1. Auf deinem Server: Kanal (z. B. `#flug-deals`) → ⚙️ *Kanal bearbeiten* → *Integrationen* → *Webhooks* → *Neuer Webhook* → *Webhook-URL kopieren*.
2. Die URL als Secret `DISCORD_WEBHOOK_URL` speichern.
3. Optional, damit dein Handy klingelt: *Benutzereinstellungen → Erweitert → Entwicklermodus* an, dann Rechtsklick auf deinen Namen → *Benutzer-ID kopieren* → Secret `DISCORD_MENTION_USER_ID`. Jede Deal-Meldung erwähnt dich dann mit @. Alternativ stellst du die Benachrichtigungen für den Kanal auf „Alle Nachrichten“.

**Variante B – dein vorhandener Bot**
1. <https://discord.com/developers/applications> → deine App → *Bot* → *Reset Token* → Token kopieren → Secret `DISCORD_BOT_TOKEN`.
   (Achtung: Ein Reset macht den alten Token ungültig. Läuft der Bot schon woanders, dort den neuen Token eintragen.)
2. Entweder in einen Kanal posten: Rechtsklick auf den Kanal → *Kanal-ID kopieren* → Secret `DISCORD_CHANNEL_ID`. Der Bot braucht dort „Nachrichten senden“ und „Links einbetten“.
   Oder als Direktnachricht: deine Benutzer-ID als Secret `DISCORD_USER_ID`. Der Bot muss dafür mit dir auf einem Server sein.
3. Optional `DISCORD_MENTION_USER_ID` wie oben.

Jede Deal-Meldung kommt als übersichtliche Karte: Preis, Route und Daten, Hin- und Rückflug mit allen Einzelflügen, Zeiten, Dauer, Umstiegen, Gepäck, Hinweisen und einem anklickbaren Google-Flights-Link.

Webhook-URL und Bot-Token sind Passwörter: Wer sie kennt, kann in deinem Kanal posten. Sie gehören nur in die GitHub-Secrets, nie in Dateien im Repository.

### 2a. Telegram (optional)
In Telegram **@BotFather** → `/newbot` → Token als `TELEGRAM_BOT_TOKEN`. Dem Bot einmal schreiben und dann `https://api.telegram.org/bot<TOKEN>/getUpdates` öffnen. Die Zahl bei `"chat":{"id":…}` ist `TELEGRAM_CHAT_ID`.

### 2b. E-Mail (optional, zusätzlich oder statt Telegram)
Für Gmail: Zwei-Faktor-Anmeldung aktivieren → <https://myaccount.google.com/apppasswords> → App-Passwort erzeugen.
Secrets: `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=465`, `SMTP_USER=deine@gmail.com`, `SMTP_PASS=<App-Passwort>`, `MAIL_TO=deine@gmail.com`.

### 3. GitHub-Repository anlegen
1. Auf <https://github.com/new> ein Repository anlegen, z. B. `indonesia-flight-monitor`.
   - **Öffentlich**: GitHub Pages (Dashboard) ist kostenlos. Das Repo enthält nur Flugpreise und deinen Reisezeitraum, keine Namen und keine Schlüssel (die liegen verschlüsselt in den Secrets).
   - **Privat**: Actions laufen ebenfalls kostenlos (ein Lauf dauert < 1 Minute). Das Dashboard über Pages braucht dann aber GitHub Pro. Ohne Pro öffnest du `docs/index.html` lokal oder nutzt nur die Benachrichtigungen.
2. Alle Dateien aus diesem Ordner hochladen (*Add file → Upload files*, den entpackten Inhalt inkl. `.github`-Ordner hineinziehen) oder per `git push`.
   - Auf dem Mac sind Ordner mit Punkt (`.github`) im Finder versteckt (Cmd+Shift+. zeigt sie). Fehlt nach dem Upload der Ordner `.github/workflows`, lege die beiden Workflow-Dateien über *Add file → Create new file* an (Dateiname z. B. `.github/workflows/monitor.yml`) und kopiere den Inhalt hinein.

### 4. Secrets eintragen
*Settings → Secrets and variables → Actions → New repository secret*:

| Name | Wert |
|---|---|
| `SERPAPI_KEY` | SerpApi-Schlüssel (Pflicht) |
| `DISCORD_WEBHOOK_URL` | Variante A: Webhook-URL |
| `DISCORD_BOT_TOKEN` + `DISCORD_CHANNEL_ID` oder `DISCORD_USER_ID` | Variante B: eigener Bot |
| `DISCORD_MENTION_USER_ID` | optional: deine Benutzer-ID für @-Erwähnung |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | optional |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `MAIL_TO` | optional, für E-Mail |

### 5. Testen
1. *Actions* → Workflows aktivieren, falls GitHub danach fragt.
2. *Test-Benachrichtigung → Run workflow*. Du bekommst eine Testnachricht in Discord. Fehler (z. B. „Discord HTTP 401“ = falscher Token, „404“ = falsche ID oder gelöschter Webhook) stehen im Log.
3. *Flight Deal Monitor → Run workflow* startet den ersten echten Suchlauf (≈ 1 Minute). Danach liegen Daten in `data/` und das Dashboard in `docs/`.

### 6. Dashboard aktivieren
*Settings → Pages → Build and deployment → Source: Deploy from a branch → Branch `main`, Ordner `/docs` → Save.*
Nach 1–2 Minuten erreichbar unter `https://<dein-github-name>.github.io/<repo-name>/`. Diese Adresse in `config.toml` bei `dashboard_url` eintragen, dann enthält jede Meldung einen Dashboard-Link.

Ab jetzt läuft alles automatisch um ca. 06:17 und 18:17 Uhr (Winterzeit). GitHub startet geplante Läufe manchmal einige Minuten verspätet.

---

## Anpassen (`config.toml`)
- Reisezeitraum: `depart_from`, `depart_to`, `durations`, `depart_step_days`
- Schwellen: Abschnitt `[thresholds]`
- Varianten: `[[variants]]` hinzufügen/entfernen, `weight` = Häufigkeit, `penalty_eur` = Aufschlag
- Gepäck: `usually_included`, `surcharge_unknown_eur`
- Budget: `monthly_searches` (bei bezahltem SerpApi-Plan erhöhen), `max_searches_per_run`
- `weekly_status = true` schickt zusätzlich eine kurze Wochenübersicht.

## Grenzen und Transparenz
- Preise sind **Momentaufnahmen** von Google Flights. Sie können sich bis zur Buchung ändern. Es wird nie automatisch gebucht.
- Ohne Detailprüfung kennt Google Flights bei Gabel-/Rückflügen zunächst nur den **Gesamtpreis** und die Hinflug-Details. Rückflug, Gepäck und Anbieter werden nur für Kandidaten nachgeladen (Budget). Was nicht geprüft wurde, steht ausdrücklich in Meldung und Dashboard.
- Mit 250 Suchen/Monat wird jeder Termin der Hauptvariante etwa wöchentlich geprüft (andere Varianten seltener), der aktuell günstigste täglich. Für dichtere Abdeckung: SerpApi Starter (25 $/Monat, 1.000 Suchen). Das ist eine kostenpflichtige Entscheidung, die nur du triffst.
- Zahlungsgebühren einzelner Buchungsportale sind nicht enthalten. Google Flights zeigt Endpreise inkl. Steuern und Gebühren der Airline.
- Fehler (Quelle nicht erreichbar, Budget erschöpft, fehlender Schlüssel, keine Treffer) erscheinen im Dashboard unter *Letzte Suchläufe*. Bei Dauerfehlern kommt eine Warnung.
- GitHub pausiert geplante Workflows in Repos ohne Aktivität für 60 Tage. Die automatischen Daten-Commits verhindern das. Falls doch: *Actions → Enable workflow*.

## Lokal ausführen (optional)
```bash
export SERPAPI_KEY=...  TELEGRAM_BOT_TOKEN=...  TELEGRAM_CHAT_ID=...
python3 monitor.py run          # Suchlauf
python3 monitor.py dashboard    # nur Dashboard
python3 -m unittest discover tests   # Tests (ohne Netzwerk)
```
Benötigt nur Python ≥ 3.11, keine Zusatzpakete.
