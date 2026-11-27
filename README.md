> Disclaimer: Due to ongoing health issues I am no longer able to concentrate on developing software by myself. This project is therefore vibe-coded: written together with an AI assistant (Claude) and directed, tested and run by me. It is not a demo or proof of concept. It exists because I need it: it fixes a real problem in my day-to-day life and is in daily use. Read the code and the security notes below with that in mind.

# Reoview

Self-hosted web UI for local Reolink cameras, built to run as one Docker container on a Raspberry Pi. It shows all camera streams on one page (desktop and mobile), turns camera webhooks into filtered alerts with photo and video, and sends push notifications to an Android app (Firebase Cloud Messaging) or a PWA.

## Features

- Live view of all cameras (RTSP or HTTP-FLV → HLS in the browser), fullscreen modal per camera
- Cameras are added and edited (including the protocol, RTSP or HTTP-FLV) by IP in the UI; the list is shared by every visitor (no login). Leaving the password empty when editing keeps the stored one
- Camera controls: spotlight on/off, siren on/off, move left/right to change the camera order (saved for everyone)
- Webhook receiver for Reolink's default webhook, with filtering by time window, "away from home" mode and detection type (person, animal, vehicle, any)
- Alerts tab: per alert a snapshot, the live stream and a saved video, with resolve/delete
- Android app (APK) with push notifications through Firebase Cloud Messaging; the server can download new app releases from GitHub and offer them on its "Get the app" page
- Push notifications (Web Push / VAPID) to the installed PWA
- Storage management for the camera FTP folder (age-based deletion, free-space guard, one-time prune)
- Logs tab: last 100 reported detections, with a "Find video" search for any entry
- PWA: manifest, icons and a service worker

## Architecture

One Flask app (`app.py`) served by gunicorn (1 worker, 16 threads) plus a vanilla-JS single page (`static/index.html`). No database: state is JSON files in `DATA_DIR`. All state is held in memory in a single process, which is why there is exactly one worker.

```
Camera ──RTSP / HTTP-FLV──► ffmpeg (one subprocess per camera) ──HLS segments──► /tmp/hls (tmpfs) ──► browser (hls.js)
Camera ──HTTP webhook──► POST /api/webhook ──► filter ──► alert (snapshot + video) ──► Web Push
Camera ──FTP──► (external FTP server) ──► /storage (mounted) ──► video copied into the alert
```

**Streams.** For each camera, `ffmpeg` reads the RTSP stream over TCP (or, if the camera was added with protocol HTTP-FLV, `http://<ip>/flv?port=1935&app=bcs&stream=<stream>`; default stream `channel0_sub.bcs`, port 80) and writes a rolling HLS playlist (2 s segments, 6 kept) to `HLS_DIR`. By default the video is copied without re-encoding (`REOVIEW_TRANSCODE=0`); with `1` it is re-encoded to H.264 baseline, 640px wide, 15 fps. A monitor thread checks every 10 s and restarts a camera whose process exited or whose playlist was not updated for 20 s, with exponential backoff (10 s up to 120 s). ffmpeg's error output is logged with the camera password masked. On every restart a diagnostic line shows whether port 80 and the RTSP port answer. With `REOVIEW_AUTO_REBOOT=1`, a camera whose RTSP port refuses connections while its HTTP API answers is rebooted via the API (`Reboot`) after `REOVIEW_REBOOT_AFTER` failed restarts, at most once per `REOVIEW_REBOOT_COOLDOWN_MIN`. The reboot call is untested on real hardware. On shutdown all ffmpeg processes are stopped in parallel (SIGTERM, so RTSP TEARDOWN is sent; SIGKILL after 8 s); `stop_grace_period` is set to 30 s so Docker does not kill the container earlier. The page plays streams with hls.js (downloaded at image build time), only while the Camera(s) tab is open; in the Android app streams keep running while other tabs are open, so returning to the cameras does not reload them. A card shows a spinner on a grey background until its stream plays.

**Camera API.** Snapshots, device name, spotlight and siren use the camera's HTTP API (`/cgi-bin/api.cgi`). Reoview logs in once per camera, caches the token (about 1 h) and serialises calls per camera, with one re-login on failure. Snapshots are retried up to 3 times. `Snap` uses the substream by default (`REOVIEW_SNAP_TYPE`).

