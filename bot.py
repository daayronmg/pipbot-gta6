"""
Pip te lo explica - GTA 6 clip bot
Busca los clips de GTA VI más vistos en Twitch, los monta en vertical con Pip,
los aloja en este repo (servidos por jsDelivr) y los programa en Buffer.
Se ejecuta solo con GitHub Actions (ver .github/workflows/bot.yml).
"""
import json, os, re, subprocess, sys, time, random, datetime as dt, textwrap, pathlib
import requests
from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).parent
ASSETS = ROOT / "assets"
FONT = str(ASSETS / "Poppins-Bold.ttf")
WORK = ROOT / "work"
MEDIA = ROOT / "media"          # carpeta que se sube a la rama 'media'
STATE = ROOT / "state.json"

TWITCH_ID = os.environ.get("TWITCH_CLIENT_ID", "")
TWITCH_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "")
# Sin claves de Twitch: se leen los clips públicos de estos canales con yt-dlp
STREAMERS = [s.strip() for s in os.environ.get("STREAMERS",
    "auronplay,elxokas,ibai,thegrefg,rubius,spursito,illojuan,knekro,perxitaa,zeling,werlyb,vegetta777,willyrex,alexby11,elmariana,spreen,carreraaa,ampeterby7").split(",") if s.strip()]
GAME_WORDS = ["gta", "vice", "leonida", "lucia", "jason"]
BUFFER_KEY = os.environ["BUFFER_API_KEY"]
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
DRAFT_MODE = os.environ.get("DRAFT_MODE", "false").lower() == "true"
CLIPS_PER_RUN = int(os.environ.get("CLIPS_PER_RUN", "4"))
MAX_PER_DAY = int(os.environ.get("MAX_PER_DAY", "48"))
MIN_SCORE = float(os.environ.get("MIN_SCORE", "7"))
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "6"))
TARGETS = [s.strip().lower() for s in os.environ.get("TARGET_SERVICES", "youtube,tiktok").split(",")]
GAME_NAME = os.environ.get("GAME_NAME", "Grand Theft Auto VI")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODELS = [m.strip() for m in os.environ.get("GEMINI_MODEL",
    "gemini-3-flash-preview,gemini-3.1-flash-lite-preview,gemini-3-flash-preview").split(",") if m.strip()]
HAS_AI = bool(ANTHROPIC_KEY or GEMINI_KEY)
# Con IA se analizan más candidatos y se quedan solo los que tienen sentido
N_CANDIDATES = int(os.environ.get("N_CANDIDATES", "10")) if HAS_AI else CLIPS_PER_RUN

W, H = 1080, 1920
INK = (35, 18, 52); YEL = (255, 214, 64); PINK = (255, 64, 150); WHITE = (255, 255, 255)

# Palabras que suelen indicar spoiler de historia o contenido que no queremos
BLOCK = ["spoiler", "ending", "final", "cutscene", "story", "historia", "dies", "death", "muere",
         "boss", "credits", "créditos", "leak", "mission", "misión", "plot", "twist", "nsfw",
         "ban", "racist", "slur"]

HOOKS = [
    "ESTO YA PASA EN GTA 6 👀", "MIRA LO QUE HACE GTA 6", "GTA 6 ES UNA LOCURA",
    "NADIE ESPERABA ESTO EN GTA 6", "EL DETALLE DE GTA 6 QUE NADIE VIO", "GTA 6 VA EN SERIO",
    "LA FÍSICA DE GTA 6 😳", "ASÍ SE VE GTA 6 DE VERDAD",
]

def log(*a): print("[pipbot]", *a, flush=True)

# ---------------- estado ----------------
def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"posted": [], "hook_i": 0}

def save_state(s):
    s["posted"] = s["posted"][-500:]
    STATE.write_text(json.dumps(s, indent=1, ensure_ascii=False))

# ---------------- Twitch ----------------
def twitch_token():
    r = requests.post("https://id.twitch.tv/oauth2/token", data={
        "client_id": TWITCH_ID, "client_secret": TWITCH_SECRET, "grant_type": "client_credentials"}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]

def twitch_get(path, tok, **params):
    r = requests.get(f"https://api.twitch.tv/helix/{path}", params=params,
                     headers={"Client-Id": TWITCH_ID, "Authorization": f"Bearer {tok}"}, timeout=30)
    r.raise_for_status()
    return r.json()["data"]

