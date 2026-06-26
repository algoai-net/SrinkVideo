#!/usr/bin/env python3
"""SrinkVideo - due modalità:

Tab 1 ("Singolo file"): carichi un file audio/video e ottieni subito uno ZIP con
solo l'audio (spezzato) oppure il video diviso in parti.

Tab 2 ("Cartella"): carichi una cartella intera, indichi la dimensione massima
oltre la quale un file va elaborato e la dimensione di uscita desiderata. I file
più grandi della soglia vengono ricompressi (re-encode a peso target) oppure
divisi in parti; i file già sotto soglia vengono ignorati. L'elaborazione gira in
background: al termine arriva una mail con il link a una pagina da cui scaricare i
file uno a uno. Tutti i dati (caricati ed elaborati) vengono cancellati dopo 7
giorni.
"""
import json
import os
import re
import shutil
import smtplib
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from email.message import EmailMessage
from pathlib import Path

from flask import (Flask, abort, render_template, request, send_file, url_for)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_MB", "20480")) * 1024 * 1024

ALLOWED_EXT = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v",
               ".mpg", ".mpeg", ".ts", ".mp3", ".m4a", ".aac", ".ogg", ".opus",
               ".wav", ".flac", ".wma"}

AUDIO_BITRATE_KBPS = 192  # bitrate MP3 usato per l'estrazione/divisione audio

# Lunghezza massima del nome base dei file prodotti. I nomi lunghi (es. quelli
# delle registrazioni Teams) sommati al percorso di estrazione superano il
# limite di Windows (MAX_PATH = 260) e l'utente riceve un errore in estrazione.
MAX_BASE_LEN = 60

# --- storage persistente dei job del tab 2 -------------------------------
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
JOBS_DIR = DATA_DIR / "jobs"
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "7"))
RETENTION_SECONDS = RETENTION_DAYS * 24 * 3600

# --- invio mail ----------------------------------------------------------
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp2.algo.it")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "25"))
MAIL_FROM = os.environ.get("MAIL_FROM", "srinkvideo@algo.it")
# URL pubblico del server usato nei link via mail: deve puntare all'IP/hostname
# raggiungibile dagli utenti, non all'host della richiesta (che sotto proxy o
# in test può essere 127.0.0.1). Se vuoto si usa request.host_url come ripiego.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

_meta_lock = threading.Lock()


def run(cmd):
    """Esegue un comando e solleva un errore con lo stderr di ffmpeg se fallisce."""
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace")[-2000:])
    return proc


def probe(path):
    """Ritorna (durata_secondi, bitrate_totale_bps) del file."""
    proc = run(["ffprobe", "-v", "error", "-show_entries",
                "format=duration,bit_rate", "-of", "json", str(path)])
    fmt = json.loads(proc.stdout)["format"]
    duration = float(fmt.get("duration", 0) or 0)
    bitrate = int(fmt.get("bit_rate", 0) or 0)
    if not bitrate and duration:
        bitrate = int(os.path.getsize(path) * 8 / duration)
    return duration, bitrate


def safe_basename(filename):
    """Nome base del file di ingresso, ripulito per usarlo nei nomi di output.

    Il carattere % va rimosso perché avrebbe significato speciale nel
    pattern di output di ffmpeg."""
    stem = Path(filename).stem
    stem = re.sub(r"[%/\\\x00-\x1f]", "_", stem).strip() or "output"
    return stem[:MAX_BASE_LEN]


def extract_audio(src, outdir, max_mb, base):
    """Estrae l'audio in MP3 e lo spezza in file da al massimo max_mb MB."""
    max_bytes = max_mb * 1024 * 1024
    # secondi di audio che stanno in max_bytes al bitrate scelto (5% di margine)
    segment_time = max(1, int(max_bytes * 8 / (AUDIO_BITRATE_KBPS * 1000) * 0.95))
    pattern = str(outdir / f"{base}_%03d.mp3")
    run(["ffmpeg", "-y", "-i", str(src), "-vn",
         "-c:a", "libmp3lame", "-b:a", f"{AUDIO_BITRATE_KBPS}k",
         "-f", "segment", "-segment_time", str(segment_time),
         "-reset_timestamps", "1", pattern])


