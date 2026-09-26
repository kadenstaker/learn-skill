#!/usr/bin/env python3
"""The learn skill's goal server: serves each goal page and keeps its answers.json and state.json.

usage:
  serve.py ensure --root DIR              make sure DIR's server is running; start it detached if not
  serve.py stop --root DIR                stop DIR's server
  serve.py run --root DIR [--port N]      serve every goal under DIR on 127.0.0.1, in the foreground
  serve.py merge GOAL answers|state FILE  merge FILE's records into GOAL/answers.json or state.json
  serve.py lock GOAL [--owner NAME]       take GOAL's run lock; exit 3 if a fresh one is held
  serve.py unlock GOAL                    drop GOAL's run lock
  serve.py new-token --root DIR           replace the token (every bookmark then needs the new link)

GOAL is a goal folder; FILE is {"schema": 1, "records": {id: record}}, or "-" for stdin.
Each command prints one JSON line to stdout; diagnostics go to stderr.

Pages are at http://127.0.0.1:<port>/<token>/<goal-slug>/, with api/answers, api/state and api/version
beside them, and /<token>/api/whoami. Port and token live in <root>/serve.json (0600) and are kept across
restarts. Every request needs the token and a Host on the allowlist; nothing else under the root is served.
A running server keeps its pid in <root>/serve.pid; one started by `ensure` logs to <root>/serve.log (0600),
which never holds the token.

Records merge the same way here as in the page (RECORDS in template.html): the newer `at` wins, answers
join their histories, nothing is deleted. Every read-merge-write holds the goal's write lock and rereads
the file inside it, so the server and `merge` can write at the same moment without losing a record.
The server keeps no copy in memory, so `merge` writes the files directly, running server or not.

Python 3.7+ standard library only.
"""
import argparse, errno, hmac, http.client, json, math, os, re, secrets, signal, socket, subprocess, sys, tempfile, threading, time
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCHEMA = 1
COLLECTIONS = ("answers", "state")
MAX_BODY = 1 << 20              # the page sends at most about 250,000 characters per POST
RUN_LOCK_STALE = 2 * 3600       # seconds; a run lock older than this was left by a run that died
CONFIG = "serve.json"
PID_FILE = "serve.pid"
LOG_FILE = "serve.log"
LOG_MAX = 1 << 20               # bytes; a larger serve.log moves to serve.log.1 when `ensure` next starts the server
ENSURE_LOCK = ".ensure.lock"
WRITE_LOCK = ".write.lock"
RUN_LOCK = ".run.lock"
SLUG_RE = re.compile(r"[a-z0-9-]+")
RESERVED = {"api"}              # /<token>/api/... is the server's own


def now_at():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def say(obj, code=0):
    print(json.dumps(obj), flush=True)
    return code


# ---------------------------------------------------------------- merge (keep in step with RECORDS in template.html)
def js_string(x):
    if x is None:
        return "null"
    if isinstance(x, bool):
        return "true" if x else "false"
    return x if isinstance(x, str) else json.dumps(x)


def ints(x):
    """JSON numbers as JS writes them: an integral float is an int."""
    if isinstance(x, float) and x.is_integer():
        return int(x)
    if isinstance(x, list):
        return [ints(v) for v in x]
    if isinstance(x, dict):
        return {k: ints(v) for k, v in x.items()}
    return x