def find_clips_public(state):
    seen = set(state["posted"]); good = []
    for ch in STREAMERS:
        url = f"https://www.twitch.tv/{ch}/clips?filter=clips&range=24hr"
        r = subprocess.run(["yt-dlp", "--flat-playlist", "-J", "--playlist-end", "20", url],
                           capture_output=True, text=True)
        if r.returncode != 0:
            log("No pude leer clips de", ch); continue
        for e in (json.loads(r.stdout).get("entries") or []):
            cid = e.get("id") or ""
            title = (e.get("title") or "").lower()
            if not cid or cid in seen: continue
            if any(b in title for b in BLOCK): continue
            if not HAS_AI and not any(w in title for w in GAME_WORDS): continue  # con IA lo decide ella
            good.append({"id": cid, "url": e.get("url") or f"https://clips.twitch.tv/{cid}",
                         "title": e.get("title") or "", "broadcaster_name": ch,
                         "view_count": e.get("view_count") or 0, "duration": e.get("duration") or 30})
            if sum(1 for g in good if g["broadcaster_name"] == ch) >= 3:
                break  # máximo 3 candidatos por streamer
    good.sort(key=lambda c: c["view_count"], reverse=True)
    log(f"{len(good)} clips válidos (modo sin claves)")
    return good[:N_CANDIDATES]

def find_clips(state):
    if not (TWITCH_ID and TWITCH_SECRET):
        return find_clips_public(state)
    tok = twitch_token()
    games = twitch_get("games", tok, name=GAME_NAME)
    if not games:
        sys.exit(f"No encuentro el juego '{GAME_NAME}' en Twitch")
    gid = games[0]["id"]
    now = dt.datetime.now(dt.timezone.utc)
    start = (now - dt.timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    clips = twitch_get("clips", tok, game_id=gid, started_at=start, ended_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"), first=100)
    seen = set(state["posted"])
    seen_streamers = set()
    good = []
    for c in sorted(clips, key=lambda c: c["view_count"], reverse=True):
        title = c["title"].lower()
        if c["id"] in seen: continue
        if any(b in title for b in BLOCK): continue
        if c["duration"] < 8: continue
        # variedad: un clip por streamer y ejecución
        if c["broadcaster_name"] in seen_streamers: continue
        seen_streamers.add(c["broadcaster_name"])
        good.append(c)
    log(f"{len(clips)} clips encontrados, {len(good)} válidos")
    return good[:N_CANDIDATES]

def download(clip):
    WORK.mkdir(exist_ok=True)
    out = WORK / f"{clip['id']}.mp4"
    subprocess.run(["yt-dlp", "-q", "-f", "best[ext=mp4]/best", "-o", str(out), clip["url"]], check=True)
    return out

# ---------------- análisis con IA ----------------
_whisper = None
def transcribe(src):
    global _whisper
    try:
        from faster_whisper import WhisperModel
        if _whisper is None:
            _whisper = WhisperModel("base", device="cpu", compute_type="int8")
        segs, _ = _whisper.transcribe(str(src), language=None, vad_filter=True)
        return " ".join(s.text.strip() for s in segs)[:1500]
    except Exception as e:
        log("Sin transcripción:", e)
        return ""

def frames_b64(src, n=6):
    import base64
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src)],
                               capture_output=True, text=True).stdout.strip() or 20)
    out = []
    for k in range(n):
        t = dur * (k + 0.5) / n
        jpg = WORK / f"f_{k}.jpg"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
                        "-vf", "scale=640:-2", "-q:v", "4", str(jpg)], check=True)
        out.append(base64.b64encode(jpg.read_bytes()).decode())
    return out

