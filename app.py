#!/usr/bin/env python3
"""SrinkVideo - carica un file audio/video e ottieni uno ZIP con:
- solo l'audio, spezzato in file di dimensione massima scelta, oppure
- il video diviso in parti della dimensione scelta.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path

from flask import Flask, render_template, request, send_file, abort

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_MB", "4096")) * 1024 * 1024

ALLOWED_EXT = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v",
               ".mpg", ".mpeg", ".ts", ".mp3", ".m4a", ".aac", ".ogg", ".opus",
               ".wav", ".flac", ".wma"}

AUDIO_BITRATE_KBPS = 192  # bitrate MP3 usato per l'estrazione audio


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
    return stem


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
    """Divide il video in parti MP4 da circa max_mb MB senza ricodifica."""
    max_bytes = max_mb * 1024 * 1024
    duration, bitrate = probe(src)
    if not bitrate:
        raise RuntimeError("Impossibile determinare il bitrate del file.")
    # il taglio avviene sui keyframe: 10% di margine sulla dimensione richiesta
    segment_time = max(1, int(max_bytes * 8 / bitrate * 0.90))
    pattern = str(outdir / f"{base}_%03d.mp4")
    run(["ffmpeg", "-y", "-i", str(src), "-c", "copy", "-map", "0",
         "-f", "segment", "-segment_format", "mp4",
         "-segment_time", str(segment_time),
         "-reset_timestamps", "1", pattern])


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/process", methods=["POST"])
def process():
    upload = request.files.get("file")
    mode = request.form.get("mode")
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

    workdir = Path(tempfile.mkdtemp(prefix="srinkvideo_"))
    try:
        src = workdir / f"input{ext}"
        upload.save(src)

        base = safe_basename(upload.filename)
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


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")  # raggiungibile dalla 192.168.129.0/24
    port = int(os.environ.get("PORT", "8129"))
    app.run(host=host, port=port)