def canon(x):
    return json.dumps(ints(x), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def utf16(s):
    return s.encode("utf-16-be", "surrogatepass")


def larger(a, b):
    """a > b as JS compares strings: by UTF-16 code units."""
    return utf16(a) > utf16(b)


AT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")


def at_time(at):
    """Only toISOString()'s form counts; anything else is oldest."""
    if not isinstance(at, str) or not AT_RE.fullmatch(at):
        return -math.inf
    try:
        return datetime.strptime(at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp() * 1000
    except ValueError:
        return -math.inf


def winner(a, b):
    ta, tb = at_time(a.get("at")), at_time(b.get("at"))
    if ta != tb:
        return b if tb > ta else a
    return b if larger(canon(b), canon(a)) else a


def flag(x):
    return x if isinstance(x, bool) else None


def clean_answer(d):
    if not isinstance(d, dict):
        return None
    r = dict(d)
    r["value"] = d.get("value")
    r["correct"] = flag(d.get("correct"))
    a = d.get("attempts")
    r["attempts"] = a if isinstance(a, (int, float)) and not isinstance(a, bool) and a > 0 else 0
    r["history"] = [{"at": h.get("at"), "value": h.get("value"), "correct": flag(h.get("correct"))}
                    for h in (d.get("history") if isinstance(d.get("history"), list) else []) if isinstance(h, dict)]
    if not isinstance(r.get("draft"), str):
        r.pop("draft", None)
    return r


def join_history(a, b):
    by_at = {}
    for h in a + b:
        k = js_string(h.get("at"))
        if k not in by_at or larger(canon(h), canon(by_at[k])):
            by_at[k] = h
    return sorted(by_at.values(), key=lambda h: (at_time(h.get("at")), utf16(js_string(h.get("at")))))


def merge_answer(a, b):
    if a is None or b is None:
        return a or b
    newer = winner(a, b)
    r = dict(newer)
    r["history"] = join_history(a["history"], b["history"])
    r["attempts"] = max(a["attempts"], b["attempts"], len(r["history"]))
    last = r["history"][-1] if r["history"] else None
    if last and (newer["history"] or newer.get("value") is None):
        r["value"], r["correct"] = last["value"], last["correct"]
    if r.get("correct") is True:
        r.pop("draft", None)
    return r


def clean_state(d):
    return dict(d) if isinstance(d, dict) else None


def merge_state(a, b):
    if a is None or b is None:
        return a or b
    return winner(a, b)


KINDS = {"answers": (clean_answer, merge_answer), "state": (clean_state, merge_state)}
ID_RE = re.compile(r"[A-Za-z0-9_.~:@+-]{1,200}")


def usable(rid):
    return isinstance(rid, str) and bool(ID_RE.fullmatch(rid)) and rid not in (".", "..")


# ---------------------------------------------------------------- files
class StoreError(Exception):
    """A record file this server will not overwrite: unreadable, or in a format it does not know."""


_thread_lock = threading.Lock()   # flock excludes other processes; this excludes the server's own threads


@contextmanager
def file_lock(path):
    with _thread_lock:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                while True:
                    try:
                        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                        break
                    except OSError:
                        time.sleep(0.05)
                try:
                    yield
                finally:
                    os.lseek(fd, 0, 0)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)   # flock, not lockf: lockf locks belong to the whole process
                try:
                    yield
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def write_lock(goal):
    return file_lock(os.path.join(goal, WRITE_LOCK))


def read_records(goal, coll):
    """The file's records by id; no file yet is no records."""
    path = os.path.join(goal, coll + ".json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise StoreError("%s: not readable as JSON (%s)" % (path, e))
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or not isinstance(doc.get("records"), dict):
        raise StoreError("%s: not a schema %d record file" % (path, SCHEMA))
    return doc["records"]


def write_atomic(path, data, mode=0o600):
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def merge_into(goal, coll, incoming):
    """Merge incoming records into the goal's file; returns the merged copies of those records.
    Ids no store takes and records that are not objects are skipped."""
    clean, merge = KINDS[coll]
    with write_lock(goal):
        have = read_records(goal, coll)
        out = {}
        for rid, rec in incoming.items():
            r = clean(rec)
            if not usable(rid) or r is None:
                continue
            old = clean(have.get(rid)) if rid in have else None
            have[rid] = out[rid] = merge(old, r)
        if out:
            body = json.dumps({"schema": SCHEMA, "records": have}, ensure_ascii=False, indent=1, sort_keys=True)
            write_atomic(os.path.join(goal, coll + ".json"), (body + "\n").encode("utf-8"))
        return out


# ---------------------------------------------------------------- config
def load_config(root, create=True):
    path = os.path.join(root, CONFIG)
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        os.chmod(path, 0o600)
    except FileNotFoundError:
        if not create:
            return None
        cfg = {}
    if not isinstance(cfg, dict):
        raise SystemExit("%s is not a JSON object" % path)
    if not isinstance(cfg.get("token"), str) or not re.fullmatch(r"[0-9a-f]{32}", cfg["token"]):
        cfg["token"] = secrets.token_hex(16)
        save_config(root, cfg)
    cfg.setdefault("lan", False)
    return cfg


def save_config(root, cfg):
    write_atomic(os.path.join(root, CONFIG), (json.dumps(cfg, indent=1, sort_keys=True) + "\n").encode("utf-8"))


# ---------------------------------------------------------------- server
class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64   # the default 5 resets a burst of connections, such as a page reload's reads

    def __init__(self, root, port, token):
        self.root, self.token = root, token
        super().__init__(("127.0.0.1", port), Handler)
        self.hosts = {"127.0.0.1:%d" % self.server_address[1]}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "learn"
    sys_version = ""

    def log_message(self, fmt, *args):   # the token never reaches the log
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), (fmt % args).replace(self.server.token, "<token>")))

    def log_request(self, code="-", size="-"):
        self.log_message("%s %s", self.command, code)

    def reply(self, status, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def refuse(self, status, why):
        # the body of a refused POST is never read, so the connection cannot be reused
        if self.command == "POST":
            self.close_connection = True
        self.reply(status, {"error": why})

    def route(self):
        """(slug, what) for an allowed request, or None after refusing it."""
        if self.headers.get("Host") not in self.server.hosts:
            self.refuse(403, "unknown host")
            return None
        path = self.path.split("?", 1)[0]
        parts = path.split("/")
        # "", token, then slug and tail; no empty, dot or percent-encoded segments anywhere
        if len(parts) < 3 or parts[0] != "" or not hmac.compare_digest(parts[1].encode(), self.server.token.encode()):
            self.refuse(404, "not found")
            return None
        rest = parts[2:]
        if rest == ["api", "whoami"]:
            return None, "whoami"
        slug = rest[0]
        tail = "/".join(rest[1:])
        if not SLUG_RE.fullmatch(slug) or slug in RESERVED or tail not in ("", "index.html", "api/answers", "api/state", "api/version"):
            self.refuse(404, "not found")
            return None
        goal = os.path.join(self.server.root, slug)
        index = os.path.join(goal, "index.html")
        if os.path.islink(goal) or os.path.islink(index) or not os.path.isfile(index) \
                or os.path.dirname(os.path.realpath(index)) != os.path.join(self.server.root, slug):
            self.refuse(404, "not found")
            return None
        return goal, tail

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        r = self.route()
        if r is None:
            return
        goal, what = r
        try:
            if what == "whoami":
                return self.reply(200, {"root": self.server.root})
            index = os.path.join(goal, "index.html")
            if what in ("", "index.html"):
                with open(index, "rb") as f:
                    return self.reply(200, f.read(), "text/html; charset=utf-8")
            if what == "api/version":
                return self.reply(200, {"version": str(os.stat(index).st_mtime_ns)})
            coll = what[len("api/"):]
            return self.reply(200, {"schema": SCHEMA, "records": read_records(goal, coll)})
        except StoreError as e:
            sys.stderr.write("%s\n" % e)
            return self.reply(500, {"error": "the record file could not be read"})
        except OSError as e:
            sys.stderr.write("read failed: %s\n" % e)
            return self.reply(500, {"error": "read failed"})

    def do_POST(self):
        r = self.route()
        if r is None:
            return
        goal, what = r
        if what not in ("api/answers", "api/state"):
            return self.refuse(405, "read only")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self.refuse(415, "JSON only")
        if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length") is None:
            return self.refuse(411, "Content-Length needed")
        try:
            n = int(self.headers["Content-Length"])
        except ValueError:
            return self.refuse(400, "bad Content-Length")
        if n < 0 or n > MAX_BODY:
            return self.refuse(413, "body too large")
        raw = self.rfile.read(n)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self.reply(400, {"error": "not JSON"})
        if not isinstance(body, dict) or body.get("schema") != SCHEMA or not isinstance(body.get("records"), dict):
            return self.reply(400, {"error": "not a schema %d record body" % SCHEMA})
        try:
            merged = merge_into(goal, what[len("api/"):], body["records"])
        except StoreError as e:
            sys.stderr.write("%s\n" % e)
            return self.reply(500, {"error": "the record file could not be read"})
        except OSError as e:
            sys.stderr.write("write failed: %s\n" % e)
            return self.reply(500, {"error": "write failed"})
        return self.reply(200, {"schema": SCHEMA, "records": merged})


# ---------------------------------------------------------------- commands
def real_root(path):
    root = os.path.realpath(path)
    if not os.path.isdir(root):
        raise SystemExit("%s is not a folder" % path)
    return root


def cmd_run(args):
    root = real_root(args.root)
    cfg = load_config(root)
    port = args.port if args.port is not None else cfg.get("port", 0)
    try:
        httpd = Server(root, port, cfg["token"])
    except OSError as e:
        if e.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1)):
            return say({"ok": False, "error": "port %d is in use" % port, "port": port}, 2)
        raise
    got = httpd.server_address[1]
    if cfg.get("port") != got:
        cfg["port"] = got
        save_config(root, cfg)
    write_atomic(os.path.join(root, PID_FILE), ("%d\n" % os.getpid()).encode("ascii"))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))   # so `finally` drops the pid file
    try:
        say({"ok": True, "port": got, "root": root, "url": "http://127.0.0.1:%d/%s/" % (got, cfg["token"])})
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        drop_pid(root, os.getpid())
    return 0


