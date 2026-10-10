# Client di sincronizzazione Koofr per GNOME: documento di progetto

Stato: **bozza da approvare** (fase 1). Data: 2026-10-10. Nome provvisorio del progetto: `koofr-sync`, app ID provvisorio `io.github.daniel_g_carrasco.koofr-sync`. Entrambi cambiano quando sarà scelto il nome definitivo (fase 2); il nome finale non dovrebbe contenere "Koofr", che è un marchio altrui.

Legenda delle fonti: **[V]** verificato (sorgente, documentazione primaria o prova su questa macchina), **[I]** dedotto, da confermare con una prova. Le prove e i comandi sono nell'appendice A.

## 0. In breve

- **Motore: `rclone bisync`**, un processo per ogni esecuzione e per ogni coppia, non un `rclone rcd` di lunga durata. Il motivo principale è la sicurezza: via API rc il limite alle cancellazioni vale 0% se non lo si passa (difetto aperto, PR rclone #10015) e non esiste un arresto pulito del singolo job. rclone è compilato dai sorgenti nel manifest Flatpak (versione minima 1.75.2).
- **Nessun motore scritto da noi.** Lo stato di "sincronizzato" si legge dagli elenchi che bisync stesso scrive.
- **Protezioni a strati**: controllo d'identità della cartella radice prima di ogni giro, limite alle cancellazioni più stretto del default, copie di riserva locali di tutto ciò che viene cancellato o sovrascritto, conflitti sempre con entrambe le versioni conservate, nessun `--resync` automatico, anteprima `--dry-run` obbligatoria per ogni operazione di riparazione o di primo allineamento.
- **App**: Python, GTK 4 + libadwaita, un unico processo `Adw.Application` che resta vivo in background (portale Background, `hold()`), con icona StatusNotifierItem scritta in Python puro e servizio D-Bus per Nautilus.
- **Nautilus**: estensione nautilus-python che risponde solo da una cache in memoria e chiede gli stati al servizio D-Bus per cartella, in modo asincrono. Emblemi sulle cartelle e sui file, con icone nostre (il tema Adwaita 50 non ha emblemi adatti).
- **Flatpak**: runtime GNOME 50, accesso statico alla home (il portale documenti non segnala le modifiche fatte da altri programmi), tray tramite `org.kde.StatusNotifierWatcher`, password nel portachiavi tramite il portale Secret, nessun altro permesso.

## 1. Obiettivi e limiti

Obiettivi: sostituire Koofr Desktop per l'uso quotidiano su Margine OS (GNOME e Lucciola), con sincronizzazione a due vie affidabile, stato leggibile, tray che non disturba Noctalia, emblemi in Nautilus, distribuzione tramite repository Flatpak proprio.

Fuori dal progetto, almeno all'inizio: unità di rete o montaggio virtuale stile "file su richiesta" (rclone mount); account multipli; dispositivi Koofr diversi dallo storage principale (mount Dropbox o Google collegati in Koofr); versioni per Windows o macOS; Flathub.

## 2. Il punto di partenza

**Koofr Desktop oggi** [V]: 16 coppie configurate in `~/.koofr/config.json`, tutte con lo stesso `mountId`, tutte sotto `~/data` più `~/scratch/Elettrico`; una in pausa (`data/imports`). Ogni coppia ha `useTrash: true`, cioè il client tiene un cestino locale `SyncTrash/` dentro la cartella sincronizzata (oggi `data/work/SyncTrash` occupa 8,4 GB in 9.683 voci). Lo stato di Koofr Desktop è in database SQLite in `~/.koofr/syncdb/`.

**Dimensioni** [V], misurate sulle cartelle locali:

| Coppia | Voci | Cartelle | Spazio |
|---|---:|---:|---:|
| data/imports (in pausa) | 213.723 | 22.551 | 628 GB |
| data/work | 59.501 | 8.308 | 172 GB |
| data/projects | 42.442 | 4.275 | 6,5 GB |
| data/media/photos/camera-roll | 9.151 | 12 | 48 GB |
| scratch/Elettrico | 8.157 | 696 | 31 GB |
| le altre 11 insieme | circa 6.700 | circa 900 | circa 59 GB |

Senza `imports`: circa 135.000 voci, circa 15.000 cartelle, circa 330 GB. La cartella singola più affollata è `camera-roll` con 7.752 voci, seguita da una cartella di `data/work` con 1.752. Limite inotify della macchina: 271.718 osservazioni per utente [V], cioè abbastanza anche includendo `imports`. Il filesystem è btrfs.

**Sull'host ci sono già** [V]: rclone 1.74.3, Nautilus 50.3, nautilus-python 4.1.0 (arriva solo come dipendenza di `nautilus-gsconnect`, non è dichiarato dall'immagine di Margine), estensione AppIndicator attiva nella sessione GNOME, gnome-keyring attivo anche sotto niri.

## 3. Architettura

```
 ┌──────────────────────────── Flatpak (sandbox) ─────────────────────────────┐
 │  Processo app (Python, Adw.Application, sempre vivo con hold())             │
 │                                                                             │
 │   Finestra GTK ◄──┐                                                         │
 │                   │        ┌──────────────┐     ┌──────────────────────┐    │
 │   Tray SNI ◄──────┼────────┤ Modello di   │◄────┤ Pianificatore        │    │
 │   (Gio.DBus)      │        │ stato        │     │ (coda, debounce,     │    │
 │                   │        │ (per coppia  │     │  timer, rete, pausa) │    │
 │   Servizio D-Bus ◄┘        │  e percorso) │     └──────────┬───────────┘    │
 │   <app-id>.Sync1           └──────▲───────┘                │ avvia          │
 │                                   │ eventi JSON            ▼                │
 │   Monitor file (inotify) ─────────┘             rclone bisync (figlio,      │
 │   Portachiavi (portale Secret)                   uno per giro)              │
 └───────────────────────────────────────────────────────────┬────────────────┘
        ▲ D-Bus di sessione                                   │ HTTPS
        │                                                     ▼
 Estensione nautilus-python (host)                       API Koofr
```

