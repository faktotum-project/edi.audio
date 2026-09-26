# DDJ-FLX6 + Mixxx + edi.audio — linea guida

Configurazione condivisa, così sappiamo entrambi cosa c'è e perché.

## Stato

| | |
|---|---|
| Controller | Pioneer DDJ-FLX6, USB `2b73:0038` |
| MIDI | `hw:2,0` — «DDJ-FLX6 MIDI 1» |
| Audio | scheda `DDJFLX6`, 4 canali, 44.1 kHz, 16/24 bit |
| Mixxx | 2.5.4 di sistema, 4 deck |
| Mappatura | community `fixxiefixx/DDJ-FLX6-Mixxx-mapping` |

## Audio

`~/.mixxx/soundconfig.xml`:

- canali **1-2 → Master**, canali **3-4 → Cuffie**
- 44100 Hz fisso (il controller non offre altro)
- buffer indice 5 ≈ 20 ms, valore prudente

Non è configurato l'ingresso microfono e il master non è duplicato sulle casse
del portatile: il suono esce solo dal controller.

Se un giorno l'audio sparisce, il sospetto numero uno è il nome del dispositivo:
Mixxx cerca `DDJ-FLX6: USB Audio` e ripiega su `hw:2,0`. Cambiando porta USB il
numero di scheda cambia, ma il nome no — per questo la corrispondenza per nome
viene prima (`src/soundio/soundmanagerconfig.cpp:147`).

## Mappatura

Installata in `~/.mixxx/controllers/`, attivata in `~/.mixxx/mixxx.cfg`:

```
[Controller]        DDJ-FLX6_MIDI_1  1
[ControllerPreset]  DDJ-FLX6_MIDI_1  /home/edi/.mixxx/controllers/Pioneer-DDJ-FLX6.midi.xml
```

**Provenienza**: derivata dalla mappatura ufficiale Mixxx del DDJ-FLX4 (a sua
volta dalla DDJ-400), stessi autori a monte — Warker, nschloe, dj3730, jusko,
Robert904, jaimearu — più `fixxJ` per l'adattamento all'FLX6.

**Cosa ho controllato prima di installarla**: lo script usa solo API Mixxx
(`engine.*`, `midi.*`, `script.*`, `console.*`). Nessun `eval`, nessuna rete,
nessun accesso al filesystem. I due SysEx sono il keep-alive del controller e la
richiesta di stato iniziale, entrambi normali per i Pioneer.

**Copertura**: 4 deck, mixer completo, jog con scratch, 8 pad per deck, modalità
Hot Cue / Beat Loop / Beat Jump / Sampler / Keyboard / Pad FX, 228 binding di
retroazione LED.

**Limiti noti**: dichiara `mixxxVersion="2.3"` e la descrizione interna cita
ancora l'FLX4 (residuo della derivazione). Il repository non ha licenza.

All'avvio Mixxx stampa un centinaio di avvisi *«MIDI device not open for
output»*: la mappatura invia gli stati dei LED prima che Mixxx apra la porta di
uscita. È un ordine di operazioni di Mixxx stesso — `setMapping()` viene prima
di `open()` (`src/controllers/controllermanager.cpp:318-328`) — non un difetto
della mappatura.

## Pad: come edi.audio li riempie

Primo banco, modalità **Hot Cue**:

| Pad | Cue | Colore | Cos'è |
|---|---|---|---|
| 1 | MIX IN | verde | primo downbeat utile |
| 2 | DROP | rosso | salto di energia più marcato |
| 3 | BREAK | blu | rientro di bassa energia dopo il drop |
| 4 | MIX OUT | arancione | inizio della coda |
| 5 | →INTRO | ciano | inizio del loop di intro |
| 6 | →OUTRO | magenta | inizio del loop di outro |
| 7-8 | — | | liberi |

In modalità **Saved Loop** ci sono anche i due loop veri da 16 battute, su intro
e outro.

**Perché i pad 5 e 6 duplicano l'inizio dei loop.** In Serato loop e hot cue
hanno numerazioni separate; in Mixxx condividono lo stesso spazio di indici, e
all'import Mixxx somma 8 all'indice dei loop
(`src/track/serato/tags.cpp:36-44`). I loop salvati finiscono quindi sugli hot
cue **13 e 14**, fuori dal primo banco di pad. I due cue in più mettono gli
stessi punti d'aggancio sotto le dita senza cambiare modalità.

Un cue che non si individua con sicurezza viene **omesso**, non piazzato a caso:
capita soprattutto al BREAK quando coincide col punto di uscita. Quelli si
rifiniscono a mano — ed è il motivo per cui esiste il round-trip qui sotto.

## Round-trip delle correzioni

Attivo: `~/.mixxx/mixxx.cfg` ha `SyncTrackMetadata 1` e `SeratoMetadataExport 1`.

Correggi un cue in Mixxx → la scrittura nel file è **differita** a quando la
traccia non è più in un deck, quindi arriva alla chiusura di Mixxx. Se la vuoi
subito: tasto destro sulla traccia → *Metadati → Esporta su File Tags*, poi
espelli dai deck.

Da lì in avanti edi.audio riconosce la traccia come «corretta a mano» e non la
tocca più. Per riscriverla comunque: `dj_prep.py rescan --force`.

L'impronta di riferimento sta in `~/.dj_downloader/analysis-state.json`, fuori
dai file, perché Mixxx elimina i campi che non conosce quando riscrive i tag.

## Dopo aver ri-preparato tracce già in libreria

Mixxx importa i cue quando aggiunge la traccia, non a ogni caricamento. Se
edi.audio riscrive i cue di una traccia già presente, in Mixxx si aggiornano con
tasto destro → *Metadati → Importa Da File Tags*.

## Comandi

```bash
python web_app.py                        # interfaccia su :5000
python dj_prep.py doctor                 # stato dell'ambiente
python dj_prep.py prepare "<link|titolo>"
python dj_prep.py rescan ~/Musica/DJ --export
python dj_prep.py inspect <file>.flac    # griglia e cue scritti nel file
```
