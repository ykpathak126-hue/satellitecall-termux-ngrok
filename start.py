"""Run the SatelliteCall server and ngrok, then show the copy-ready app URL."""

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener, urlopen

HOME = Path.home()
HERE = Path(__file__).resolve().parent
STATE = HOME / ".satellitecall-ngrok"
ORIGIN = "http://127.0.0.1:8080"
AGENT_API = "http://127.0.0.1:4040/api/tunnels"
LOCAL = build_opener(ProxyHandler({}))


def get_json(url, timeout, local=False):
    try:
        request = Request(url, headers={"Accept": "application/json", "ngrok-skip-browser-warning": "1"})
        open_url = LOCAL.open if local else urlopen
        with open_url(request, timeout=timeout) as response:
            if response.status != 200:
                return {}, ""
            body = json.loads(response.read(65536))
            return (body if isinstance(body, dict) else {}), response.headers.get("Server", "")
    except (OSError, ValueError):
        return {}, ""


def server_ready():
    data, header = get_json(ORIGIN + "/healthz", 2, local=True)
    return data.get("ok") is True and header.startswith("SatelliteCall/")


def has_accounts(path):
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            return connection.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
        finally:
            connection.close()
    except sqlite3.Error:
        return False


def database_path():
    explicit = os.environ.get("SATCALL_DB")
    if explicit:
        return Path(explicit).expanduser().resolve()
    candidates = [
        HOME / "satellitecall" / "backend" / "satellitecall.sqlite3",
        HOME / "satellitecall.sqlite3",
        HOME / "satellitecall" / "satellitecall.sqlite3",
        STATE / "satellitecall.sqlite3",
    ]
    existing = [p for p in candidates if p.is_file()]
    with_accounts = [p for p in existing if has_accounts(p)]
    if with_accounts:
        return max(with_accounts, key=lambda p: p.stat().st_mtime)
    return existing[0] if existing else STATE / "satellitecall.sqlite3"


def tunnels():
    data, _ = get_json(AGENT_API, 1, local=True)
    result = data.get("tunnels")
    return result if isinstance(result, list) else []


def matches_origin(address):
    if not isinstance(address, str) or not address:
        return False
    try:
        url = urlsplit(address if "://" in address else "http://" + address)
        return (url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost")
                and url.port == 8080 and url.path in ("", "/") and not url.query)
    except ValueError:
        return False


def https_address(address):
    if not isinstance(address, str):
        return None
    try:
        url = urlsplit(address)
        if (url.scheme == "https" and url.hostname and not url.username and not url.password
                and url.path in ("", "/") and not url.query and not url.fragment):
            return address.rstrip("/")
    except ValueError:
        pass
    return None


def matching_address(entries):
    for entry in entries:
        if isinstance(entry, dict) and matches_origin((entry.get("config") or {}).get("addr")):
            address = https_address(entry.get("public_url"))
            if address:
                return address
    return None


def logged_address(path):
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("msg") == "started tunnel" and matches_origin(entry.get("addr")):
            address = https_address(entry.get("url"))
            if address:
                return address
    return None


def launch(arguments, log_path, env=None):
    with log_path.open("w", encoding="utf-8") as log:
        return subprocess.Popen(arguments, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def wait_for(probe, process, seconds, message):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and process.poll() is None:
        result = probe()
        if result:
            return result
        time.sleep(0.35)
    raise RuntimeError(message)


def show_link(address, ngrok):
    verified = False
    for _ in range(3):
        if ngrok is not None and ngrok.poll() is not None:
            raise RuntimeError("ngrok band ho gaya. Dekho: tail -n 20 ~/.satellitecall-ngrok/ngrok.log")
        data, _ = get_json(address + "/healthz", 5)
        if data.get("ok") is True:
            verified = True
            break
        time.sleep(1)
    if ngrok is not None and ngrok.poll() is not None:
        raise RuntimeError("ngrok band ho gaya. Dekho: tail -n 20 ~/.satellitecall-ngrok/ngrok.log")
    if verified:
        print("Public server check pass. Dono phones mein ye HTTPS address use karo.")
    else:
        print("Public server check confirm nahi hua. Chrome mein kholo:")
        print(address + "/healthz")
    print("\n=== SATELLITECALL APP MEIN YE LINK PASTE KARO ===")
    print(address, flush=True)


def stop_owned(children):
    for process in reversed(children):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def run():
    STATE.mkdir(mode=0o700, exist_ok=True)
    owned = []
    try:
        entries = tunnels()
        address = matching_address(entries)
        if entries and not address:
            raise RuntimeError("Ek ngrok tunnel doosre port par chal raha hai. Purani ngrok session mein Ctrl+C dabao aur setup dobara chalao.")
        binary = shutil.which("ngrok")
        if not address and not binary:
            raise RuntimeError("ngrok command nahi mili. Apna installed ngrok Termux PATH mein rakho.")

        if server_ready():
            print("Chal raha SatelliteCall Python server use ho raha hai.", flush=True)
        else:
            db = database_path()
            print("Python server start ho raha hai. Database: " + str(db), flush=True)
            env = dict(os.environ, SATCALL_DB=str(db), SATCALL_PORT="8080")
            backend = launch([sys.executable, "-u", str(HERE / "server.py")], STATE / "server.log", env)
            owned.append(backend)
            wait_for(server_ready, backend, 12, "Python server start nahi hua. Dekho: tail -n 20 ~/.satellitecall-ngrok/server.log")

        ngrok = None
        if not address:
            print("ngrok start ho raha hai...", flush=True)
            log = STATE / "ngrok.log"
            ngrok = launch([binary, "http", ORIGIN, "--inspect=false", "--log=stdout", "--log-format=json"], log)
            owned.append(ngrok)
            address = wait_for(lambda: matching_address(tunnels()) or logged_address(log), ngrok, 45,
                               "ngrok ka HTTPS link nahi mila. Dekho: tail -n 20 ~/.satellitecall-ngrok/ngrok.log")
        else:
            print("Chal raha ngrok tunnel use ho raha hai.", flush=True)

        print("Termux mein server aur ngrok chalu rakho.", flush=True)
        show_link(address, ngrok)
        while owned and all(process.poll() is None for process in owned):
            time.sleep(1)
        if owned:
            raise RuntimeError("Setup ka ek process band hua. Logs: ~/.satellitecall-ngrok/")
        return 0
    except KeyboardInterrupt:
        print("\nSetup band kiya.")
        return 130
    except (OSError, RuntimeError) as error:
        print(error)
        return 1
    finally:
        stop_owned(owned)


if __name__ == "__main__":
    raise SystemExit(run())