def split_video(src, outdir, max_mb, base):
    """Divide il video in parti MP4 che NON superano max_mb MB, senza ricodifica.

    Usa il limite di dimensione di ffmpeg (-fs): scrive ogni parte fino al
    limite, poi riparte dal punto raggiunto. Così la dimensione richiesta è
    rispettata anche con bitrate molto variabile (es. registrazioni Teams, dove
    le schermate statiche e i tratti animati hanno pesi molto diversi). La
    divisione a tempo fisso, basata sul bitrate medio, produceva invece parti di
    peso irregolare, alcune oltre il limite richiesto."""
    max_bytes = max_mb * 1024 * 1024
    duration, _ = probe(src)
    start = 0.0
    idx = 0
    while start < duration - 0.5:
        out = outdir / f"{base}_{idx:03d}.mp4"
        # -ss prima di -i: il taglio parte da un keyframe, parte riproducibile
        run(["ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", str(src),
             "-fs", str(max_bytes), "-c", "copy", "-map", "0",
             "-avoid_negative_ts", "make_zero", str(out)])
        part_dur, _ = probe(out)
        if part_dur <= 0.1:  # nessun avanzamento: evita un ciclo infinito
            out.unlink(missing_ok=True)
            break
        start += part_dur
        idx += 1


def recompress_audio(src, outdir, target_mb, base):
    """Ricomprime l'audio in un singolo MP3 che pesa ~target_mb MB."""
    target_bytes = target_mb * 1024 * 1024
    duration, _ = probe(src)
    if duration <= 0:
        raise RuntimeError("Durata non rilevabile.")
    # bitrate (kbps) che fa pesare l'intero audio quanto richiesto (3% margine)
    kbps = int(target_bytes * 8 / duration / 1000 * 0.97)
    kbps = max(32, min(320, kbps))
    out = outdir / f"{base}.mp3"
    run(["ffmpeg", "-y", "-i", str(src), "-vn",
         "-c:a", "libmp3lame", "-b:a", f"{kbps}k", str(out)])


def recompress_video(src, outdir, target_mb, base):
    """Ricomprime il video in un singolo MP4 che pesa ~target_mb MB (2-pass).

    Il bitrate video si ricava dal peso target e dalla durata, tolta la quota
    dell'audio. Con bitrate molto basso il risultato può superare di poco il
    target: è il meglio ottenibile mantenendo un file riproducibile."""
    target_bytes = target_mb * 1024 * 1024
    duration, _ = probe(src)
    if duration <= 0:
        raise RuntimeError("Durata non rilevabile.")
    audio_kbps = 128
    total_kbps = target_bytes * 8 / duration / 1000 * 0.97
    video_kbps = int(max(100, total_kbps - audio_kbps))
    out = outdir / f"{base}.mp4"
    passlog = str(outdir / f"{base}_pass")
    # pass 1: analisi, nessun audio, output scartato
    run(["ffmpeg", "-y", "-i", str(src), "-c:v", "libx264",
         "-b:v", f"{video_kbps}k", "-pass", "1", "-passlogfile", passlog,
         "-an", "-f", "mp4", "/dev/null"])
    # pass 2: encode definitivo con audio
    run(["ffmpeg", "-y", "-i", str(src), "-c:v", "libx264",
         "-b:v", f"{video_kbps}k", "-pass", "2", "-passlogfile", passlog,
         "-c:a", "aac", "-b:a", f"{audio_kbps}k", str(out)])
    for f in outdir.glob(f"{base}_pass*"):  # ripulisce i log di ffmpeg 2-pass
        f.unlink(missing_ok=True)


def process_one(src, outdir, mode, method, out_mb, base):
    """Elabora un singolo file secondo modalità e metodo scelti."""
    if mode == "audio":
        if method == "split":
            extract_audio(src, outdir, out_mb, base)
        else:
            recompress_audio(src, outdir, out_mb, base)
    else:
        if method == "split":
            split_video(src, outdir, out_mb, base)
        else:
            recompress_video(src, outdir, out_mb, base)


# --- gestione dei job del tab 2 ------------------------------------------

def job_dir(token):
    return JOBS_DIR / token


def read_meta(token):
    path = job_dir(token) / "meta.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def write_meta(meta):
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    path = job_dir(meta["token"]) / "meta.json"
    tmp = path.with_suffix(".tmp")
    with _meta_lock:
        with open(tmp, "w") as f:
            json.dump(meta, f)
        tmp.replace(path)


