import atexit, base64, ipaddress, json, os, re, shutil, subprocess, threading, time, urllib.request, uuid
from datetime import datetime
from urllib.parse import quote, urlencode

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

DATA = os.environ.get("DATA_DIR", "/data")
HLS = os.environ.get("HLS_DIR", "/tmp/hls")
CFG = os.path.join(DATA, "cameras.json")

app = Flask(__name__, static_folder="static", static_url_path="/static")
lock = threading.RLock()
TRANSCODE = os.environ.get("REOVIEW_TRANSCODE", "0") == "1"
players = {}  # id -> (process, started_at)
cameras = []
subs = []
last_alert = {}
SUBS = os.path.join(DATA, "subscriptions.json")
VAPID_KEY = os.path.join(DATA, "vapid_private.pem")
VAPID_SUB = os.environ.get("REOVIEW_VAPID_SUB", "mailto:reoview@example.com")
WEBHOOK_SECRET = os.environ.get("REOVIEW_WEBHOOK_SECRET", "")
COOLDOWN = int(os.environ.get("REOVIEW_COOLDOWN", "30"))  # seconds between alerts per camera+type
VAPID_PUB = ""
STORAGE = os.environ.get("STORAGE_ROOT", "/storage")
ALERTS_DIR = os.path.join(DATA, "alerts")
ALERTS_JSON = os.path.join(DATA, "alerts.json")
SETTINGS_JSON = os.path.join(DATA, "settings.json")
MIN_VIDEO = int(os.environ.get("REOVIEW_MIN_VIDEO_KB", "500")) * 1024
KEEP = 5  # unresolved alerts kept per type
RESOLVED_DAYS = float(os.environ.get("REOVIEW_RESOLVED_DAYS", "7"))
MAX_BYTES = int(float(os.environ.get("REOVIEW_MAX_ALERT_GB", "3")) * 1024 ** 3)
SNAP_TYPE = os.environ.get("REOVIEW_SNAP_TYPE", "sub")  # "sub" (fast, low-res) or "main" (full quality)
VIDEO_WAIT = int(os.environ.get("REOVIEW_VIDEO_WAIT_S", "300"))
VIDEO_SETTLE = int(os.environ.get("REOVIEW_VIDEO_SETTLE_S", "10"))
tokens = {}  # cam id -> (token, expires)
api_locks = {}  # cam id -> lock serialising calls to that camera
backoff = {}  # cam id -> (failures, not_before)
light_cfg = {}  # cam id -> (read_at, mode, bright) so toggling needs only one camera call
VIDEO_EXT = (".mp4", ".mov", ".avi", ".mkv")
TYPE_KEYS = ("any", "person", "animal", "vehicle")
alerts = []
detections = []  # last 100 reported detections, whatever happened to them
MEDIA_EXT = (".mp4", ".mov", ".avi", ".mkv", ".jpg", ".jpeg", ".png")
DETECT_JSON = os.path.join(DATA, "detections.json")
usage_cache = (0, None)
maint_lock = threading.Lock()
FIND_DIR = os.path.join(DATA, "find")
find_jobs = {}
find_sem = threading.Semaphore(1)  # one heavy search/convert at a time
settings = {"start": "00:00", "end": "00:00", "away": False, "types": {k: True for k in TYPE_KEYS},
            "retention_on": False, "retention_days": 30, "lowspace_on": False, "lowspace_gb": 10}


def load():
    global cameras
    if os.path.exists(CFG):
        with open(CFG) as f:
            cameras = json.load(f)


def save():
    os.makedirs(DATA, exist_ok=True)
    with open(CFG, "w") as f:
        json.dump(cameras, f, indent=2)


