# Spola: documento di progetto

Stato: **approvato** il 2026-10-10 (fase 1). Data: 2026-10-10. Nome: **Spola** (scelto il 2026-10-10: "fare la spola", e la spola del telaio che porta il filo da un lato all'altro). App ID `io.github.daniel_g_carrasco.spola`. Koofr compare solo nella descrizione ("Sincronizza le tue cartelle con Koofr", "Non affiliato a Koofr").

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
4. La radice non è una cartella pericolosa: mai la home stessa, `/`, una cartella di sistema o una che contiene la home; mai dentro o sopra `~/.var`, `~/.local`, `~/.config`, `~/.ssh`, `~/.gnupg`. Con l'accesso a tutta la home (punto 11) è l'unica barriera contro un errore di percorso, quindi è coperta da test.
5. Si registra l'identità della radice: dispositivo e inode della cartella locale, ID della coppia.

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

**Limite alle cancellazioni.** Il default di bisync è 50% per lato [V], troppo largo per `data/work` (vorrebbe dire 30.000 file). Deciso: 10% per lato. Quando il limite scatta, l'app mostra l'elenco delle cancellazioni ottenuto con `--dry-run` e solo dopo una conferma esplicita ripete il giro con `--force`. Il percentuale da solo non basta per le cartelle piccole (in `templates` un solo file è il 33%): sotto le 50 voci il limite si applica in valore assoluto (soglia esatta da tarare con le prove).

**Dove finisce ciò che viene cancellato o sovrascritto:**

- Lato locale: `--backup-dir1` in una cartella di backup dell'app sullo stesso filesystem (spostamento istantaneo su btrfs). Pulizia automatica dopo 30 giorni, con dimensione visibile nelle preferenze. Per coppie su dischi esterni il backup va sullo stesso disco.
- Lato Koofr: Koofr ha il cestino (7 giorni gratis, 30 a pagamento) e le versioni precedenti (120 giorni gratis, 2 anni a pagamento, esclusi i file oltre 5 GB) [V, documentazione Koofr]. **Non è verificato che le cancellazioni fatte tramite API finiscano nel cestino**: va provato sulla cartella di test. Se non ci finiscono, si attiva `--backup-dir2` in una cartella nascosta su Koofr, a costo di spazio.

**Interruzioni.** Chiusura dell'app, logout o pausa mandano SIGINT: rclone smette di accodare, aspetta fino a 30 secondi i trasferimenti in corso e salva lo stato [V]. Un secondo segnale o la sospensione del computer a metà giro si recuperano con `--recover`. Sotto niri non si può impedire la sospensione (il portale Inhibit sotto niri gestisce solo l'inattività) [V], per questo ogni giro deve essere riprendibile; sotto GNOME l'app chiede di non sospendere durante un giro attivo.

**Riparazione.** Mai `--resync` automatico. "Ripara" esegue `--resync --dry-run`, mostra il riepilogo, chiede conferma, poi esegue il resync vero. Esiste anche "Verifica" (`--check-sync only`), che confronta gli elenchi senza trasferire nulla.

### 5.4 Quando parte un giro

- **Modifiche locali**: monitor inotify ricorsivo (un `Gio.FileMonitor` per cartella, aggiornato quando nascono o spariscono cartelle). Debounce: il giro parte dopo 10 secondi senza nuove modifiche, o al massimo dopo 2 minuti di modifiche continue. Le modifiche fatte da rclone stesso durante il giro vengono ignorate.
- **Modifiche remote** (aggiornato il 2026-10-10, vedi Appendice D): Spola interroga ogni minuto l'elenco delle attività di Koofr (`/api/v2.1/user/activity`) e sincronizza solo le coppie toccate; i giri fatti da Spola stessa si riconoscono dallo user agent di rclone, che contiene un identificativo dell'installazione. Con il feed funzionante il giro completo di sicurezza è ogni 60 minuti; se il feed non risponde si torna da soli al giro ogni 10 minuti. Provato su Koofr: un file caricato da fuori è arrivato in 56 secondi, i caricamenti di Spola non fanno ripartire giri.
- **Modifiche remote, prima versione del progetto**: rclone non ha un canale di notifiche per Koofr e il backend non ha elenchi ricorsivi rapidi (`ListR` assente) [V]. Si fa un giro periodico, proposta ogni 10 minuti, più uno all'avvio, al ritorno della rete e alla ripresa dalla sospensione. Ogni giro elenca entrambi i lati per intero: va misurato quanto costa elencare `data/work` su Koofr (domanda aperta 3).
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

Menu del tray: in cima lo stato e l'avanzamento (le stesse righe del suggerimento al passaggio del mouse), poi i sottomenu dei file in trasferimento e dei sincronizzati di recente, "Sincronizza ora", "Metti in pausa"/"Riprendi", "Apri Spola", "Esci".

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

**Cosa implica, provato** (sandbox del runtime GNOME 50 con `--filesystem=home`): l'app può leggere e scrivere tutta la home, compresi `~/.ssh`, `~/.gnupg`, `~/.local/share/keyrings` (file cifrati), `~/.koofr` con i token di Koofr Desktop e `~/.var/app` con i dati delle altre app Flatpak. Restano protetti `~/.local/share/flatpak` (vuota vista da dentro), tutto ciò che sta fuori dalla home (`/run/media` e `/mnt` non ci sono senza permesso apposito), i programmi dell'host (niente `flatpak-spawn --host`) e il bus di sessione, filtrato. Per i file, quindi, la sandbox non protegge più; protegge ancora per il resto. Rispetto a oggi è comunque un passo avanti: Koofr Desktop gira fuori da ogni sandbox, vede tutto il disco e parla liberamente col bus. Contromisure: radici pericolose vietate (punto 5.1), percorsi sempre assoluti e normalizzati prima di arrivare a rclone, test sulla costruzione dei comandi, nessuna operazione di cancellazione fatta dall'app fuori da rclone tranne lo spostamento nel cestino delle versioni scartate nei conflitti.

Deciso il 2026-10-10: `--filesystem=home`, con la possibilità di restringere a mano dopo.

## 12. Repository, test, distribuzione

Struttura, sul modello di p7m-extractor e pdf-cropper:

```
README.md, LICENSE (MIT, come le altre app e come rclone)
src/<pacchetto>/          codice dell'app
nautilus/                 estensione per Nautilus
data/                     .desktop, metainfo, icone app, icone di stato, emblemi, servizio D-Bus
po/                       traduzioni (sorgenti in inglese, italiano completo)
tests/                    test della logica e d'integrazione
tools/                    compile_po.py, flatpak_repo_files.py, make_icons.py
build-aux/flatpak/        manifest (rclone dai sorgenti + app)
site/                     sito del progetto (Cloudflare Pages, come pdf-cropper)
.github/workflows/        build.yml (test, validazione, flatpak), flatpak-repo.yml (repository su Pages)
docs/                     questo documento e le note tecniche
```

**Test automatici:**

