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
