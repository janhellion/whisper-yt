import os
import sys
import tempfile
import threading
from tkinter import Tk, Label, Entry, Button, filedialog, StringVar, NORMAL, DISABLED
from tkinter import ttk
from tkinter import scrolledtext

import whisper
import yt_dlp

# ------------------------------
# Runtime hardening for windowed builds (tqdm/whisper may write to stdout/err)
# ------------------------------
class _NullWriter:
    def write(self, s):
        pass
    def flush(self):
        pass

if getattr(sys, 'stderr', None) is None:
    sys.stderr = _NullWriter()
if getattr(sys, 'stdout', None) is None:
    sys.stdout = _NullWriter()

# Ensure bundled ffmpeg.exe (placed next to the EXE) is discoverable
if getattr(sys, 'frozen', False):
    _exe_dir = os.path.dirname(sys.executable)
else:
    _exe_dir = os.path.dirname(os.path.abspath(__file__))
os.environ["PATH"] = _exe_dir + os.pathsep + os.environ.get("PATH", "")

# ------------------------------
# Global state
# ------------------------------
model = None            # lazy-loaded Whisper model
cookies_path = None     # optional cookies.txt, set via GUI

# Selected yt client profile after probing; dict with 'name', 'headers', 'extractor_args'
yt_client_selected = None

# Candidate client profiles (order matters)
_YT_CLIENTS = [
    {
        'name': 'web',
        'headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0',
            'Accept-Language': 'en-US,en;q=0.5',
        },
        'extractor_args': {'youtube': {'player_client': ['web']}},
    },
    {
        'name': 'android',
        'headers': {
            'User-Agent': 'com.google.android.youtube/19.16.39 (Linux; U; Android 13) gzip',
            'Accept-Language': 'en-US,en;q=0.5',
        },
        'extractor_args': {'youtube': {'player_client': ['android']}},
    },
    {
        'name': 'ios',
        'headers': {
            'User-Agent': 'com.google.ios.youtube/19.16.2 (iPhone15,3; iOS 17.5; gzip)',
            'Accept-Language': 'en-US,en;q=0.5',
        },
        'extractor_args': {'youtube': {'player_client': ['ios']}},
    },
    {
        'name': 'tv',
        'headers': {
            'User-Agent': 'YouTube/ytlr (Linux; U; Android TV 12) gzip',
            'Accept-Language': 'en-US,en;q=0.5',
        },
        'extractor_args': {'youtube': {'player_client': ['tv']}},
    },
]

# ------------------------------
# GUI helpers
# ------------------------------

def log(message: str):
    log_widget.config(state=NORMAL)
    log_widget.insert('end', message + '\n')
    log_widget.see('end')
    log_widget.config(state=DISABLED)


def set_progress(val: float):
    progress['value'] = max(0, min(100, val))
    root.update_idletasks()


# ------------------------------
# yt-dlp progress hook (download → first 40% of the bar)
# ------------------------------

def ytdlp_hook(d):
    status = d.get('status')
    if status == 'downloading':
        total = d.get('total_bytes') or d.get('total_bytes_estimate')
        downloaded = d.get('downloaded_bytes', 0)
        if total and total > 0:
            pct = (downloaded / total) * 40.0
            set_progress(pct)
    elif status == 'finished':
        set_progress(40)
        log('Download complete, starting transcription...')


# ------------------------------
# Whisper loader (logs Whisper/tqdm messages into the GUI)
# ------------------------------

def load_model():
    global model
    if model is not None:
        return
    log('Loading Whisper model (this can take a while the first time)...')

    class _PipeToLog:
        def write(self, s):
            s = str(s).strip('\n')
            if s:
                log(s)
        def flush(self):
            pass

    old_err = sys.stderr
    sys.stderr = _PipeToLog()
    try:
        model = whisper.load_model('base')
    finally:
        sys.stderr = old_err
    log('Model loaded.')


# ------------------------------
# Format probing with multi-client fallback
# ------------------------------

def _compact_fmt_row(f: dict) -> str:
    return (
        f"{f.get('format_id')}|{f.get('ext')}|a:{f.get('acodec')}|"
        f"v:{f.get('vcodec')}|abr:{f.get('abr')}|tbr:{f.get('tbr')}|h:{f.get('height')}"
    )