- Logica pura: costruzione dei comandi (le opzioni vietate non compaiono mai, `--max-delete` c'è sempre, la password non è mai negli argomenti), lettura del log JSON da registrazioni reali, lettura degli elenchi, aggregazione degli stati, controlli di `preflight` (sovrapposizioni, nomi con emoji, collisioni di maiuscole), debounce del pianificatore.
- Integrazione senza rete, in CI: bisync tra due cartelle locali con il rclone del progetto, scenari scritti apposta (modifica da un lato, cancellazione, conflitto, cancellazione di massa che deve fermarsi, disco "non montato", interruzione a metà con SIGINT, cambio dei filtri).
- Tray e D-Bus: un bus di sessione privato (`dbus-run-session`) per contare i segnali e provare l'interfaccia.
- Prova vera su Koofr, a mano: una cartella dedicata sul cloud (per esempio `/spola-test`) e una locale (`~/scratch/spola-test`), sempre prima in `--dry-run`.

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

Risposte del 2026-10-10: **sì a tutte** (la 1 dopo la spiegazione delle implicazioni, ora nel punto 11). Restano qui come promemoria di cosa è stato deciso.

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

## Appendice C. Fase 3 (MVP): cosa è verificato

Prove automatiche con rclone 1.74.3 tra due cartelle locali (`tests/test_bisync_integration.py`, `tests/test_engine.py`), quindi valide per la logica di bisync e di Spola ma non ancora per il backend Koofr:

- il primo allineamento in modalità `newer` copia ciò che manca da ciascuna parte, fa vincere la versione più recente e mette quella locale sostituita in `--backup-dir1` (la domanda 8 è confermata per il lato locale);
- i conflitti conservano entrambe le versioni con l'estensione al suo posto (`doc.conflict1.odt`, `doc.conflict2.odt`) grazie a `--suffix-keep-extension`;
- un file cancellato su Koofr finisce nel backup locale, in una cartella per ogni giro (un file sostituito due volte conserva entrambe le copie);
- cancellare il 25% dei file ferma il giro senza toccare nulla; i giri automatici restano fermi finché l'utente non conferma l'anteprima, poi il giro con `--force` applica le cancellazioni;
- bisync ha anche una protezione sua quando cambiano **tutti** i file di un lato ("all files changed"): Spola la tratta come il limite alle cancellazioni;
- un lato svuotato produce un errore critico (codice 7, "Riparazione necessaria"), mai una cancellazione dall'altra parte;
- una cartella sostituita (eliminata e ricreata, come un disco non montato) viene fermata da Spola prima di avviare rclone;
- due giri della stessa coppia non partono mai insieme (lock nostro);
- un giro interrotto con SIGINT si riprende al giro successivo senza riparazione;
- l'icona del tray non emette `NewIcon` a riposo e riduce 40 cambi rapidi a pochi segnali (`tests/test_tray.py`, bus D-Bus privato).

Misura su Koofr vero, in sola lettura (2026-10-10, `data/technology`, cartella sincronizzata da Koofr Desktop): 287 file su entrambi i lati, nessuna differenza di dimensione né di MD5, date di modifica uguali entro 1 ms su tutti i file. Koofr Desktop conserva quindi le date, e il primo allineamento di una cartella già sincronizzata non dovrebbe ricaricare nulla. L'elenco completo su Koofr ha richiesto 2,0 secondi per 112 cartelle: senza elenco ricorsivo rapido il tempo cresce con il numero di cartelle, quindi va misurato su `data/work` (8.308 cartelle) prima di fissare l'intervallo del controllo remoto.

Prove su Koofr vero, cartella di test `/spola-test` ⇄ `~/scratch/spola-test` (2026-10-10, rclone 1.74.3, riga di comando di Spola):

- accesso con password per applicazioni, verificato con la quota; password nel portachiavi;
- primo allineamento con anteprima: 23 file caricati, 2 scaricati, la versione locale più vecchia di `comune.txt` sostituita da quella su Koofr e conservata nel backup; poi `rclone check`: 0 differenze;
- giro normale con modifiche da entrambe le parti, una cancellazione locale propagata; 0 differenze;
- conflitto: restano entrambe le versioni su entrambi i lati (`appunti.conflict1.txt` locale, `appunti.conflict2.txt` di Koofr);
- 10 file su 30 cancellati da Koofr: il giro si ferma (limite al 17% per le cartelle piccole), nessun file locale toccato; dopo l'anteprima e la conferma i 10 file locali vanno nel backup;
- **un file con un'emoji nel nome, creato dopo l'aggiunta della coppia, faceva fallire tutto il giro**: Koofr risponde 400, rclone non applica nemmeno le cancellazioni di quel giro ("not deleting files as there were IO errors") e bisync dichiara un errore critico. Correzione: prima di ogni giro Spola cerca i nomi che Koofr rifiuta (emoji, percorsi oltre 1023 caratteri, nomi che differiscono solo per maiuscole) e li esclude da quel giro con `--exclude-from`, un file separato da `--filters-file` perché bisync non pretenda un nuovo allineamento; nella finestra la coppia mostra "Alcuni file non si possono sincronizzare". Verificato su Koofr: il giro riesce, il resto passa, il file resta solo in locale. Costo della ricerca: 0,25 s per le 50.000 voci di `data/work`. Nelle 16 cartelle di Koofr Desktop c'è un solo caso, una collisione di maiuscole in `data/imports`;
- **interruzione del primo giro dopo un allineamento**: circa una volta su sette il giro successivo chiedeva una riparazione, perché dopo un resync bisync non ha ancora la copia `.lst-old` su cui si basa `--recover`. Correzione: ogni allineamento e ogni riparazione sono seguiti subito da un giro normale che la crea. 32 interruzioni su 32 si sono poi riprese da sole;
- dopo un errore critico `--recover` torna a uno stato di due giri prima: le cancellazioni già confermate risultano di nuovo da confermare. Nessuna perdita di dati, solo una conferma ripetuta.

Misura dell'elenco di `data/work` su Koofr (sola lettura): 49.876 voci in 5.610 cartelle, 95 s con 8 richieste parallele (il default di rclone), 49 s con 16. Ogni giro di bisync elenca tutto il lato Koofr: per le cartelle grandi conviene alzare `--checkers` e non scendere sotto i 10 minuti tra un controllo e l'altro, salvo un canale di notifiche dal lato Koofr (lo studio di Koofr Desktop è in corso).

Da verificare con Koofr vero, sulla cartella di test: accesso con password per applicazioni, date di modifica e hash MD5 sul lato Koofr, cestino di Koofr per le cancellazioni fatte da rclone, formato del link "Apri su Koofr" (oggi `https://app.koofr.net/app/storage/?path=...`, da controllare), tempi di elenco; icona del tray in Noctalia e nell'estensione AppIndicator con l'app avviata dai sorgenti.

## Appendice D. Studio di Koofr Desktop (2026-10-10)

Koofr Desktop è chiuso: lo studio è stato fatto in sola lettura, a fini di interoperabilità, su ciò che si vede senza eseguirlo (informazioni di build e simboli dei binari Go, tipi DWARF, copie dei suoi database SQLite, log, `/proc`, sorgenti pubblici di Koofr su GitHub). Nessuna chiamata di rete a Koofr.

**Come funziona.** Un processo `storagegui` (interfaccia web locale su `127.0.0.1:27001`, tray) avvia un processo `storagesync` per coppia. Modifiche locali: inotify su ogni cartella, eventi raggruppati ogni secondo, scansione completa ogni ora, dopo una sospensione (lo capisce da un salto dell'orologio controllato ogni 60 s) e al ritorno della rete. Modifiche remote: un **websocket di eventi** (`wss://…/events`, eventi `fileCreated`, `fileRemoved`, `fileMoved`, `fileCopied` per cartella registrata) più la scansione oraria; quando elenca il remoto usa un endpoint privato **`/content/api/v2/mounts/{id}/files/listrecursive`** (JSON a righe, in streaming): circa 40 s per `data/work`, contro i 95 s di rclone con 8 richieste parallele e i 49 s con 16. Stato per file in SQLite: percorso, mtime locale e remota, MD5, dimensione, inode.

**Cosa fa meglio di Spola, oggi:** latenza sulle modifiche remote (evento quasi immediato invece del controllo periodico); caricamenti e cancellazioni condizionati (riscrive solo se il file remoto è ancora quello atteso), dove rclone invece sovrascrive senza condizioni (rischio basso, perché Koofr conserva le versioni precedenti); nomi dei conflitti leggibili (`nome (host's conflicted copy 2026-07-03).ext`); rilevamento della sospensione dal salto dell'orologio, che funziona anche nella sandbox e sotto niri.

**Cosa fa peggio:** **nessuna protezione dalle cancellazioni di massa** (nei log 2.035 cancellazioni locali in un'ora su `data/work` il 2026-10-07, senza nessuna conferma; protegge solo `SyncTrash`, 30 giorni); controllo della radice solo sull'esistenza (una cartella vuota al posto di un disco non montato sembrerebbe una cancellazione totale); un errore di rete letto come "cartella remota inesistente", con processi che escono e si riavviano in ciclo (158 volte); un crash dell'interfaccia ferma tutte le sincronizzazioni; il websocket cade spesso (6.039 riconnessioni) e circa due terzi delle volte non segue un controllo di recupero; segue i collegamenti simbolici di cartella dentro la radice, duplicandone il contenuto; il cestino `SyncTrash` sta dentro la cartella sincronizzata ed è osservato da inotify (2.700 osservazioni in più su `data/work`); icona del tray reimpostata ogni secondo (confermato il difetto aggirato in Margine); server di debug Go **pprof aperto su `127.0.0.1:6060`**, da cui qualunque programma locale può scaricare la memoria del processo, token compresi; Go 1.18 e librerie di rete del 2021.

**Dati utili per la migrazione:** nei database di Koofr Desktop 104.805 file su 104.807 hanno la stessa data di modifica sui due lati al millisecondo, e tutti hanno l'MD5: il primo allineamento non dovrebbe ricaricare nulla (conferma la misura su `data/technology`, va comunque verificato con l'anteprima). Nelle cartelle ci sono 85 copie di conflitto di Koofr (`… conflicted copy …`) da far vedere prima della migrazione, e in ogni radice una cartella `.cache/` vuota di Koofr. In `data/projects` 41.912 voci su circa 42.400 sono dentro due cartelle `node_modules`.

**Esclusioni predefinite di Koofr** (24 regole): `desktop.ini`, `Thumbs.db`, `.DS_Store`, `Icon\r`, `~$*`, `.~*`, `~*.tmp`, `*.part`, `.part-*`, `*.crdownload`, `*.lnk`, `*.ffs_tmp`, `*.syncState`, `$RECYCLE.BIN/`, `System Volume Information/`, `.Spotlight-V100/`, `.fseventsd/`, `.Trashes/`, `SyncTrash/`, `.cache/`, `.syncignore` e varianti.

**Canali remoti pubblici o semipubblici:** `GET /api/v2.1/user/activity?sinceTime=…&mountId=…` (usato dal progetto terzo koofr-watch con autenticazione Basic), non documentato; `GET /api/v2/mounts/{id}/files/tree` nel client Go pubblico di Koofr. Nessuno dei due è usato da rclone. Da provare se accettano la password per applicazioni.

**Verifiche sulle API di Koofr con la password per applicazioni (2026-10-10, sola lettura):**

- **Cestino:** `GET /api/v2/trash` (paginato con `cursor`, `retentionDays: 30` su questo account) contiene tutte le 13 cancellazioni fatte da rclone nella cartella di test, ciascuna con un `versionId`. Le cancellazioni di Spola su Koofr sono quindi recuperabili dal cestino per il periodo del piano: `--backup-dir2` non serve.
- **Attività:** `GET /api/v2.1/user/activity?limit=…&sinceTime=…&mountId=…` risponde in 0,2 s e riporta ogni caricamento, cancellazione, spostamento e creazione di cartella con percorso, orario e `user_agent` (rclone si presenta come `rclone/v1.74.3`; con `--user-agent` Spola può avere un'identità propria e riconoscere i propri giri). È il candidato per sapere quali coppie sono cambiate su Koofr senza elencarle tutte, con un giro completo di sicurezza a intervalli lunghi. L'API non è documentata: va sempre affiancata dal giro completo periodico.

## Appendice E. Interfaccia dopo le prove di Daniel (2026-10-10)

Dalle prove sulla finestra vera: la riga della coppia che segnala conflitti o file lasciati fuori si apre con un clic (riga, pulsante "Controlla…" o notifica) sulla vista corrispondente; i conflitti si risolvono da lì (tieni la versione di questo computer, quella di Koofr, o entrambe rinominate; la scartata va nel cestino locale e, al giro dopo, nel cestino di Koofr; mai sovrascritto un file esistente; rifiutato durante un giro in corso). Il riepilogo dell'ultimo giro è nella riga ("3 file su Koofr, 1 eliminato"), con una barra di avanzamento durante i giri e un toast di risposta a "Sincronizza ora". Finestra **Preferenze**: pausa su rete a consumo e in risparmio energetico, avvio all'accesso (portale Background nel Flatpak, file in `~/.config/autostart` fuori), giorni di conservazione dei backup, lingua dei nomi delle copie in conflitto (le copie si riconoscono comunque in tutte le lingue). **Esclusioni** per coppia dal menu della riga: testo in sintassi rclone, con riallineamento mostrato in anteprima quando cambiano. La finestra ricorda le proprie dimensioni.

Difetto trovato dai test di questa parte: un'anteprima (giro di prova) azzerava lo stato "da riparare" o "da confermare" della coppia, e i giri automatici sarebbero ripartiti; corretto, l'anteprima non decide nulla.

Della fase 5 resta solo il limite di banda per coppia.

## Appendice F. Migrazione da Koofr Desktop (2026-10-10)

Koofr Desktop fermato (`systemctl --user stop koofr.service`) e il suo avvio automatico messo da parte (`~/.config/autostart/koofr.desktop.disabled-by-spola`); `ujust install-koofr launchers` lo ricreerebbe. Nuovo comando `spola import-koofr-desktop`: legge `~/.koofr/config.json` e crea le coppie con gli stessi percorsi, in pausa se lo erano, da allineare; rifiuta di partire se Koofr Desktop gira o è impostato per avviarsi all'accesso (il controllo sulle cartelle di Koofr Desktop vale solo in quel caso). Importate 16 coppie. Anteprime del primo allineamento: 14 coppie su 15 "niente da fare" (conferma che Koofr Desktop conserva date e contenuti), allineate in pochi secondi l'una senza trasferimenti.

**`data/work`**: l'anteprima voleva scaricare 49 file da Koofr, tutti sotto `freelance-admin/00-index/`, che in locale è una cartella di soli collegamenti simbolici verso altre cartelle (`../invoices`, `../clients/mcm/invoicing`, `../business-profile`, `../business-registration`, più alcune verso `data/personal`). Koofr Desktop seguiva i collegamenti interni alla radice e aveva caricato su Koofr copie duplicate di quelle cartelle; Spola (`--skip-links`) non li segue, quindi su Koofr i duplicati risultano file mancanti in locale. Scaricarli scriverebbe attraverso i collegamenti sopra i file veri: allineamento **non confermato**, in attesa di decidere se cancellare i duplicati su Koofr (vanno nel cestino di Koofr) o escludere `freelance-admin/00-index/**`. Lezione generale per la migrazione: prima del primo allineamento cercare i collegamenti simbolici a cartelle dentro la radice e confrontarli con ciò che Koofr Desktop ha caricato.

## Appendice G. Fase 4: Nautilus (2026-10-10)

Realizzata come previsto al punto 8. Nel processo di Spola: `states.py` tiene per coppia l'elenco dell'ultimo giro (sincronizzato) più gli insiemi piccoli di ciò che sta succedendo (in attesa dal monitor, in trasferimento dai messaggi di coda di rclone, errori, conflitti, nomi rifiutati); una cartella prende lo stato più grave trovato sotto di sé guardando solo quegli insiemi, quindi costa nulla anche con 50.000 voci. `dbus_service.py` espone `io.github.daniel_g_carrasco.spola.Sync1`: `GetRoots`, `GetFolderStates` (una chiamata per cartella, stati di tutti i figli), `GetStates`, `SyncNow`, `GetWebUrl`, `CreateShareLink` (rclone link in un thread), `ShowWindow`; segnali `StatesChanged` raggruppati ogni 300 ms e `RootChanged` a fine giro.

L'estensione (`nautilus/spola-nautilus.py`, un solo file) risponde a `update_file_info` solo dalla cache e ritorna subito, mai `IN_PROGRESS`; alla prima assenza chiede la cartella intera in modo asincrono e poi invalida i soli file che Nautilus ha (`FileInfo.lookup_for_uri`); cache a stringhe limitata a 400 cartelle; con Spola spenta non fa nulla. Emblemi propri `emblem-spola-<stato>-symbolic` (il tema non ne ha), un solo emblema per file, con le classi di colore `success`, `warning`, `error`. Menu: "Sincronizza ora con Koofr", "Apri su Koofr", "Copia il link di condivisione Koofr" (solo su un elemento). L'installazione la fa Spola dalle Preferenze ("Mostra lo stato di sincronizzazione in File"): copia estensione, emblemi e catalogo delle traduzioni in `~/.local/share`, e all'avvio tiene aggiornata la copia; mai nell'immagine di Margine, perché nautilus-python 4.1.0 caricherebbe due volte un'estensione presente in due cartelle. Banco di prova `tools/nautilus_smoke.sh`: Nautilus vero su display Broadway con bus privato e home temporanea, estensione che registra su file cosa fa.

Gotcha confermato di nautilus-python 4.1.0: costruisce il percorso di Python dal primo `python3` nel PATH; con un virtualenv davanti (uv, venv) nessuna estensione si carica ("nautilus_python_init_python failed"). Corretto in nautilus-python 4.2.0; nel banco di prova il PATH è ripulito.

Proposta per `margine-image`: ramo locale `feat/nautilus-python` (commit 7cc9742), che dichiara `nautilus-python` in `gnome_tools` e lo aggiunge ai pacchetti che `15-devstack` reinstalla se mancano; oggi è nell'immagine solo come dipendenza di gsconnect. Da aprire come PR dopo il via.

**Esito per `work` (2026-10-10, sera):** i 49 duplicati su Koofr sono stati confrontati hash per hash con gli originali indicati dai collegamenti (`rclone check --one-way`, 0 differenze in tutte e quattro le cartelle) e poi cancellati (cestino di Koofr, 30 giorni); `00-index` su Koofr è rimasta vuota. La nuova anteprima del primo allineamento era "niente da fare" e la coppia è stata allineata. Tutte le 16 coppie di Koofr Desktop sono ora in Spola: 15 attive e allineate, `imports` in pausa com'era. La PR per `margine-image` è la #474.

## Appendice H. Fasi 5, 6 e 7 (2026-10-10, sera)

**Fase 5, chiusa:** limiti di velocità in caricamento e scaricamento nelle Preferenze (KB/s, passati a rclone come `--bwlimit up:down`). Conflitti, esclusioni, pausa e coppie multiple erano già fatti.

**Fase 6, pronta in locale, in attesa del repository su GitHub:** manifest `build-aux/flatpak/io.github.daniel_g_carrasco.spola.yaml` (runtime GNOME 50, rclone 1.75.2 compilato dagli archivi ufficiali sorgente e vendor con somme di controllo fisse, Go dall'estensione `golang//25.08`, `CGO_ENABLED=0`; permessi come al punto 11 più `/media`). Compilato in locale con flatpak-builder: Go 1.27.1, `rclone version` e `spola --version` funzionano nella sandbox, bundle da 19,7 MB; le traduzioni finiscono nell'estensione `.Locale`, che il repository installa da solo. Workflow `build.yml` (test con rclone scaricato, validazioni, Flatpak, release con bundle sui tag) e `flatpak-repo.yml` (repository firmato su GitHub Pages a ogni tag, copia di quello delle altre app con l'estensione Go in più). Chiave GPG dedicata RSA 4096 "Spola Flatpak repository <code@danielgrasso.com>", impronta `DFE782B97EA0ED33B6A2C0F2BFB2EC997FDBC5B1`, privata in `~/.config/spola-flatpak/private-key.asc` (600): se si perde, i client installati rifiutano gli aggiornamenti. Sito in `site/` per Cloudflare Pages (`spola.neistar.com`, progetto da creare dal pannello). Da fare al via: repository GitHub, segreto `FLATPAK_GPG_PRIVATE_KEY` e variabile `FLATPAK_GPG_KEY_ID`, Pages dal ramo `gh-pages`, tag `v0.1.0`.

**Fase 7, pronta in locale:** ramo `feat/ujust-spola` in `margine-image` con `ujust install-spola [install|migrate|remove]` (`migrate` ferma Koofr Desktop, rileva le coppie con `spola import-koofr-desktop`, riavvia Spola; l'allineamento resta nella finestra, con anteprima). Da aprire come PR quando il repository Flatpak è pubblicato, perché la ricetta installa da lì.

Incidente da ricordare: le sessioni D-Bus private dei banchi di prova avviavano un secondo portale documenti che montava `/run/user/1000/doc` sopra quello della sessione vera; dopo la fine della prova le app Flatpak non partivano più ("bwrap: Can't find source path .../doc/by-app"). Rimedio: `systemctl --user restart xdg-document-portal.service`; prevenzione: i banchi di prova usano un `XDG_RUNTIME_DIR` privato.

## Appendice I. Pubblicazione (2026-10-10, sera)

- Repository pubblico https://github.com/daniel-g-carrasco/spola (25 commit locali caricati, poi le correzioni del CI).
- Repository Flatpak firmato su GitHub Pages: https://daniel-g-carrasco.github.io/spola/spola.flatpakref, pubblicato dal workflow al tag `v0.1.0`; installazione verificata da un client (firma accettata, estensione `.Locale`, `rclone` e `spola` funzionanti nella sandbox). Flatpak scarica solo le lingue configurate: con la sessione in inglese il catalogo italiano non c'è (`flatpak config --user --set languages "en;it"`).
- GitHub Pages attivato dal ramo `gh-pages`. Sito: progetto Cloudflare Pages da creare dal pannello (cartella `site`), dominio `spola.neistar.com`.
- PR per Margine: #474 (nautilus-python dichiarato) unita; #477 (`ujust install-spola`) aperta con i check verdi.
- Il CI ha trovato un difetto reale che su btrfs non si vede: su ext4 una cartella cancellata e ricreata può riavere lo stesso inode, e il controllo d'identità della radice basato su dispositivo e inode non la riconosceva. Ora ogni radice sincronizzata contiene un file nascosto `.spola-root` con l'ID della coppia (escluso dalla sincronizzazione dalla regola `.spola-*`); se manca, la coppia si ferma. Alle coppie di prima il marcatore viene scritto al primo controllo, solo se dispositivo e inode coincidono e almeno una voce dell'ultimo elenco è ancora al suo posto. La chiave della firma, l'impronta e la posizione del privato sono nell'Appendice H.

Sul laptop Spola gira ancora dai sorgenti (`~/dev/spola`, unità `spola.service` e avvio automatico): il passaggio al Flatpak richiede di copiare configurazione e stato in `~/.var/app/io.github.daniel_g_carrasco.spola/` e un nuovo accesso (il portachiavi del Flatpak è separato); da fare con calma, non è urgente.

## Appendice J. Versione 0.2: finestra, aggiunta delle cartelle, Flatpak sul laptop (2026-10-11)

- Finestra secondo le linee guida GNOME: navigazione a pagine; in cima una scheda con lo stato complessivo, ad altezza fissa (la barra di avanzamento ha sempre il suo spazio, i testi stanno su una riga) perché la lista sotto non salti; cartelle raggruppate per cartella madre, e una cartella da sola sale nel gruppo superiore con il percorso relativo (`photos/camera-roll` sotto `~/data/media`); righe tutte uguali; una pagina per cartella; l'account passa nelle preferenze.
- Aggiungi cartella: suggerimenti (stesso percorso relativo alla home, stesso nome nei primi due livelli di Koofr) e navigazione delle cartelle con `GET /api/v2/mounts/{id}/files/list`, creazione con `POST /api/v2/mounts/{id}/files/folder`. bisync rifiuta una cartella Koofr che non esiste (`Bisync critical error: directory not found`): una cartella suggerita che manca si crea solo al clic su Aggiungi, dopo tutti i controlli. I confronti ignorano maiuscole e minuscole, come Koofr.
- Tray: sottomenu dei file in trasferimento e dei cinque sincronizzati di recente (in `recent.json` nella cartella di stato), "_" raddoppiato nelle etichette (in dbusmenu è un mnemonico: l'estensione AppIndicator lo toglie), aggiornamenti del menu limitati. Colori: neutro quando è tutto aggiornato o in pausa, azzurro durante la sincronizzazione, arancio quando c'è da guardare, rosso sugli errori. Sono icone a colori senza suffisso `-symbolic`: le classi simboliche di GTK e di GNOME Shell hanno verde, arancio e rosso ma non l'azzurro. Le frecce hanno punte isosceli: la punta sta sulla tangente alla fine dell'arco, non sul suo cerchio.
- Difetti emersi con il passaggio al Flatpak sul laptop: l'estensione di Nautilus finiva nella cartella dati della sandbox, dove File non guarda; dopo un riavvio di Spola File restava senza emblemi, perché l'applicazione prende il nome sul bus un attimo prima che esista l'oggetto del servizio (ora il servizio annuncia "radici cambiate" appena è pronto, e l'estensione riprova e rinfresca i file già mostrati); una coppia in pausa mai allineata risultava "da allineare", e una sola coppia in pausa faceva dire "in pausa" a tutto il tray.
- Migrazione del laptop al Flatpak: configurazione, stato (226 MB di liste e log) e backup copiati in `~/.var/app/io.github.daniel_g_carrasco.spola/`, password per applicazioni passata al portachiavi del Flatpak con una pipe, vecchie cartelle rinominate `*.migrated-to-flatpak-2026-10-11` come via di ritorno. Le 16 coppie attive sono ripartite dalle liste copiate: 33 giri riusciti, nessun riallineamento, nessuna copia né cancellazione.

## Appendice K. Gli altri file manager (2026-10-12)

- Nemo e Caja sono derivati da Nautilus e ne hanno conservato l'interfaccia per le estensioni (`InfoProvider.update_file_info`, `FileInfo.lookup_for_uri`, `invalidate_extension_info`, `add_emblem`, `MenuProvider`; cambia solo la firma dei menu, che ha la finestra in più): una sola estensione Python, `extensions/spola-files.py`, copiata da Spola nella cartella di ciascuno con `FILE_MANAGER` impostato (`Nautilus`, `Nemo`, `Caja`). Le estensioni utente stanno in `~/.local/share/{nautilus,nemo,caja}-python/extensions/`. Nemo e Caja disegnano gli emblemi con i colori scritti nel file SVG, non con le classi simboliche: gli emblemi hanno ora anche i colori letterali (verde, azzurro, grigio, rosso, arancio), che GTK 4 e GNOME Shell ignorano.
- Dolphin: il menu contestuale è un service menu KIO (`~/.local/share/kio/servicemenus/`, eseguibile perché Plasma 6 non esegue quelli che non lo sono), le icone sovrapposte un plugin C++ `KOverlayIconPlugin` (`extensions/dolphin/`) che segue le stesse regole dell'estensione Python (cache, una richiesta asincrona per cartella, segnali, nuovo tentativo quando Spola compare). Va compilato contro le KDE Frameworks del sistema: né l'app né il Flatpak possono installarlo, le istruzioni sono nel README della cartella. Installato per l'utente finisce in `~/.local/lib64/plugins/kf6/overlayicon/` e serve `QT_PLUGIN_PATH`.
- Thunar non ha un'interfaccia per gli emblemi delle estensioni e `thunarx-python` non c'è in Fedora 44: solo il menu, come azioni personalizzate aggiunte a `~/.config/Thunar/uca.xml` (Thunar lo crea al primo avvio copiando i suoi esempi; Spola tocca solo un file esistente e solo le proprie azioni, `unique-id` con prefisso `spola-`).
- I menu di Dolphin e Thunar possono solo lanciare un comando: `gdbus call` con il percorso del file. Il servizio D-Bus accetta quindi anche percorsi semplici e ha due azioni complete, `OpenOnKoofr` (Spola apre il browser) e `ShowShareLink` (Spola crea il link e lo mostra con un pulsante Copia: su Wayland un'app in secondo piano non può scrivere negli appunti); fuori dalle cartelle sincronizzate risponde con una notifica.
- Quali file manager: quelli che hanno lasciato le loro cartelle (`~/.config/nemo`, `~/.config/caja`, `~/.config/dolphinrc`, `uca.xml`), Files sempre; uno installato dopo viene preparato al successivo avvio di Spola.
- Verifica: `tools/file_managers_smoke.sh` avvia ciascun file manager in un container Fedora 44 (`tools/file-managers.Containerfile`) con una home finta, Spola dai sorgenti e uno schermo X virtuale, e ne salva le schermate; per Dolphin compila e installa il plugin come dice il README e lancia i comandi del menu. Nel container serve seccomp non confinato: i caricatori di immagini di glycin aprono una propria sandbox bwrap.

## Appendice L. Versione 0.3.1: Koofr Desktop, l'elenco dei file, l'avanzamento nel tray (2026-10-12)

- Incidente: sul laptop Koofr Desktop girava ancora accanto a Spola. `ujust install-spola migrate` l'aveva disattivato rinominando `~/.config/autostart/koofr.desktop` in `koofr.desktop.disabled-by-spola`; gnome-session legge solo i `*.desktop`, ma sotto niri le voci di avvio automatico le avvia il generatore `xdg-autostart` di systemd, che esegue qualsiasi file della cartella, qualunque nome abbia. In un paio di minuti Koofr Desktop ha caricato su Koofr i marcatori `.spola-root` di 15 cartelle e alcuni file `.partial` di rclone (vuoti), e ha cancellato su Koofr un file appena scaricato da Spola. Recupero: il file è tornato dal cestino di Koofr con `POST /api/v2/trash/undelete` e l'elenco esplicito `{"files": [{"mountId", "path"}]}` (attenzione: con l'elenco vuoto Koofr ripristina tutto il cestino; il job si segue con `GET /api/v2/jobs/{id}`), i marcatori caricati sono stati cancellati, le cartelle `SyncTrash` di Koofr Desktop pure. margine-image (#498) ora sposta la voce fuori dalla cartella di avvio e ferma le unità `app-koofr*@autostart.service`.
- Guardia: prima di ogni giro, anteprime e allineamenti compresi, `jobs.prepare` chiama `preflight.check_koofr_desktop`, che rifiuta se gira un processo di Koofr Desktop (`storagegui`, `storagesync`), se nella cartella di avvio automatico dell'host c'è un file che inizia con "koofr", lancia `storagegui` e non ha `Hidden=true`, o se i log in `~/.koofr/log` sono stati scritti negli ultimi 15 minuti. Nel Flatpak la cartella dell'host si trova con `HOST_XDG_CONFIG_HOME` o `~/.config`. Koofr Desktop su altri computer non c'entra: è il normale uso da più dispositivi.
- Align e Review lasciavano la finestra ferma qualche secondo: la preparazione di un giro (controlli, nomi che Koofr rifiuta: 5 secondi su una cartella con migliaia di foto) avveniva nel thread principale. Ora avviene in un thread: il posto in coda si prende subito, con lo stato "Preparazione…", e il giro parte nel ciclo principale quando è pronto; Ferma durante la preparazione lo annulla.
- Elenco dei file sotto lo stato (`activity.RunActivity`), dagli eventi di bisync: "Queue copy to Path1/Path2" e "Queue delete" (il lato dice la direzione), le statistiche "transferring" di rclone (l'avanzamento di ciascun file), "Copied", "Deleted" e lo spostamento nel backup. Un primo allineamento non accoda nulla: le sue copie si conoscono solo dalle statistiche e da "Copied". Un "Deleted" su questo computer arriva anche quando la vecchia versione va nel backup prima di scaricare la nuova: chiude solo una cancellazione. Quattro righe fisse (la finestra non cambia altezza): prima quelle in corso, poi le ultime finite, poi quelle in attesa. Icone di Adwaita: `folder-download-symbolic`, `send-to-symbolic`, `user-trash-symbolic`, `dialog-warning-symbolic`.
- Tray: il suggerimento e le prime voci del menu dicono quale cartella si sincronizza, la percentuale, quanto resta e il tempo stimato. Se cambiano solo le etichette il menu manda `ItemsPropertiesUpdated` invece di `LayoutUpdated`, così un menu aperto non si ricostruisce, al massimo ogni 2 secondi; corretta la firma delle proprietà tolte (`a(ias)`).
- Rete a consumo: se la preferenza è attiva, un giro automatico in corso si ferma quando la rete diventa a consumo; quelli avviati a mano e quelli confermati (allineamenti, riparazioni) proseguono. Quando la rete torna normale si riparte con `sync_all`, e un giro interrotto riprende con il recupero di bisync.
- Colori di Noctalia nel Flatpak: Noctalia scrive `~/.config/gtk-4.0/gtk.css`, che importa `noctalia.css`; nel Flatpak GTK legge `$XDG_CONFIG_HOME/gtk-4.0`, dentro `~/.var/app`. Con `--filesystem=xdg-config/gtk-4.0:ro` Flatpak monta lì la cartella dell'host, in sola lettura.

## Appendice M. Versione 0.3.2: conflitti in blocco, regola automatica, nomi da rinominare (2026-10-12)

- Prima una verifica: se un file cambia da tutte e due le parti in modo identico, bisync non crea un conflitto (provato con rclone 1.75.2 e `--compare size,modtime,checksum`). I conflitti che Spola mostra sono quindi sempre due versioni diverse, e scegliere vuol dire scartarne una: per questo niente decide senza mostrare prima cosa succede, e la versione scartata va sempre nel cestino.
- Regole (`conflicts.decide`): la più recente (data di modifica; sotto i 2 secondi di differenza non decide, perché FAT e alcuni server tengono i tempi a 2 secondi), quella di questo computer, quella di Koofr, entrambe rinominate. Ogni decisione porta il suo motivo ("più recente di 25 minuti"); quello che la regola non sa decidere (stessa data, una versione sparita) resta all'utente.
- "Risolvi tutti…" nella finestra Conflitti: si sceglie la regola, sotto compare per ogni file la versione che resta e perché; il pulsante diventa rosso se qualcosa va nel cestino. `Engine.resolve_conflicts` applica le decisioni e chiede un solo giro alla fine.
- Regola automatica: generale nelle Preferenze (Chiedimelo, la più recente, entrambe) e per cartella (in più "questo computer" e "Koofr", utile per una cartella che si modifica da una parte sola). Vale solo per i conflitti nuovi, trovati dal giro appena finito, e dell'ultimo conflitto di ogni file; quelli già presenti restano a "Risolvi tutti". Una notifica dice quanti ne ha risolti; quelli rimasti arrivano con la notifica di sempre. Predefinito: chiedere.
- Nomi rifiutati (`preflight.rename_plan`): le emoji si tolgono dal nome (con il selettore di variante e il joiner che le accompagnano; un nome rimasto vuoto diventa "Senza nome"), di due nomi che differiscono solo per maiuscole quello lasciato fuori diventa "nome (2)". Ogni proposta è controllata contro i nomi della stessa cartella, maiuscole a parte, e contro le altre proposte; per un percorso troppo lungo non c'è proposta. La rinomina è solo locale (`Engine.rename_refused`), il giro dopo carica i file. Dalla finestra "File non sincronizzati" e dall'anteprima del primo allineamento, che dopo la rinomina si rifà da sola.
- Nell'anteprima un clic su un file lo mostra nel gestore di file, o apre la sua cartella su Koofr se per ora sta solo lì (il sito di Koofr non sa evidenziare un singolo file).
- Prove: le regole e i nomi proposti con test unitari; con rclone vero, un conflitto risolto dalla regola della cartella e portato su Koofr, due conflitti lasciati all'utente e poi risolti in blocco, due nomi rinominati e caricati. Le finestre nuove fotografate su Broadway con `SCENE_MORE=1` e `SPOLA_DEV_OPEN_DIALOG=resolve-all|rename`.

## Appendice N. Versione 0.3.3: gli spostamenti restano spostamenti (2026-10-13)

- Prima, riordinare una cartella voleva dire cancellare su Koofr ogni file spostato e caricarlo di nuovo. Con `--track-renames` bisync riconosce un file spostato o rinominato (stessa dimensione e stesso MD5, che Koofr dà nell'elenco) e lo sposta dall'altra parte: su Koofr con `FilesMove` sul server, qui con una rinomina. Non si usa nei primi allineamenti, che non cancellano mai: rclone lo ignorerebbe con una riga di errore. Uno spostamento conta comunque come cancellazione per il limite del 10%, quindi una riorganizzazione grossa si ferma e chiede conferma come prima.
- Provato su Koofr vero, in `/spola-test/.Trash-spola-probe` (esclusa dalla coppia di prova dalla regola `.Trash-*/**`, poi cancellata): una rinomina fatta qui diventa uno spostamento sul server con dimensione, data e MD5 identici. Una rinomina fatta su Koofr diventa uno spostamento qui, ma il giro successivo ricaricava il file: rclone, spostando il file qui, annota nel suo elenco la data di Koofr (al millisecondo) mentre il file conserva la sua (al nanosecondo), e la differenza di una frazione di millisecondo sembrava una modifica. Rimedio: `--modify-window 1ms` in tutti i giri, provato sullo stesso caso. Senza `--track-renames` il problema non c'era, perché un download imposta la data del file.
- Riconoscere gli spostamenti nel log: "Moved (server-side) to: nuovo" (oggetto: il vecchio nome) o, in anteprima, il campo `"skipped": "move to nuovo"`; bisync usa la stessa riga per rinominare le copie di un conflitto, quindi conta solo uno spostamento confermato dalla riga "Renamed from", che `--track-renames` scrive per i suoi. La direzione viene dalla fase ("Do queued copies to Path2" o "Path1"). Uno spostamento esce dalle copie e dalle cancellazioni del piano ed entra in `moved_remote` (spostato qui, poi su Koofr) o `moved_local` (il contrario).
- Nell'anteprima un gruppo "Spostati o rinominati", con il nome nuovo e quello vecchio; nella conferma dopo un arresto di sicurezza una riga spiega che gli spostamenti contano come cancellazioni ma non lo sono. Nell'elenco dei file del giro la cancellazione e la copia di uno spostamento diventano una riga sola. Gli spostamenti fatti da rclone qui non vanno presi per modifiche dell'utente: senza saperlo, il motore faceva partire un giro in più (c'è un test).
- Fixture registrate su Koofr vero (`tests/fixtures/koofr-renames*.jsonl`, `koofr-renamed-there*.jsonl`): due rinomine qui (una in una cartella nuova), un file nuovo, uno cancellato e un conflitto; poi una rinomina su Koofr. Anteprima e giro vero danno lo stesso piano.
- Costo misurato: per riconoscere una rinomina rclone calcola l'MD5 di ogni file nuovo di questo computer prima di caricarlo. Con un file nuovo da 1 GiB il giro passa da 1,8 a 2,9 secondi (tra due cartelle locali); il caricamento su Koofr dello stesso file dura minuti, quindi è circa l'1%. Le cancellazioni non costano niente in più.

## Appendice O. Lo stato e ciò che aspetta l'utente, separati (2026-10-13)

- La scheda in cima mostrava per prima la cartella che aspettava l'utente: con `imports` da allineare, "“imports” is waiting for its first alig…" (troncato) copriva le altre cartelle che intanto si sincronizzavano. Ora sono due cose distinte, come nei client di altri servizi cloud e come le linee guida GNOME usano i banner (un messaggio persistente legato alla vista, con l'azione che lo risolve): la scheda dice cosa fa Spola (sincronizzazione con avanzamento, aggiornato, in pausa), un `Adw.Banner` sotto la barra del titolo dice cosa aspetta l'utente (allineamento, conferma, riparazione, conflitti, nomi rifiutati, errore) con Rivedi o Riprova. Il banner resta visibile scorrendo e il testo va a capo invece di essere troncato.
- A riposo, se qualcosa aspetta l'utente, la scheda dice "Tutto il resto è aggiornato" e conta solo le altre cartelle, per non contraddire il banner; se aspettano tutte, "Serve una tua scelta" con i nomi. Con più cartelle in attesa, il banner ne dice il numero e Rivedi apre la più seria (riparazione, conferma, errore, conflitti, nomi, allineamento).
- `Adw.Banner` non si può estendere (è un tipo final): la classe `AttentionBanner` lo contiene.

## Appendice P. Le anteprime non bloccano più niente (2026-10-13)

- Prima: un'anteprima era un giro di rclone come gli altri, uno alla volta per tutto l'account, messo in testa alla coda. Su `imports` (643.000 voci tra le due parti) rclone impiegava 35-45 secondi solo per gli elenchi, e intanto nessun'altra cartella si sincronizzava; se un caricamento era in corso, l'anteprima aspettava la sua fine; il dialogo bloccava la finestra e Annulla lo chiudeva ma l'anteprima continuava fino in fondo, tenendo ferma la coda.
- Due corsie nel motore: `active` per i giri veri, `previewing` per un'anteprima, che legge e basta. Possono girare insieme, mai due cose sulla stessa cartella (una cartella occupata aspetta nell'altra corsia). Un'anteprima non cambia più lo stato della cartella.
- `Preview`: l'anteprima conservata per cartella, con risultato, avanzamento (le statistiche di rclone, "listed"), spazio libero su Koofr (`rclone about`, letto durante la preparazione) e chi aspetta il risultato. Chiudere il dialogo la lascia andare (e se era una revisione chiesta dall'utente arriva una notifica quando è pronta); Annulla la ferma davvero, in coda, in preparazione o in esecuzione; riaprire mostra subito il risultato se ha meno di 30 minuti e nulla è cambiato (modifiche locali, attività su Koofr, un giro della cartella, esclusioni, rinomine, conflitti risolti), con l'ora e Aggiorna se ha più di un minuto.
- Preparata in anticipo: una cartella che aspetta l'utente (primo allineamento, riparazione, conferma) ha la sua anteprima dopo un minuto di quiete, mai su rete a consumo, in risparmio energetico, in pausa; quelle preparate così si fermano se le condizioni cambiano. La riga della cartella dice già cosa farà ("189.902 file verso Koofr · 664,8 GB · spazio insufficiente su Koofr").
- Riepilogo: i byte per direzione vengono dai `"skipped": "copy"` della prova a secco, che portano la dimensione (in un primo allineamento sono esatti; in un giro normale le copie dei conflitti non compaiono). Se quanto va su Koofr supera lo spazio libero, un avviso in cima e Allinea non più evidenziato.
- Trovato misurando: un'anteprima grande bloccava la finestra fino a quasi un secondo. rclone scrive una riga per file (150.000 in pochi secondi) e ognuna arrivava al ciclo principale come un `idle_add` a sé; GLib le esegue a blocchi interi senza tornare a disegnare. Ora le righe passano da una coda smaltita a fette di 8 ms (con la fine del giro in coda, perché l'ordine resti quello di rclone), e un'anteprima manda solo le statistiche. Caso peggiore misurato con 150.000 file: da 955 a 101 ms, lo stesso livello di un allineamento vero.

## Appendice Q. "Apri Spola" che chiudeva Spola (2026-10-13)

- Spola si è chiuso due volte da solo, alle 01:02 e alle 10:20, con codice di uscita 0 e senza errori: un'uscita ordinata, cioè l'azione Esci (alle 10:20 un minuto dopo la richiesta, il tempo di fermare rclone su camera-roll). Esclusi crash, memoria, sospensione e l'aggiornamento del Flatpak, che era avvenuto poco prima ma per coincidenza: la vecchia versione restava in `.removed/` con i suoi file.
- La causa: le voci del menu del tray erano numerate per posizione, contando anche le voci dei sottomenu (file in trasferimento, file recenti). Durante un giro la posizione cambia di continuo: un file finito, la riga di avanzamento che sparisce a fine giro. Un pannello (Noctalia) che mostra il menu di un attimo prima manda, al clic, il numero vecchio; con la numerazione per posizione "Apri Spola" diventava "Esci", "Sincronizza ora" diventava "Pausa". Riprodotto con il codice vero del tray e in un test (con la vecchia numerazione registra "quit" al posto di "open").
- Rimedio (`tray.Ids`): una voce che fa qualcosa ha un nome (dato dall'app, o l'etichetta) e tiene lo stesso numero ovunque si sposti; un numero dato a una voce non va mai a un'altra, quindi un numero vecchio porta alla voce giusta o a nessuna. Le righe di solo testo tengono il numero del loro posto, perché un testo che cambia ogni secondo si aggiorni sul posto (ItemsPropertiesUpdated) e non ricostruisca il menu. I file hanno come nome coppia e percorso.

## Appendice R. Conflitti che non lo erano (2026-10-13)

- La conferma di camera-roll, dopo una riorganizzazione interrotta a metà (9.866 file, 9.140 spariti dai vecchi percorsi), contava 6.370 conflitti. bisync scrive "New or changed in both paths" per ogni file nuovo o cambiato da tutte e due le parti, poi lo confronta: 6.355 erano identici ("Files are equal! Skipping") e 15 erano cartelle per anno create dalle due parti, che non hanno seguito. Un conflitto vero è quello di cui bisync rinomina le due copie ("Renaming Path1 copy", in anteprima `"skipped": "rename"` sul file): ora il piano e l'elenco dei file contano solo quelli. Senza la correzione un giro vero avrebbe mandato una notifica di migliaia di conflitti inesistenti.
- Nello stesso log, i percorsi Koofr avevano il prefisso `koofr{kFw_7}:`: rclone marca un remoto con opzioni cambiate con un codice in base64 per URL, che può contenere `_` e `-`, e l'espressione che lo toglie accettava solo lettere e cifre. I percorsi restavano lunghi e gli spostamenti non uscivano dai caricamenti e dalle cancellazioni.
- Con le due correzioni la conferma di camera-roll dice: 2.756 spostati, 4.019 file verso Koofr (19,9 GB), 28 cancellati, nessun conflitto. I 28 sono copie vecchie di file che esistono ancora, rinominati per data o spostati in un'altra cartella sincronizzata.

## Appendice S. Un giro che sembrava fermo (2026-10-13)

- Su `data/work`, dopo una riorganizzazione, il giro è durato 22 minuti: 1.937 copie finite in 7 minuti, poi 7.468 cancellazioni su Koofr (una chiamata ciascuna, 11 minuti) e 1.331 cartelle rimaste vuote tolte una ogni mezzo secondo (10 minuti). L'avanzamento restava fermo su "1937 of 1937 files" e le righe delle cancellazioni non si chiudevano: sembrava bloccato.
- Le statistiche di rclone contano già entrambe le cose (`deletes`, `deletedDirs`) e bisync annuncia ogni cancellazione ("Queue delete"): finite le copie, l'avanzamento (`progress.py`, senza GTK) dice "Eliminazione: 2.000 di 8.148" con la barra che avanza, poi "Rimozione delle cartelle vuote: 1.331". Una riga "Removing directory", che non dice da quale parte, chiude la cancellazione della cartella da qualunque parte sia.
- Le righe dei file non si chiudevano anche per il prefisso `koofr{kFw_7}` (appendice R).

## Appendice T. Lo spazio su Koofr, solo quando conta (2026-10-13)

- Scelta: niente account e spazio fissi in cima alla finestra, che resta su cosa fa Spola e sulle cartelle. Spola legge lo spazio su Koofr (`rclone about`, in un thread) all'accesso, dopo un giro che ha caricato o cancellato qualcosa (al massimo una volta al minuto) e ogni ora; quando ne resta meno del 10% il banner dice "Lo spazio su Koofr sta finendo: restano 40 GB su 1,1 TB" con Apri Koofr. Le cartelle che aspettano l'utente hanno la precedenza nel banner. L'anteprima avverte già quando un caricamento non ci starebbe (appendice P), e il dettaglio resta nelle Preferenze, alla voce Account.

## Appendice U. Una conferma che sembrava persa (2026-10-14)

- Confermate le revisioni di camera-roll e technology, la finestra non cambiava: le due cartelle restavano "In attesa della tua conferma". Le conferme non erano perse. C'è una sola corsia per i giri veri, e personal (confermata un attimo prima) stava facendo 2.662 spostamenti su Koofr, circa 5 minuti; le due confermate aspettavano in coda e sono partite dopo, con `--force`. Il difetto era che `confirm()` cambiava lo stato senza dirlo alla finestra.
- Ora una cartella confermata che aspetta il suo turno è "Confermata · parte dopo la sincronizzazione in corso", subito, e un avviso nella finestra lo dice quando un'altra cartella sta sincronizzando. Le conferme partono nell'ordine in cui sono state date (prima ogni conferma era messa in testa, quindi l'ultima passava davanti), prima dei giri automatici. Un giro automatico della stessa cartella non si accoda più dietro a quello confermato e non cambia quello che la cartella dice (su rete a consumo diventava "In pausa" mentre il giro confermato partiva comunque). Confermare ferma l'anteprima della stessa cartella ancora in corso.
- Un giro confermato non interrompe quello in corso: fermarlo a metà è sicuro (bisync riprende), ma una riorganizzazione lasciata a metà fa più danni di qualche minuto di attesa.
- Anteprima in background: chiuso il dialogo con Continua in background, il banner dice "Preparo l'anteprima di “technology”…" con Mostra, e quando è pronta "L'anteprima di “technology” è pronta" con Controlla, finché non la si guarda. Se la finestra è davanti, al posto della notifica di sistema compare un avviso nella finestra con Controlla.

## Appendice V. Rinomine che cambiano solo le maiuscole (2026-10-14)

- Su data/media/video la riorganizzazione ha rinominato `Screencasts` in `screencasts` e spostato dentro i file di `00. Screen` e `Kooha`. L'anteprima diceva 38 eliminazioni su Koofr, comprese quelle dei file di `Screencasts`, che in locale ci sono ancora sotto `screencasts`.
- Provato nella cartella di prova su Koofr con i flag di Spola. Koofr non distingue "Screencasts" da "screencasts", e bisync pianifica sia l'eliminazione sia il caricamento degli stessi file. Nel giro vero non fa né l'una né l'altro, quindi non si perde niente. Però gli spostamenti verso `screencasts/` finiscono in `Screencasts`, che è il nome rimasto su Koofr, mentre bisync annota `screencasts`. Il giro dopo vede su Koofr file "spariti" e "nuovi", li sposta anche in locale in una seconda cartella `Screencasts` e si ferma chiedendo una riparazione. Nessuna perdita, ma due cartelle in locale e la coppia bloccata.
- Koofr fa la rinomina di sole maiuscole sul server (`rclone moveto`, una cartella in un colpo, un file passando da un nome provvisorio). Ora, prima di un giro vero, Spola confronta l'ultimo elenco di questo computer con la cartella com'è adesso. Un nome sparito, con al suo posto un solo nome uguale a meno delle maiuscole, è una rinomina (`preflight.case_renames`). Spola la ripete su Koofr, prima la cartella e poi quello che contiene, e bisync trova le due parti già uguali. Koofr viene interrogato prima di ogni rinomina, così un giro fermato dal controllo delle eliminazioni dopo la rinomina la trova già fatta. L'anteprima, che non rinomina niente, conta la rinomina come uno spostamento e toglie le finte eliminazioni e i finti caricamenti (`rclone.fold_case_renames`). Verificato su Koofr: dopo la correzione, `screencasts/` con tutti i file e un giro successivo senza niente da fare.
- La rinomina avviene anche in un giro che poi si ferma per le troppe eliminazioni (bisync conta comunque i vecchi nomi come eliminati in locale): è una rinomina, non elimina niente.
- Nello stesso controllo, templates: la cartella locale è vuota (dalle 20:42 del 13), su Koofr c'è ancora `00-readme.md`. bisync rifiuta di sincronizzare verso un elenco vuoto ("Empty current Path1 listing"), una protezione da tenere.

## Appendice W. Una cartella eliminata su Koofr per sbaglio, e il ripristino dal cestino (2026-10-14)

- Alle 16:53 la cartella `data` è stata eliminata su Koofr dall'app web. Spola era in pausa: in locale non è successo niente. Koofr elimina sul server una cartella alla volta (circa 40 minuti per 320 GB); nel cestino finisce un file per voce, tutti con la stessa ora, e nessuna cartella.
- Ripristino: solo le voci di quell'eliminazione (87.999 file, 320,7 GB). Un elenco vuoto chiede a Koofr di ripristinare tutto il cestino, comprese le eliminazioni volute della mattina. Koofr fa circa 500 file in 85 secondi per lavoro, e ne regge quattro insieme. Un 404 vuol dire "non è più nel cestino". Tre file risultavano ripristinati dal lavoro ma erano ancora nel cestino: il cestino va riletto alla fine. Le cartelle rimaste vuote non tornano (266 in `work`) e vanno ricreate, altrimenti bisync le toglierebbe anche in locale.
- Il problema vero: Koofr dà a ogni file ripristinato la data di oggi e permette di cambiarla solo ricaricando il file (nessuna API la cambia; le versioni non conservano quella originale). Spola, ripartendo, avrebbe visto 88.000 file "cambiati su Koofr" e li avrebbe riscaricati sopra quelli identici, con le copie locali nel backup. Correggere solo l'elenco di Koofr non basta: a fine giro bisync confronta i due elenchi (`check-sync`) e le date diverse fanno chiedere una riparazione, che in modalità "newer" avrebbe riscaricato tutto. La strada giusta: in entrambi gli elenchi la data che Koofr ha ora, per i file identici (stessa dimensione, invariati qui). Il giro dopo li vede cambiati qui e li ricarica con la loro data; a fine giro i due elenchi coincidono. Se cambiano tutti i file di una cartella, bisync si ferma per la conferma.
- Diventa la funzione "Ripristina dal cestino di Koofr" nella pagina di ogni cartella (`trash.py`, `Engine.restore_from_trash`, `listings.retime`): le eliminazioni della cartella ancora nel cestino, raggruppate per momento, con numero di file, dimensione e cartelle toccate; si sceglie quale ripristinare; nessun giro della cartella durante il ripristino; poi cartelle vuote ricreate, date sistemate negli elenchi, giro che ricarica.

## Appendice X. Mai una copia su Koofr eliminata prima del suo caricamento (2026-10-15)

- Durante il ricaricamento di `work` (le date originali da rimettere dopo il ripristino dal cestino), il laptop si è sospeso alle 8:22: al risveglio 10.020 file mancavano su Koofr, intatti qui. Causa, nel codice di rclone (`operations.equal`): Koofr non sa cambiare la data di un file (`SetModTime` risponde "non senza eliminarlo"), quindi per un file cambiato solo nella data rclone elimina la copia su Koofr già mentre confronta, e la carica dopo; il confronto corre fino a 10.000 file avanti ai caricamenti (`--max-backlog`). Interrotto il giro, i file eliminati e non ancora caricati restano solo nel cestino. Succede a ogni giro con molti file cambiati solo nella data (un backup rimesso, un checkout), non solo dopo un ripristino.
- Il giro dopo non ha eliminato niente qui (la modifica qui vince sull'eliminazione su Koofr, e il limite del 10% ha fermato tutto), ma per quel tempo i file mancano su Koofr, e un altro computer sincronizzato sulla stessa cartella potrebbe eliminarli da sé.
- Provato su Koofr, 60 file cambiati solo nella data, giro fermato dopo 6 secondi: così com'era mancavano 51 file; con `--max-backlog 4`, 21; con `--backup-dir2`, 44 (rclone sposta in anticipo anche lì); con `--ignore-times`, 0. Con `--ignore-times` rclone copia i file che bisync gli passa senza chiedersi prima se l'altra parte li ha già, e su Koofr un file viene sovrascritto (`ForceOverwrite`) solo quando parte il suo caricamento. bisync passa solo i file cambiati e riconosce a modo suo quelli uguali dalle due parti: conflitti, file uguali, spostamenti sul server e rinomine su Koofr provati, come prima. Solo nei giri normali: in un riallineamento (`--resync`) copierebbe tutto.

## Appendice Y. L'anteprima di un grande allineamento, ad albero (2026-10-16)

- La conferma di `work` dopo l'eliminazione voluta di Vantage DC elencava 20.772 percorsi completi uno sotto l'altro, fermandosi a 300 con "…e altri N": non si capiva quali progetti fossero toccati. Ora ogni tipo di modifica dell'anteprima (copiati su Koofr, eliminati qui...) è una riga con numero di file e dimensione, che apre una pagina con l'albero delle cartelle: cartelle prima dei file, in ordine naturale ("old-2" prima di "old-10"), ognuna con quanti file contiene e quanto pesano, aperte e chiuse a piacere; una catena di cartelle con una sola sottocartella sta su una riga ("electrical-design / 10-clients / mcm"), come nei gestori di file.
- Le dimensioni vengono dalla prova a secco stessa: per ogni file che copierebbe, eliminerebbe o sposterebbe rclone dice quanti byte ha; le cartelle sono quelle che direbbe di togliere o creare, o che contengono altri percorsi (`Plan.sizes`, `Plan.folders`, `filetree.py`, senza GTK, con i suoi test). La pagina (`plan_tree.py`) è una `Gtk.ListView` su un `Gtk.TreeListModel` con `Gtk.TreeExpander`: costruisce solo le righe visibili e i figli di una cartella solo quando si apre, quindi ventimila file restano immediati (l'albero di 7.000 file si costruisce in 30 ms) e nessuno resta fuori dall'elenco. Le icone dei trasferimenti sono quelle del tema per scaricare e inviare: le frecce di Adwaita sono a forma di "V" e accanto alla freccia della riga sembrerebbero "espandi".
- Percentuale (0.3.17): sotto ogni cartella dell'albero, quanta parte ne tocca la modifica ("tutta la cartella", "il 31% della cartella"), e lo stesso per l'intera cartella sincronizzata nella riga di ogni tipo di modifica. La base sono i file dell'ultimo giro riuscito, dagli elenchi di bisync del lato giusto (computer per ciò che parte da qui o vi viene eliminato, Koofr per il resto), più i file nuovi: una cartella nuova è "tutta la cartella". Leggere i due elenchi di `work` (42.000 voci) costa circa due secondi: lo fa il motore nel thread che prepara l'anteprima (`jobs.listed_files`, `Preview.bases`), non la finestra. Mai "100%" finché resta un file, mai "0%": sotto l'1% è "meno dell'1%".

## Appendice Z. Una cartella sparita o svuotata da una parte (2026-10-17)

- `spola-test` chiedeva una riparazione: la cartella su Koofr era stata eliminata la mattina del 16, quasi certamente dal PC Windows insieme alle cartelle di Vantage DC. bisync si ferma quando un lato non ha niente ("Empty current Path2 listing"), per non svuotare l'altro: giusto, ma "Ripara" non poteva funzionare (il riallineamento cerca la cartella e risponde "directory not found").
- Ora Spola riconosce il caso dal log (`Outcome.empty_side`, `Outcome.root_missing`), chiede a Koofr in un thread se la cartella c'è ancora (`rclone lsjson --stat`, `jobs.koofr_folder_exists`) e lo dice: "non c'è più su Koofr", "è vuota su Koofr", "è vuota su questo computer" (`PairRuntime.gone`). La revisione apre un avviso con le scelte che hanno senso: ripristinare dal cestino di Koofr, ricaricare i file di questo computer (ricreando la cartella se serve, poi con la solita anteprima), copiare qui di nuovo i file di Koofr, smettere di sincronizzare (nessun file eliminato). Nessuna anteprima preparata in anticipo per una cartella che non c'è; una riuscita o un ripristino dal cestino tolgono lo stato.
- Trovato lungo la strada: `Adw.Banner` mostra il titolo come testo semplice se non gli si chiede il markup, mentre Spola lo passava già con i caratteri speciali convertiti: un apostrofo diventava "&apos;". Ora il banner interpreta il markup.

## Appendice AA. Analisi di affidabilità e le correzioni a rischio più alto (2026-10-17)

- L'analisi completa (tre revisioni indipendenti del codice più prove dirette) è in `~/scratch/spola-analisi-2026-10-17.md` fuori dal repository; qui le correzioni fatte.
- **La conferma vale quanto l'anteprima.** Prima: `confirm()` accodava un giro con `--force`, che in bisync toglie ogni limite alle eliminazioni, senza controllare che l'anteprima esistesse ancora; il dialogo aperto ignorava l'invalidazione. Provato con rclone: anteprima con 3 eliminazioni su 20, poi 18 su 20 prima della conferma: `--force` le applica tutte; con `--max-delete 16` bisync si ferma e chiede di nuovo. Ora `confirm()` accetta solo l'anteprima conservata per quella domanda, finita, fresca e non superata da una modifica (altrimenti risponde False e il dialogo la rifà); il giro confermato passa `--max-delete` pari alla quota di eliminazioni dell'anteprima (spostamenti compresi, come li conta bisync; `confirmed_max_delete`, margine 2 punti) sulle voci degli elenchi (`listings.counts`), mai `--force`. bisync controlla prima le eliminazioni e solo dopo "tutti i file cambiati" (verificato): se il giro confermato si ferma per quello, le eliminazioni sono appena state verificate entro il limite e il giro viene ripetuto subito con `--force`. Il dialogo, a ogni `pair-changed` e ogni 30 s, se l'anteprima mostrata non è più quella del motore o è scaduta, disattiva Conferma e mostra "Qualcosa è cambiato dopo questa anteprima" con Aggiorna. La CLI fa lo stesso.
- **Tre confini resi robusti.** Il thread che prepara un giro catturava solo `Busy`, `NotReady`, `OSError`: un `ValueError` di `listings.load` (elenco corrotto) saltava `_start_prepared` e lasciava corsia e coppia prese per sempre. La pompa degli eventi (`_drain_events`) senza try/except: un'eccezione toglieva la sorgente idle lasciando `_events_scheduled` a True, nessun evento più smaltito, Esci appeso. Il lettore di rclone chiamava `on_exit` fuori dal `finally`: un log non scrivibile lasciava rclone congelato sulla pipe. Ora: catch-all nei tre punti con l'errore detto (`log.complain`, sullo stderr, cioè nel journal) e riportato come esito della coppia; le conseguenze della fine di un giro in un `finally` (`_lane_freed`); un log non scrivibile viene saltato e il giro va avanti; un lettore che fallisce uccide rclone e produce un esito di fallimento. Lo spegnimento aspetta anche un task in preparazione nell'altra corsia e dopo due SIGINT ha uno stadio con SIGKILL. Il file di esclusione per giro è scritto in modo atomico.
- **Sospensione.** `Gtk.Application.inhibit` passa dal portale Inhibit, che su niri (backend gnome/gtk, senza gnome-session) risponde senza inibire niente: verificato con `systemd-inhibit --list`, nessun inibitore; su GNOME funzionerebbe. Ora `sleep.py` chiede a logind sul bus di sistema un lock "sleep" in modo "block" per la durata di un giro vero (`--system-talk-name=org.freedesktop.login1` nel manifest; il descrittore passa dal proxy, verificato nel sandbox), con il portale come ripiego, e ascolta `PrepareForSleep`: se la macchina dorme comunque (sospensione privilegiata, batteria critica) il giro si ferma in modo graduale e riparte al risveglio. Preferenza "Tieni sveglio il computer durante la sincronizzazione", accesa.
- **CI.** I test in un workflow riutilizzabile (`test.yml`) richiesto da Build e dal repository Flatpak, che prima pubblicava a ogni tag senza test né controllo tag == versione; il pacchetto Windows dipende dal suo job di test; i test dei dialoghi GTK, che in CI venivano saltati in silenzio, girano su Ubuntu 26.04 sotto xvfb con un controllo che non siano stati saltati.

## Appendice AB. Lotto A dell'analisi: le correzioni di affidabilità (2026-10-17)

- **Richiesta persa durante un giro:** `_enqueue` segnava una richiesta arrivata a giro in corso con il segnaposto `""` in `changed_during_run`; `_follow_up` scartava ciò che sta in `parents` dei file toccati, e `dirname("a.txt") == ""`: bastava un file scaricato nella radice e la richiesta spariva fino al giro orario. Ora una bandiera `rerun_requested`, onorata dopo un giro riuscito o annullato.
- **Lock lasciato da un crash:** "prior lock file found" arriva come avviso, non come errore, quindi il messaggio restava vuoto e la cartella diceva "fermata con un errore"; ora è riconosciuto, detto in parole ("una sincronizzazione precedente è stata interrotta: riprovo tra 3 min") e ritentato dopo i 2 minuti del lock senza contare come fallimento.
- **Giro che non parte:** `fail()` in `_start_prepared` metteva ERROR senza riprova e `_idle_status` lo faceva decadere ad "Aggiornata" al primo cambio di condizioni; ora `last_failure` tiene l'errore finché un giro parte, con una riprova dopo un minuto. Pausa annulla anche un task in preparazione (`task.cancelled`). Una conferma ricorda cosa stava rispondendo (`before_run`), così Stop o Pausa durante la preparazione riportano "in attesa della tua conferma", non "aggiornata".
- **Esclusioni cambiate durante un giro:** `set_filters` impostava lo stato senza emetterlo e l'esito OK lo azzerava (bisync rifiutava comunque il giro dopo); ora la bandiera `filters_changed` produce la riparazione a fine giro, anche quando è bisync stesso ad accorgersene ("filters file has changed"), con il messaggio di Spola.
- **Sigillo dopo un giro trasformato in riallineamento:** le "due cartelle vuote" trasformano un SYNC in `--resync` in `jobs.prepare`, e il motore non accodava il sigillo (niente `.lst-old` per il recupero); ora `rt.ran_resync` lo fa, ma mai per un sigillo che a sua volta si è trasformato (con entrambi i lati vuoti sarebbe un ciclo infinito: trovato dai test).
- **Nomi con virgolette o barra rovesciata:** bisync li scrive tra virgolette alla Go nelle righe di delta e di coda; `parse_line` ora li scioglie (`go_unquote`). Un nome con a capo in un file rifiutato spezzava il file di esclusione in due regole sbagliate (verificato): i caratteri di controllo diventano `?` nel pattern (un carattere qualsiasi; verificato con rclone).
- **Anteprima illeggibile rifiutata:** `summarize` riconosce un riallineamento dalla frase di fase di bisync; se un rclone futuro la cambiasse, l'anteprima direbbe "niente da fare" mentre il giro vero sovrascrive. Ora un piano vuoto con statistiche attive (trasferimenti, eliminazioni, rinomine) è un errore, mai "niente da fare". `FORBIDDEN_FLAGS` copre anche `--size-only`, `--ignore-checksum`, `--immutable`, `--backup-dir`, `--suffix` e i valori pericolosi (`--conflict-loser delete`, `--conflict-resolve` diverso da none).
- **Ripristino dal cestino:** le date venivano riscritte confrontando solo le dimensioni; ora anche la data locale (quella dell'elenco, entro il millisecondo di Koofr) e l'MD5 di Koofr del file ripristinato (`lsjson --hash`) contro l'hash dell'elenco; un file cambiato qui nel frattempo resta a bisync, che lo risolve con i suoi backup o come conflitto.
- **Memoria:** il lettore teneva ogni evento, compresa una statistica al secondo con l'elenco dei trasferimenti (86.000 in 24 ore); ora le statistiche consecutive si fondono nell'ultima.
- **Monitor delle cartelle:** le esclusioni della coppia (`- pattern` nei filtri) non consumano più watch (`excluded_by`, generalizzazione di `default_excluded`); l'overflow (`TreeWatcher.overflow`) è detto nel dettaglio dello stato. Feed attività: qualunque eccezione nel thread fa tornare al controllo periodico (prima solo `ApiError`: un `KeyError` lasciava il feed morto in silenzio). Anteprima anticipata preparata una volta sola finché non cambia qualcosa; niente "anteprima pronta" per una già superata. CLI: `sync` e `preview` rifiutati mentre l'app gira (condividerebbero elenchi e file di esclusione). Abbassare i giorni di backup chiede conferma dicendo quanti backup e quanti byte vanno, e applica la potatura in un thread. `_post_json` usa il contesto TLS con i certificati inclusi (Windows).

## Appendice AC. Lotto B dell'analisi: le due soglie che mancavano (2026-10-17)

Due buchi nelle protezioni, rilevati dall'analisi del 17 ottobre (punti 2 e 3 della lista "a rischio alto" non ancora coperti).

- **Un tetto assoluto alle eliminazioni.** Il limite di `--max-delete` è una percentuale (10% di default) con un pavimento di 5 file per le cartelle piccole, ma senza tetto: su `/data/photos` (42.579 file) il 10% sono 4.257 file eliminabili senza chiedere. Ora `effective_max_delete` abbassa la percentuale finché il 10% non supera `MAX_FREE_DELETES` (500 file): su 42.579 file diventa il 2% (851 file: la percentuale è intera, quindi "il più basso che copra il tetto"), su 60.000 l'1%. Il pavimento di 5 file resta. Una conferma continua a passare per `confirmed_max_delete`, che il tetto non tocca: chi ha visto l'anteprima può eliminare quanto ha visto.
- **Una soglia sulle sovrascritture di massa.** bisync ferma le eliminazioni oltre il limite e il "tutto cambiato" (tutti i file di un lato cambiati), ma niente fermava una cartella con *molti* file cambiati qui tutti insieme: uno snapshot rimesso al suo posto, un programma che riscrive una libreria intera, un ripristino da backup. Ogni file sarebbe salito su Koofr sopra la sua copia, e Koofr tiene sì la versione precedente, ma per file, a mano. La soglia non può essere di bisync (non ne ha una per i trasferimenti); e fermare il giro a metà, dopo un conteggio sulle righe di delta, lascerebbe metà delle copie sostituite. Quindi la contiamo *prima* del giro: `jobs.changed_here(root, previous)` confronta i file dell'ultimo elenco locale (`path1.lst`) con lo stato di adesso, per dimensione o data oltre il millisecondo (lo stesso `--modify-window` di bisync), una `stat` per file (un secondo o due su 40.000 file, nel thread di preparazione). Oltre `allowed_changes(percent, n)` (la percentuale della coppia applicata ai file dell'elenco, mai meno di `MIN_FREE_CHANGES` = 50 né più di `MAX_FREE_CHANGES` = 500) `prepare` solleva `TooManyChanges` e il giro non parte: la cartella passa in NEEDS_CONFIRMATION con `rt.safety = "changes"` e il messaggio "N file su T cambiati su questo computer tutti insieme"; l'anteprima li mostra tra i caricamenti, la conferma li fa salire. La conferma porta con sé `confirmed_max_changes` (i file del piano già presenti nell'elenco, più il 2% o 2): se nel frattempo ne cambiano altri oltre quel margine, il giro si ferma di nuovo e chiede ancora, come per le eliminazioni. Solo i giri SYNC veri (non anteprime, non riallineamenti, non confermati con la loro soglia) la applicano; i file nuovi, non nell'elenco, non contano (non sovrascrivono nulla); un file sparito è un'eliminazione, già coperta. La CLI rifiuta `sync` allo stesso modo e indica `preview --force` e `sync --force`; il `--force` passa la soglia della sua anteprima e la porta anche nel secondo giro dopo un "tutto cambiato".
- **Verificato con i test:** 10 di 30 file riscritti in pausa con la soglia abbassata a 3: NEEDS_CONFIRMATION senza che nulla salga, anche al controllo periodico; anteprima con 10 caricamenti; durante la preparazione del giro confermato ne cambiano altri 15: ferma di nuovo con "25 of 30"; nuova anteprima e conferma: tutto su Koofr e OK. Il vecchio test che accettava 6.000 file eliminabili su 60.000 è stato corretto nel senso opposto.

