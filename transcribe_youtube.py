import os, subprocess, sys, tempfile, yt_dlp, whisper

def download_audio(url: str, out: str):
    ydl_opts = {
        "format": "bestaudio",
        "quiet": True,
        "outtmpl": out,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "wav",
            "preferredquality": "192"
        }]
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

def transcribe(path: str, model_name: str = "base"):
    model = whisper.load_model(model_name)
    return model.transcribe(path)["text"]

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: transcribe_youtube.py <youtube-url>")
        sys.exit(1)

    url = sys.argv[1]
    with tempfile.TemporaryDirectory() as tmp:
        audio_path = os.path.join(tmp, "audio.%(ext)s")
        download_audio(url, audio_path)
        # yt-dlp will replace %(ext)s with .wav
        wav = [f for f in os.listdir(tmp) if f.endswith(".wav")][0]
        text = transcribe(os.path.join(tmp, wav))
        print(text)
