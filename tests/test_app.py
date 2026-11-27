"""Run in the image: docker run --rm -v "$PWD/tests:/app/tests" reoview python3 -m unittest discover -s tests"""
import base64, io, json, os, sys, tempfile, unittest, urllib.error
from unittest import mock

tmp = tempfile.mkdtemp()
os.environ.update(DATA_DIR=tmp, HLS_DIR=os.path.join(tmp, "hls"), STORAGE_ROOT=os.path.join(tmp, "st"),
                  REOVIEW_APK_CHECK_H="0")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app  # noqa: E402

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402


def resp(obj):
    return io.BytesIO(obj if isinstance(obj, bytes) else json.dumps(obj).encode())


def unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Reorder(unittest.TestCase):
    def test_move(self):
        app.cameras[:] = [{"id": i, "name": i, "ip": "10.0.0.1", "port": 554, "user": "", "path": "p"} for i in "abc"]
        c = app.app.test_client()
        with mock.patch.object(app, "save"):
            self.assertEqual([x["id"] for x in c.post("/api/cameras/b/move", json={"dir": "left"}).json], list("bac"))
            self.assertEqual([x["id"] for x in c.post("/api/cameras/b/move", json={"dir": "left"}).json], list("bac"))
            self.assertEqual([x["id"] for x in c.post("/api/cameras/c/move", json={"dir": "right"}).json], list("bac"))
            self.assertEqual([x["id"] for x in c.post("/api/cameras/a/move", json={"dir": "right"}).json], list("bca"))
            self.assertEqual(c.post("/api/cameras/zz/move", json={}).status_code, 404)


class Fcm(unittest.TestCase):
    def setUp(self):
        self.pk = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = self.pk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()).decode()
        self.key = {"client_email": "x@y.iam", "token_uri": "https://oauth2.example/token", "private_key": pem,
                    "project_id": "proj"}
        app.fcm_auth = ("", 0)

    def test_token_jwt_is_signed(self):
        sent = []
        with mock.patch("urllib.request.urlopen", lambda req, timeout: sent.append(req) or resp(
                {"access_token": "AT", "expires_in": 3600})):
            self.assertEqual(app.fcm_access_token(self.key), "AT")
            self.assertEqual(app.fcm_access_token(self.key), "AT")  # cached
        self.assertEqual(len(sent), 1)
        jwt = dict(x.split("=") for x in sent[0].data.decode().split("&"))["assertion"]
        h, c, s = jwt.split(".")
        self.pk.public_key().verify(unb64(s), f"{h}.{c}".encode(), padding.PKCS1v15(), hashes.SHA256())
        claims = json.loads(unb64(c))
        self.assertEqual(claims["aud"], self.key["token_uri"])
        self.assertEqual(claims["exp"] - claims["iat"], 3600)

    def test_send_drops_unregistered(self):
        app.fcm_auth = ("AT", 1e12)
        app.fcm_tokens[:] = ["good" * 10, "gone" * 10]

        def fake(req, timeout):
            if json.loads(req.data)["message"]["token"].startswith("gone"):
                raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, resp(b"{}"))
            self.assertEqual(req.headers["Authorization"], "Bearer AT")
            return resp({})
        with mock.patch.object(app, "jload", return_value=self.key), mock.patch("urllib.request.urlopen", fake):
            app.notify_fcm({"title": "t", "body": "b", "tag": "x", "url": "/#alerts"})
        self.assertEqual(app.fcm_tokens, ["good" * 10])

    def test_register_validates(self):
        c = app.app.test_client()
        self.assertEqual(c.post("/api/fcm/register", json={"token": "short"}).status_code, 400)
        self.assertEqual(c.post("/api/fcm/register", json={"token": "t" * 30}).status_code, 204)
        self.assertIn("t" * 30, app.fcm_tokens)


class Apk(unittest.TestCase):
    def test_fetch_and_serve(self):
        os.makedirs(app.APK_DIR, exist_ok=True)
        open(os.path.join(app.APK_DIR, "reoview-1.0.9.apk"), "wb").write(b"old")
        release = {"assets": [{"name": "../evil.apk", "size": 1, "browser_download_url": "https://x/0"},
                              {"name": "reoview-1.0.10.apk", "size": 5, "browser_download_url": "https://x/1"}]}
        with mock.patch("urllib.request.urlopen",
                        lambda req, timeout: resp(release if "api.github.com" in req.full_url else b"APKv2")):
            app.fetch_apk()
        self.assertEqual(sorted(os.listdir(app.APK_DIR)), ["reoview-1.0.10.apk"])  # 1.0.10 > 1.0.9 numerically
        c = app.app.test_client()
        self.assertEqual(c.get("/api/app").json["version"], "1.0.10")
        r = c.get("/download/app")
        self.assertEqual(r.data, b"APKv2")
        self.assertIn("reoview-1.0.10.apk", r.headers["Content-Disposition"])


if __name__ == "__main__":
    unittest.main()