**Webhook and filtering.** `POST /api/webhook` maps the alert to an added camera by the sender's IP, or else by the device name in the payload. Reolink's `type` is mapped to a category: `PEOPLE`/`PERSON` → person, `VEHICLE`/`CAR` → vehicle, `DOG_CAT`/`ANIMAL`/`PET` → animal, everything else → other. An alert is created only if (a) "away from home" is on or the current local time is inside the configured window (equal start and end means all day) and (b) "any" or that category is enabled. A per-camera, per-type cooldown (`REOVIEW_COOLDOWN`) suppresses repeats. Every webhook, whatever its outcome, is added to the detection log (last 100).

**Alerts.** An alert stores a snapshot (plus a 640px thumbnail) and, if the camera has a storage folder, a video. At most 5 unresolved alerts per type are kept; older ones are dropped with their files. Resolved alerts can be deleted and are removed automatically after `REOVIEW_RESOLVED_DAYS`. The total size of `DATA_DIR/alerts` is capped by `REOVIEW_MAX_ALERT_GB` (resolved alerts are removed first, then the oldest).

**Video capture.** The newest video file in the camera's storage folder (searched recursively) is used; if it is smaller than `REOVIEW_MIN_VIDEO_KB`, the second newest is used. Reoview waits until the file stops changing (up to `REOVIEW_VIDEO_WAIT_S`), then rewrites it with ffmpeg (`-c copy`, faststart) to repair truncated files. If the clip is not H.264/AAC or wider than 1920px, a 1280px H.264/AAC copy is made in the background for playback; downloads give the original.

**Find video.** For a log entry, Reoview looks for the clip that was recording at that time (file mtime minus duration), repairs it and makes a small preview (640px, 10 fps), starting at the detection. Only one search/convert runs at a time. The preview is deleted when the window is closed, after 30 minutes, and at container start.

**Storage management.** Optional age-based deletion and a free-space guard (deletes oldest files first, never files modified in the last 10 minutes), run every 10 minutes and on settings save, plus a one-time prune that shows a count before deleting. Only `.mp4 .mov .avi .mkv .jpg .jpeg .png` are touched. This needs the storage mount to be writable.

**Android app.** `android/` is a small Kotlin app: one WebView showing the Reoview server (address entered on first start, asked again when the page cannot load), a Firebase messaging service and a download handler that hands file downloads (videos, photos, APKs) to Android's download manager, saving them in Downloads. Video fullscreen and the back button work. On start the app registers its FCM token with `POST /api/fcm/register`; tokens are stored in `fcm_tokens.json` and removed when FCM reports them as unregistered. Each alert is sent to every token as a high-priority data message, and the app shows it as a notification that opens the Alerts tab. FCM is used only if `DATA_DIR/fcm-service-account.json` (a Firebase service-account key) exists; web push keeps working alongside. The web UI recognises the app by `ReoviewApp/<version>` in the user agent and hides the PWA notification switch and the "Get the app" button there. In its place it shows "Update to <version>" when the server holds a newer APK (checked on start and every 10 minutes); tapping it downloads the APK and the app opens the Android installer when the download finishes (Android asks once to allow installs from Reoview). Requires Android 10 or newer.

**App releases.** The workflow `.github/workflows/android.yml` runs on every push to `main` that changes `android/` (or by hand from the Actions tab). It builds a signed APK and creates a GitHub release `v<version>` with the asset `reoview-<version>.apk`. The version is `<major.minor from android/VERSION>.<workflow run number>`, the run number is also the Android `versionCode`, so every build is newer than the last. Bump `android/VERSION` for bigger changes. The server checks the latest release of `REOVIEW_APK_REPO` every `REOVIEW_APK_CHECK_H` hours, downloads a newer APK to `DATA_DIR/apk/` (the previous one is deleted) and serves it on the "Get the app" page (`/download/app`). An APK named `reoview-<version>.apk` that you place in that folder yourself is offered too.

**Push.** A VAPID key pair is generated on first start (`DATA_DIR/vapid_private.pem`). Each device subscribes from Settings; subscriptions are stored in `subscriptions.json` and removed when the push service reports 404/410. Pushes are sent with urgency high and a 1 h TTL. Browsers require HTTPS (or localhost) for service workers and push; on iOS the app must be added to the Home Screen.

## Deployment