def _ydl_opts_base(client: dict, quiet: bool = True, outtmpl: str | None = None, hooks=None):
    opts = {
        'quiet': quiet,
        'noplaylist': True,
        'geo_bypass': True,
        'ignoreerrors': False,
        'forceipv4': True,
        'http_headers': client['headers'],
        'extractor_args': client['extractor_args'],
        'retries': 5,
        'fragment_retries': 5,
        'skip_unavailable_fragments': True,
    }
    if cookies_path:
        opts['cookiefile'] = cookies_path
    if outtmpl is not None:
        opts['outtmpl'] = outtmpl
    if hooks:
        opts['progress_hooks'] = hooks
    return opts


def _probe_formats(url: str) -> tuple[dict, dict]:
    """Try each client profile until extract_info succeeds. Returns (info, client)."""
    global yt_client_selected
    last_err = None
    for client in _YT_CLIENTS:
        try:
            with yt_dlp.YoutubeDL(_ydl_opts_base(client, quiet=True)) as ydl:
                info = ydl.extract_info(url, download=False)
            yt_client_selected = client
            log(f"Selected YouTube client: {client['name']}")
            return info, client
        except Exception as e:
            last_err = e
            log(f"Probe failed with client '{client['name']}': {e}")
    if last_err:
        raise last_err
    raise RuntimeError('Failed to probe formats with all clients')


# ------------------------------
# Resilient audio download (reuses selected client, falls back if needed)
# ------------------------------

def download_audio(url: str, tmpdir: str) -> str:
    """Probe formats, attempt multiple selectors, extract to WAV, return WAV path."""
    outtmpl = os.path.join(tmpdir, 'audio.%(ext)s')

    # 1) Probe using multi-client fallback
    info, client = _probe_formats(url)
    formats = info.get('formats') or []
    log("Available formats (id|ext|acodec|vcodec|abr|tbr|height):")
    for f in formats[:30]:
        log("  " + _compact_fmt_row(f))

    # Compute best audio-only candidate id when possible
    audio = [f for f in formats if (f.get('acodec') not in (None, 'none'))]
    audio_only = [f for f in audio if (f.get('vcodec') in (None, 'none'))]
    candidates = audio_only or audio

    def _rank(ff):
        return (ff.get('abr') or 0, ff.get('tbr') or 0, ff.get('filesize') or 0)

    best_audio_id = None
    if candidates:
        best_audio_id = sorted(candidates, key=_rank, reverse=True)[0].get('format_id')

    base_opts = _ydl_opts_base(client, quiet=False, outtmpl=outtmpl, hooks=[ytdlp_hook])
    base_opts['postprocessors'] = [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'wav',
        'preferredquality': '192',
    }]

    selectors = []
    if best_audio_id:
        selectors.append(best_audio_id)         # exact audio format id
    selectors += [
        'bestaudio*',                           # separate audio preferred
        'ba/bestaudio/best',                    # legacy robust chain
        'best',                                 # anything that exists
    ]

    last_err = None
    clients_to_try = [client] + [c for c in _YT_CLIENTS if c is not client]

    for c in clients_to_try:
        for sel in selectors:
            try:
                opts = _ydl_opts_base(c, quiet=False, outtmpl=outtmpl, hooks=[ytdlp_hook])
                opts['postprocessors'] = base_opts['postprocessors']
                opts['format'] = sel
                log(f"Attempting selector: {sel} with client: {c['name']}")
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
                # locate produced WAV
                for name in os.listdir(tmpdir):
                    if name.lower().endswith('.wav'):
                        path = os.path.join(tmpdir, name)
                        log(f"Found WAV: {path}")
                        return path
                log("Selector succeeded but no WAV found; trying next.")
            except Exception as e:
                last_err = e
                log(f"Selector failed ({sel}) on client {c['name']}: {e}")
        log(f"All selectors failed on client {c['name']}. Trying next client…")

    log("All clients and selectors failed; check the probed formats above.")
    if last_err:
        raise last_err
    raise FileNotFoundError('No WAV generated')


