"""
remote_access.py — lets other devices (your phone, another PC, or the app
running on a server) use Ame, protected by an access key.

Why a key: the app's API returns your settings (API keys included) and
every message spends your provider credit.

  · This computer (127.0.0.1) never needs the key.
  · Any other device must enter the key once (a small login page); after
    that a cookie remembers it on that device for a year.
  · Remote access is off unless the AME_ACCESS_KEY environment variable is
    set: the launcher's REMOTE ACCESS switch sets it (the key is kept in
    .remote_access so devices stay logged in), and on a server you set it
    yourself.
    With no key, remote requests get a page explaining how to turn it on.

Behind a reverse proxy (nginx, Caddy, a hosting platform) every request
arrives from 127.0.0.1; requests carrying proxy headers are therefore
treated as remote, and AME_TRUST_LOCALHOST=0 forces the key for everyone.
"""

import hmac
import os

COOKIE = "ame_key"
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_PROXY_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded", "cf-connecting-ip")
# Always reachable: the login itself and the launcher's health check.
_OPEN_PATHS = {"/login", "/onboarding_status"}


def access_key() -> str:
    """The key in force. Empty string = remote access off."""
    return os.environ.get("AME_ACCESS_KEY", "").strip()


def is_local(request) -> bool:
    if os.environ.get("AME_TRUST_LOCALHOST", "1") == "0":
        return False
    host = request.client.host if request.client else ""
    if host.startswith("::ffff:127."):
        host = "127.0.0.1"
    if host not in _LOOPBACK and host != "testclient":
        return False
    return not any(h in request.headers for h in _PROXY_HEADERS)


def key_ok(candidate: str | None) -> bool:
    key = access_key()
    return bool(key) and bool(candidate) and hmac.compare_digest(candidate.strip(), key)


def allowed(request) -> bool:
    if is_local(request) or request.url.path in _OPEN_PATHS:
        return True
    return key_ok(request.cookies.get(COOKIE))


LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ame</title><style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#131215;color:#ece7e0;font-family:-apple-system,'Segoe UI',sans-serif}
form{background:#1c1a1d;padding:28px;border-radius:14px;width:min(320px,90vw);text-align:center}
h1{font-size:20px;margin:0 0 6px}p{color:#9c948a;font-size:13px;margin:0 0 18px;line-height:1.4}
input{width:100%;box-sizing:border-box;padding:12px;border-radius:8px;border:1px solid #2a2724;
background:#131215;color:#ece7e0;font-size:16px;text-align:center;letter-spacing:2px}
button{margin-top:14px;width:100%;padding:12px;border:none;border-radius:8px;background:#b8935a;
color:#131215;font-size:15px;font-weight:600;cursor:pointer}.err{color:#e07a6e;margin-top:12px;font-size:13px}
</style></head><body><form method="post" action="/login">
<h1>Ame</h1><p>__MSG__</p>
<input name="key" autocomplete="current-password" autofocus placeholder="access key">
<button type="submit">Enter</button>__ERR__</form></body></html>"""

OFF_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Ame</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#131215;color:#ece7e0;font-family:-apple-system,'Segoe UI',sans-serif}
div{background:#1c1a1d;padding:28px;border-radius:14px;width:min(380px,90vw);line-height:1.5;font-size:14px}
b{color:#b8935a}</style></head><body><div>
<b>Remote access is off.</b><br><br>
On the computer running Ame, switch on <b>REMOTE ACCESS</b> in the launcher
(it shows the address and the access key to use here).<br><br>
On a server: set the <b>AME_ACCESS_KEY</b> environment variable.
</div></body></html>"""


def login_page(error: bool = False) -> str:
    return (LOGIN_PAGE
            .replace("__MSG__", "Enter the access key shown in the launcher on the computer running Ame.")
            .replace("__ERR__", '<div class="err">Wrong key.</div>' if error else ""))