# ---------------------------------------------------------------- the detached server (ensure, stop)
def read_pid(root):
    try:
        with open(os.path.join(root, PID_FILE), "r", encoding="ascii") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def drop_pid(root, pid):
    """Remove the pid file if it still names pid, and not a server that started since (a login job's restart)."""
    if read_pid(root) == pid:
        try:
            os.unlink(os.path.join(root, PID_FILE))
        except OSError:
            pass


def is_ours(pid):
    """True if pid is a live serve.py process. False for a dead pid, or one the system gave to another program."""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False   # another user's process
    try:
        cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
    except OSError:
        return True    # no ps: trust the pid file
    return "serve.py" in cmd


def whoami(port, token, timeout=2.0):
    """The learning root the server on port answers for, or None."""
    if not port:
        return None
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            c.request("GET", "/%s/api/whoami" % token, headers={"Host": "127.0.0.1:%d" % port})
            r = c.getresponse()
            body = r.read()
        finally:
            c.close()
        return json.loads(body.decode("utf-8")).get("root") if r.status == 200 else None
    except (OSError, ValueError, AttributeError, http.client.HTTPException):
        return None


def serving(root, port, token, wait=0.0):
    """Whether the server on port answers for root, asking again for up to wait seconds."""
    end = time.monotonic() + wait
    while True:
        if whoami(port, token) == root:
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.2)