def jload(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def jsave(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(obj, f)
    os.replace(path + ".tmp", path)


def load_state():
    global alerts, detections
    alerts = jload(ALERTS_JSON, [])
    detections = jload(DETECT_JSON, [])
    shutil.rmtree(FIND_DIR, ignore_errors=True)  # leftovers of "Find video" sessions
    for a in alerts:
        a["pending"] = False
    saved = jload(SETTINGS_JSON, {})
    settings.update({k: v for k, v in saved.items() if k != "types"})
    settings["types"] = {**settings["types"], **saved.get("types", {})}


def category(raw):
    tokens = set(re.split(r"[^A-Z]+", (raw or "").upper()))
    if tokens & {"PEOPLE", "PERSON"}:
        return "person"
    if tokens & {"VEHICLE", "CAR"}:
        return "vehicle"
    if tokens & {"DOG", "CAT", "ANIMAL", "PET"}:
        return "animal"
    return "other"


def in_window(start, end):
    t = datetime.now().hour * 60 + datetime.now().minute
    s = int(start[:2]) * 60 + int(start[3:])
    e = int(end[:2]) * 60 + int(end[3:])
    if s == e:
        return True
    return s <= t < e if s < e else (t >= s or t < e)


def filter_reason(cat):
    if not settings["away"] and not in_window(settings["start"], settings["end"]):
        return "schedule"
    types = settings["types"]
    return "" if (types.get("any") or types.get(cat)) else "type"


def log_detection(name, raw, cat, result, cam_id=""):
    with lock:
        detections.append({"ts": time.time(), "cam": name, "cam_id": cam_id, "raw": raw, "type": cat, "result": result})
        del detections[:-100]
        jsave(DETECT_JSON, detections)


def passes(cat):
    if not settings["away"] and not in_window(settings["start"], settings["end"]):
        return False
    types = settings["types"]
    return bool(types.get("any") or types.get(cat))


def list_storage():
    try:
        return sorted(d for d in os.listdir(STORAGE) if os.path.isdir(os.path.join(STORAGE, d)))
    except OSError:
        return []


def newest_video(sub):
    files = []
    for dp, _, fns in os.walk(os.path.join(STORAGE, sub)):
        for fn in fns:
            if fn.lower().endswith(VIDEO_EXT):
                p = os.path.join(dp, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                files.append((st.st_mtime, st.st_size, p))
    files.sort(reverse=True)
    if not files:
        return None
    if files[0][1] < MIN_VIDEO and len(files) > 1:
        return files[1][2]
    return files[0][2]


def fetch_snap(cam):
    for attempt in range(3):
        try:
            data = cam_call(cam, "Snap", None, None, {"channel": 0, "iLogicChannel": 0, "snapType": SNAP_TYPE,
                                                      "rs": uuid.uuid4().hex[:8]}, timeout=25)
            if isinstance(data, bytes):
                return data
            raise RuntimeError("no image in reply")
        except Exception as e:
            print(f"[reoview] snap attempt {attempt + 1} failed for {cam['name']} ({cam['ip']}): {e}", flush=True)
            time.sleep(2)
    return None


def probe_duration(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        return float(out)
    except Exception:
        return 0.0


def probe_ok(path):
    return probe_duration(path) > 0


def remux(src, dst):
    """Rewrite the clip with a clean index (repairs truncated files that still have readable data)."""
    tmp = dst + ".tmp.mp4"
    for codec in (["-c", "copy"], ["-c:v", "copy", "-c:a", "aac"]):
        try:
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-err_detect", "ignore_err", "-i", src,
                            *codec, "-movflags", "+faststart", "-f", "mp4", tmp], timeout=900)
        except Exception:
            pass
        if os.path.exists(tmp) and probe_ok(tmp):
            os.replace(tmp, dst)
            return True
    if os.path.exists(tmp):
        os.remove(tmp)
    return False


def probe_streams(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width",
                              "-of", "json", path], capture_output=True, text=True, timeout=60).stdout
        info = {"v": None, "a": None, "w": 0}
        for st in json.loads(out).get("streams", []):
            if st.get("codec_type") == "video" and not info["v"]:
                info["v"], info["w"] = st.get("codec_name"), int(st.get("width") or 0)
            elif st.get("codec_type") == "audio" and not info["a"]:
                info["a"] = st.get("codec_name")
        return info
    except Exception:
        return None


def needs_web_copy(info):
    # phones/browsers: H.264 + AAC, and a width their hardware decoders handle
    return bool(info) and (info["v"] != "h264" or info["w"] > 1920 or (info["a"] and info["a"] != "aac"))


def read_progress(path):
    try:
        with open(path) as f:
            hits = re.findall(r"out_time_us=(\d+)", f.read()[-4000:])
        return int(hits[-1]) / 1e6 if hits else 0.0
    except OSError:
        return 0.0


def make_web_copy(src, dst, width, ss=None, t=None, cancel=None, total=None, progress=None, quick=False):
    tmp = dst + ".tmp.mp4"
    prog = dst + ".progress"
    if quick:  # "dirty" preview: small, 10 fps, rough quality, decoder shortcuts
        tw, vf, crf = min(640, width or 640) // 2 * 2, "fps=10,scale={w}:-2:flags=fast_bilinear", "35"
        pre, audio = ["-skip_loop_filter", "all"], ["-c:a", "aac", "-b:a", "48k", "-ac", "1"]
    else:
        tw, vf, crf = min(1280, width or 1280) // 2 * 2, "fps=15,scale={w}:-2:flags=fast_bilinear", "28"
        pre, audio = [], ["-c:a", "aac", "-b:a", "96k"]
    cmd = ["nice", "-n", "19", "ffmpeg", "-y", "-loglevel", "error", "-nostats", "-progress", prog]
    if ss is not None:
        cmd += ["-ss", str(ss)]
    cmd += pre + ["-i", src]
    if t:
        cmd += ["-t", str(t)]
    cmd += ["-vf", vf.format(w=tw), "-c:v", "libx264", "-preset", "ultrafast", "-crf", crf, "-pix_fmt", "yuv420p",
            *audio, "-movflags", "+faststart", "-f", "mp4", tmp]
    try:
        proc = subprocess.Popen(cmd)
        deadline = time.time() + 1800
        while proc.poll() is None:
            if (cancel and cancel()) or time.time() > deadline:
                proc.kill()
                break
            if progress and total:
                progress(min(0.99, read_progress(prog) / total))
            time.sleep(1)
    except Exception as e:
        print(f"[reoview] web copy failed: {e}", flush=True)
    if os.path.exists(prog):
        os.remove(prog)
    if os.path.exists(tmp) and probe_ok(tmp) and not (cancel and cancel()):
        os.replace(tmp, dst)
        return True
    if os.path.exists(tmp):
        os.remove(tmp)
    return False


def wait_settled(path):
    deadline = time.time() + VIDEO_WAIT
    last = -1
    while time.time() < deadline:
        try:
            st = os.stat(path)
        except OSError:
            return
        if st.st_size == last and time.time() - st.st_mtime >= VIDEO_SETTLE:
            return
        last = st.st_size
        time.sleep(5)


def capture_snap(a, cam):
    d = os.path.join(ALERTS_DIR, a["id"])
    os.makedirs(d, exist_ok=True)
    upd = {}
    img = fetch_snap(cam)
    if img:
        with open(os.path.join(d, "snap.jpg"), "wb") as f:
            f.write(img)
        upd["snap"] = True
        try:
            rc = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", os.path.join(d, "snap.jpg"),
                                 "-vf", "scale=640:-2", "-q:v", "4", os.path.join(d, "thumb.jpg")],
                                timeout=30).returncode
            upd["thumb"] = rc == 0
        except Exception:
            pass
    with lock:
        if any(x is a for x in alerts):
            a.update(upd)
            jsave(ALERTS_JSON, alerts)
        else:
            shutil.rmtree(d, ignore_errors=True)


