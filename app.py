import os, io, json, requests, logging
from flask import Flask, jsonify, request, Response, send_from_directory, stream_with_context
from dotenv import load_dotenv
from deezer import Deezer
from deemix.decryption import generateCryptedStreamURL
from deemix.utils.crypto import generateBlowfishKey, decryptChunk
from deezer.gw import GWAPIError

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("wave")

app = Flask(__name__, template_folder="templates", static_folder="static")

ARL = os.getenv("DEEZER_ARL", "").strip()
DEEZER_API = "https://api.deezer.com"

# ── Sessão Deezer autenticada ──────────────────────────────────────────────────
dz = Deezer()

def ensure_login():
    if not dz.logged_in:
        ok = dz.login_via_arl(ARL)
        if not ok:
            raise RuntimeError("ARL inválido ou expirado. Verifique seu .env")

try:
    ensure_login()
    log.info(f"✅ Deezer logado como: {dz.current_user.get('name', '?')}")
except Exception as e:
    log.warning(f"⚠️  {e}")

# ── Helpers API pública ────────────────────────────────────────────────────────
def pub_get(path, params=None):
    r = requests.get(f"{DEEZER_API}{path}", params=params, timeout=10)
    r.raise_for_status()
    return r.json()

# ── Rotas estáticas ────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("templates", "index.html")

# ── API pública ────────────────────────────────────────────────────────────────
@app.route("/api/search")
def search():
    q = request.args.get("q", "").strip()
    t = request.args.get("type", "track")
    if not q:
        return jsonify({"error": "Query vazia"}), 400
    return jsonify(pub_get(f"/search/{t}", {"q": q, "limit": 40}))

@app.route("/api/track/<int:tid>")
def track_info(tid):
    return jsonify(pub_get(f"/track/{tid}"))

@app.route("/api/album/<int:aid>")
def album_info(aid):
    return jsonify(pub_get(f"/album/{aid}"))

@app.route("/api/playlist/<int:pid>")
def playlist_info(pid):
    return jsonify(pub_get(f"/playlist/{pid}"))

@app.route("/api/artist/<int:aid>/top")
def artist_top(aid):
    return jsonify(pub_get(f"/artist/{aid}/top", {"limit": 10}))

@app.route("/api/chart")
def chart():
    return jsonify(pub_get("/chart/0/tracks", {"limit": 25}))

# ── Stream completo com descriptografia Blowfish ───────────────────────────────
@app.route("/api/stream/<int:track_id>")
def stream(track_id):
    try:
        ensure_login()

        # 1. Buscar dados internos da track via GW (contém MD5, token, versão)
        gw_track = dz.gw.get_track_with_fallback(track_id)

        md5        = gw_track.get("MD5_ORIGIN", "")
        media_ver  = gw_track.get("MEDIA_VERSION", "1")
        track_token= gw_track.get("TRACK_TOKEN", "")
        sng_id     = str(track_id)

        # 2. Tentar URL autenticada via license_token (MP3_320 ou MP3_128)
        stream_url = None
        for fmt in ("MP3_320", "MP3_128"):
            try:
                stream_url = dz.get_track_url(track_token, fmt)
                if stream_url:
                    log.info(f"Stream URL ({fmt}): {stream_url[:60]}...")
                    break
            except Exception as e:
                log.warning(f"get_track_url {fmt} falhou: {e}")

        # 3. Fallback: URL criptografada CDN (sempre funciona com ARL válido)
        if not stream_url:
            from deemix.types.TrackFormats import TrackFormats
            stream_url = generateCryptedStreamURL(sng_id, md5, media_ver, TrackFormats.MP3_128)
            log.info(f"Fallback CDN URL: {stream_url[:60]}...")

        is_crypted = "/mobile/" in stream_url or "/media/" in stream_url

        # 4. Fazer proxy do áudio com descriptografia chunk a chunk
        headers = {"User-Agent": "Mozilla/5.0"}
        req_range = request.headers.get("Range", "")
        if req_range:
            headers["Range"] = req_range

        upstream = requests.get(stream_url, headers=headers, stream=True, timeout=15)
        upstream.raise_for_status()

        content_type = upstream.headers.get("Content-Type", "audio/mpeg")
        resp_headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": content_type,
        }
        if "Content-Length" in upstream.headers:
            resp_headers["Content-Length"] = upstream.headers["Content-Length"]
        if "Content-Range" in upstream.headers:
            resp_headers["Content-Range"] = upstream.headers["Content-Range"]

        blowfish_key = generateBlowfishKey(sng_id) if is_crypted else None

        def generate():
            for chunk in upstream.iter_content(chunk_size=2048 * 3):
                if not chunk:
                    continue
                if is_crypted and blowfish_key and len(chunk) >= 2048:
                    chunk = decryptChunk(blowfish_key, chunk[:2048]) + chunk[2048:]
                yield chunk

        status = 206 if req_range else 200
        return Response(stream_with_context(generate()), status=status, headers=resp_headers)

    except Exception as e:
        log.error(f"Erro no stream {track_id}: {e}")
        # Último recurso: preview de 30s público
        try:
            track = pub_get(f"/track/{track_id}")
            preview = track.get("preview")
            if preview:
                r = requests.get(preview, stream=True, timeout=10)
                return Response(r.iter_content(4096),
                                content_type="audio/mpeg",
                                headers={"X-Stream-Mode": "preview"})
        except:
            pass
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    if not ARL:
        print("⚠️  Defina DEEZER_ARL no arquivo .env")
    app.run(host="0.0.0.0", port=5000, debug=False)