def free_port(after):
    """The first port above after that 127.0.0.1 can bind, else 0 (the system picks)."""
    for p in range(after + 1, min(after + 100, 65536)):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    return 0


def spawn(root, port):
    """Start `run` detached from this process and its session, on port (0: any).
    Returns run's first stdout line as a dict, or None when it said nothing in time (see serve.log)."""
    log = os.path.join(root, LOG_FILE)
    try:
        if os.path.getsize(log) > LOG_MAX:
            os.replace(log, log + ".1")
    except OSError:
        pass
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.chmod(log, 0o600)
    cmd = [sys.executable, os.path.abspath(__file__), "run", "--root", root] + (["--port", str(port)] if port else [])
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": fd, "cwd": root, "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True   # fork and setsid: no terminal, not in the agent's process group
    try:
        proc = subprocess.Popen(cmd, **kw)
    finally:
        os.close(fd)
    box = []
    t = threading.Thread(target=lambda: box.append(proc.stdout.readline()), daemon=True)
    t.start()
    t.join(15)
    if not box:
        return None
    proc.stdout.close()   # run writes nothing more to stdout
    try:
        return json.loads(box[0])
    except ValueError:
        return None


def cmd_ensure(args):
    root = real_root(args.root)
    with file_lock(os.path.join(root, ENSURE_LOCK)):   # two ensures at once start one server
        cfg = load_config(root)
        token, saved = cfg["token"], cfg.get("port")

        def running(port, started, note=None):
            out = {"ok": True, "started": started, "pid": read_pid(root), "port": port,
                   "url": "http://127.0.0.1:%d/%s/" % (port, token)}
            if note:
                out["note"] = note
            return say(out)

        if serving(root, saved, token):
            return running(saved, False)
        pid = read_pid(root)
        if is_ours(pid):
            # a server of ours that does not answer yet: give it a moment, never move its port from under it
            if serving(root, saved, token, wait=5):
                return running(saved, False)
            return say({"ok": False, "pid": pid, "error": "the server (pid %d) does not answer; "
                        "run `serve.py stop --root %s`, then ensure again" % (pid, root)}, 2)
        if pid is not None:
            drop_pid(root, pid)   # left by a server that died
        port, note = saved, None
        for _ in range(3):
            info = spawn(root, port)
            if info is None:
                return say({"ok": False, "error": "the server did not start; see %s" % os.path.join(root, LOG_FILE)}, 2)
            if info.get("ok"):
                port = info["port"]
                if not serving(root, port, token, wait=5):
                    return say({"ok": False, "error": "the server started but does not answer; see %s"
                                % os.path.join(root, LOG_FILE)}, 2)
                return running(port, True, note)
            # the port is taken; by a server of ours that started meanwhile (a login job), or by another program
            if serving(root, port, token, wait=1):
                return running(port, False)
            port = free_port(port)
            note = ("port %d is taken by another program, so the server moved to a new port: "
                    "bookmarks and any `tailscale serve` mapping need redoing" % saved)
        return say({"ok": False, "error": "no free port found"}, 2)