def send_mail(to, link, n_files):
    msg = EmailMessage()
    msg["From"] = MAIL_FROM
    msg["To"] = to
    msg["Subject"] = "SrinkVideo — cartella pronta"
    msg.set_content(
        "Ciao,\n\n"
        f"l'elaborazione della tua cartella è terminata: {n_files} file pronti.\n"
        f"Scaricali da questa pagina:\n\n{link}\n\n"
        f"I file verranno cancellati automaticamente dopo {RETENTION_DAYS} giorni.\n\n"
        "SrinkVideo")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.send_message(msg)


def worker(token):
    """Elabora in background tutti i file caricati nel job."""
    meta = read_meta(token)
    if not meta:
        return
    indir = job_dir(token) / "in"
    outdir = job_dir(token) / "out"
    outdir.mkdir(exist_ok=True)
    threshold_bytes = meta["max_mb"] * 1024 * 1024
    used_bases = set()

    meta["status"] = "processing"
    write_meta(meta)

    inputs = sorted(p for p in indir.iterdir() if p.is_file())
    for src in inputs:
        ext = src.suffix.lower()
        if ext not in ALLOWED_EXT:
            meta["skipped"].append({"name": src.name, "reason": "estensione non supportata"})
            write_meta(meta)
            continue
        if src.stat().st_size <= threshold_bytes:
            meta["skipped"].append({"name": src.name, "reason": "sotto la soglia"})
            meta["processed"] += 1
            write_meta(meta)
            continue
        # nome base unico per non sovrascrivere file con lo stesso nome
        base = safe_basename(src.name)
        cand, i = base, 1
        while cand in used_bases:
            i += 1
            cand = f"{base}_{i}"
        used_bases.add(cand)
        try:
            process_one(src, outdir, meta["mode"], meta["method"], meta["out_mb"], cand)
        except Exception as exc:  # un file in errore non blocca gli altri
            meta["errors"].append({"name": src.name, "error": str(exc)[-500:]})
        meta["processed"] += 1
        write_meta(meta)

    # i file caricati non servono più: liberano spazio subito
    shutil.rmtree(indir, ignore_errors=True)

    files = sorted(p.name for p in outdir.iterdir() if p.is_file())
    meta["files"] = files
    meta["status"] = "done"
    write_meta(meta)

    if meta.get("email"):
        # costruito a mano: il thread non ha il contesto applicativo per url_for
        link = meta["base_url"].rstrip("/") + "/job/" + token
        try:
            send_mail(meta["email"], link, len(files))
            meta["mail_sent"] = True
        except Exception as exc:
            meta["mail_error"] = str(exc)[-500:]
        write_meta(meta)


def cleanup_loop():
    """Cancella i job (input + output) più vecchi di RETENTION_DAYS."""
    while True:
        try:
            now = time.time()
            if JOBS_DIR.exists():
                for d in JOBS_DIR.iterdir():
                    if not d.is_dir():
                        continue
                    meta = read_meta(d.name)
                    created = meta.get("created_at") if meta else d.stat().st_mtime
                    if now - created > RETENTION_SECONDS:
                        shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass
        time.sleep(3600)  # controlla ogni ora