ANALYSIS_PROMPT = """Eres el mejor clipper de habla hispana y el guionista de "Pip te lo explica": un científico loco animado (el Profe Pip) que comenta clips de GTA 6 en TikTok y YouTube Shorts para España.
CONTEXTO IMPORTANTE: GTA 6 (Grand Theft Auto VI) YA HA SALIDO: se lanzó el 19 de noviembre de 2026 en PS5 y Xbox Series, y hoy es {today}. Los streamers están jugándolo de verdad. No descartes un clip por pensar que el juego no ha salido o que es un mod: si se ve un juego con estética de Vice City / Leonida (Florida), con Jason o Lucía, es GTA 6 real.
Te paso un clip de Twitch del streamer {streamer}. Título del clip: "{title}". Audio: "{transcript}"

1) FILTRO (sé muy exigente, solo publicamos lo que un clipper top publicaría):
Descártalo (usable=false) si: sale una escena de sexo explícito; no es GTA 6 (otro juego, Just Chatting…); es sobre todo el streamer hablando u opinando; la mayor parte es su cámara; es una cinemática o momento de la historia (spoiler); es un menú o pantalla de carga; no pasa nada concreto; o no se entiende sin haber visto el directo.
Vale si EN EL JUEGO pasa algo concreto: un detalle escondido, una física loca, un bug, una reacción de la IA, algo muy gracioso, un truco útil o algo que la gente no sabía que se podía hacer.

2) GUION (si vale):
- gancho: texto en pantalla, MAYÚSCULAS, máximo 6 palabras. Tiene que abrir una pregunta en la cabeza (curiosidad), ser concreto y no contar el final. Prohibido: "increíble", "brutal", "mira esto", "locura", "GTA 6 es…", signos de exclamación dobles.
  Buenos: "NADIE HA VISTO ESTO EN LA CÁRCEL", "LA POLICÍA TE RECUERDA LA CARA", "NO TOQUES A ESTE NPC", "ROCKSTAR ESCONDIÓ ESTO EN LA DUCHA".
- narracion: lo que DICE Pip con voz al principio del vídeo (6-9 segundos, 16-28 palabras). Español de España, coloquial y con chispa, como un amigo que te cuenta un secreto ("Atento, que esto no lo ha visto casi nadie…"). Primera frase = gancho hablado (distinto al texto en pantalla). Después explica qué está pasando y por qué mola, con un dato o matiz que solo se ve si te fijas. Termina dejando con ganas de ver el resto ("y mira lo que pasa al final"). Nada de saludos, nada de "hola", nada de "en este vídeo". No inventes mecánicas que no se vean u oigan; si dudas, describe lo que se ve.
- explicacion: 1 frase corta (máx 90 caracteres) con el dato clave, por si no hay voz.
- titulo: título de YouTube Short con gancho, máx 70 caracteres, termina en #GTA6.
- texto: texto del post, 1-2 frases con gancho + una pregunta que provoque comentarios (polémica sana o "¿tú qué harías?"), sin hashtags.
- nota: del 1 al 10, lo viral que puede ser siendo honesto (un 9-10 solo si es de los que comparte todo el mundo).

Responde SOLO con este JSON:
{{"usable": true/false, "motivo": "breve", "tipo": "CURIOSIDAD" | "DETALLE" | "TRUCO" | "FÍSICAS" | "BUG" | "MOMENTAZO",
 "gancho": "...", "narracion": "...", "explicacion": "...", "titulo": "...", "texto": "...", "nota": 0}}"""

def analyze_gemini(clip, src):
    """Gemini ve el vídeo entero con audio (plan gratuito de Google AI Studio)."""
    import base64
    small = WORK / f"{clip['id']}_small.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-t", "60", "-vf", "scale=-2:360", "-r", "15",
                    "-c:v", "libx264", "-crf", "32", "-preset", "veryfast", "-c:a", "aac", "-b:a", "48k", "-ac", "1", str(small)], check=True)
    data = base64.b64encode(small.read_bytes()).decode()
    prompt = ANALYSIS_PROMPT.format(today=dt.date.today().isoformat(), streamer=clip["broadcaster_name"], title=clip["title"].replace('"', "'"),
                                    transcript="(escúchalo tú en el vídeo)").replace("fotogramas de un clip", "un clip")
    body = {"contents": [{"parts": [{"inline_data": {"mime_type": "video/mp4", "data": data}}, {"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.6}}
    last = None
    for model in GEMINI_MODELS:
        r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                          params={"key": GEMINI_KEY}, json=body, timeout=180)
        if r.status_code == 200:
            txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(re.search(r"\{.*\}", txt, re.S).group(0))
        last = f"{model}: {r.status_code} {r.text[:200]}"
        log("Gemini", last)
        if r.status_code in (429, 500, 503):
            time.sleep(20)
    raise RuntimeError(last)

def analyze(clip, src):
    """Devuelve dict con el análisis o None si no hay IA."""
    if not HAS_AI:
        return None
    if not ANTHROPIC_KEY:
        a = analyze_gemini(clip, src)
        log(f"IA: {clip['broadcaster_name']} usable={a.get('usable')} nota={a.get('nota')} · {a.get('motivo')}")
        return a
    transcript = transcribe(src)
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}} for b in frames_b64(src)]
    content.append({"type": "text", "text": ANALYSIS_PROMPT.format(
        today=dt.date.today().isoformat(), streamer=clip["broadcaster_name"], title=clip["title"].replace('"', "'"), transcript=transcript.replace('"', "'"))})
    r = requests.post("https://api.anthropic.com/v1/messages", timeout=120, headers={
        "x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": ANTHROPIC_MODEL, "max_tokens": 600, "messages": [{"role": "user", "content": content}]})
    r.raise_for_status()
    txt = r.json()["content"][0]["text"]
    a = json.loads(re.search(r"\{.*\}", txt, re.S).group(0))
    log(f"IA: {clip['broadcaster_name']} usable={a.get('usable')} nota={a.get('nota')} · {a.get('motivo')}")
    return a

# ---------------- voz de Pip ----------------
PIP_VOICE = os.environ.get("PIP_VOICE", "es-ES-AlvaroNeural")

def pip_voice(text, out_mp3):
    """Genera la voz de Pip con edge-tts. Devuelve lista de (inicio_s, fin_s, palabra) o None."""
    try:
        import asyncio, edge_tts
        async def go():
            kw = dict(voice=PIP_VOICE, rate="+14%", pitch="+28Hz")
            try:
                com = edge_tts.Communicate(text, boundary="WordBoundary", **kw)
            except TypeError:
                com = edge_tts.Communicate(text, **kw)
            words = []
            with open(out_mp3, "wb") as f:
                async for ch in com.stream():
                    if ch["type"] == "audio":
                        f.write(ch["data"])
                    elif ch["type"] == "WordBoundary":
                        st = ch["offset"] / 1e7; du = ch["duration"] / 1e7
                        words.append((st, st + du, ch["text"]))
            return words
        words = asyncio.run(go())
        if not pathlib.Path(out_mp3).exists() or pathlib.Path(out_mp3).stat().st_size < 2000:
            return None
        if not words:  # sin tiempos por palabra: repartir uniformemente
            dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out_mp3)],
                                       capture_output=True, text=True).stdout or 6)
            ws = text.split(); step = dur / max(1, len(ws))
            words = [(i * step, (i + 1) * step, w) for i, w in enumerate(ws)]
        return words
    except Exception as e:
        log("Sin voz de Pip:", e)
        return None