Un solo processo Python contiene tutto: interfaccia, tray, servizio D-Bus, monitor dei file, pianificatore. rclone gira come processo figlio, uno per esecuzione. La scelta di un processo unico (invece di un demone separato dall'interfaccia) segue lo schema GNOME standard: la finestra si chiude e l'app resta in background, "Esci" la termina davvero. È più semplice da impacchettare e da attivare via D-Bus, e se il processo cade non si perdono dati: rclone ha già i suoi meccanismi di ripresa (`--recover`).

### Moduli previsti

Le app esistenti (p7m-extractor, pdf-cropper) sono un file Python unico. Qui le parti sono troppe (motore, stato, D-Bus, tray, interfaccia) e conviene un pacchetto, mantenendo il resto delle convenzioni: nessuna dipendenza Python esterna oltre a PyGObject, gettext con `po/` e `tools/compile_po.py`, test eseguibili con `python tests/...`, CLI accanto alla GUI.

| Modulo | Compito |
|---|---|
| `config.py` | coppie e preferenze in JSON (`$XDG_CONFIG_HOME/<app>/pairs.json`), scrittura atomica |
| `secrets.py` | password per applicazioni nel portachiavi (libsecret) |
| `rclone.py` | costruzione dei comandi (lista di opzioni ammesse, mai quelle vietate), lettura del log JSON, codici d'uscita, SIGINT |
| `preflight.py` | controlli prima di ogni giro e prima di aggiungere una coppia |
| `scheduler.py` | coda globale, debounce, intervalli, pausa per rete a consumo o risparmio energetico |
| `watcher.py` | monitor ricorsivo (un `Gio.FileMonitor` per cartella) |
| `listings.py` | lettura degli elenchi `.lst` di bisync |
| `state.py` | stato per coppia e per percorso, aggregazione sulle cartelle |
| `conflicts.py` | individuazione e risoluzione dei conflitti |
| `dbus_service.py` | interfaccia `<app-id>.Sync1` |
| `tray.py` | StatusNotifierItem e `com.canonical.dbusmenu` in Python puro |
| `portal.py` | portale Background |
| `ui/` | finestra, accesso, aggiunta coppia, conflitti, preferenze |
| `cli.py` | `status`, `pairs`, `sync --dry-run`, utile per prove senza GUI |
| `nautilus/` | l'estensione, file Python separato installato sull'host |

## 4. Scelta del motore

### 4.1 Perché rclone bisync

- bisync è **stabile dalla 1.71** (agosto 2025), non più beta [V, changelog rclone]. Il backend Koofr non compare tra quelli con problemi noti [V].
- Ha già le protezioni che servono: limite percentuale alle cancellazioni, controllo dei file sentinella, cartelle di backup per lato, conflitti con entrambe le versioni, ripresa dopo interruzione (`--recover`), arresto pulito con SIGINT, codice d'uscita 7 per "serve riparazione" [V].
- Il backend Koofr usa la libreria ufficiale di Koofr, ha hash MD5 lato server, date di modifica, link pubblici, quota [V].
- L'esempio negativo è Celeste (GTK4 + rclone): aveva scritto un proprio algoritmo a due vie sopra rclone e ha accumulato segnalazioni di cancellazioni non propagate, file che ricompaiono, modifiche perse; il progetto è archiviato da novembre 2025 [V]. Conferma la regola: niente motori scritti da zero.

### 4.2 Un processo per giro, non `rclone rcd`

| | `rclone bisync` come processo figlio | `rclone rcd` + `sync/bisync` asincrono |
|---|---|---|
| Limite cancellazioni | default documentato (50%), noi lo passiamo sempre | se non passato vale **0%**; anche il dry-run globale viene ignorato (PR #10015 aperta) [V sorgente 1.75.2] |
| Arresto | SIGINT: arresto pulito di quella sola coppia, stato salvato | `job/stop` annulla il contesto: errore critico, elenchi da recuperare; SIGINT ferma tutti i job |
| Avanzamento | log JSON su stderr con l'elenco dei file in trasferimento, ogni secondo | stesse statistiche via `core/stats`, ma il log di bisync arriva solo alla fine, in un'unica stringa |
| Isolamento | un crash tocca una coppia | un crash ferma tutto e lascia più lock |
| Memoria | liberata a fine giro | perdite corrette solo nella 1.75.1 |

Scelta: **processo figlio**. Il costo di avvio (qualche decina di MB e un avvio di rclone per giro) è trascurabile rispetto al tempo di elenco dei file. Un `rcd` piccolo resta un'opzione futura solo per azioni rapide dell'interfaccia (sfogliare il cloud, quota, link), mai per la sincronizzazione.

### 4.3 Come si passa la configurazione

- Il remote si definisce con variabili d'ambiente del solo processo figlio: `RCLONE_CONFIG_KOOFR_TYPE=koofr`, `..._USER`, `..._PASSWORD` (offuscata con `rclone obscure -` via stdin). Mai nella riga di comando, che è leggibile da tutti in `/proc/<pid>/cmdline` [V].
- `--config` punta a un file inesistente dentro i dati dell'app, così rclone non legge mai l'eventuale `~/.config/rclone/rclone.conf` personale, che con l'accesso alla home sarebbe visibile.
- L'offuscamento di rclone è reversibile (chiave nel sorgente) [V]: la protezione vera è il portachiavi. La password per applicazioni di Koofr si può revocare da sola e non può cambiare le impostazioni dell'account.
- Il nome del remote resta sempre `koofr`: bisync lega gli elenchi al nome del remote, rinominarlo li farebbe perdere [V].

### 4.4 Opzioni di base per ogni giro

```
rclone bisync <locale> koofr:<remoto>
  --config <dati-app>/rclone-no.conf
  --workdir <dati-app>/bisync           # non ~/.cache, che può essere svuotata
  --max-delete <N>                      # sempre esplicito, vedi 5.3
  --backup-dir1 <backup locale>/<coppia>
  --conflict-resolve none --conflict-loser num --conflict-suffix conflict
  --suffix-keep-extension
  --compare size,modtime,checksum --slow-hash-sync-only
  --resilient --recover --max-lock 2m
  --filters-file <dati-app>/filters/<coppia>.txt
  --create-empty-src-dirs
  --use-json-log --stats 1s --stats-log-level NOTICE --color NEVER
  -v
```

Opzioni **mai** esposte né usate: `--delete-excluded` (legata a un caso di remote svuotato segnalato a marzo 2026), `--inplace` (lascia file parziali che il giro dopo prende per veri), `--copy-links` (sconsigliata da Koofr), `--force` automatico, `--resync` automatico.

`--compare size,modtime,checksum` con `--slow-hash-sync-only`: l'MD5 lato Koofr è gratuito (lo dà il server), quello locale si calcola solo per i file da copiare, non per tutti i 330 GB a ogni giro. È la combinazione consigliata dalla documentazione; il comportamento esatto con Koofr va provato sulla cartella di test [I].

### 4.5 rclone nel Flatpak

rclone pubblica a ogni versione un archivio ufficiale con tutte le dipendenze Go (`rclone-vX-vendor.tar.gz`, verificato per la 1.75.2) [V]. Il manifest usa due sorgenti `archive` con sha256 fisso e compila senza rete:

```
go build -mod=vendor -trimpath -tags noselfupdate \
  -ldflags "-s -X github.com/rclone/rclone/fs.Version=v1.75.2"
```

Serve Go 1.26 o più recente: l'estensione `org.freedesktop.Sdk.Extension.golang//25.08` ha Go 1.27.1 ed è compatibile con GNOME 50 [V]. Il binario completo pesa circa 60-70 MB [I]; in seguito si può ridurre con un `main.go` che importa solo i backend `koofr` e `local`. Aggiornare rclone sarà un'operazione ricorrente (arrivano spesso correzioni di sicurezza): Renovate o Dependabot possono aprire la PR con i nuovi sha256.

## 5. Flusso di sincronizzazione e sicurezza dei dati

### 5.1 Aggiunta di una coppia

Controlli bloccanti prima di salvare la coppia:

1. La cartella locale esiste, è una cartella vera (non un collegamento simbolico alla radice) e non si sovrappone a un'altra coppia (né dentro né sopra). Stesso controllo sul percorso remoto: niente `koofr:/data` insieme a `koofr:/data/work`.
2. La cartella non è già sincronizzata da Koofr Desktop: l'app legge `~/.koofr/config.json` e, se trova la stessa cartella in una coppia non in pausa, rifiuta. **Due motori sulla stessa cartella sono il modo più rapido per perdere dati.**
3. Nomi che Koofr non accetta [V]: oltre 255 caratteri per nome o 1023 per percorso, caratteri fuori dal piano base Unicode (quasi tutte le emoji), nomi che differiscono solo per maiuscole/minuscole nella stessa cartella (Koofr non distingue maiuscole e minuscole). rclone non li codifica e un file che non si può caricare è un errore critico di bisync, che si ripeterebbe a ogni giro [V/I]. L'app li elenca e propone di escluderli o rinominarli.
4. Si registra l'identità della radice: dispositivo e inode della cartella locale, ID della coppia.

### 5.2 Primo allineamento

| Situazione | Cosa succede |
|---|---|
| Entrambe vuote | si scrivono gli elenchi iniziali, nessun trasferimento |
| Solo la locale ha file | `--resync` che carica tutto, dopo un'anteprima |
| Solo la remota ha file | `--resync` che scarica tutto, dopo un'anteprima |
| Entrambe con file | **anteprima obbligatoria** di `--resync --dry-run`: quanti file salgono, quanti scendono, quanti sono diversi e quale versione vince; l'utente conferma |

`--resync` non cancella mai nulla, fa l'unione dei due lati; con la modalità di default il lato locale vince sui file diversi [V]. Proposta: `--resync-mode newer` (vince il più recente) e la versione perdente finisce nel backup locale o resta nelle versioni precedenti di Koofr, così nemmeno qui si perde nulla in silenzio. Da verificare sulla cartella di test che `--backup-dir1` venga usata anche durante il resync [I].

**Rischio specifico di Koofr** [V]: Koofr non permette di cambiare la data di modifica di un file già caricato; rclone in quel caso cancella e ricarica il file. Se le date sul cloud dei file caricati da Koofr Desktop non coincidono con quelle locali, il primo allineamento potrebbe ricaricare centinaia di GB. Prima della migrazione va misurato su un campione (vedi 13, domanda 4).

### 5.3 Giro normale

Ogni giro, in quest'ordine:

1. **Controllo della radice** (nostro, prima di rclone): la cartella locale esiste, ha ancora lo stesso dispositivo e inode registrati, il percorso remoto esiste. Protegge dal caso classico del disco esterno non montato o della cartella rinominata, in cui il lato "vuoto" verrebbe preso per una cancellazione di massa. Se fallisce, la coppia va in errore e non si tocca nulla.
2. **Lock nostro** sulla coppia (`flock`), perché il lock di bisync ha una corsa nota (issue #9793) [V].
3. **rclone bisync** con le opzioni del punto 4.4.
4. **Esito**: 0 tutto bene; 1 errore non critico, si ritenta con attesa crescente; 7 errore critico, la coppia passa in "Riparazione necessaria" e si ferma; superamento di `--max-delete`, la coppia si ferma e chiede conferma.

**Limite alle cancellazioni.** Il default di bisync è 50% per lato [V], troppo largo per `data/work` (vorrebbe dire 30.000 file). Proposta: 10% per lato. Quando il limite scatta, l'app mostra l'elenco delle cancellazioni ottenuto con `--dry-run` e solo dopo una conferma esplicita ripete il giro con `--force`. Il percentuale da solo non basta per le cartelle piccole (in `templates` un solo file è il 33%): sotto le 50 voci il limite si applica in valore assoluto [I, da decidere].

**Dove finisce ciò che viene cancellato o sovrascritto:**

- Lato locale: `--backup-dir1` in una cartella di backup dell'app sullo stesso filesystem (spostamento istantaneo su btrfs). Pulizia automatica dopo 30 giorni, con dimensione visibile nelle preferenze. Per coppie su dischi esterni il backup va sullo stesso disco.
- Lato Koofr: Koofr ha il cestino (7 giorni gratis, 30 a pagamento) e le versioni precedenti (120 giorni gratis, 2 anni a pagamento, esclusi i file oltre 5 GB) [V, documentazione Koofr]. **Non è verificato che le cancellazioni fatte tramite API finiscano nel cestino**: va provato sulla cartella di test. Se non ci finiscono, si attiva `--backup-dir2` in una cartella nascosta su Koofr, a costo di spazio.

**Interruzioni.** Chiusura dell'app, logout o pausa mandano SIGINT: rclone smette di accodare, aspetta fino a 30 secondi i trasferimenti in corso e salva lo stato [V]. Un secondo segnale o la sospensione del computer a metà giro si recuperano con `--recover`. Sotto niri non si può impedire la sospensione (il portale Inhibit sotto niri gestisce solo l'inattività) [V], per questo ogni giro deve essere riprendibile; sotto GNOME l'app chiede di non sospendere durante un giro attivo.

**Riparazione.** Mai `--resync` automatico. "Ripara" esegue `--resync --dry-run`, mostra il riepilogo, chiede conferma, poi esegue il resync vero. Esiste anche "Verifica" (`--check-sync only`), che confronta gli elenchi senza trasferire nulla.

### 5.4 Quando parte un giro

- **Modifiche locali**: monitor inotify ricorsivo (un `Gio.FileMonitor` per cartella, aggiornato quando nascono o spariscono cartelle). Debounce: il giro parte dopo 10 secondi senza nuove modifiche, o al massimo dopo 2 minuti di modifiche continue. Le modifiche fatte da rclone stesso durante il giro vengono ignorate.
- **Modifiche remote**: rclone non ha un canale di notifiche per Koofr e il backend non ha elenchi ricorsivi rapidi (`ListR` assente) [V]. Si fa un giro periodico, proposta ogni 10 minuti, più uno all'avvio, al ritorno della rete e alla ripresa dalla sospensione. Ogni giro elenca entrambi i lati per intero: va misurato quanto costa elencare `data/work` su Koofr (domanda aperta 3).
- **Coda**: un giro alla volta su tutto l'account nell'MVP. In seguito al massimo due coppie in parallelo, mai sovrapposte.
- **Pausa automatica** su rete a consumo e in risparmio energetico (`Gio.NetworkMonitor` e `Gio.PowerProfileMonitor` funzionano via portale anche sotto niri) [V], disattivabile.

### 5.5 Esclusioni

Un file di filtri per coppia, nella sintassi di rclone. Valori di partenza: file temporanei (`~$*`, `.~lock.*#`, `*.part`, `*.crdownload`, `.goutputstream-*`, `*.swp`), `.Trash-*/`, `SyncTrash/` di Koofr Desktop, i file `.conflictN` in corso di risoluzione. Proposte da valutare: `node_modules/` (in `data/projects` ce ne sono 2).

bisync registra un'impronta del file dei filtri e, se il file cambia, pretende un resync [V]: è voluto, perché altrimenti i file appena esclusi sembrerebbero cancellati. L'interfaccia, quando si cambiano le esclusioni, avvia direttamente il percorso di riparazione con anteprima. I file esclusi non vengono mai cancellati né da una parte né dall'altra.

I collegamenti simbolici non vengono seguiti né sincronizzati (`--skip-links` lato locale) e in Nautilus appaiono come esclusi. Da confermare come si comporta oggi Koofr Desktop con i link [I].

### 5.6 Conflitti

Un conflitto è un file cambiato su entrambi i lati dall'ultimo giro. Con `--conflict-resolve none` bisync conserva entrambe le versioni su entrambi i lati, come `nome.conflict1.ext` (locale) e `nome.conflict2.ext` (remota) [V]. L'app:

- riconosce i conflitti dal log del giro e dai nomi dei file;
- li mostra in una vista dedicata: le due versioni con dimensione, data, provenienza, pulsanti per aprirle;
- permette di scegliere: tenere la locale, tenere la remota, tenere entrambe con nomi distinti;
- la versione scartata va nel cestino locale (`Gio.File.trash`) o nel cestino di Koofr, mai cancellata in modo definitivo;
- manda una notifica con azioni ("Mostra", "Tieni entrambe"), una sola per giro e non una per file.

## 6. Modello di stato

**Per coppia**, dal più grave al meno grave:

| Stato | Significato |
|---|---|
| Riparazione necessaria | errore critico di bisync o limite cancellazioni superato: ferma finché l'utente non decide |
| Errore | errore ripetibile (rete, autenticazione, radice mancante), con messaggio leggibile |
| Conflitti | ultimo giro riuscito ma con N conflitti aperti |
| In sincronizzazione | giro in corso: file, byte, velocità, file in trasferimento |
| In attesa | modifiche locali rilevate, giro in coda |
| In pausa | dall'utente, per rete a consumo, risparmio energetico, assenza di rete |
| Aggiornato | ultimo giro riuscito, con l'orario |

Lo stato globale (tray, titolo della finestra) è il più grave tra quelli delle coppie attive.

**Per percorso** (per gli emblemi), con priorità errore > conflitto > in sincronizzazione > in attesa > sincronizzato:

- *sincronizzato*: presente in entrambi gli elenchi dell'ultimo giro riuscito e non cambiato da allora;
- *in attesa*: segnalato dal monitor dopo l'ultimo giro;
- *in sincronizzazione*: tra i file del giro in corso (righe "queue copy" del log) o in trasferimento;
- *errore*: citato in un errore del giro;
- *conflitto*: ha un `.conflictN` aperto;
- *escluso*: corrisponde ai filtri o è un collegamento simbolico.

Gli elenchi di bisync (`*.path1.lst`) sono la fonte del "sincronizzato": nessun database nostro da tenere coerente. Il loro formato è interno a rclone, quindi è fissato dalla versione inclusa nel Flatpak e coperto da un test; un aggiornamento di rclone che lo cambia fa fallire la CI, non i dati. Lo stato delle cartelle si calcola nel processo dell'app, tenendo per ogni cartella i contatori degli stati dei figli (albero dei prefissi), così il calcolo è incrementale.

## 7. Interfaccia D-Bus

Il nome sul bus è l'app ID, che `Gio.Application` possiede già; un'app Flatpak può possedere il proprio ID e i sottonomi senza permessi aggiuntivi, e i processi dell'host possono chiamarla liberamente [V].

```
Nome:      <app-id>
Oggetto:   /io/github/daniel_g_carrasco/<app>/Sync
Interfaccia: <app-id>.Sync1

GetVersion() -> (u api)
GetRoots() -> a(ss)                     # (URI radice locale, ID coppia)
GetFolderStates(s dir_uri) -> a{su}     # nome figlio -> stato, una chiamata per cartella
GetStates(as uris) -> a{su}
SyncNow(s uri)
GetWebUrl(s uri) -> s                   # "Apri su Koofr"
CreateShareLink(s uri) -> s             # "Copia link di condivisione"
ShowWindow(s page)

signal StatesChanged(a{su})             # raggruppato, al massimo ogni 300 ms, include le cartelle madri
signal RootsChanged()
```

Gli stati sono interi (enumerazione documentata). Il numero di versione nel nome dell'interfaccia permette di cambiarla senza rompere un'estensione più vecchia. Il Flatpak esporta un file di attivazione D-Bus: "Sincronizza ora" dal menu di Nautilus può avviare l'app, mentre la semplice navigazione no (le chiamate per gli emblemi usano `DO_NOT_AUTO_START`).

## 8. Integrazione con Nautilus

### 8.1 Come Nautilus 50 mostra gli emblemi

[V, sorgente di Nautilus 50.3]: gli emblemi sono tornati con GNOME 43 come icone separate, non più sovrapposte all'icona del file. In vista a griglia stanno in una colonna larga 18 px accanto all'icona; in vista a elenco dopo il nome. Sono sempre a 16 px e attenuati. Non compaiono nella barra laterale né nella barra del percorso. Ne entrano 2-4 a seconda dello zoom, condivisi con quelli di Nautilus (preferiti, sola lettura, collegamento): **un solo emblema per file**.

`FileInfo.add_emblem` resta l'unica API, non deprecata. Il tema Adwaita 50 non ha emblemi simbolici utili (ha solo `important` e `system`; `emblem-synchronizing-symbolic` non esiste su questa macchina) [V]. Quindi icone nostre: SVG simbolici a 16 px che riempiono tutta la superficie (gli emblemi a "badge d'angolo" di Nextcloud risultano minuscoli), con le classi `success`/`warning`/`error` per il colore.

Emblemi previsti: sincronizzato (spunta discreta), in sincronizzazione (frecce circolari), in attesa (orologio), errore, conflitto, escluso (cerchio barrato). Da decidere se lo stato "sincronizzato" si mostra con la spunta o con nessun emblema (domanda aperta 6).

### 8.2 Prestazioni: perché si può fare anche sui file

[V, sorgente]: Nautilus chiama `update_file_info` per **tutti** i file della cartella aperta, uno alla volta, nel thread principale. Se la funzione aspetta qualcosa, Nautilus si blocca (caso MEGA); se usa la variante asincrona, in nautilus-python è difettosa (handle non inizializzato, issue #19 e #20 aperte). Il caso Insync (15.000 file, oltre un minuto per aprire la cartella) è il risultato di un'estensione lenta.

Il disegno che evita il problema:

1. `update_file_info` lavora solo in memoria: controlla se l'URI è sotto una radice, cerca nella cache, se c'è aggiunge un emblema, se non c'è annota la cartella e ritorna subito. Niente `stat`, niente `realpath`, niente D-Bus, mai `IN_PROGRESS`.
2. Alla prima assenza in una cartella parte **una** chiamata asincrona `GetFolderStates(cartella)`; alla risposta si riempie la cache e si chiama `invalidate_extension_info()` solo sui file che Nautilus ha davvero (`FileInfo.lookup_for_uri`). Il secondo passaggio trova tutto in cache.
3. Il segnale `StatesChanged` aggiorna la cache e invalida solo i file visibili.
4. La cache contiene stringhe, non oggetti `FileInfo` (Nextcloud tiene gli oggetti e la memoria cresce), con limite LRU per cartelle.
5. Se l'app non è in esecuzione l'estensione non mostra nulla e non rallenta nulla.

Stima [I]: per una cartella di 10.000 file, 0,2-0,5 secondi di lavoro distribuiti, e solo alla prima apertura. Va misurato su `camera-roll` (7.752 voci). **Se la misura risultasse cattiva**, la riserva è: emblemi solo sulle cartelle e sui file con stato diverso da "sincronizzato" (errore, conflitto, in corso), che sono pochi.

### 8.3 Menu contestuale

`get_file_items` e `get_background_items` costruiscono le voci dalla cache, solo dentro le radici: "Sincronizza ora", "Apri su Koofr", "Copia link di condivisione". Le azioni chiamano il servizio in modo asincrono. Il link viene messo negli appunti dall'estensione stessa, perché sotto Wayland l'app in background non ha il focus [I]. Attenzione: `rclone link` su Koofr ignora scadenza e revoca [V], quindi la revoca di un link va fatta dal sito di Koofr (o con una chiamata API diretta, più avanti). Il formato dell'URL per "Apri su Koofr" va verificato.

### 8.4 Installazione dell'estensione

L'estensione è un file Python che parla solo D-Bus. Proposta: la fornisce l'app stessa e, quando l'utente attiva "Integrazione con Nautilus" nelle preferenze, la copia in `~/.local/share/nautilus-python/extensions/` insieme alle icone degli emblemi in `~/.local/share/icons/hicolor/scalable/emblems/`, poi propone di riavviare Nautilus (`nautilus -q`). Così l'estensione è sempre allineata alla versione dell'app e funziona su qualunque distribuzione con nautilus-python.

L'immagine di Margine **non** deve installare anche lei l'estensione: nautilus-python 4.1.0 registra due volte un'estensione presente in due cartelle (corretto solo nella 4.2.0) [V]. All'immagine si chiede solo di dichiarare nautilus-python in modo esplicito, perché oggi c'è solo come dipendenza di GSConnect e sparirebbe con lui (per esempio al cambio di base). La modifica sarà una PR separata su `margine-image`, preparata nella fase 4.

Altri file manager (Nemo, Dolphin): da valutare dopo, come previsto.

## 9. Tray, background, notifiche

**Icona di stato** [V]: StatusNotifierItem scritto in Python con `Gio.DBusConnection` (non c'è libappindicator per GTK 4, e la libreria Ayatana nuova esporta i menu in un formato che Noctalia non legge). L'oggetto si registra sul nome univoco della connessione con `RegisterStatusNotifierItem("/StatusNotifierItem")`, che sia l'estensione AppIndicator di GNOME sia Noctalia accettano; così non serve possedere nomi `org.kde.*`. Quando il servizio del tray si riavvia (Noctalia ricaricato), l'app si registra di nuovo.

La lezione di Koofr Desktop, trasformata in regole e in un test:

- `IconName` usa icone dell'app installate nel Flatpak con nomi che iniziano con l'app ID (`<app-id>-synced-symbolic`, `-syncing-`, `-paused-`, `-error-`), che Flatpak esporta all'host e Noctalia ricolora. Niente file temporanei, niente pixmap rigenerati.
- `NewIcon` si emette **solo quando l'icona cambia davvero**, al massimo una volta ogni 500 ms. Il suggerimento con l'avanzamento (`NewToolTip`) al massimo ogni 2 secondi.
- Stato sempre `Active`: con `Passive` l'estensione AppIndicator nasconde l'icona.
- Test automatico: un minuto di inattività e un giro di sincronizzazione finto, contando i segnali sul bus; zero `NewIcon` a riposo.

Menu del tray: stato, ultimo giro, "Sincronizza tutto", "Metti in pausa"/"Riprendi", "Apri", "Esci".

**Background e avvio** [V]: `org.freedesktop.portal.Background.RequestBackground` con avvio automatico e riga di comando con `--background`. Il portale stesso scrive `~/.config/autostart/<app-id>.desktop`, sia sotto GNOME sia sotto niri. libportal non è nel runtime GNOME 50, quindi la chiamata si fa con `Gio.DBusProxy`. L'app fa `hold()` e offre l'azione `app.quit`, che GNOME usa per il pulsante di chiusura delle app in background e che ferma rclone in modo pulito. `SetStatus` (il messaggio sotto il nome nelle app in background di GNOME) sotto niri fallisce: si chiama e si ignora l'errore.

**Notifiche** [V]: `Gio.Notification` passa dal portale; sotto niri arrivano a Noctalia, ma va provato se Noctalia mostra i pulsanti. Solo per eventi che chiedono qualcosa: conflitti, riparazione necessaria, credenziali non valide, radice sparita. Nessuna notifica per i giri riusciti.

## 10. Credenziali

Accesso con indirizzo e password per applicazioni di Koofr (si crea da impostazioni dell'account, sezione password). La finestra di accesso spiega dove crearla e la verifica subito con `rclone about koofr:` (quota). La password va nel portachiavi con `Secret.password_store`. Dentro il Flatpak libsecret usa da sola il portale Secret: salva in un portachiavi cifrato dell'app in `~/.var/app/<app-id>/data/keyrings/`, la cui chiave sta in gnome-keyring sull'host [V]. Funziona anche sotto niri (gnome-keyring attivo, portale Secret configurato in `niri-portals.conf`) [V]. Non serve il permesso `org.freedesktop.secrets`.

Il token OAuth di Koofr Desktop non è riutilizzabile: per migrare serve comunque creare una password per applicazioni.

## 11. Permessi Flatpak

| Permesso | Perché |
|---|---|
| `--share=network` | rclone parla con Koofr; serve anche al portale che segnala la rete a consumo |
| `--socket=wayland` | finestra GTK |
| `--socket=fallback-x11`, `--share=ipc` | solo dove manca Wayland |
| `--device=dri` | rendering GTK 4 |
| `--talk-name=org.kde.StatusNotifierWatcher` | registrare l'icona del tray, senza possedere nomi altrui |
| `--filesystem=home` | vedi sotto |
| `--filesystem=/run/media` | coppie su dischi rimovibili (facoltativo) |

Non servono: `org.freedesktop.secrets` (portale Secret), `org.freedesktop.Notifications` (portale), `xdg-config/autostart` (lo scrive il portale Background), nessun `--own-name` (l'app ID è già concesso), nessun accesso a `org.freedesktop.Flatpak` (sarebbe un'uscita dalla sandbox).

**Il compromesso sulla home.** La soluzione ideale sarebbe concedere solo le cartelle scelte dall'utente tramite il portale documenti. Non funziona per un client di sincronizzazione [V]: sui percorsi del portale (`/run/user/1000/doc/...`, un filesystem FUSE) le modifiche fatte da altri programmi non generano eventi inotify (issue xdg-desktop-portal #567, aperta), ogni accesso passa dal demone del portale (lento su 100.000 file) e i percorsi non coincidono con quelli che vede Nautilus. Con un permesso statico, invece, inotify funziona e i percorsi sono identici a quelli dell'host (provato). Tutti i client di sincronizzazione su Flathub usano permessi statici: Nextcloud `host`, Dropbox `home`, Syncthing Tray `home`.

Scelta proposta: `--filesystem=home`. Serve anche a leggere `~/.koofr/config.json` per la migrazione e a installare l'estensione e le icone in `~/.local/share`. Chi vuole restringere può farlo con `flatpak override --user --nofilesystem=home --filesystem=~/data <app-id>`; l'app se ne accorge e segnala le coppie non raggiungibili invece di trattarle come vuote (è lo stesso controllo della radice del punto 5.3). Effetto collaterale da tenere presente: con la home visibile l'app vedrebbe anche `~/.config/rclone`, ed è il motivo per cui `--config` punta sempre a un file proprio.

## 12. Repository, test, distribuzione

Struttura, sul modello di p7m-extractor e pdf-cropper:

```
README.md, LICENSE (MIT, come le altre app e come rclone)
src/<pacchetto>/          codice dell'app
nautilus/                 estensione per Nautilus
data/                     .desktop, metainfo, icone app, icone di stato, emblemi, servizio D-Bus
po/                       traduzioni (sorgenti in inglese, italiano completo)
tests/                    test della logica e d'integrazione
tools/                    compile_po.py, flatpak_repo_files.py, make_icon.py
build-aux/flatpak/        manifest (rclone dai sorgenti + app)
site/                     sito del progetto (Cloudflare Pages, come pdf-cropper)
.github/workflows/        build.yml (test, validazione, flatpak), flatpak-repo.yml (repository su Pages)
docs/                     questo documento e le note tecniche
```

**Test automatici:**

- Logica pura: costruzione dei comandi (le opzioni vietate non compaiono mai, `--max-delete` c'è sempre, la password non è mai negli argomenti), lettura del log JSON da registrazioni reali, lettura degli elenchi, aggregazione degli stati, controlli di `preflight` (sovrapposizioni, nomi con emoji, collisioni di maiuscole), debounce del pianificatore.
- Integrazione senza rete, in CI: bisync tra due cartelle locali con il rclone del progetto, scenari scritti apposta (modifica da un lato, cancellazione, conflitto, cancellazione di massa che deve fermarsi, disco "non montato", interruzione a metà con SIGINT, cambio dei filtri).
- Tray e D-Bus: un bus di sessione privato (`dbus-run-session`) per contare i segnali e provare l'interfaccia.
- Prova vera su Koofr, a mano: una cartella dedicata sul cloud (per esempio `/koofr-sync-test`) e una locale (`~/scratch/koofr-sync-test`), sempre prima in `--dry-run`.

**Distribuzione** (fase 6, dopo il via): repository Flatpak su GitHub Pages con lo stesso workflow e le stesse correzioni delle altre app (cartelle vuote di ostree ricreate dopo il clone), chiave GPG dedicata in `~/.config/<app>-flatpak/`, sito su Cloudflare Pages.

## 13. Rischi e domande aperte

### Rischi

| Rischio | Mitigazione |
|---|---|
| Cancellazione di massa per radice non montata o rinominata | controllo d'identità della radice prima di ogni giro, `--max-delete` stretto, backup locale |
| Due motori sulla stessa cartella (Koofr Desktop ancora attivo) | rifiuto della coppia se Koofr Desktop la sincronizza; migrazione che mette in pausa le coppie di Koofr Desktop |
| Ricaricamento completo per date diverse su Koofr | misura su un campione prima della migrazione; per le cartelle grandi eventuale `--compare size,checksum` |
| Nomi non accettati da Koofr che bloccano il giro | controllo preventivo e proposta di esclusione o rinomina |
| Elenchi di bisync rovinati da un crash o da una sospensione | `--recover`, riparazione sempre con anteprima, mai resync automatico |
| Formato degli elenchi cambiato da un nuovo rclone | versione di rclone fissata nel Flatpak, test in CI |
| Difetti di rclone (correzioni frequenti, anche di sicurezza) | aggiornamenti guidati da Renovate, scenari di integrazione a ogni aggiornamento |
| Nautilus rallentato dall'estensione | cache in memoria, misura su `camera-roll`, riserva con emblemi solo sulle cartelle |
| Cancellazioni via API non nel cestino di Koofr | prova sulla cartella di test; in caso, `--backup-dir2` su Koofr |
| Qualunque programma dell'utente può chiedere `CreateShareLink` via D-Bus | stesso utente, che potrebbe comunque leggere i file; si valuta una conferma nell'app per i link |
| Elenchi completi a ogni giro lenti sulle cartelle grandi | misura su `data/work`; intervallo remoto adattivo per coppia |

### Domande aperte

Decisioni da prendere:

1. **Accesso alla home** (`--filesystem=home`) con la spiegazione del punto 11: va bene, o si preferisce un insieme più stretto (`~/data`, `~/scratch` più le cartelle per estensione e icone) a costo di funzionare solo per queste cartelle?
2. **Limite alle cancellazioni**: 10% per lato, più una soglia assoluta per le cartelle piccole, va bene?
3. **Intervallo del controllo remoto**: 10 minuti come punto di partenza?
4. **Migrazione da Koofr Desktop**: si parte con una o due coppie piccole e poco importanti, `data/imports` resta per ultima. La misura delle date (punto 5.2) richiede di **leggere**, senza modificarli, i metadati di una cartella vera già sincronizzata (`rclone lsl`, sola lettura): serve il via esplicito.
5. **Remoto in caso di cancellazioni**: se le cancellazioni via API non finiscono nel cestino di Koofr, va bene una cartella di backup nascosta su Koofr che occupa spazio, con pulizia dopo 30 giorni?
6. **Emblema "sincronizzato"**: spunta discreta su tutto, o nessun emblema quando è tutto a posto (più pulito, meno rassicurante)?
7. **Cestino locale `SyncTrash/` di Koofr Desktop** (8,4 GB in `data/work`): escluso dalla sincronizzazione e lasciato lì da svuotare a mano, va bene?

Da verificare con prove, senza decisioni:

8. Comportamento di `--compare size,modtime,checksum --slow-hash-sync-only` con Koofr; uso di `--backup-dir1` durante il resync.
9. Tempo per elencare `data/work` su Koofr (solo lettura, rientra nel via del punto 4).
10. Formato dell'URL per "Apri su Koofr".
11. Pulsanti delle notifiche in Noctalia; icone di stato del tray in Noctalia e nell'estensione AppIndicator.
12. Emblemi con nomi esportati dal Flatpak al posto delle icone copiate in `~/.local/share/icons` (eviterebbe una copia).

Cosa serve da parte tua, alla fine della fase 3: creare una password per applicazioni di Koofr dedicata alle prove e decidere il nome della cartella di test sul cloud.

## Appendice A. Verifiche fatte

Comandi eseguiti su questa macchina (Lucciola 44, sessione niri), tutti in sola lettura:

| Cosa | Comando | Esito |
|---|---|---|
| Versione rclone | `rclone version` | v1.74.3 (l'ultima è la 1.75.2 del 2026-10-09) |
| Pacchetti Nautilus | `rpm -q nautilus nautilus-python` | nautilus-50.3, nautilus-python-4.1.0 |
| Origine di nautilus-python | `rpm -q --whatrequires nautilus-python` | solo `nautilus-gsconnect` |
| Emblemi nel tema | `find /usr/share/icons -iname 'emblem-*'` | Adwaita 50: solo `important` e `system` simbolici; niente `synchronizing-symbolic` |
| Runtime | `flatpak run --command=sh org.gnome.Platform//50 ...` | Python 3.13.15, libsecret 0.21.8 con typelib, nessun libportal |
| Estensione Go | `flatpak remote-ls flathub` | `Sdk.Extension.golang` 25.08 con Go 1.27.1 |
| Coppie di Koofr Desktop | lettura di `~/.koofr/config.json` con segreti oscurati | 16 coppie, 1 in pausa, `useTrash: true` |
| Dimensioni | `find` e `du` sulle cartelle | tabella del punto 2 |
| Limite inotify | `/proc/sys/fs/inotify/max_user_watches` | 271.718 |
| Estensione AppIndicator | `git grep` in `margine-image` | attiva nei default GNOME |
| inotify con permesso statico | `GFileMonitor` dentro una sandbox di prova con `--filesystem=<dir>` | eventi ricevuti, percorsi identici all'host |
| Portale Background sotto niri | `busctl` su `GetAppState` e `SetStatus` | `RequestBackground` ok, `SetStatus` fallisce |
| Portale Secret sotto niri | `busctl --user list`, `niri-portals.conf` | gnome-keyring attivo, portale configurato |
| Tray da Flatpak | `busctl` sul Watcher | EasyEffects (Flatpak, solo `--talk-name` del Watcher) registrato come `:1.78/StatusNotifierItem` |
| Rete e risparmio energetico | portale NetworkMonitor | risponde anche sotto niri |

Nessun comando ha toccato il cloud Koofr, `~/.config/rclone` o le cartelle sincronizzate (solo lettura di nomi e dimensioni).

## Appendice B. Fonti principali

- rclone bisync: https://rclone.org/bisync/ ; changelog https://rclone.org/changelog/ ; PR #10015, issue #9793, #9824, #9248; segnalazione forum di marzo 2026 sul remote svuotato.
- Backend Koofr: https://rclone.org/koofr/ e la tabella https://rclone.org/overview/ ; aiuto Koofr su nomi, cestino e versioni (koofr.eu/help).
- Nautilus 50.3: MR !870 (ritorno degli emblemi), !1490, !1983; nautilus-python issue #19, #20, MR !14 e !18.
- Flatpak e portali: docs.flatpak.org (conventions, sandbox-permissions, desktop-integration); xdg-desktop-portal 1.22.1 (`background.c`, issue #567); gnome-shell 50 `backgroundApps.js`; libsecret `secret-backend.c`.
- Esempi: Nextcloud `syncstate.py`, nautilus-dropbox, MEGA shell extension, Celeste, Rclone Shuttle (manifest Flathub).
