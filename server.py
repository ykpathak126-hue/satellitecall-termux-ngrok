"""SatelliteCall beta signalling. Put this listener behind an HTTPS reverse proxy."""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import deque
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

DB = os.environ.get("SATCALL_DB", "satellitecall.sqlite3")
WAIT = 14
CALL_TTL = 8 * 3600
RING_TTL = 55
PRESENCE_TTL = 40
ACTIVE_LEASE = 135
CODE = re.compile(r"^[A-F0-9]{20}$")
ID = re.compile(r"^[a-f0-9]{32}$")
TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
TURN_SECRET = os.environ.get("SATCALL_TURN_SECRET", "")
TURN_URLS = [s.strip() for s in os.environ.get("SATCALL_TURN_URLS", "").split(",") if s.strip()]
STUN_URL = os.environ.get("SATCALL_STUN_URL", "stun:stun.l.google.com:19302")
if bool(TURN_SECRET) != bool(TURN_URLS):
    raise RuntimeError("Configure both SATCALL_TURN_SECRET and SATCALL_TURN_URLS")

cv = threading.Condition()
inbox, seen, calls, busy, rates, revoked = {}, {}, {}, {}, {}, {}
sequence = time.time_ns() // 1000
boot = secrets.token_hex(12)
slots = threading.BoundedSemaphore(96)
pollers = threading.BoundedSemaphore(70)


@contextmanager
def database():
    with closing(sqlite3.connect(DB, timeout=8)) as db, db:
        db.row_factory = sqlite3.Row
        yield db


