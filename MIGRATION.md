# Migrazione dell'app locale al repository GitHub

Questa guida riguarda **Kodi Smart Bridge App v0.1.0 Pilot**. Non sostituire l'app locale prima del collaudo.

## Stato di partenza

- App locale: `local_kodi-smart-bridge`
- API dell'app locale: porta host `8790`, porta interna `8787`
- Integrazione Home Assistant: `kodi_smart_bridge`, configurabile da **Riconfigura**
- Card Lovelace: HACS, non deve essere modificata
- Database persistenti nell'app: `/data/catalogs`
- Playlist nella condivisione HA: `/share/kodi-smart-bridge/tv.m3u8` e `radio.m3u`

## Prima del passaggio

1. Eseguire un backup completo Home Assistant, **includendo l'app locale e i relativi dati**. Conservarne una copia protetta: opzioni e backup possono contenere token e credenziali IPTV.
2. Registrare in un gestore segreti i valori di configurazione dall'interfaccia dell'app locale. Non inserirli mai nei commit, nei log pubblicati o nelle issue.
3. Verificare che il bridge locale continui a funzionare sulla porta host `8790` e che i file di playlist siano presenti in `/share/kodi-smart-bridge/`.
4. Il repository GitHub deve essere accessibile dal Supervisor. Un repo GitHub **privato** normalmente non viene scaricato dal Supervisor senza un sistema di autenticazione esplicitamente supportato. Non renderlo pubblico prima di una revisione completa di sicurezza.

## Installazione parallela (dopo che il repository è accessibile)

1. Aggiungere il repository dalla Raccolta delle app e installare **una seconda istanza** dell'app GitHub, senza rimuovere quella locale.
2. Prima di avviare la nuova app, cambiare la porta host esposta da `8790` a **`8791`** in modo da evitare conflitti. La porta interna rimane `8787`.
3. Trascrivere la configurazione della nuova app tramite l'interfaccia del Supervisor, con un token nuovo di almeno 32 caratteri. Verificare che il path delle playlist utilizzi `/share/kodi-smart-bridge/`.
4. Avviare la nuova app e verificare i log: refresh dei cataloghi, autenticazione, Kodi JSON-RPC, ricerche, directory delle serie, TV/radio e un playback reale.
5. La nuova app potrà ricostruire i propri database: **non presumere che l'installazione GitHub erediti `/data/catalogs` dalla vecchia istanza**. Verificare i conteggi dei cataloghi e la persistenza dopo il riavvio prima del cutover.
6. Dall'integrazione Home Assistant aprire **Riconfigura** e sostituire endpoint/token con la nuova porta `8791`. Conservare i parametri precedenti per il rollback; non eliminare la voce d'integrazione.
7. Collaudare l'intera card, poi arrestare l'app locale. Lasciarla installata e con i dati intatti per un periodo di osservazione.
8. Soltanto dopo conferma e backup aggiornato, decidere se rimuovere l'app locale.

## Rollback

Se la nuova app smette di funzionare, riavviare `local_kodi-smart-bridge` e tramite **Riconfigura** riportare l'integrazione all'endpoint precedente (`HA_IP:8790` e relativo vecchio token). Non cancellare né sovrascrivere i dati locali prima di aver validato il rollback.

## Note di rilascio

Il codice attuale ha superato i test funzionali nell'installazione locale, **non** un test end-to-end di installazione da repository GitHub. Non etichettarlo come release stabile finché il percorso di distribuzione e il ripristino non sono stati verificati. Aggiornamenti automatici disabilitati fino al completamento del collaudo.