# --- rotte ----------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/process", methods=["POST"])
def process():
    """Tab 1: elaborazione sincrona di un singolo file, download ZIP."""
    upload = request.files.get("file")
    mode = request.form.get("mode")
    out_name = (request.form.get("out_name") or "").strip()
    try:
        max_mb = int(request.form.get("max_mb", "0"))
    except ValueError:
        max_mb = 0

    if not upload or not upload.filename:
        abort(400, "Nessun file caricato.")
    if mode not in ("audio", "video"):
        abort(400, "Scelta non valida.")
    if max_mb < 1:
        abort(400, "La dimensione deve essere almeno 1 MB.")
    ext = Path(upload.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        abort(400, f"Estensione {ext} non supportata.")

    # Nome base dei file prodotti: quello indicato dall'utente, altrimenti il
    # nome del file caricato. Se è troppo lungo lo ZIP non si estrae su Windows
    # (MAX_PATH), quindi chiediamo all'utente un nome più corto.
    base = safe_basename(out_name) if out_name else safe_basename(upload.filename)
    if out_name and len(safe_basename(out_name)) >= MAX_BASE_LEN and len(out_name) > MAX_BASE_LEN:
        abort(400, f"Il nome scelto è troppo lungo (max {MAX_BASE_LEN} caratteri).")

    workdir = Path(tempfile.mkdtemp(prefix="srinkvideo_"))
    try:
        src = workdir / f"input{ext}"
        upload.save(src)

        outdir = workdir / "out"
        outdir.mkdir()
        if mode == "audio":
            extract_audio(src, outdir, max_mb, base)
        else:
            split_video(src, outdir, max_mb, base)

        parts = sorted(outdir.iterdir())
        if not parts:
            abort(500, "Nessun file prodotto dalla conversione.")

        zip_path = workdir / f"{base}_{uuid.uuid4().hex[:8]}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            for p in parts:
                zf.write(p, p.name)

        # su Linux il file aperto resta leggibile anche dopo la rimozione
        # della cartella, così la pulizia non interferisce con il download
        zip_handle = open(zip_path, "rb")
        return send_file(zip_handle, as_attachment=True,
                         download_name=f"{base}_{mode}.zip",
                         mimetype="application/zip")
    except RuntimeError as exc:
        abort(500, f"Errore di conversione: {exc}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@app.route("/process_folder", methods=["POST"])
def process_folder():
    """Tab 2: avvia un job in background per un'intera cartella."""
    uploads = request.files.getlist("files")
    mode = request.form.get("mode")
    method = request.form.get("method")
    email = (request.form.get("email") or "").strip()
    try:
        max_mb = int(request.form.get("max_mb", "0"))
        out_mb = int(request.form.get("out_mb", "0"))
    except ValueError:
        max_mb = out_mb = 0

    uploads = [u for u in uploads if u and u.filename]
    if not uploads:
        abort(400, "Nessun file caricato.")
    if mode not in ("audio", "video"):
        abort(400, "Scelta non valida.")
    if method not in ("recompress", "split"):
        abort(400, "Metodo non valido.")
    if max_mb < 1 or out_mb < 1:
        abort(400, "Le dimensioni devono essere almeno 1 MB.")
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        abort(400, "Indirizzo email non valido.")

    token = uuid.uuid4().hex
    indir = job_dir(token) / "in"
    indir.mkdir(parents=True, exist_ok=True)
    saved = 0
    for u in uploads:
        # webkitdirectory invia il percorso relativo: teniamo solo il nome file
        name = Path(u.filename.replace("\\", "/")).name
        if not name or name.startswith("."):
            continue
        u.save(indir / name)
        saved += 1
    if saved == 0:
        shutil.rmtree(job_dir(token), ignore_errors=True)
        abort(400, "Nessun file valido nella cartella.")

    meta = {
        "token": token,
        "created_at": time.time(),
        "email": email,
        "mode": mode,
        "method": method,
        "max_mb": max_mb,
        "out_mb": out_mb,
        "base_url": PUBLIC_BASE_URL or request.host_url,
        "status": "queued",
        "total": saved,
        "processed": 0,
        "skipped": [],
        "errors": [],
        "files": [],
    }
    write_meta(meta)

    threading.Thread(target=worker, args=(token,), daemon=True).start()
    link = url_for("job_page", token=token)
    return {"token": token, "link": link, "total": saved}


@app.route("/job/<token>")
def job_page(token):
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        abort(404)
    meta = read_meta(token)
    if not meta:
        abort(404)
    return render_template("job.html", meta=meta, retention_days=RETENTION_DAYS)


@app.route("/job/<token>/status")
def job_status(token):
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        abort(404)
    meta = read_meta(token)
    if not meta:
        abort(404)
    return {k: meta[k] for k in
            ("status", "total", "processed", "files", "errors", "skipped")}


@app.route("/job/<token>/file/<path:name>")
def job_file(token, name):
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        abort(404)
    safe = Path(name).name  # blocca i path traversal
    path = job_dir(token) / "out" / safe
    if not path.is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=safe)


# il thread di pulizia parte all'import (vale anche sotto gunicorn)
threading.Thread(target=cleanup_loop, daemon=True).start()


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")  # raggiungibile dalla 192.168.129.0/24
    port = int(os.environ.get("PORT", "8129"))
    app.run(host=host, port=port)