def ass_time(t):
    t = max(0, t); h = int(t // 3600); m = int(t % 3600 // 60); sec = t % 60
    return f"{h}:{m:02d}:{sec:05.2f}"

def build_captions(words, path, delay):
    """Subtítulos estilo clipper: grupos de 3 palabras, la que suena en amarillo."""
    head = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Pip,Poppins,84,&H00FFFFFF,&H00FFFFFF,&H00341223,&H96000000,-1,0,0,0,100,100,0,0,1,7,3,5,40,40,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    clean = lambda w: re.sub(r"[{}\\]", "", w).upper()
    merged = []
    for st, en, w in words:  # que "GTA 6" no se parta
        if merged and merged[-1][2].upper().strip(".,") == "GTA" and w.strip(".,!?") in ("6", "VI"):
            merged[-1] = (merged[-1][0], en, merged[-1][2] + "\u00a0" + w)
        else:
            merged.append((st, en, w))
    words = merged
    groups = [words[i:i + 3] for i in range(0, len(words), 3)]
    lines = []
    for g in groups:
        for j, (st, en, w) in enumerate(g):
            nxt = g[j + 1][0] if j + 1 < len(g) else en + 0.15
            parts = []
            for k, (_, _, ww) in enumerate(g):
                parts.append("{\\c&H40D6FF&}" + clean(ww) + "{\\c&HFFFFFF&}" if k == j else clean(ww))
            pop = "{\\fscx112\\fscy112\\t(0,90,\\fscx100\\fscy100)}" if j == 0 else ""
            lines.append(f"Dialogue: 0,{ass_time(st + delay)},{ass_time(nxt + delay)},Pip,,0,0,0,,{{\\pos(540,1395)}}{pop}{' '.join(parts)}")
    pathlib.Path(path).write_text(head + "\n".join(lines) + "\n", encoding="utf-8")

# ---------------- textos ----------------
def write_texts(clip, state):
    """Devuelve (gancho para el vídeo, título YouTube, texto del post)."""
    streamer = clip["broadcaster_name"]
    hook = title = None
    if ANTHROPIC_KEY:
        try:
            prompt = (
                "Eres el community manager de 'Pip te lo explica', una página fan de GTA 6 en español de España. "
                f"Un streamer ({streamer}) tiene un clip de GTA 6 titulado: \"{clip['title']}\". "
                "Escribe en JSON con claves hook y title: "
                "hook = gancho en MAYÚSCULAS de máximo 7 palabras que dé ganas de ver el clip, sin spoilers de la historia, sin inventar datos; "
                "title = título de YouTube Short de máximo 70 caracteres que acabe en '#GTA6'. Solo el JSON.")
            r = requests.post("https://api.anthropic.com/v1/messages", timeout=60, headers={
                "x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5"), "max_tokens": 200,
                      "messages": [{"role": "user", "content": prompt}]})
            r.raise_for_status()
            txt = r.json()["content"][0]["text"]
            j = json.loads(re.search(r"\{.*\}", txt, re.S).group(0))
            hook, title = j["hook"].strip(), j["title"].strip()
        except Exception as e:
            log("IA no disponible, uso plantillas:", e)
    if not hook:
        hook = HOOKS[state["hook_i"] % len(HOOKS)]
        state["hook_i"] += 1
        title = (hook.capitalize().replace("gta 6", "GTA 6") + " #GTA6")[:95]
    text = (f"{hook} 🌴\n\n🎥 Clip de {streamer} (twitch.tv/{clip['broadcaster_name']})\n"
            f"Sígueme y Pip te lo explica todo de GTA 6.\n\n#GTA6 #GTAVI #GTA6España #gaming #videojuegos")
    return hook, title, text

