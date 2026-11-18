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
    "auronplay,elxokas,ibai,thegrefg,rubius,spursito,illojuan,knekro,perxitaa,zeling").split(",") if s.strip()]
GAME_WORDS = ["gta", "vice", "leonida", "lucia", "jason"]
BUFFER_KEY = os.environ["BUFFER_API_KEY"]
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
DRAFT_MODE = os.environ.get("DRAFT_MODE", "false").lower() == "true"
CLIPS_PER_RUN = int(os.environ.get("CLIPS_PER_RUN", "2"))
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "6"))
TARGETS = [s.strip().lower() for s in os.environ.get("TARGET_SERVICES", "youtube,tiktok,instagram").split(",")]
GAME_NAME = os.environ.get("GAME_NAME", "Grand Theft Auto VI")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

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
        r = subprocess.run(["yt-dlp", "--flat-playlist", "-J", "--playlist-end", "15", url],
                           capture_output=True, text=True)
        if r.returncode != 0:
            log("No pude leer clips de", ch); continue
        for e in (json.loads(r.stdout).get("entries") or []):
            cid = e.get("id") or ""
            title = (e.get("title") or "").lower()
            if not cid or cid in seen: continue
            if any(b in title for b in BLOCK): continue
            if not any(w in title for w in GAME_WORDS): continue
            good.append({"id": cid, "url": e.get("url") or f"https://clips.twitch.tv/{cid}",
                         "title": e.get("title") or "", "broadcaster_name": ch,
                         "view_count": e.get("view_count") or 0, "duration": e.get("duration") or 30})
            break  # uno por streamer
    good.sort(key=lambda c: c["view_count"], reverse=True)
    log(f"{len(good)} clips válidos (modo sin claves)")
    return good[:CLIPS_PER_RUN]

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
    return good[:CLIPS_PER_RUN]

def download(clip):
    WORK.mkdir(exist_ok=True)
    out = WORK / f"{clip['id']}.mp4"
    subprocess.run(["yt-dlp", "-q", "-f", "best[ext=mp4]/best", "-o", str(out), clip["url"]], check=True)
    return out

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
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if font.getlength(t) <= maxw: cur = t
        else: lines.append(cur); cur = w
    if cur: lines.append(cur)
    return lines

def build_overlay(hook, streamer, path):
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
    size = 96
    while True:
        f = F(size); lines = wrap(clean, f, W - 120)
        if len(lines) <= 3 or size <= 60: break
        size -= 6
    y = 270
    for i, ln in enumerate(lines):
        col = YEL if i == len(lines) - 1 else WHITE
        lw = f.getlength(ln)
        d.text(((W - lw) / 2, y), ln, font=f, fill=col, stroke_width=max(5, size // 12), stroke_fill=INK)
        y += int(size * 1.15)
    # crédito bajo el vídeo
    f = F(44); t = f"🎥 @{streamer}".replace("🎥 ", "Clip: ")
    lw = f.getlength(t)
    d.rounded_rectangle([(W - lw) / 2 - 28, 1330, (W + lw) / 2 + 28, 1410], radius=40, fill=(20, 8, 36, 220))
    d.text(((W - lw) / 2, 1340), t, font=f, fill=WHITE)
    f = F(30); t = "Página fan · No afiliada a Rockstar Games"; lw = f.getlength(t)
    d.text(((W - lw) / 2, 1418), t, font=f, fill=(235, 220, 245), stroke_width=3, stroke_fill=INK)
    im.save(path)

def render(src, overlay, out):
    # fondo: el propio clip ampliado y desenfocado; delante: el clip a todo el ancho; encima: textos + Pip
    flt = (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=30:5,eq=brightness=-0.18[bg];"
        "[0:v]scale=1080:-2[fg];"
        "[bg][fg]overlay=(W-w)/2:700[v1];"
        "[v1][1:v]overlay=0:0[v2];"
        "[2:v]format=rgba[pip];"
        "[v2][pip]overlay=20:H-h+12:shortest=1[v]"
    )
    pip = str(ASSETS / "pip" / "%03d.png")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-i", str(overlay),
           "-framerate", "30", "-stream_loop", "-1", "-i", pip,
           "-filter_complex", flt, "-map", "[v]", "-map", "0:a?", "-t", "59",
           "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
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

def schedule(chans, video_url, title, text, due):
    for c in chans:
        svc = c["service"].lower()
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
        if "message" in res and res["message"]:
            log(f"ERROR Buffer {svc}:", res["message"])
            ERRORS.append(f"{svc}: {res['message']}")
        else:
            log(f"Programado en {svc} para {due:%d/%m %H:%M} UTC")

# ---------------- main ----------------
def main():
    state = load_state()
    clips = find_clips(state)
    if not clips:
        log("Nada nuevo que publicar"); return
    MEDIA.mkdir(exist_ok=True)
    made = []
    for c in clips:
        try:
            src = download(c)
            hook, title, text = write_texts(c, state)
            ov = WORK / f"{c['id']}_ov.png"
            build_overlay(hook, c["broadcaster_name"], ov)
            safe = re.sub(r"[^A-Za-z0-9_-]", "", c["id"])[:60]
            out = MEDIA / f"pip_{safe}.mp4"
            render(src, ov, out)
            made.append((c, out, title, text))
            log("Montado:", out.name, "|", hook)
        except Exception as e:
            log("Fallo con el clip", c["id"], e)
    if not made: return
    urls = {} if DRY_RUN else publish_media([m[1] for m in made])
    chans = buffer_channels()
    now = dt.datetime.now(dt.timezone.utc)
    gap = int(os.environ.get("MINUTES_BETWEEN_POSTS", "75"))
    for i, (c, out, title, text) in enumerate(made):
        due = now + dt.timedelta(minutes=20 + i * gap)
        schedule(chans, urls.get(out.name, f"DRY/{out.name}"), title, text, due)
        state["posted"].append(c["id"])
    save_state(state)
    if ERRORS:
        sys.exit("Buffer rechazó algunos posts: " + " | ".join(ERRORS))

if __name__ == "__main__":
    main()