def cmd_stop(args):
    root = real_root(args.root)
    pid = read_pid(root)
    if not is_ours(pid):
        if pid is not None:
            drop_pid(root, pid)
        return say({"ok": True, "note": "no server was running"})
    os.kill(pid, signal.SIGTERM)
    end = time.monotonic() + 10
    while is_ours(pid):
        if time.monotonic() >= end:
            return say({"ok": False, "pid": pid, "error": "the server did not stop"}, 2)
        time.sleep(0.1)
    drop_pid(root, pid)   # Windows ends the process without running its cleanup
    return say({"ok": True, "stopped": pid})


def goal_dir(path):
    goal = os.path.realpath(path)
    if not os.path.isdir(goal):
        raise SystemExit("%s is not a goal folder" % path)
    return goal


def cmd_merge(args):
    goal = goal_dir(args.goal)
    try:
        text = sys.stdin.read() if args.file == "-" else open(args.file, "r", encoding="utf-8").read()
        doc = json.loads(text)
    except (OSError, ValueError) as e:
        return say({"ok": False, "error": "cannot read %s: %s" % (args.file, e)}, 2)
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or not isinstance(doc.get("records"), dict):
        return say({"ok": False, "error": "%s is not {\"schema\": %d, \"records\": {...}}" % (args.file, SCHEMA)}, 2)
    try:
        merged = merge_into(goal, args.collection, doc["records"])
    except StoreError as e:
        return say({"ok": False, "error": str(e)}, 2)
    skipped = sorted(set(doc["records"]) - set(merged))
    return say({"ok": True, "merged": len(merged), "skipped": skipped})


def cmd_lock(args):
    goal = goal_dir(args.goal)
    path = os.path.join(goal, RUN_LOCK)
    mine = {"owner": args.owner, "at": now_at(), "host": socket.gethostname()}
    for _ in range(2):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                age = time.time() - os.stat(path).st_mtime
                with open(path, "r", encoding="utf-8") as f:
                    held = json.load(f)
            except (OSError, ValueError):
                held, age = {}, RUN_LOCK_STALE + 1
            if age <= RUN_LOCK_STALE:
                return say({"ok": False, "held": held, "age_s": int(age)}, 3)
            try:
                os.unlink(path)   # stale: the run that took it died
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(mine, f)
        return say({"ok": True, "lock": mine})
    return say({"ok": False, "error": "could not take the lock"}, 3)


def cmd_unlock(args):
    goal = goal_dir(args.goal)
    try:
        os.unlink(os.path.join(goal, RUN_LOCK))
        return say({"ok": True})
    except FileNotFoundError:
        return say({"ok": True, "note": "no lock was held"})


def cmd_new_token(args):
    root = real_root(args.root)
    cfg = load_config(root)
    cfg["token"] = secrets.token_hex(16)
    save_config(root, cfg)
    port = cfg.get("port")
    return say({"ok": True, "note": "run `serve.py stop` and then `serve.py ensure` to serve the new token; old links stop working",
                "url": "http://127.0.0.1:%d/%s/" % (port, cfg["token"]) if port else None})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("ensure")
    p.add_argument("--root", required=True)
    p.set_defaults(fn=cmd_ensure)
    p = sub.add_parser("stop")
    p.add_argument("--root", required=True)
    p.set_defaults(fn=cmd_stop)
    p = sub.add_parser("run")
    p.add_argument("--root", required=True)
    p.add_argument("--port", type=int)
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("merge")
    p.add_argument("goal")
    p.add_argument("collection", choices=COLLECTIONS)
    p.add_argument("file")
    p.set_defaults(fn=cmd_merge)
    p = sub.add_parser("lock")
    p.add_argument("goal")
    p.add_argument("--owner", default="interactive")
    p.set_defaults(fn=cmd_lock)
    p = sub.add_parser("unlock")
    p.add_argument("goal")
    p.set_defaults(fn=cmd_unlock)
    p = sub.add_parser("new-token")
    p.add_argument("--root", required=True)
    p.set_defaults(fn=cmd_new_token)
    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_help(sys.stderr)
        return 2
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