# ---------------- montaje ----------------
def F(s): return ImageFont.truetype(FONT, s)

def wrap(text, font, maxw):
    words, lines, cur = [w for w in text.split(" ") if w], [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if font.getlength(t) <= maxw: cur = t
        else: lines.append(cur); cur = w
    if cur: lines.append(cur)
    return lines

def build_overlay(hook, streamer, path, tipo=None, explicacion=None, narrated=False):
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    # marca
    f = F(44); t = "PIP TE LO EXPLICA"; b = f.getbbox(t)
    pw, ph = b[2] - b[0] + 60, b[3] - b[1] + 34
    x0 = (W - pw) // 2
    d.rounded_rectangle([x0, 110, x0 + pw, 110 + ph], radius=ph // 2, fill=PINK, outline=INK, width=6)
    d.text((x0 + 30 - b[0], 110 + 17 - b[1]), t, font=f, fill=WHITE)
    # gancho (emojis fuera: la fuente no los tiene)
    clean = re.sub(r"[^\w\s¿?¡!.,:'\-áéíóúüñÁÉÍÓÚÜÑ0-9]", "", hook).strip()
    clean = re.sub(r"\s+", " ", clean).replace("GTA 6", "GTA\u00a06").replace("GTA VI", "GTA\u00a0VI")
    size = 96
    while True:
        f = F(size); lines = wrap(clean, f, W - 120)
        if len(lines) <= 2 or size <= 64: break
        size -= 6
    y = 260
    for i, ln in enumerate(lines[:3]):
        col = YEL if i == len(lines) - 1 else WHITE
        lw = f.getlength(ln)
        d.text(((W - lw) / 2, y), ln, font=f, fill=col, stroke_width=max(5, size // 12), stroke_fill=INK)
        y += int(size * 1.15)
    # etiqueta de tipo encima del vídeo
    if tipo:
        f = F(40); t = re.sub(r"[^\wÁÉÍÓÚÑ ]", "", tipo.upper()); b = f.getbbox(t)
        pw, ph = b[2] - b[0] + 50, b[3] - b[1] + 28
        x0 = (W - pw) // 2
        d.rounded_rectangle([x0, 615, x0 + pw, 615 + ph], radius=ph // 2, fill=YEL, outline=INK, width=5)
        d.text((x0 + 25 - b[0], 615 + 14 - b[1]), t, font=f, fill=INK)
    y = 1328
    if narrated:
        # Pip habla: los subtítulos van en y≈1395; crédito y aviso a la derecha de Pip
        f = F(36); t = f"Clip: @{streamer}"; lw = f.getlength(t)
        d.rounded_rectangle([400, 1500, 400 + lw + 44, 1560], radius=30, fill=(20, 8, 36, 220))
        d.text((422, 1507), t, font=f, fill=WHITE)
        f = F(26); t = "Página fan · No afiliada a Rockstar"
        d.text((404, 1576), t, font=f, fill=(235, 220, 245), stroke_width=3, stroke_fill=INK)
        im.save(path); return
    # explicación bajo el vídeo
    if explicacion:
        f = F(46); lines = wrap(explicacion.strip(), f, W - 140)[:3]
        bh = len(lines) * 58 + 30
        d.rounded_rectangle([40, y, W - 40, y + bh], radius=28, fill=(255, 255, 255, 235), outline=INK, width=5)
        yy = y + 15
        for ln in lines:
            lw = f.getlength(ln); d.text(((W - lw) / 2, yy), ln, font=f, fill=INK); yy += 58
        y += bh + 18
    # crédito
    f = F(38); t = f"Clip: @{streamer}"
    lw = f.getlength(t)
    d.rounded_rectangle([(W - lw) / 2 - 24, y, (W + lw) / 2 + 24, y + 62], radius=31, fill=(20, 8, 36, 220))
    d.text(((W - lw) / 2, y + 8), t, font=f, fill=WHITE)
    f = F(28); t = "Página fan · No afiliada a Rockstar Games"; lw = f.getlength(t)
    d.text((W - lw - 40, y + 76), t, font=f, fill=(235, 220, 245), stroke_width=3, stroke_fill=INK)
    im.save(path)

def render(src, overlay, out, voice=None, words=None):
    """voice: mp3 con la voz de Pip; words: tiempos por palabra para subtítulos."""
    DELAY = 0.35
    pip_talk = str(ASSETS / "pip" / "%03d.png")
    pip_idle = str(ASSETS / "pip_idle" / "%03d.png")
    inputs = ["-i", str(src), "-i", str(overlay),
              "-framerate", "30", "-stream_loop", "-1", "-i", pip_talk,
              "-framerate", "30", "-stream_loop", "-1", "-i", pip_idle]
    vdur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src)],
                                capture_output=True, text=True).stdout.strip() or 30)
    if voice and words:
        talk_end = words[-1][1] + DELAY
        ass = WORK / (pathlib.Path(out).stem + ".ass")
        build_captions(words, ass, DELAY)
        inputs += ["-i", str(voice)]
        fonts = str(ASSETS).replace("\\", "/").replace(":", "\\:")
        assp = str(ass).replace("\\", "/").replace(":", "\\:")
        # Pip habla, audio del clip bajado mientras habla
        flt = (
            "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=30:5,eq=brightness=-0.22[bg];"
            "[0:v]scale=1080:-2[fg];"
            "[bg][fg]overlay=(W-w)/2:700[v1];"
            "[v1][1:v]overlay=0:0[v2];"
            "[2:v]format=rgba[pt];[3:v]format=rgba[pi];"
            f"[v2][pt]overlay=10:H-h+12:enable='lt(t\\,{talk_end:.2f})':shortest=1[v3];"
            f"[v3][pi]overlay=10:H-h+12:enable='gte(t\\,{talk_end:.2f})':shortest=1[v4];"
            f"[v4]subtitles='{assp}':fontsdir='{fonts}'[v];"
            f"[0:a]volume='if(lt(t\\,{talk_end:.2f})\\,0.22\\,1)':eval=frame[ca];"
            f"[4:a]adelay={int(DELAY*1000)}|{int(DELAY*1000)},volume=1.7,apad[na];"
            "[ca][na]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]"
        )
        maps = ["-map", "[v]", "-map", "[a]"]
        # si la voz dura más que el clip, alarga el vídeo congelando el último frame
        if talk_end + 1 > vdur:
            flt = flt.replace("[0:v]scale=1080:1920", f"[0:v]tpad=stop_mode=clone:stop_duration={talk_end + 1.5 - vdur:.2f},scale=1080:1920", 1)
            flt = flt.replace("[0:v]scale=1080:-2", f"[0:v]tpad=stop_mode=clone:stop_duration={talk_end + 1.5 - vdur:.2f},scale=1080:-2", 1)
            flt = flt.replace("[0:a]volume", f"[0:a]apad=pad_dur={talk_end + 1.5 - vdur:.2f},volume", 1)
    else:
        flt = (
            "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=30:5,eq=brightness=-0.18[bg];"
            "[0:v]scale=1080:-2[fg];"
            "[bg][fg]overlay=(W-w)/2:700[v1];"
            "[v1][1:v]overlay=0:0[v2];"
            "[2:v]format=rgba,scale=iw*0.8:-1[pip];"
            "[v2][pip]overlay=20:H-h+12:shortest=1[v]"
        )
        maps = ["-map", "[v]", "-map", "0:a?"]
    cmd = ["ffmpeg", "-y", "-loglevel", "error"] + inputs + ["-filter_complex", flt] + maps + [
           "-t", "59", "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True)

# ---------------- alojamiento (rama 'media' + jsDelivr) ----------------
def publish_media(files):
    """Sube los mp4 a la rama 'media' y devuelve URLs públicas directas de jsDelivr."""
    if not files: return {}
    tmp = ROOT / "_media_branch"
    subprocess.run(["rm", "-rf", str(tmp)])
    url = f"https://x-access-token:{os.environ['GITHUB_TOKEN']}@github.com/{REPO}.git"
    r = subprocess.run(["git", "clone", "-q", "--depth", "1", "-b", "media", url, str(tmp)])
    if r.returncode != 0:
        tmp.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "media"], cwd=tmp, check=True)
        subprocess.run(["git", "remote", "add", "origin", url], cwd=tmp, check=True)
    existing = sorted(tmp.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
    for old in existing[:-40]:  # guarda solo los 40 últimos
        old.unlink()
    for f in files:
        subprocess.run(["cp", str(f), str(tmp / f.name)], check=True)
    g = ["git", "-c", "user.name=pipbot", "-c", "user.email=pipbot@users.noreply.github.com"]
    subprocess.run(g + ["add", "-A"], cwd=tmp, check=True)
    subprocess.run(g + ["commit", "-q", "-m", "clips"], cwd=tmp, check=True)
    subprocess.run(["git", "push", "-q", "origin", "media"], cwd=tmp, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp, capture_output=True, text=True).stdout.strip()
    urls = {f.name: f"https://cdn.jsdelivr.net/gh/{REPO}@{sha}/{f.name}" for f in files}
    # espera a que jsDelivr lo sirva
    for name, u in urls.items():
        for _ in range(30):
            try:
                if requests.head(u, timeout=20).status_code == 200: break
            except requests.RequestException: pass
            time.sleep(10)
        else:
            log("AVISO: jsDelivr no sirve todavía", u)
    return urls

# ---------------- Buffer ----------------
def buffer(query, variables=None):
    r = requests.post("https://api.buffer.com", timeout=60,
                      headers={"Authorization": f"Bearer {BUFFER_KEY}", "Content-Type": "application/json"},
                      json={"query": query, "variables": variables or {}})
    r.raise_for_status()
    j = r.json()
    if j.get("errors"): raise RuntimeError(j["errors"])
    return j["data"]

def buffer_channels():
    orgs = buffer("query { account { organizations { id name } } }")["account"]["organizations"]
    chans = []
    for o in orgs:
        q = 'query($o: OrganizationId!) { channels(input: { organizationId: $o }) { id name displayName service isQueuePaused } }'
        try:
            data = buffer(q, {"o": o["id"]})
        except Exception:
            data = buffer('query { channels(input: { organizationId: "%s" }) { id name displayName service isQueuePaused } }' % o["id"])
        chans += data["channels"]
    chans = [c for c in chans if c["service"].lower() in TARGETS and not c.get("isQueuePaused")]
    log("Canales:", [(c["service"], c.get("displayName") or c["name"]) for c in chans])
    return chans

CREATE = """mutation($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess { post { id } }
    ... on MutationError { message }
  }
}"""

ERRORS = []

YT_PER_DAY = int(os.environ.get("YT_PER_DAY", "8"))  # Buffer deja 10 Shorts/24 h a cuentas sin verificar

def schedule(chans, video_url, title, text, due, state=None, nota=0):
    for c in chans:
        svc = c["service"].lower()
        if svc == "youtube" and state is not None:
            if state.get("yt_count", 0) >= YT_PER_DAY:
                log("YouTube: ya van los Shorts permitidos hoy, este va solo a TikTok"); continue
        inp = {"text": text, "channelId": c["id"], "schedulingType": "automatic",
               "mode": "customScheduled", "dueAt": due.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
               "assets": [{"video": {"url": video_url, "metadata": {"thumbnailOffset": 1500}}}]}
        if svc == "youtube":
            inp["metadata"] = {"youtube": {"title": title[:100], "categoryId": "20", "madeForKids": False}}
        elif svc == "instagram":
            inp["metadata"] = {"instagram": {"type": "reel", "shouldShareToFeed": True}}
        if DRAFT_MODE:
            inp["saveToDraft"] = True
        if DRY_RUN:
            log("DRY_RUN", svc, json.dumps(inp, ensure_ascii=False)[:300]); continue
        res = buffer(CREATE, {"input": inp})["createPost"]
        # TikTok a veces no lee el vídeo a la primera: reintenta y prueba otro enlace
        alt = video_url.replace("https://cdn.jsdelivr.net/gh/", "https://raw.githubusercontent.com/").replace("@", "/", 1) \
            if "cdn.jsdelivr.net/gh/" in video_url else video_url
        for attempt, url in enumerate([video_url, alt, alt]):
            if not (res.get("message") and "could not be read" in res["message"].lower()):
                break
            log(f"{svc}: Buffer no pudo leer el vídeo, reintento {attempt + 1}")
            time.sleep(30)
            inp["assets"][0]["video"]["url"] = url
            res = buffer(CREATE, {"input": inp})["createPost"]
        if "message" in res and res["message"]:
            log(f"ERROR Buffer {svc}:", res["message"])
            if "maximum number of posts" in res["message"]:
                if state is not None and svc == "youtube": state["yt_count"] = YT_PER_DAY
                continue  # límite diario de la plataforma: no es un fallo del bot
            ERRORS.append(f"{svc}: {res['message']}")
            continue
        if svc == "youtube" and state is not None:
            state["yt_count"] = state.get("yt_count", 0) + 1
        log(f"Programado en {svc} para {due:%d/%m %H:%M} UTC")

# ---------------- main ----------------
def main():
    state = load_state()
    today = dt.date.today().isoformat()
    if state.get("day") != today:
        state["day"], state["today_count"], state["yt_count"] = today, 0, 0
    global CLIPS_PER_RUN
    CLIPS_PER_RUN = min(CLIPS_PER_RUN, MAX_PER_DAY - state.get("today_count", 0))
    if CLIPS_PER_RUN <= 0:
        log(f"Ya van {state['today_count']} clips hoy (máximo {MAX_PER_DAY})"); save_state(state); return
    clips = find_clips(state)
    if not clips:
        log("Nada nuevo que publicar"); return
    MEDIA.mkdir(exist_ok=True)
    made = []
    for c in clips:
        if len(made) >= CLIPS_PER_RUN:
            break
        try:
            src = download(c)
            if any(m[0]["broadcaster_name"] == c["broadcaster_name"] for m in made):
                continue  # variedad: un clip por streamer en cada tanda
            last = state.get("last_by_streamer", {}).get(c["broadcaster_name"], 0)
            if time.time() - last < 2 * 3600:
                continue  # evita repetir el mismo momento con clips distintos del mismo streamer
            a = None
            try:
                a = analyze(c, src)
            except Exception as e:
                log("Análisis IA falló:", e)
                if HAS_AI:
                    continue  # sin análisis no publicamos clips sin sentido
            if a is not None and a.get("usable") and float(a.get("nota") or 0) < MIN_SCORE:
                log(f"Descartado por nota baja ({a.get('nota')})")
                a["usable"] = False
            if a is not None and not a.get("usable"):
                state["posted"].append(c["id"])  # descartado: no volver a mirarlo
                continue
            hook, title, text = write_texts(c, state)
            tipo = expl = None
            if a:
                hook = a.get("gancho") or hook
                title = (a.get("titulo") or title)[:100]
                tipo, expl = a.get("tipo"), a.get("explicacion")
                text = (f"{a.get('texto') or hook}\n\n🎥 Clip de {c['broadcaster_name']} (twitch.tv/{c['broadcaster_name']})\n"
                        "Sígueme y Pip te lo explica todo de GTA 6.\n\n#GTA6 #GTAVI #GTA6España #gaming #videojuegos")
            voice = words = None
            if a and a.get("narracion"):
                voice = WORK / f"{c['id']}_voz.mp3"
                words = pip_voice(a["narracion"], voice)
            ov = WORK / f"{c['id']}_ov.png"
            build_overlay(hook, c["broadcaster_name"], ov, tipo, expl, narrated=bool(words))
            safe = re.sub(r"[^A-Za-z0-9_-]", "", c["id"])[:60]
            out = MEDIA / f"pip_{safe}.mp4"
            render(src, ov, out, voice if words else None, words)
            made.append((c, out, title, text, float((a or {}).get('nota') or 0)))
            log("Montado:", out.name, "|", hook)
        except Exception as e:
            log("Fallo con el clip", c["id"], e)
    if not made: return
    urls = {} if DRY_RUN else publish_media([m[1] for m in made])
    made.sort(key=lambda m: m[4], reverse=True)  # el mejor primero (se lleva el hueco de YouTube)
    chans = buffer_channels()
    now = dt.datetime.now(dt.timezone.utc)
    gap = int(os.environ.get("MINUTES_BETWEEN_POSTS", "15"))
    for i, (c, out, title, text, nota) in enumerate(made):
        due = now + dt.timedelta(minutes=20 + i * gap)
        schedule(chans, urls.get(out.name, f"DRY/{out.name}"), title, text, due, state, nota)
        state["posted"].append(c["id"])
        state["today_count"] = state.get("today_count", 0) + 1
        state.setdefault("last_by_streamer", {})[c["broadcaster_name"]] = time.time()
    save_state(state)
    if ERRORS:
        sys.exit("Buffer rechazó algunos posts: " + " | ".join(ERRORS))

if __name__ == "__main__":
    main()
