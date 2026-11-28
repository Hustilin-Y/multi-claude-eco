"""The local HTTP API services call: a ThreadingHTTPServer on the loopback only, inside the router,
which the add-in starts from its first on_tick. Every request carries `Authorization: Bearer
<token>`; the service is whoever that token belongs to. JSON in and out:

  POST   /v1/messages         send       {"text", "to"?, "buttons"?, "files"?, "silent"?, "dedupe"?,
                                           "expires_in"?, "once"?}  -> {"message_id"[, "duplicate"]}
  PATCH  /v1/messages/<id>    edit       {"text"?, "buttons"?, "expires_in"?, "once"?} -> {"message_id"}
  DELETE /v1/messages/<id>    delete     -> {"message_id"}
  GET    /v1/presses?wait=N   the presses waiting, held up to N seconds (at most 25) -> {"presses"}
  GET    /v1/health           -> {"ok", "telegram": "reachable" | "unreachable"}

Errors are {"error", "detail"?, "retry_after"?}. Nothing of a request but its service, route,
status, time and sizes is logged: never a token, a text, a button id or a file."""
import collections
import http.server
import json
import math
import re
import select
import socket
import threading
import time
import traceback
import urllib.parse

from multiclaude import config
from multiclaude.addons.service_notify import messages, store, taps
from multiclaude.log import log

DEFAULT_PORT = 8790
LOOPBACK = ("127.0.0.1", "::1")
MAX_BODY = 20 * 2**20
PRESS_FIELDS = ("seq", "message_id", "button_id", "label", "by", "at", "via")  # what a service gets of a press
MAX_WAIT = 25
MAX_POLLS = 4
MAX_CONNECTIONS = 16  # open at once, from every service
IDLE_TIMEOUT = 30     # seconds a connection may send nothing
LIMITS = {"send": 20, "change": 60}  # a minute, per service
RETRY_START_EVERY = 60
MESSAGE_RE = re.compile(r"/v1/messages/(\d{1,15})")

_windows = collections.defaultdict(collections.deque)  # (service, kind) -> the times of recent calls
_polls = collections.Counter()  # service -> long-polls held now
_counts_lock = threading.Lock()


def polls(service):
    with _counts_lock:
        return _polls[service]


def within_limit(service, kind, now):
    """Count a call against the service's limit; seconds to wait instead when it's over it."""
    with _counts_lock:
        window = _windows[(service, kind)]
        while window and now - window[0] >= 60:
            window.popleft()
        if len(window) >= LIMITS[kind]:
            return max(1, math.ceil(window[0] + 60 - now))
        window.append(now)
        return None


class Listener:
    """The API's server on host:port, which must be the loopback."""

    def __init__(self, port, host="127.0.0.1", network=None):
        if host not in LOOPBACK:
            raise ValueError(f"the service API listens on the loopback only, not {host}")
        self.host, self.port, self.network, self.server = host, port, network, None
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)  # a thread each, so a cap

    def start(self):
        """Serve in the background; False (logged) when the port can't be had."""
        listener = self

        class Server(http.server.ThreadingHTTPServer):
            address_family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
            daemon_threads = True

            def process_request(self, request, client_address):
                if not listener.slots.acquire(blocking=False):
                    self.shutdown_request(request)  # past the cap: refused before a thread is started
                    return
                super().process_request(request, client_address)

            def process_request_thread(self, request, client_address):
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    listener.slots.release()

        server_class = Server
        try:
            self.server = server_class((self.host, self.port), handler_for(self))
        except OSError as e:
            log(f"service-notify: can't listen on {self.host} port {self.port}: {e.strerror or e}")
            return False
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True, name="service-notify api").start()
        log(f"service-notify: listening on {self.host} port {self.port}")
        return True

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


