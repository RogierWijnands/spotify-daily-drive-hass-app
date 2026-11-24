# Daily Drive voor Home Assistant

Een lokale Home Assistant-app die één eigen Spotify-playlist vult met muziek en podcasts. De app draait in een container op Home Assistant OS; er is geen externe server nodig.

## Installatie

1. Kopieer de projectmap naar de lokale apps-map van Home Assistant, als `local_apps/daily_drive`. Afhankelijk van de gebruikte Samba- of SSH-app kan deze map ook `addons` heten. Zie [de Home Assistant-handleiding](https://contributors.home-assistant.io/apps/first-app/).
2. Ga naar **Instellingen → Apps → App Store**, kies **Controleren op updates**, installeer **Daily Drive** onder lokale apps en start de app. Open de webinterface via Home Assistant.
3. Maak op [Spotify for Developers](https://developer.spotify.com/dashboard) een app met Web API-toegang. Voor Development Mode heeft de eigenaar Spotify Premium nodig. Voeg exact `http://127.0.0.1:8888/callback` toe als redirect-URI.
4. Maak zelf een Spotify-playlist met de naam Daily Drive. Plak in de app de Spotify-client-ID en de URL van deze playlist. Voeg eventueel URL's van eigen of samen beheerde bronplaylists toe, één per regel, en sla op.
5. Kies **Koppeling starten**, open de Spotify-link en geef toestemming. Na de redirect verschijnt mogelijk een foutpagina: kopieer de volledige URL uit de adresbalk en plak deze in de app. De URL bevat een eenmalige autorisatiecode: deel hem niet. Er hoeft niets op poort 8888 te draaien.
6. Kies **Nu verversen** en controleer de geschiedenis en het Home Assistant-app-log. Automatisch verversen gebeurt om 09:00, 14:00 en 18:00 in `Europe/Amsterdam`, zolang de app draait. Een gemist tijdstip wordt niet ingehaald.

Na een update vanaf versie 0.1.0 moet je Spotify opnieuw koppelen en toestemming geven om je bibliotheek te lezen. Zonder die toestemming mislukt de refresh; de bestaande playlist blijft intact.

De app vraagt alleen de Spotify-client-ID, geen client secret. Spotify-tokens, instellingen, refreshgeschiedenis en gebruikte afleveringen worden in `/data/daily_drive.sqlite3` opgeslagen. Deze opslag is privé voor de app en blijft bij een herstart behouden. Verwijdering van de app of haar gegevens kan deze historie en koppeling verwijderen.

## Selectie en beperkingen

- De drie nieuwste afspeelbare afleveringen van de opgegeven NU.nl-show, en de nieuwste afleveringen van Alledaagse Vragen en Lang Verhaal, krijgen voorrang. Dezelfde aflevering verschijnt niet tweemaal in één playlist. Eerder gebruikte afleveringen worden bijgehouden, maar blijven voorkomen zolang zij de nieuwste zijn.
- Muziek komt uit huidige en eerdere Spotify-toptracks, recent afgespeelde en gelikete nummers, releases en samenwerkingen van favoriete artiesten, en optionele bronplaylists. Gelikete nummers worden willekeurig uit je bibliotheek geselecteerd; niet elke refresh bevat noodzakelijk een geliket nummer. Het langste Spotify-toptrackoverzicht beslaat ongeveer één jaar en is geen volledige luistergeschiedenis. Ontdekkingen zijn geen Spotify-aanbevelingen.
- Voor nieuwe Spotify-apps zijn alleen playlists die je zelf bezit of samen beheert uitleesbaar. Discover Weekly en Release Radar kunnen niet rechtstreeks worden uitgelezen; eigen, handmatig bijgehouden kopieën kunnen als bron worden toegevoegd. Een ontoegankelijke bron zorgt voor een mislukte refresh.
- De app streeft naar 120 minuten ±2 minuten. De vijf nieuwste afleveringen hebben voorrang; als zij zelf te lang zijn, mag de totale duur afwijken. De podcastduur bepaalt de verhouding: 70/30 is een streven, geen garantie. Is er onvoldoende afspeelbare muziek om het doel te bereiken, dan wordt niet vervangen.
- De volledige selectie wordt vooraf gemaakt en vervolgens in één Spotify-aanvraag van maximaal 100 items vervangen. Bij een netwerkfout wordt gecontroleerd of de oude of nieuwe inhoud op Spotify staat. Als dit niet vast te stellen is, blijft de uitkomst onzeker en worden verdere wijzigingen geblokkeerd totdat de inhoud automatisch kan worden bevestigd. Mocht dat niet lukken, controleer dan de playlist in Spotify en hef de blokkade expliciet op in de app; de geschiedenis blijft de eerdere uitkomst als onzeker tonen. Een garantie dat een time-out de bestaande playlist ongemoeid liet is niet mogelijk. Wijzig de playlist niet gelijktijdig handmatig. Playlists met lokale of onleesbare items worden niet vervangen.
- Spotify kan API-toegang beperken of rate limits opleggen. Controleer bij fouten het app-log; de bestaande playlist wordt niet gewijzigd als de voorbereiding mislukt.

## Lokaal testen met Spotify

Dit werkt zonder Home Assistant of Docker, met Python 3.9 of nieuwer:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp local-config.example.json .daily-drive.json
```

Maak een Spotify Developer-app en registreer `http://127.0.0.1:8888/callback` als redirect-URI, zoals bij de Home Assistant-installatie hierboven. Vul vervolgens in `.daily-drive.json` de Spotify-client-ID, de URL of ID van je Daily Drive-playlist en eventueel de URL's of ID's van je eigen bronplaylists in. Gebruik bij voorkeur een aparte testplaylist.

```sh
python -m app.cli --authorize  # eenmalig koppelen: open de link en plak de volledige terugkeer-URL
python -m app.cli              # toont de selectie; verandert je playlist niet
python -m app.cli --apply      # vervangt daadwerkelijk de ingestelde playlist
```

Met `--config /pad/naar/config.json` gebruik je een ander configuratiebestand. De app bewaart tokens en geschiedenis in `.daily-drive.sqlite3` naast dat bestand; bescherm deze database en deel hem niet. `.gitignore` sluit JSON-bestanden en SQLite-databases uit, behalve het voorbeeldbestand `local-config.example.json`: zet daarin geen persoonlijke gegevens. Controleer zelf dat configuraties met andere extensies, alternatieve locaties en andere gevoelige bestanden niet in versiebeheer terechtkomen. De lokale database staat los van die van Home Assistant; gebruik niet tegelijkertijd dezelfde database vanuit meerdere processen.

Elke run maakt een nieuwe willekeurige selectie. `--apply` gebruikt dezelfde ververslogica en herstelcontrole als de Home Assistant-app, maar past dus niet noodzakelijk de eerder getoonde selectie toe. Een proefrun leest Spotify-gegevens en vernieuwt zo nodig je toegangstoken, maar wijzigt de playlist niet. Bij een onzekere uitkomst van `--apply` controleert een volgende `--apply` eerst de playlist. Komt die niet overeen met de oude of nieuwe inhoud, bekijk dan beide met `python -m app.cli --pending`. Controleer de playlist in Spotify en hef zo nodig de blokkade handmatig op met `python -m app.cli --release-pending`; de eerdere uitkomst blijft dan als onzeker in de historie staan.

## Geautomatiseerde tests

Met Python 3.9 of nieuwer en de afhankelijkheden uit `daily_drive/requirements.txt`:

```sh
python -m unittest discover -s tests
```