# ------------------------------
# Transcription
# ------------------------------

def transcribe_file(input_path: str, output_path: str):
    set_progress(50)
    log(f'Transcribing: {input_path}')
    load_model()
    set_progress(60)
    res = model.transcribe(input_path)
    text = res.get('text', '')
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(text)
    set_progress(100)
    log(f'Transcription saved to {output_path}')


# ------------------------------
# GUI callbacks
# ------------------------------

def browse_input():
    path = filedialog.askopenfilename(
        title='Select media file',
        filetypes=[('Media', '*.mp4 *.mkv *.webm *.mov *.m4a *.wav *.mp3'), ('All', '*.*')]
    )
    if path:
        input_var.set(path)


def browse_output():
    path = filedialog.asksaveasfilename(
        title='Save transcript as',
        defaultextension='.txt',
        filetypes=[('Text', '*.txt'), ('All', '*.*')]
    )
    if path:
        output_var.set(path)


def browse_cookies():
    global cookies_path
    path = filedialog.askopenfilename(
        title='Select cookies.txt (optional for age/region-gated videos)',
        filetypes=[('Text', '*.txt'), ('All', '*.*')]
    )
    if path:
        cookies_path = path
        log(f'Using cookies file: {cookies_path}')


def list_formats():
    url = input_var.get().strip()
    if not (url.startswith('http://') or url.startswith('https://')):
        log('Provide a URL in the input field to list formats.')
        return
    try:
        info, client = _probe_formats(url)
        formats = info.get('formats') or []
        log(f"Client used for listing: {client['name']}")
        log("Available formats (id|ext|acodec|vcodec|abr|tbr|height):")
        for f in formats:
            log("  " + _compact_fmt_row(f))
    except Exception as e:
        log(f'Format probe failed: {e}')


def on_transcribe():
    btn_transcribe.config(state=DISABLED)
    set_progress(0)
    log_widget.config(state=NORMAL)
    log_widget.delete('1.0', 'end')
    log_widget.config(state=DISABLED)

    source = input_var.get().strip()
    out = output_var.get().strip()

    def worker():
        try:
            if source.startswith(('http://', 'https://')):
                with tempfile.TemporaryDirectory() as tmp:
                    wav = download_audio(source, tmp)
                    transcribe_file(wav, out)
            else:
                # Assume local file; Whisper can read many formats directly
                transcribe_file(source, out)
            log('Process complete!')
        except Exception as e:
            log(f'Error: {e}')
        finally:
            root.after(0, lambda: btn_transcribe.config(state=NORMAL))

    threading.Thread(target=worker, daemon=True).start()


# ------------------------------
# GUI layout
# ------------------------------
root = Tk()
root.title('Whisper Transcriber')

input_var = StringVar()
output_var = StringVar()

Label(root, text='Input file or URL:').grid(row=0, column=0, sticky='e', padx=4, pady=4)
Entry(root, textvariable=input_var, width=64).grid(row=0, column=1, padx=4)
Button(root, text='Browse…', command=browse_input).grid(row=0, column=2, padx=4)

Label(root, text='Output .txt file:').grid(row=1, column=0, sticky='e', padx=4, pady=4)
Entry(root, textvariable=output_var, width=64).grid(row=1, column=1, padx=4)
Button(root, text='Save as…', command=browse_output).grid(row=1, column=2, padx=4)

# Optional helpers
Button(root, text='Use cookies.txt…', command=browse_cookies).grid(row=2, column=1, sticky='w', pady=6)
Button(root, text='List formats', command=list_formats).grid(row=2, column=2, sticky='e', pady=6)

btn_transcribe = Button(root, text='Transcribe', command=on_transcribe)
btn_transcribe.grid(row=3, column=1, pady=10)

progress = ttk.Progressbar(root, mode='determinate', maximum=100)
progress.grid(row=4, column=0, columnspan=3, sticky='we', padx=4, pady=4)

log_widget = scrolledtext.ScrolledText(root, state='disabled', height=14)
log_widget.grid(row=5, column=0, columnspan=3, padx=4, pady=4)

root.mainloop()