def handler_for(listener):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass  # request lines are logged by handle(), without anything a service sent

        def do_GET(self):
            self.handle_request("GET")

        def do_POST(self):
            self.handle_request("POST")

        def do_PATCH(self):
            self.handle_request("PATCH")

        def do_DELETE(self):
            self.handle_request("DELETE")

        def setup(self):
            self.timeout = IDLE_TIMEOUT  # a connection that sends nothing for this long is dropped
            super().setup()

        def length(self):
            """The body's Content-Length; Refused for one that isn't a count."""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0:
                raise messages.Refused(400, "bad_request", "bad Content-Length")
            return length

        def handle_request(self, method):
            started, route = time.monotonic(), urllib.parse.urlsplit(self.path)
            service, sizes, self.body_read = None, "", False
            try:
                self.length()
                service = self.service()
                status, answer, sizes = self.route(method, route, service)
            except messages.Refused as e:
                status = e.status
                answer = {"error": e.code, **({"detail": e.detail} if e.detail else {}),
                          **({"retry_after": e.retry_after} if e.retry_after else {})}
            except Exception:
                log(f"service-notify: {method} {route.path} failed:\n{traceback.format_exc().rstrip()}")
                status, answer = 500, {"error": "internal"}
            if not self.body_read and (self.headers.get("Content-Length") or "0") != "0":
                self.close_connection = True  # a body left unread would be taken for the next request
            self.answer(status, answer)
            ms = round((time.monotonic() - started) * 1000)
            log(f"service-notify: {service or '?'} {method} {route.path} {status} {ms}ms{sizes}")

        def service(self):
            header = self.headers.get("Authorization") or ""
            token = header[7:].strip() if header.startswith("Bearer ") else ""
            service = store.service_of(token)
            if not service:
                raise messages.Refused(401, "unauthorized")
            return service

        def body(self):
            length = self.length()
            if length > MAX_BODY:
                raise messages.Refused(413, "too_large")
            self.body_read = True
            try:
                return json.loads(self.rfile.read(length) or b"null")
            except ValueError:
                raise messages.Refused(400, "bad_request", "the body isn't JSON") from None

        def route(self, method, route, service):
            now = time.time()
            message = MESSAGE_RE.fullmatch(route.path)
            if method == "GET" and route.path == "/v1/health":
                online = listener.network.online() if listener.network else True
                return 200, {"ok": True, "telegram": "reachable" if online else "unreachable"}, ""
            if method == "GET" and route.path == "/v1/presses":
                return 200, {"presses": self.presses(service, route.query)}, ""
            if method == "POST" and route.path == "/v1/messages":
                self.limit(service, "send", now)  # before the body, which may be 20 MB
                request = self.body()
                answer = messages.send(service, request, now)
                return 200, answer, sizes_of(request)
            if method in ("PATCH", "DELETE") and message:
                self.limit(service, "change", now)
                request = self.body() if method == "PATCH" else None
                if method == "PATCH":
                    return 200, messages.edit(service, message[1], request, now), sizes_of(request)
                return 200, messages.delete(service, message[1]), ""
            raise messages.Refused(404, "not_found")

        def limit(self, service, kind, now):
            if wait := within_limit(service, kind, now):
                raise messages.Refused(429, "rate_limited", retry_after=wait)

        def presses(self, service, query):
            try:
                wait = min(MAX_WAIT, max(0.0, float(urllib.parse.parse_qs(query).get("wait", ["0"])[0])))
            except ValueError:
                raise messages.Refused(400, "bad_request", "wait is a number of seconds") from None
            kept = store.Service(service)
            if wait and not kept.has_presses(time.time()):
                with _counts_lock:
                    if _polls[service] >= MAX_POLLS:
                        raise messages.Refused(429, "rate_limited", f"at most {MAX_POLLS} long-polls at once",
                                               retry_after=1)
                    _polls[service] += 1
                try:
                    condition = taps.wake(service)
                    with condition:
                        condition.wait_for(lambda: kept.has_presses(time.time()), timeout=wait)
                finally:
                    with _counts_lock:
                        _polls[service] -= 1
                if self.peer_gone():  # its service restarted meanwhile: the presses wait for the next poll
                    return []
            return [{k: p.get(k) for k in PRESS_FIELDS} for p in kept.take_presses(time.time())]

        def peer_gone(self):
            """Whether the caller hung up: its end of the connection reads as closed."""
            try:
                readable, _, _ = select.select([self.connection], [], [], 0)
                return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
            except OSError:
                return True

        def answer(self, status, answer):
            body = json.dumps(answer, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def sizes_of(request):
    if not isinstance(request, dict):
        return ""
    text = request.get("text")
    files = request.get("files")
    return f" text={len(text) if isinstance(text, str) else 0} files={len(files) if isinstance(files, list) else 0}"


# --- started by the router's loop ---

_started = {"listener": None, "next_try": 0.0}


def port():
    configured = (config.read().get("service_notify") or {}).get("port")
    return configured if isinstance(configured, int) and not isinstance(configured, bool) else DEFAULT_PORT


def start_once(desk, now):
    """Start the listener from the router's loop, trying again every minute while it can't."""
    if _started["listener"] or now < _started["next_try"]:
        return
    _started["next_try"] = now + RETRY_START_EVERY
    listener = Listener(port(), network=getattr(desk, "network", None))
    if listener.start():
        _started["listener"] = listener