def init():
    with database() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS users(
            code TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE)""")
    os.chmod(DB, 0o600)


class Fault(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def rate(key, limit, interval):
    now = time.monotonic()
    bucket = rates.setdefault(key, deque())
    while bucket and bucket[0] < now - interval:
        bucket.popleft()
    if len(bucket) >= limit:
        raise Fault(429, "Too many requests. Try again shortly.")
    bucket.append(now)


def digest(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def profile(code):
    with database() as db:
        return db.execute("SELECT code, name FROM users WHERE code=?", (code,)).fetchone()


def emit(code, kind, call, **details):
    global sequence
    sequence += 1
    queue = inbox.setdefault(code, deque(maxlen=256))
    queue.append(dict(seq=sequence, type=kind, callId=call, **details))
    cv.notify_all()


def close_call(call_id, reason):
    call = calls.pop(call_id, None)
    if call:
        for code in (call["caller"], call["callee"]):
            busy.pop(code, None)
            emit(code, "ended", call_id, reason=reason)


def clean():
    now = time.monotonic()
    for call_id, call in list(calls.items()):
        if now >= call["expires"]:
            close_call(call_id, "Call expired" if call["accepted"] else "No answer")
        elif call["accepted"] and any(now - seen.get(party, 0) > ACTIVE_LEASE
                                      for party in (call["caller"], call["callee"])):
            close_call(call_id, "Contact lost connection")
    for code, last in list(seen.items()):
        if now - last > 300 and code not in busy:
            seen.pop(code, None)
            inbox.pop(code, None)
    for key, bucket in list(rates.items()):
        if not bucket or now - bucket[-1] > 3600:
            rates.pop(key, None)
    for code, since in list(revoked.items()):
        if now - since > 60:
            revoked.pop(code, None)


class Api(BaseHTTPRequestHandler):
    server_version = "SatelliteCall/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def setup(self):
        self.request.settimeout(24)
        super().setup()

    def reply(self, status, payload):
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def request_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 2 <= length <= 48_000:
                raise ValueError()
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError, RecursionError):
            raise Fault(400, "Invalid request")

    def who(self):
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if TOKEN.fullmatch(token):
            with database() as db:
                row = db.execute("SELECT code, name FROM users WHERE token_hash=?", (digest(token),)).fetchone()
            if row:
                return row["code"], row["name"]
        raise Fault(401, "This device account is no longer valid.")

    def do_GET(self):
        self.handle_api("GET")

    def do_POST(self):
        self.handle_api("POST")

    def handle_api(self, method):
        try:
            payload = self.dispatch(method)
            self.reply(200, payload)
        except Fault as err:
            self.reply(err.status, {"error": err.message})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    def dispatch(self, method):
        request = urlsplit(self.path)
        path = request.path
        if method == "GET" and path == "/healthz":
            return {"ok": True}
        if method == "POST" and path == "/v1/register":
            name = self.request_body().get("name")
            if not isinstance(name, str) or not 2 <= len(name.strip()) <= 32 or not name.strip().isprintable():
                raise Fault(400, "Enter a display name with 2–32 characters.")
            name = name.strip()
            with cv:
                clean()
                rate("signup-global", 60, 3600)
                code, token = secrets.token_hex(10).upper(), secrets.token_urlsafe(32)
                with database() as db:
                    db.execute("INSERT INTO users VALUES (?, ?, ?)", (code, name, digest(token)))
            return {"code": code, "name": name, "token": token}
        code, _ = self.who()
        if method == "GET" and path == "/v1/events":
            args = parse_qs(request.query)
            try:
                cursor = int(args.get("after", ["0"])[0])
            except ValueError:
                raise Fault(400, "Invalid event cursor")
            if cursor < 0:
                raise Fault(400, "Invalid event cursor")
            if not pollers.acquire(blocking=False):
                raise Fault(503, "Signalling is busy. Retrying.")
            try:
                deadline = time.monotonic() + WAIT
                with cv:
                    rate(("poll", code), 60, 60)
                    seen[code] = time.monotonic()
                    if args.get("boot", [""])[0] != boot:
                        cursor = 0
                        deadline = time.monotonic()
                    while True:
                        if code in revoked:
                            raise Fault(401, "Account deleted.")
                        clean()
                        events = [e for e in inbox.get(code, ()) if e["seq"] > cursor][:64]
                        remaining = deadline - time.monotonic()
                        if events or remaining <= 0:
                            return {"events": events, "boot": boot}
                        cv.wait(min(remaining, 2))
            finally:
                pollers.release()
        if method == "GET" and path == "/v1/ice":
            with cv:
                rate(("ice", code), 16, 60)
            servers = [{"urls": [STUN_URL]}] if STUN_URL else []
            if TURN_SECRET:
                username = f"{int(time.time()) + 3600}:{code}"
                password = base64.b64encode(hmac.digest(TURN_SECRET.encode(), username.encode(), "sha1")).decode()
                servers.append({"urls": TURN_URLS, "username": username, "credential": password})
            return {"servers": servers}
        if method != "POST":
            raise Fault(404, "Unknown endpoint")
        body = self.request_body()
        with cv:
            clean()
            rate(("actions", code), 220, 60)
            if code in revoked:
                raise Fault(401, "Account deleted.")
            if path == "/v1/delete":
                close_call(busy.get(code), "Account deleted")
                with database() as db:
                    db.execute("DELETE FROM users WHERE code=?", (code,))
                seen.pop(code, None)
                inbox.pop(code, None)
                revoked[code] = time.monotonic()
                cv.notify_all()
                return {"ok": True}
            if path == "/v1/lookup":
                target = body.get("to")
                if not isinstance(target, str) or not CODE.fullmatch(target):
                    raise Fault(400, "Enter a valid contact code.")
                rate(("lookup", code), 25, 60)
                target_row = profile(target)
                if not target_row:
                    raise Fault(404, "Contact unavailable")
                return dict(target_row)
            if path != "/v1/call":
                raise Fault(404, "Unknown endpoint")
            return self.call_action(code, body)

    def call_action(self, code, body):
        action, call_id = body.get("action"), body.get("callId")
        if not isinstance(call_id, str) or not ID.fullmatch(call_id):
            raise Fault(400, "Invalid call ID")
        if action == "invite":
            target = body.get("to")
            if not isinstance(target, str) or not CODE.fullmatch(target) or target == code:
                raise Fault(400, "Enter another device’s contact code.")
            rate(("invite", code), 12, 60)
            if call_id in calls and calls[call_id]["caller"] == code and calls[call_id]["callee"] == target:
                return {"ok": True}
            if code in busy:
                raise Fault(409, "You already have an active call.")
            if call_id in calls:
                raise Fault(409, "Call ID already in use.")
            if target in busy:
                raise Fault(409, "Contact is busy.")
            if time.monotonic() - seen.get(target, -10_000_000) > PRESENCE_TTL:
                raise Fault(409, "Contact is offline. Ask them to open SatelliteCall.")
            origin = profile(code)
            if not origin or not profile(target):
                raise Fault(404, "Contact unavailable.")
            calls[call_id] = dict(caller=code, callee=target, accepted=False,
                                  expires=time.monotonic()+RING_TTL, requests=set())
            busy[code] = busy[target] = call_id
            emit(target, "incoming", call_id, name=origin["name"], fromCode=code)
            return {"ok": True}
        call = calls.get(call_id)
        if not call or code not in (call["caller"], call["callee"]):
            if action == "end":
                return {"ok": True}
            raise Fault(409, "This call is no longer available.")
        request_id = body.get("requestId")
        if not isinstance(request_id, str) or not ID.fullmatch(request_id):
            raise Fault(400, "Invalid request ID")
        identity = (code, request_id)
        if identity in call["requests"]:
            return {"ok": True}
        if len(call["requests"]) >= 2000:
            call["requests"].clear()
        peer = call["callee"] if code == call["caller"] else call["caller"]
        if action == "end":
            close_call(call_id, "Declined" if code == call["callee"] and not call["accepted"] else "Call ended")
        elif action == "accept" and code == call["callee"]:
            if not call["accepted"]:
                call["accepted"] = True
                call["expires"] = time.monotonic()+CALL_TTL
                emit(peer, "accepted", call_id)
        elif action in ("offer", "answer", "candidate", "restart") and call["accepted"]:
            generation = body.get("generation")
            if type(generation) is not int or not 0 <= generation <= 25:
                raise Fault(400, "Invalid media generation")
            if action in ("offer", "answer"):
                if (action == "offer") != (code == call["caller"]):
                    raise Fault(403, "Invalid media direction")
                sdp = body.get("sdp")
                if not isinstance(sdp, str) or not 10 <= len(sdp) <= 32_000 or not sdp.startswith("v=0"):
                    raise Fault(400, "Invalid session description")
                emit(peer, action, call_id, sdp=sdp, generation=generation)
            elif action == "candidate":
                mid, line, candidate = body.get("mid"), body.get("line"), body.get("candidate")
                if not isinstance(mid, str) or not 1 <= len(mid) <= 80 or type(line) is not int or not 0 <= line <= 4:
                    raise Fault(400, "Invalid candidate")
                if not isinstance(candidate, str) or not candidate.startswith("candidate:") or len(candidate) > 1600:
                    raise Fault(400, "Invalid candidate")
                emit(peer, action, call_id, mid=mid, line=line, candidate=candidate, generation=generation)
            elif code == call["callee"]:
                emit(peer, action, call_id, generation=generation)
            else:
                raise Fault(403, "Invalid media direction")
        else:
            raise Fault(409, "Call action unavailable")
        call["requests"].add(identity)
        return {"ok": True}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 96

    def process_request(self, request, address):
        if slots.acquire(blocking=False):
            try:
                super().process_request(request, address)
            except Exception:
                slots.release()
                raise
        else:
            self.shutdown_request(request)

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            slots.release()


if __name__ == "__main__":
    os.umask(0o077)
    init()
    server = Server(("127.0.0.1", int(os.environ.get("SATCALL_PORT", "8080"))), Api)
    server.serve_forever()
