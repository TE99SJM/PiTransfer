"""Small WSGI app; production runs one threaded Gunicorn worker on loopback."""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path

from core import Hardware, Transfers

WEB = Path(__file__).parent / "web"


class App:
    def __init__(self, config, hardware=None):
        self.config = config
        self.hardware = hardware or Hardware(config)
        self.transfers = Transfers(self.hardware, config["history_path"])
        self.sessions = {}
        self.failures = []
        self.auth_lock = threading.RLock()
        self.assets = {"/": (WEB / "index.html").read_bytes(), "/app.js": (WEB / "app.js").read_bytes(), "/style.css": (WEB / "style.css").read_bytes()}

    def __call__(self, env, start):
        status, payload, headers = "200 OK", None, []
        ctype = "application/json; charset=utf-8"
        try:
            host = env.get("HTTP_HOST", "")
            allowed = self.config["allowed_hosts"]
            if host not in allowed:
                raise PermissionError("Unrecognized host")
            path, method = env.get("PATH_INFO", "/"), env["REQUEST_METHOD"]
            if method == "GET" and path in self.assets:
                payload = self.assets[path]
                ctype = "text/html; charset=utf-8" if path == "/" else "text/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8"
            else:
                cookie = SimpleCookie()
                cookie.load(env.get("HTTP_COOKIE", ""))
                sid = cookie["pidrop"].value if "pidrop" in cookie else ""
                with self.auth_lock:
                    self.sessions = {k: v for k, v in self.sessions.items() if v["expires"] > time.time()}
                    session = self.sessions.get(sid)
                if method == "POST":
                    origin = env.get("HTTP_ORIGIN", "")
                    if origin not in self.config["allowed_origins"]:
                        raise PermissionError("Unrecognized request origin")
                    if env.get("CONTENT_TYPE", "").split(";")[0] != "application/json":
                        raise ValueError("JSON required")
                    length = int(env.get("CONTENT_LENGTH") or 0)
                    if not 0 < length <= 65536:
                        raise ValueError("Request too large or empty")
                    data = json.loads(env["wsgi.input"].read(length))
                    if not isinstance(data, dict):
                        raise ValueError("JSON object required")
                else:
                    data = {}
                if path == "/api/login" and method == "POST":
                    with self.auth_lock:
                        now = time.time()
                        self.failures = [t for t in self.failures if now - t < 300]
                        if len(self.failures) >= 10:
                            status, result = "429 Too Many Requests", {"error": "Too many attempts; wait five minutes."}
                        else:
                            password = data.get("password", "")
                            if not isinstance(password, str) or len(password) > 1024:
                                raise ValueError("Invalid password")
                            candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(self.config["password_salt"]), 600000).hex()
                            if not hmac.compare_digest(candidate, self.config["password_hash"]):
                                self.failures.append(now)
                                status, result = "401 Unauthorized", {"error": "Incorrect password"}
                            else:
                                self.sessions.pop(sid, None)
                                if len(self.sessions) >= 32:
                                    self.sessions.pop(next(iter(self.sessions)))
                                sid = secrets.token_urlsafe(32)
                                session = {"csrf": secrets.token_urlsafe(32), "expires": now + 8 * 3600}
                                self.sessions[sid] = session
                                secure = "; Secure" if self.config.get("secure_cookie") else ""
                                headers.append(("Set-Cookie", "pidrop=" + sid + "; Path=/; HttpOnly; SameSite=Strict; Max-Age=28800" + secure))
                                result = {"csrf": session["csrf"]}
                elif session is None:
                    status, result = "401 Unauthorized", {"error": "Please sign in"}
                elif path == "/api/session" and method == "GET":
                    result = {"csrf": session["csrf"]}
                else:
                    if method == "POST" and not hmac.compare_digest(env.get("HTTP_X_CSRF_TOKEN", ""), session["csrf"]):
                        raise PermissionError("Invalid request token")
                    result = self.route(path, method, data, env)
                    if path == "/api/logout":
                        with self.auth_lock:
                            self.sessions.pop(sid, None)
                        headers.append(("Set-Cookie", "pidrop=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"))
                payload = json.dumps(result, ensure_ascii=True).encode()
        except PermissionError as exc:
            status, payload = "403 Forbidden", json.dumps({"error": str(exc)}).encode()
        except (ValueError, TypeError, KeyError) as exc:
            status, payload = "400 Bad Request", json.dumps({"error": str(exc)[:400]}).encode()
        except LookupError:
            status, payload = "404 Not Found", b'{"error":"No such endpoint"}'
        except Exception as exc:
            status, payload = "409 Conflict", json.dumps({"error": str(exc)[:600]}).encode()
        headers.extend([("Content-Type", ctype), ("Content-Length", str(len(payload))), ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer"), ("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")])
        start(status, headers)
        return [payload]

    def route(self, path, method, data, env):
        if (method, path) == ("GET", "/api/status"):
            return {"devices": self.transfers.devices(), "nas": self.hardware.nas_status(),
                    "jobs": self.transfers.snapshot(), "camera_error": getattr(self.hardware, "camera_error", "")}
        if (method, path) == ("GET", "/api/jobs"):
            return {"jobs": self.transfers.snapshot()}
        # Browsing can mount a source read-only, so it is deliberately a CSRF-protected POST.
        if (method, path) == ("POST", "/api/browse"):
            return self.transfers.browse(data["device"], data.get("path", ""), int(data.get("offset", 0)))
        if (method, path) == ("POST", "/api/transfer"):
            return self.transfers.start(data["device"], data["paths"])
        if (method, path) == ("POST", "/api/eject"):
            return self.transfers.eject(data["device"])
        if (method, path) == ("POST", "/api/cancel"):
            self.transfers.cancel(data["id"])
            return {"ok": True}
        if (method, path) == ("POST", "/api/logout"):
            return {"ok": True}
        raise LookupError()


def create_app():
    config = json.loads(Path(os.environ.get("PIDROP_CONFIG", "/etc/pi-nas-drop/config.json")).read_text())
    return App(config)


if __name__ == "__main__":
    from wsgiref.simple_server import make_server
    # Local development only. Installer uses Gunicorn.
    make_server("127.0.0.1", 8090, create_app()).serve_forever()
