# SrinkVideo

Pagina web per caricare un file audio/video (es. `.mp4`) e ricevere uno **ZIP** con:

1. **Solo l'audio** — estratto in MP3 (192 kbps) e spezzato in file che non superano
   la dimensione massima indicata, **oppure**
2. **Il video diviso** — in parti **MP4** della dimensione indicata, senza ricodifica
   (`-c copy`, taglio sui keyframe: la dimensione è approssimata per difetto).

I file nello ZIP prendono il nome del file caricato con suffisso progressivo:
`MioFilmato.mp4` → `MioFilmato_000.mp4`, `MioFilmato_001.mp4`, …

Il risultato viene scaricato dal browser di chi carica il file; sul server non
resta nulla (le cartelle temporanee vengono rimosse a fine richiesta).

## Avvio con Docker (consigliato)

```bash
docker compose up -d --build
```

L'app è raggiungibile su `http://<ip-server>:8129` (subnet `192.168.129.0/24`).
L'immagine include già ffmpeg; il container riparte da solo al riavvio del server
(`restart: unless-stopped`).

## Avvio manuale (senza Docker)

Requisiti: Python ≥ 3.9, [ffmpeg](https://ffmpeg.org/) e `ffprobe` nel PATH
(su EL9: `dnf config-manager --set-enabled crb && dnf install ffmpeg-free` da EPEL),
poi `pip install -r requirements.txt`.

```bash
python3 app.py                                        # sviluppo
gunicorn -w 2 -t 900 -b 0.0.0.0:8129 app:app          # produzione
```

Variabili d'ambiente:

| Variabile       | Default   | Descrizione                          |
|-----------------|-----------|--------------------------------------|
| `HOST`          | `0.0.0.0` | Indirizzo di ascolto                 |
| `PORT`          | `8129`    | Porta di ascolto                     |
| `MAX_UPLOAD_MB` | `4096`    | Dimensione massima upload (MB)       |

## Note tecniche

- L'audio è ricodificato a bitrate costante, quindi la dimensione dei segmenti è
  prevedibile (margine del 5%).
- Il video è diviso con lo stream copy in contenitore MP4: i tagli avvengono sui
  keyframe, quindi le parti possono differire leggermente dalla dimensione
  richiesta (margine del 10%).
