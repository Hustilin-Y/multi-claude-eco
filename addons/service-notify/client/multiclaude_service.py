"""A client for multiclaude's service-notify API: message the bot's owner on Telegram, with
pictures and buttons, and hear the button presses back. One file, standard library only: copy it
into your service.

    from multiclaude_service import Service

    svc = Service("http://127.0.0.1:8790", token)  # the token from `assistant service key <name>`
    mid = svc.send("Plate 1 ready: 2 h 14 m, 38 g PLA", files=["preview.png"],
                   buttons=[[("print:7f3a", "Print plate 1"), ("cancel:7f3a", "Cancel")]],
                   expires_in=600, dedupe="confirm:job42")
    for press in svc.presses():          # long-polls forever; each press comes once
        if press["button_id"] == "print:7f3a":
            svc.edit(press["message_id"], "✅ Printing plate 1")

A connection that fails (the router restarting) is tried again, waiting 1, 2, 4… seconds; a 429
waits as long as it says, once; anything else the API refuses raises ServiceError. A send carries a
dedupe key (its own when not given), so one tried again never shows twice."""
import base64
import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

UNCHANGED = object()


class ServiceError(Exception):
    """The API refused: status (HTTP), code ("bad_request", "unauthorized", "not_found",
    "recipient_not_allowed", "too_large", "rate_limited", "telegram_refused",
    "telegram_unreachable", "not_kept", "buttons_failed"), detail and retry_after when it gave
    them, and message_id for a message that went out all the same (buttons_failed)."""

    def __init__(self, status, code, detail=None, retry_after=None, message_id=None):
        super().__init__(f"{status} {code}" + (f": {detail}" if detail else ""))
        self.status, self.code, self.detail, self.retry_after = status, code, detail, retry_after
        self.message_id = message_id


# The API is on this machine: never through an HTTP proxy from the environment, which would get
# the bearer token and every message.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Service:
    def __init__(self, url, token, timeout=35, retries=5, sleep=time.sleep):
        self.url, self.token, self.timeout, self.retries, self.sleep = url.rstrip("/"), token, timeout, retries, sleep

    # --- messages ---

    def send(self, text, buttons=None, files=None, to=None, silent=False, dedupe=None, expires_in=None, once=True):
        """Send a message (Markdown) to the owner, or to `to`, a person the owner allowed for this
        service; its message id. buttons: rows of (id, text) or {"id", "text"}; the id comes back
        in a press and never reaches Telegram. files: paths or (name, bytes); pictures show below
        the text. dedupe: a key; sending it again within 7 days sends nothing and gives the first
        message's id; without one, the send gets its own. expires_in: seconds the buttons work (24
        hours unless given, 7 days at most).
        once=False keeps the buttons on after a press."""
        body = {"text": text, "silent": silent, "once": once, "dedupe": dedupe or f"send:{uuid.uuid4().hex}"}
        if buttons is not None:
            body["buttons"] = rows_of(buttons)
        if files:
            body["files"] = [file_of(f) for f in files]
        for key, value in (("to", to), ("expires_in", expires_in)):
            if value is not None:
                body[key] = value
        return self.call("POST", "/v1/messages", body)["message_id"]

    def edit(self, message_id, text=None, buttons=UNCHANGED, expires_in=None):
        """Change a message this service sent: its text, its buttons ([] takes them off, new ones
        make it pressable again), or how long they work."""
        body = {}
        if text is not None:
            body["text"] = text
        if buttons is not UNCHANGED:
            body["buttons"] = rows_of(buttons or [])
        if expires_in is not None:
            body["expires_in"] = expires_in
        return self.call("PATCH", f"/v1/messages/{int(message_id)}", body)["message_id"]

    def delete(self, message_id):
        """Delete a message this service sent (Telegram allows it for 48 hours)."""
        return self.call("DELETE", f"/v1/messages/{int(message_id)}")["message_id"]

    # --- presses ---

    def presses(self, wait=25):
        """Each press on this service's buttons, as it comes: {"seq", "message_id", "button_id",
        "label", "by", "at"}. A press comes once: one the service didn't take within 2 minutes is
        dropped, and the owner told nothing was done."""
        while True:
            for press in self.call("GET", f"/v1/presses?wait={wait}", timeout=wait + 10)["presses"]:
                yield press

    def health(self):
        return self.call("GET", "/v1/health")

    # --- the HTTP part ---

    def call(self, method, path, body=None, timeout=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers={"Authorization": f"Bearer {self.token}",
                                                  "Content-Type": "application/json"})
        waited_for_limit = False
        for attempt in range(self.retries + 1):
            try:
                with _DIRECT.open(request, timeout=timeout or self.timeout) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as e:
                error = refusal(e)
                if error.status == 429 and error.retry_after and not waited_for_limit:
                    waited_for_limit = True
                    self.sleep(error.retry_after)
                    continue
                raise error from None
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                if attempt == self.retries:
                    raise
                self.sleep(2 ** attempt)
        raise ServiceError(429, "rate_limited")


def refusal(e):
    try:
        answer = json.loads(e.read())
    except ValueError:
        answer = {}
    return ServiceError(e.code, answer.get("error", "http_error"), answer.get("detail"), answer.get("retry_after"),
                        answer.get("message_id"))


def rows_of(buttons):
    return [[{"id": b[0], "text": b[1]} if isinstance(b, (tuple, list)) else dict(b) for b in row] for row in buttons]


def file_of(f):
    if isinstance(f, (tuple, list)):
        name, data = f
    else:
        name, data = Path(f).name, Path(f).read_bytes()
    return {"name": name, "data": base64.b64encode(data).decode()}