Requirements: Docker with Compose. The image is built on `debian:bookworm-slim`; Python packages that need compiling (`cryptography`, `aiohttp`, `requests`, Flask) come from apt, so it builds on 32-bit ARM without a compiler. Developed and run on a Raspberry Pi with Ubuntu (armhf). The first build is slow on a Pi; later builds reuse cached layers.

```bash
git clone https://github.com/lyvielle/reoview
cd reoview
# optional: set DATA_DIR, STORAGE_DIR, TZ, ... as environment variables (see below)
docker compose up -d --build
```

Open `http://<host>:8000`, add cameras with the button at the bottom, and set up notifications under Settings, or install the Android app from "Get the app" in the sidebar.

**Camera setup.** In each camera's web UI, enable RTSP and set Push → Webhook to `http://<host-lan-ip>:8000/api/webhook` (content: Default). Alerts are matched by the sender IP, so the camera must post directly to the container, not through a reverse proxy. Videos come from the camera's FTP uploads: run an FTP server on the host that stores into the folder you mount at `/storage` (one subfolder per camera), then pick that subfolder when adding the camera. The FTP server is not part of this project.

**HTTPS / PWA.** Installing the PWA and push need HTTPS. Plain HTTP works for viewing only. One option is `tailscale serve` in front of port 8000 (own HTTPS port) while the camera webhook keeps using the LAN HTTP address.

**Firebase (Android push).** One-time setup:

1. In the [Firebase console](https://console.firebase.google.com) create a project (Analytics not needed), then add an Android app with package name `io.github.lyvielle.reoview`.
2. Download its `google-services.json` and store the file's content as the repository secret `GOOGLE_SERVICES_JSON` (`gh secret set GOOGLE_SERVICES_JSON < google-services.json`).
3. In Project settings → Service accounts, generate a new private key and save it on the server as `DATA_DIR/fcm-service-account.json`. Keep it private: it can send messages to every app install.

**Signing.** Release APKs are signed with one key so that updates install over the previous version. The workflow reads it from the secrets `ANDROID_KEYSTORE_BASE64` (the PKCS12 keystore, base64) and `ANDROID_KEYSTORE_PASSWORD` (store and key password, alias `reoview`). Keep a backup of the keystore: if it is lost, the app has to be uninstalled before a build signed with a new key can be installed.

## Configuration

Set as environment variables when running Compose (or edit the defaults in `docker-compose.yml`). Values shown are the defaults.

| Variable | Default | Meaning |
|---|---|---|
| `TZ` | `Europe/Berlin` | Time zone for the alert schedule |
| `DATA_DIR` | `/data` | In the container (fixed in Compose, where `DATA_DIR` means the host folder, see below). State: cameras, settings, alerts, detections, push keys |
| `HLS_DIR` | `/tmp/hls` | In the container (fixed in Compose). Live stream segments (tmpfs) |
| `STORAGE_ROOT` | `/storage` | In the container (fixed in Compose). Mounted camera video folders |
| `REOVIEW_TRANSCODE` | `0` | `0` copy the camera stream, `1` re-encode (640px, 15 fps) |
| `REOVIEW_SNAP_TYPE` | `sub` | Alert photo from the `sub` or `main` stream |
| `REOVIEW_COOLDOWN` | `30` | Seconds between alerts per camera and type |
| `REOVIEW_WEBHOOK_SECRET` | empty | If set, webhooks must carry this secret |
| `REOVIEW_VAPID_SUB` | `mailto:reoview@example.com` | Contact sent to the push service; set your own |
| `REOVIEW_MIN_VIDEO_KB` | `500` | Smaller newest clip → use the previous one |
| `REOVIEW_VIDEO_WAIT_S` | `300` | Max wait for a clip to finish recording |
| `REOVIEW_VIDEO_SETTLE_S` | `10` | Seconds a clip must stay unchanged to count as finished |
| `REOVIEW_RESOLVED_DAYS` | `7` | Delete resolved alerts after this many days |
| `REOVIEW_MAX_ALERT_GB` | `3` | Size cap for stored alert photos and videos |
| `REOVIEW_AUTO_REBOOT` | `0` | `1` reboots a camera whose RTSP is refused while its HTTP API answers |
| `REOVIEW_REBOOT_AFTER` | `5` | Consecutive failed restarts before such a reboot |
| `REOVIEW_REBOOT_COOLDOWN_MIN` | `60` | Minimum minutes between reboots of one camera |
| `REOVIEW_APK_REPO` | `lyvielle/reoview` | GitHub repository whose latest release provides the APK |
| `REOVIEW_APK_CHECK_H` | `6` | Hours between checks for a new APK release; `0` turns it off |
| `REOVIEW_PORT` | `8000` | Host port (Compose only) |
| `REOVIEW_STOP_GRACE` | `30s` | Compose `stop_grace_period` (Compose only) |
| `DATA_DIR` | `./data` | Host folder mounted at `/data` (Compose only; the container path stays `/data`) |
| `STORAGE_DIR` | `/media/reolink` | Host folder mounted at `/storage` (Compose only) |

Alert schedule, away mode, detection types and storage rules are set in the UI and stored in `DATA_DIR/settings.json`. They apply to every visitor.

## Repository layout

```
app.py                      Flask app: API, ffmpeg streams, webhook, alerts, storage, push
static/index.html           Single-page UI (HTML, CSS, JS)
static/sw.js                Service worker: shell cache, push, notification click
static/manifest.webmanifest PWA manifest
static/icon-*.png           PWA / favicon icons
Dockerfile                  Image (Debian bookworm, ffmpeg, gunicorn)
docker-compose.yml          Service, env vars, volumes, tmpfs
requirements.txt            pywebpush, py-vapid, http-ece (installed with --no-deps)
tests/test_app.py           Server tests (reorder, FCM, APK download); run in the image, see the file header
android/                    Android app (Kotlin, Gradle); VERSION holds major.minor
.github/workflows/android.yml  Builds, signs and releases the APK
```

`DATA_DIR` contents at runtime: `cameras.json`, `settings.json`, `alerts.json`, `detections.json`, `subscriptions.json`, `vapid_private.pem`, `fcm_tokens.json`, `fcm-service-account.json` (you add it), `alerts/`, `find/`, `apk/`.

## Security notes

- **There is no authentication.** Anyone who can reach port 8000 can view all streams, add or remove cameras, trigger the spotlight and siren, change settings, delete files in the storage folder and download recordings. Run it only on a trusted LAN or behind a VPN such as Tailscale; do not expose it to the internet.
- **There is no encryption in the app.** It serves plain HTTP; HTTPS must come from a proxy or `tailscale serve`.
- **Camera passwords are stored in plain text** in `DATA_DIR/cameras.json` (never returned by the API). Protect that folder. They are also part of the stream URL handed to ffmpeg, so they appear in the container's process list. ffmpeg's error output is masked before it is logged. Do not paste logs publicly.
- **The webhook secret is optional** and empty by default; without it anyone on the network can post fake alerts (they only affect cameras that were added). The secret is masked in logs, but the rest of each webhook payload is logged.
- The `/storage` mount is writable and Reoview can delete files in it (storage management, and only the image/video extensions listed above).
- The IP field accepts any valid IP address, not just private ranges, and Reoview will send camera API requests to it.
- Anyone who can reach the server can register an FCM token and then receive alert notifications (camera name and detection type), like web push subscriptions.
- The Android app allows plain HTTP (LAN use) and keeps no credentials; it has the same access as the browser.
- The VAPID private key in `DATA_DIR` allows sending pushes to subscribed devices. Keep it out of version control (it is in `.gitignore`).

## Limitations

- Developed against Reolink Duo 2 WiFi cameras. Other models may differ in stream paths (default `h264Preview_01_sub`), the Snap/spotlight commands or webhook content.
- `PEOPLE` and `ANIMAL` have been seen in real webhooks. The vehicle mapping (`VEHICLE`/`CAR`) and the `DOG_CAT`/`PET` names are assumptions and untested.
- The siren command (`AudioAlarmPlay`) is implemented from documentation and has not been tested on a real camera. The siren state cannot be read back, so the button only reflects what was pressed in Reoview.
- Single process and single worker; state lives in memory and in JSON files, so it does not scale horizontally.
- Transcoding is CPU-heavy on a Pi; the default is to copy streams. Converting wide (e.g. 4608px) clips for playback can take minutes.
- Few automated tests (`tests/`, server only). Most behaviour was checked by hand and with ad-hoc scripts against a fake camera and generated media files. The Android app was build-tested but not yet run against a real Firebase project at the time it was added.
- A converted "Find video" copy is deleted when its window closes; in the Android app, wait for the download to finish before closing it.
- Settings, camera list and alerts are global; there are no users or per-user preferences (push subscriptions are per device).