def capture_video(a, cam):
    d = os.path.join(ALERTS_DIR, a["id"])
    ext, web = "", False
    try:
        src = newest_video(cam["storage"])
        if src:
            os.makedirs(d, exist_ok=True)
            wait_settled(src)
            if remux(src, os.path.join(d, "video.mp4")):
                ext = ".mp4"
                info = probe_streams(os.path.join(d, "video.mp4"))
                print(f"[reoview] video {a['id']}: {info}", flush=True)
                if needs_web_copy(info):
                    web = make_web_copy(os.path.join(d, "video.mp4"), os.path.join(d, "video_web.mp4"), info["w"])
            else:
                ext = os.path.splitext(src)[1].lower()
                shutil.copy2(src, os.path.join(d, "video" + ext))
                print(f"[reoview] video for {a['id']} could not be repaired, stored raw copy", flush=True)
    except Exception as e:
        print(f"[reoview] video capture failed: {e}", flush=True)
    with lock:
        if any(x is a for x in alerts):
            a.update({"video": ext, "video_web": web, "pending": False})
            jsave(ALERTS_JSON, alerts)
        else:
            shutil.rmtree(d, ignore_errors=True)
    cleanup()


def media_files():
    for dp, _, fns in os.walk(STORAGE):
        for fn in fns:
            if fn.lower().endswith(MEDIA_EXT):
                p = os.path.join(dp, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                yield p, st.st_size, st.st_mtime


def storage_usage():
    total = files = 0
    per = {}
    for p, size, _ in media_files():
        files += 1
        total += size
        parts = os.path.relpath(p, STORAGE).split(os.sep)
        top = parts[0] if len(parts) > 1 else "(root)"
        per[top] = per.get(top, 0) + size
    try:
        vs = os.statvfs(STORAGE)
        free, disk = vs.f_bavail * vs.f_frsize, vs.f_blocks * vs.f_frsize
    except OSError:
        free = disk = 0
    return {"mounted": os.path.isdir(STORAGE), "writable": os.access(STORAGE, os.W_OK), "files": files,
            "bytes": total, "free": free, "total": disk, "dirs": per}


def remove_empty_dirs():
    for dp, _, _ in os.walk(STORAGE, topdown=False):
        if dp != STORAGE and os.path.dirname(dp) != STORAGE:  # keep the per-camera top folders
            try:
                os.rmdir(dp)
            except OSError:
                pass


def prune_older(days, dry=False):
    cutoff = time.time() - max(1, days) * 86400
    n = b = 0
    for p, size, mt in list(media_files()):
        if mt < cutoff:
            if not dry:
                try:
                    os.remove(p)
                except OSError as e:
                    print(f"[reoview] could not delete {p}: {e}", flush=True)
                    continue
            n += 1
            b += size
    if not dry:
        remove_empty_dirs()
    return n, b


def free_space_guard(min_free):
    try:
        vs = os.statvfs(STORAGE)
    except OSError:
        return 0, 0
    need = min_free - vs.f_bavail * vs.f_frsize
    n = b = 0
    if need <= 0:
        return n, b
    recent = time.time() - 600  # never touch files that may still be recording
    for p, size, mt in sorted(media_files(), key=lambda f: f[2]):
        if b >= need or mt > recent:
            break
        try:
            os.remove(p)
        except OSError:
            continue
        n += 1
        b += size
    remove_empty_dirs()
    return n, b


def storage_maintain():
    if not maint_lock.acquire(blocking=False):
        return
    try:
        if not os.path.isdir(STORAGE) or not os.access(STORAGE, os.W_OK):
            return
        if settings["retention_on"]:
            n, b = prune_older(int(settings["retention_days"]))
            if n:
                print(f"[reoview] retention: deleted {n} files ({b // 2**20} MB)", flush=True)
        if settings["lowspace_on"]:
            n, b = free_space_guard(int(settings["lowspace_gb"]) * 1024 ** 3)
            if n:
                print(f"[reoview] low space: deleted {n} oldest files ({b // 2**20} MB)", flush=True)
    finally:
        maint_lock.release()


def video_files(sub):
    out = []
    for dp, _, fns in os.walk(os.path.join(STORAGE, sub)):
        for fn in fns:
            if fn.lower().endswith(VIDEO_EXT):
                p = os.path.join(dp, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                out.append((st.st_mtime, st.st_size, p))
    return out


def find_source(sub, ts):
    """The clip that was being recorded at ts: it ended (mtime) after ts and started (mtime - length) before it."""
    cands = sorted((f for f in video_files(sub) if ts - 10 <= f[0] <= ts + 3600), key=lambda f: f[0])[:15]
    near = None
    for mt, _, p in cands:
        dur = probe_duration(p)
        if not dur:
            near = near or (p, mt, dur)
        elif mt - dur <= ts + 3 and mt >= ts - 3:
            return p, mt, dur  # contains the event
        elif mt - dur <= ts + 10:
            near = near or (p, mt, dur)  # just before or after the event
    return near


def find_job(jid, cam, ts):
    job = find_jobs[jid]

    def st(status, msg=""):
        job.update(status=status, msg=msg)

    try:
        with find_sem:
            if job["cancel"]:
                return
            st("searching")
            hit = find_source(cam["storage"], ts)
            if not hit:
                st("notfound", "No video found for that time. The file may have been deleted.")
                return
            src, mt, dur = hit
            job["src"] = src
            offset = max(0, ts - (mt - dur)) if dur else 0
            st("preparing")
            os.makedirs(job["dir"], exist_ok=True)
            clip = os.path.join(job["dir"], "clip.mp4")
            if not remux(src, clip):
                st("error", "The video file is damaged and could not be repaired.")
                return
            out, info = clip, probe_streams(clip)
            if needs_web_copy(info):
                st("converting")
                ss = t = None
                if probe_duration(clip) > 180:  # long clip: only convert the part around the event
                    ss, t = max(0, offset - 30), 120
                web = os.path.join(job["dir"], "web.mp4")
                total = t if t else max(1.0, probe_duration(clip))
                if not make_web_copy(clip, web, info["w"], ss, t, lambda: job["cancel"], total,
                                     lambda f: job.update(progress=f), quick=True):
                    if not job["cancel"]:
                        st("error", "Converting the video failed.")
                    return
                out = web
                job["converted"] = True
                if ss is not None:
                    offset -= ss
            if not job["cancel"]:
                job.update(file=out, offset=round(offset), status="ready")
    except Exception as e:
        st("error", str(e))
    finally:
        if job["cancel"]:
            shutil.rmtree(job["dir"], ignore_errors=True)


def dir_size(path):
    total = 0
    for dp, _, fns in os.walk(path):
        for fn in fns:
            try:
                total += os.path.getsize(os.path.join(dp, fn))
            except OSError:
                pass
    return total


def cleanup():
    """Expire old resolved alerts, drop orphaned folders, enforce the size cap."""
    with lock:
        now = time.time()
        for a in list(alerts):
            if a["resolved"] and now - a.get("resolved_at", a["ts"]) > RESOLVED_DAYS * 86400:
                remove_alert(a)
        for jid, job in list(find_jobs.items()):
            if now - job["created"] > 1800:
                job["cancel"] = True
                find_jobs.pop(jid, None)
                shutil.rmtree(job["dir"], ignore_errors=True)
        known = {a["id"] for a in alerts}
        if os.path.isdir(ALERTS_DIR):
            for name in os.listdir(ALERTS_DIR):
                if name not in known:
                    shutil.rmtree(os.path.join(ALERTS_DIR, name), ignore_errors=True)
            total = dir_size(ALERTS_DIR)
            for a in sorted(alerts, key=lambda x: (not x["resolved"], x["ts"])):
                if total <= MAX_BYTES:
                    break
                total -= dir_size(os.path.join(ALERTS_DIR, a["id"]))
                print(f"[reoview] storage cap reached, removing alert {a['id']}", flush=True)
                remove_alert(a)
        jsave(ALERTS_JSON, alerts)


def remove_alert(a):
    alerts[:] = [x for x in alerts if x is not a]
    shutil.rmtree(os.path.join(ALERTS_DIR, a["id"]), ignore_errors=True)


def create_alert(cam, cat, raw):
    a = {"id": uuid.uuid4().hex[:10], "cam_id": cam["id"], "cam": cam["name"], "type": cat, "raw": raw,
         "ts": time.time(), "resolved": False, "snap": False, "thumb": False, "video": "",
         "storage": bool(cam.get("storage")), "pending": bool(cam.get("storage"))}
    with lock:
        alerts.append(a)
        open_ = sorted((x for x in alerts if x["type"] == cat and not x["resolved"]), key=lambda x: x["ts"])
        for old in open_[:-KEEP]:
            remove_alert(old)
        jsave(ALERTS_JSON, alerts)
    threading.Thread(target=capture_snap, args=(a, dict(cam)), daemon=True).start()
    if cam.get("storage"):
        threading.Thread(target=capture_video, args=(a, dict(cam)), daemon=True).start()


def pub_alert(a):
    keys = ("id", "cam_id", "cam", "type", "raw", "ts", "resolved", "storage", "snap", "thumb", "pending")
    return {**{k: a[k] for k in keys}, "video": bool(a["video"])}


def load_subs():
    global subs
    if os.path.exists(SUBS):
        with open(SUBS) as f:
            subs = json.load(f)


def save_subs():
    os.makedirs(DATA, exist_ok=True)
    with open(SUBS, "w") as f:
        json.dump(subs, f)


def init_vapid():
    os.makedirs(DATA, exist_ok=True)
    if os.path.exists(VAPID_KEY):
        v = Vapid.from_file(VAPID_KEY)
    else:
        v = Vapid()
        v.generate_keys()
        v.save_key(VAPID_KEY)
    raw = v.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def api_lock(cam):
    return api_locks.setdefault(cam["id"], threading.Lock())


def http_login(cam):
    body = [{"cmd": "Login", "param": {"User": {
        "Version": "0", "userName": cam["user"], "password": cam["password"]}}}]
    req = urllib.request.Request(
        f"http://{cam['ip']}/cgi-bin/api.cgi?cmd=Login", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        resp = json.load(r)[0]
    if resp.get("code") != 0:
        raise RuntimeError(f"login rejected: {json.dumps(resp.get('error', resp))}")
    t = resp["value"]["Token"]
    tokens[cam["id"]] = (t["name"], time.time() + min(t.get("leaseTime", 3600), 3600) - 120)
    return t["name"]


def cam_call(cam, cmd, param=None, action=None, query=None, timeout=8):
    """Call the camera API with a cached login token; log in again once if it fails."""
    last = None
    with api_lock(cam):
        for attempt in (0, 1):
            tok = tokens.get(cam["id"])
            tok = tok[0] if tok and tok[1] > time.time() else http_login(cam)
            q = urlencode({"cmd": cmd, "token": tok, **(query or {})})
            item = {"cmd": cmd, "param": param or {}}
            if action is not None:
                item["action"] = action
            req = urllib.request.Request(
                f"http://{cam['ip']}/cgi-bin/api.cgi?{q}", data=json.dumps([item]).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            if data[:2] == b"\xff\xd8":
                return data
            try:
                resp = json.loads(data)[0]
            except ValueError:
                resp = {"code": -1, "error": data[:80].decode(errors="replace")}
            if resp.get("code") == 0:
                return resp.get("value", {})
            last = json.dumps(resp.get("error", resp))
            tokens.pop(cam["id"], None)
    raise RuntimeError(last)


def cam_api(cam, cmd, param, action=None):
    return cam_call(cam, cmd, param, action)


def get_cam(cid):
    cam = next((c for c in cameras if c["id"] == cid), None)
    if not cam:
        abort(404)
    return cam


def fetch_devname(cam):
    try:
        return cam_api(cam, "GetDevInfo", {})["DevInfo"]["name"]
    except Exception as e:
        print(f"[reoview] GetDevInfo failed for {cam['ip']}: {e}", flush=True)
        return ""


def notify(cam, body, tag):
    payload = json.dumps({"title": cam["name"], "body": body, "tag": tag, "url": "/#alerts"})
    with lock:
        current = list(subs)
    dead = []
    print(f"[reoview] alert for {cam['name']}: sending to {len(current)} subscription(s)", flush=True)
    for sub in current:
        try:
            r = webpush(sub, payload, vapid_private_key=VAPID_KEY, vapid_claims={"sub": VAPID_SUB},
                        ttl=3600, headers={"Urgency": "high"})
            print(f"[reoview] push sent: HTTP {r.status_code} via {sub['endpoint'][:40]}...", flush=True)
        except WebPushException as e:
            code = getattr(e.response, "status_code", None)
            print(f"[reoview] push failed ({code}): {e}", flush=True)
            if code in (404, 410):
                dead.append(sub["endpoint"])
        except Exception as e:
            print(f"[reoview] push error: {e}", flush=True)
    if dead:
        with lock:
            subs[:] = [x for x in subs if x["endpoint"] not in dead]
            save_subs()


def stop(cid):
    entry = players.pop(cid, None)
    if entry:
        entry[0].terminate()
        try:
            entry[0].wait(5)
        except subprocess.TimeoutExpired:
            entry[0].kill()


def start(cam):
    stop(cam["id"])
    d = os.path.join(HLS, cam["id"])
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    auth = ""
    if cam["user"]:
        auth = f'{quote(cam["user"], safe="")}:{quote(cam["password"], safe="")}@'
    url = f'rtsp://{auth}{cam["ip"]}:{cam["port"]}/{cam["path"]}'
    if TRANSCODE:
        video = ["-vf", "scale=640:-2,fps=15", "-c:v", "libx264", "-preset", "ultrafast",
                 "-tune", "zerolatency", "-profile:v", "baseline", "-pix_fmt", "yuv420p", "-g", "30"]
    else:
        video = ["-c:v", "copy"]
    cmd = (["ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
            "-timeout", "10000000", "-use_wallclock_as_timestamps", "1", "-i", url, "-an"] + video +
           ["-f", "hls", "-hls_time", "2", "-hls_list_size", "6",
            "-hls_flags", "delete_segments", "-hls_segment_filename", f"{d}/seg%05d.ts",
            f"{d}/index.m3u8"])
    print(f"[reoview] start {cam['id']} {cam['ip']}:{cam['port']}/{cam['path']} transcode={TRANSCODE}", flush=True)
    players[cam["id"]] = (subprocess.Popen(cmd), time.time())


def monitor():
    last_clean = 0
    while True:
        time.sleep(10)
        with lock:
            for cam in list(cameras):
                cid = cam["id"]
                entry = players.get(cid)
                if not entry:
                    start(cam)
                    continue
                proc, t0 = entry
                idx = os.path.join(HLS, cid, "index.m3u8")
                fresh = os.path.exists(idx) and time.time() - os.path.getmtime(idx) <= 20
                if fresh:
                    backoff.pop(cid, None)
                stale = time.time() - t0 > 20 and not fresh
                if stale or proc.poll() is not None:
                    fails, not_before = backoff.get(cid, (0, 0))
                    if time.time() < not_before:
                        continue
                    backoff[cid] = (fails + 1, time.time() + min(120, 10 * 2 ** fails))
                    why = "stalled" if proc.poll() is None else f"exited ({proc.returncode})"
                    print(f"[reoview] restarting {cam['name']}: {why}", flush=True)
                    start(cam)
        if time.time() - last_clean > 600:
            last_clean = time.time()
            cleanup()
            threading.Thread(target=storage_maintain, daemon=True).start()


def public(cam):
    return {**{k: cam[k] for k in ("id", "name", "ip")}, "storage": cam.get("storage", "")}


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/sw.js")
def sw():
    r = send_from_directory("static", "sw.js")
    r.headers["Cache-Control"] = "no-cache"
    return r


@app.get("/api/cameras")
def list_cameras():
    return jsonify([public(c) for c in cameras])


@app.post("/api/cameras")
def add_camera():
    j = request.get_json(force=True, silent=True) or {}
    try:
        ip = str(ipaddress.ip_address(str(j.get("ip", "")).strip()))
    except ValueError:
        return jsonify(error="Invalid IP address"), 400
    storage = (j.get("storage") or "").strip()
    if storage and storage not in list_storage():
        return jsonify(error="Storage folder not found"), 400
    user = (j.get("user") or "").strip()
    pw = j.get("password") or ""
    typed = (j.get("name") or "").strip()[:40]
    cam = {
        "id": uuid.uuid4().hex[:8],
        "name": typed or ip,
        "devname": "",
        "ip": ip,
        "port": int(j.get("port") or 554),
        "user": (j.get("user") or "").strip(),
        "password": j.get("password") or "",
        "path": (j.get("path") or "h264Preview_01_sub").strip().lstrip("/"),
        "storage": storage,
    }
    if user:
        cam["devname"] = fetch_devname(cam)
        if not typed and cam["devname"]:
            cam["name"] = cam["devname"]
    with lock:
        cameras.append(cam)
        save()
        start(cam)
    return jsonify(public(cam)), 201


@app.delete("/api/cameras/<cid>")
def delete_camera(cid):
    with lock:
        cameras[:] = [c for c in cameras if c["id"] != cid]
        save()
        stop(cid)
    shutil.rmtree(os.path.join(HLS, cid), ignore_errors=True)
    return "", 204


@app.post("/api/webhook")
def webhook():
    j = request.get_json(force=True, silent=True) or {}
    print("[reoview] webhook from", request.remote_addr, json.dumps({**j, "secret": "***"}), flush=True)
    if WEBHOOK_SECRET and j.get("secret") != WEBHOOK_SECRET:
        abort(403)
    a = j.get("alarm") or {}
    keys = {str(a.get(k, "")).strip().casefold() for k in ("name", "channelName", "device")} - {""}
    cam = next((c for c in cameras if c["ip"] == request.remote_addr), None) or next(
        (c for c in cameras if c.get("devname") and c["devname"].casefold() in keys), None
    )
    cat = category(a.get("type"))
    if not cam:
        print("[reoview] webhook ignored: no matching camera", flush=True)
        log_detection(a.get("channelName") or a.get("device") or request.remote_addr, a.get("type") or "", cat, "unknown camera")
        return "", 204
    why = filter_reason(cat)
    if why:
        print(f"[reoview] webhook filtered ({why}): {cat} from {cam['name']}", flush=True)
        log_detection(cam["name"], a.get("type") or "", cat, f"filtered ({why})", cam["id"])
        return "", 204
    now = time.time()
    key = (cam["id"], cat)
    if now - last_alert.get(key, 0) < COOLDOWN:
        print(f"[reoview] webhook ignored: cooldown for {cam['name']} ({cat})", flush=True)
        log_detection(cam["name"], a.get("type") or "", cat, "cooldown", cam["id"])
        return "", 204
    last_alert[key] = now
    log_detection(cam["name"], a.get("type") or "", cat, "alert", cam["id"])
    create_alert(cam, cat, a.get("type") or "")
    body = (f"{cat.capitalize()} detected" if cat != "other"
            else str(a.get("title") or a.get("name") or a.get("message") or "Alert")[:120])
    threading.Thread(target=notify, args=(cam, body, f"{cam['id']}-{cat}"), daemon=True).start()
    return "", 204


@app.get("/api/push/key")
def push_key():
    return jsonify(key=VAPID_PUB)


@app.post("/api/push/subscribe")
def push_subscribe():
    sub = request.get_json(force=True, silent=True) or {}
    if not sub.get("endpoint"):
        return jsonify(error="Invalid subscription"), 400
    with lock:
        subs[:] = [x for x in subs if x["endpoint"] != sub["endpoint"]] + [sub]
        save_subs()
    return "", 204


@app.post("/api/push/unsubscribe")
def push_unsubscribe():
    ep = (request.get_json(force=True, silent=True) or {}).get("endpoint")
    with lock:
        subs[:] = [x for x in subs if x["endpoint"] != ep]
        save_subs()
    return "", 204


@app.get("/api/storage")
def storage_dirs():
    return jsonify(list_storage())


@app.get("/api/settings")
def get_settings():
    return jsonify(settings)


@app.put("/api/settings")
def put_settings():
    j = request.get_json(force=True, silent=True) or {}
    with lock:
        for k in ("start", "end"):
            v = str(j.get(k, ""))
            if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
                settings[k] = v
        if "away" in j:
            settings["away"] = bool(j["away"])
        if isinstance(j.get("types"), dict):
            settings["types"] = {k: bool(j["types"].get(k, settings["types"][k])) for k in TYPE_KEYS}
        for k in ("retention_on", "lowspace_on"):
            if k in j:
                settings[k] = bool(j[k])
        for k, lo, hi in (("retention_days", 1, 3650), ("lowspace_gb", 1, 100000)):
            try:
                settings[k] = min(hi, max(lo, int(j[k])))
            except (KeyError, ValueError, TypeError):
                pass
        jsave(SETTINGS_JSON, settings)
    threading.Thread(target=storage_maintain, daemon=True).start()
    return jsonify(settings)


@app.get("/api/storage/usage")
def storage_usage_api():
    global usage_cache
    if time.time() - usage_cache[0] > 30:
        usage_cache = (time.time(), storage_usage())
    return jsonify(usage_cache[1])


@app.post("/api/storage/prune")
def storage_prune():
    global usage_cache
    j = request.get_json(force=True, silent=True) or {}
    try:
        days = int(j.get("days"))
    except (TypeError, ValueError):
        return jsonify(error="Invalid number of days"), 400
    if days < 1:
        return jsonify(error="Minimum is 1 day"), 400
    dry = bool(j.get("dry"))
    if not dry and not os.access(STORAGE, os.W_OK):
        return jsonify(error="Storage is mounted read-only. Remove :ro from the volume in docker-compose.yml."), 409
    n, b = prune_older(days, dry)
    usage_cache = (0, None)
    return jsonify(files=n, bytes=b)


@app.post("/api/findvideo")
def find_start():
    j = request.get_json(force=True, silent=True) or {}
    cam = next((c for c in cameras if c["id"] == j.get("cam_id")), None)
    if not cam:
        return jsonify(error="Unknown camera"), 404
    if not cam.get("storage"):
        return jsonify(error="Storage for this camera hasn't been set up."), 400
    try:
        ts = float(j.get("ts"))
    except (TypeError, ValueError):
        return jsonify(error="Invalid time"), 400
    jid = uuid.uuid4().hex[:10]
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(ts))
    find_jobs[jid] = {"status": "queued", "msg": "", "file": None, "offset": 0, "created": time.time(),
                      "cancel": False, "progress": 0, "src": "", "converted": False, "dir": os.path.join(FIND_DIR, jid),
                      "name": re.sub(r"[^\w.-]+", "_", f"{cam['name']}_{stamp}.mp4")}
    threading.Thread(target=find_job, args=(jid, dict(cam), ts), daemon=True).start()
    return jsonify(job=jid)


@app.get("/api/findvideo/<jid>")
def find_status(jid):
    job = find_jobs.get(jid) or abort(404)
    return jsonify(status=job["status"], msg=job["msg"], offset=job["offset"], progress=job["progress"],
                   converted=job["converted"])


@app.get("/api/findvideo/<jid>/file")
def find_file(jid):
    job = find_jobs.get(jid)
    if not job or job["status"] != "ready":
        abort(404)
    return send_from_directory(os.path.dirname(job["file"]), os.path.basename(job["file"]),
                               as_attachment=bool(request.args.get("download")), download_name=job["name"])


@app.get("/api/findvideo/<jid>/original")
def find_original(jid):
    job = find_jobs.get(jid)
    if not job or job["status"] != "ready" or not os.path.isfile(job["src"]):
        abort(404)
    return send_file(job["src"], as_attachment=True, download_name=os.path.basename(job["src"]))


@app.delete("/api/findvideo/<jid>")
def find_delete(jid):
    job = find_jobs.pop(jid, None)
    if job:
        job["cancel"] = True
        shutil.rmtree(job["dir"], ignore_errors=True)
    return "", 204


@app.get("/api/logs")
def get_logs():
    return jsonify(list(reversed(detections)))


@app.get("/api/alerts")
def list_alerts():
    return jsonify([pub_alert(a) for a in sorted(alerts, key=lambda x: -x["ts"])])


@app.post("/api/alerts/<aid>/resolve")
def resolve_alert(aid):
    with lock:
        a = next((x for x in alerts if x["id"] == aid), None)
        if not a:
            abort(404)
        a["resolved"] = True
        a["resolved_at"] = time.time()
        jsave(ALERTS_JSON, alerts)
    return "", 204


@app.delete("/api/alerts/<aid>")
def delete_alert(aid):
    with lock:
        a = next((x for x in alerts if x["id"] == aid), None)
        if not a:
            abort(404)
        if not a["resolved"]:
            return jsonify(error="Resolve the alert first"), 409
        remove_alert(a)
        jsave(ALERTS_JSON, alerts)
    return "", 204


@app.get("/alerts/<aid>/<kind>")
def alert_file(aid, kind):
    a = next((x for x in alerts if x["id"] == aid), None)
    if not a:
        abort(404)
    if kind in ("snap", "thumb") and a.get(kind):
        fn, ext = kind + ".jpg", ".jpg"
    elif kind == "video" and a["video"]:
        fn, ext = "video" + a["video"], a["video"]
        if a.get("video_web") and not request.args.get("download"):
            fn = "video_web.mp4"
    else:
        abort(404)
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(a["ts"]))
    name = re.sub(r"[^\w.-]+", "_", f"{a['cam']}_{stamp}_{kind}{ext}")
    return send_from_directory(os.path.join(ALERTS_DIR, aid), fn,
                               as_attachment=bool(request.args.get("download")), download_name=name)


@app.get("/api/cameras/<cid>/light")
def light_state(cid):
    cam = get_cam(cid)
    try:
        v = cam_api(cam, "GetWhiteLed", {"channel": 0}, 0)["WhiteLed"]
        light_cfg[cid] = (time.time(), v.get("mode", 1), v.get("bright", 100))
        return jsonify(on=bool(v.get("state")))
    except Exception as e:
        return jsonify(error=str(e)), 502


@app.post("/api/cameras/<cid>/light")
def light_set(cid):
    cam = get_cam(cid)
    on = bool((request.get_json(force=True, silent=True) or {}).get("on"))
    try:
        cfg = light_cfg.get(cid)
        if not cfg or time.time() - cfg[0] > 60:
            cur = cam_api(cam, "GetWhiteLed", {"channel": 0}, 0)["WhiteLed"]
            cfg = (time.time(), cur.get("mode", 1), cur.get("bright", 100))
        cam_api(cam, "SetWhiteLed", {"WhiteLed": {
            "channel": 0, "state": int(on), "mode": cfg[1], "bright": cfg[2]}})
        return jsonify(on=on)
    except Exception as e:
        return jsonify(error=str(e)), 502


@app.post("/api/cameras/<cid>/siren")
def siren_set(cid):
    cam = get_cam(cid)
    on = bool((request.get_json(force=True, silent=True) or {}).get("on"))
    try:
        cam_api(cam, "AudioAlarmPlay", {
            "alarm_mode": "manul", "manual_switch": int(on), "times": 1, "channel": 0}, 0)
        return jsonify(on=on)
    except Exception as e:
        return jsonify(error=str(e)), 502


@app.get("/hls/<cid>/<path:name>")
def hls(cid, name):
    if cid not in {c["id"] for c in cameras}:
        abort(404)
    r = send_from_directory(os.path.join(HLS, cid), name)
    r.headers["Cache-Control"] = "no-store"
    return r


def stop_all():
    for cid in list(players):
        stop(cid)


atexit.register(stop_all)
load()
load_subs()
load_state()
VAPID_PUB = init_vapid()
for _c in cameras:
    start(_c)
threading.Thread(target=monitor, daemon=True).start()
