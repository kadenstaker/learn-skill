#!/usr/bin/env python3
"""The learn skill's scheduler: installs, removes and reports the jobs that run without an agent session.

usage:
  schedule.py install --server --root DIR [--python CMD] [--dry-run]
                                    start DIR's goal server at login, and again if it crashes
  schedule.py remove --server --root DIR [--dry-run]
                                    remove that job and stop its server
  schedule.py status --server --root DIR
                                    whether the job is installed and loaded, and whether a server answers

--server is the only job so far (the prepare-ahead run comes later). The job runs `serve.py run` in the
foreground, not `ensure`: the system keeps it running. It is a LaunchAgent on macOS; other systems get a clear
refusal until they are built. `serve.py stop` stops the job's server too, and it stays stopped until the next
login or `install`; `serve.py ensure` then starts a detached one as usual.

Never prompts. Each command prints one JSON line to stdout; diagnostics go to stderr. Safe to run again:
install replaces the job. Python 3.7+ standard library only.
"""
import argparse, hashlib, json, os, plistlib, shutil, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))   # not realpath: a skill folder symlink keeps working if the repo moves
SERVE = os.path.join(HERE, "serve.py")
sys.path.insert(0, HERE)
import serve   # noqa: E402  (whoami, the pid file, stop)


def say(obj, code=0):
    print(json.dumps(obj), flush=True)
    return code


def label(root):
    """One job per learning root: its name carries a short hash of the root's path."""
    return "learn.serve." + hashlib.sha1(root.encode("utf-8")).hexdigest()[:8]


def plist_path(root):
    return os.path.expanduser("~/Library/LaunchAgents/%s.plist" % label(root))


def domain():
    return "gui/%d" % os.getuid()


def launchctl(*args):
    p = subprocess.run(["launchctl"] + list(args), capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def loaded(root):
    return launchctl("print", "%s/%s" % (domain(), label(root)))[0] == 0


def unload(root):
    """bootout, then wait until the job is gone: a bootstrap while it is still unloading fails."""
    code, out = launchctl("bootout", "%s/%s" % (domain(), label(root)))
    end = time.monotonic() + 10
    while loaded(root):
        if time.monotonic() >= end:
            return False, out
        time.sleep(0.2)
    return True, out


def python_path(root, cmd):
    """An absolute path for the job: the --python command, else the profile's, else this interpreter.
    A PATH name such as /opt/homebrew/bin/python3 is kept as is, so a Python upgrade does not break the job."""
    if not cmd:
        try:
            with open(os.path.join(root, "profile.json"), "r", encoding="utf-8") as f:
                cmd = json.load(f).get("python")
        except (OSError, ValueError, AttributeError):
            cmd = None
    found = shutil.which(cmd) if isinstance(cmd, str) and cmd else None
    return os.path.abspath(found) if found else sys.executable


def server_state(root):
    cfg = serve.load_config(root, create=False) or {}
    port, token = cfg.get("port"), cfg.get("token")
    pid = serve.read_pid(root)
    return {"port": port, "pid": pid if serve.is_ours(pid) else None,
            "serving": bool(port and token and serve.whoami(port, token) == root)}


def job(root, python):
    return {
        "Label": label(root),
        "ProgramArguments": [python, SERVE, "run", "--root", root],
        "WorkingDirectory": root,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},   # a crash restarts it; `serve.py stop` (exit 0) does not
        "StandardOutPath": "/dev/null",           # run's first line carries the token
        "StandardErrorPath": os.path.join(root, serve.LOG_FILE),
    }


def refuse_platform():
    return say({"ok": False, "error": "the login job is built for macOS (launchd) only so far; on this system "
                "the server runs while `serve.py ensure` started it, until a restart"}, 2)


def cmd_install(args):
    root = serve.real_root(args.root)
    if sys.platform != "darwin":
        return refuse_platform()
    serve.load_config(root)   # port and token exist before the job first starts
    spec = job(root, python_path(root, args.python))
    path = plist_path(root)
    if args.dry_run:
        return say({"ok": True, "dry_run": True, "label": spec["Label"], "plist": path, "job": spec})
    # a server that ensure started holds the saved port: stop it, so the job's server gets that port
    if loaded(root):
        gone, out = unload(root)
        if not gone:
            return say({"ok": False, "error": "launchctl bootout failed: %s" % out}, 2)
    stopped = subprocess.run([sys.executable, SERVE, "stop", "--root", root], capture_output=True, text=True)
    if stopped.returncode != 0:
        return say({"ok": False, "error": "could not stop the running server: %s" % stopped.stdout.strip()}, 2)
    log = os.path.join(root, serve.LOG_FILE)
    os.close(os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))
    os.chmod(log, 0o600)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    serve.write_atomic(path, plistlib.dumps(spec), 0o644)
    code, out = launchctl("bootstrap", domain(), path)
    if code != 0:
        return say({"ok": False, "error": "launchctl bootstrap failed: %s" % out, "plist": path}, 2)
    up, end = False, time.monotonic() + 15
    while not up and time.monotonic() < end:
        cfg = serve.load_config(root)   # read each time: a first start with no saved port picks one and saves it
        up = bool(cfg.get("port")) and serve.serving(root, cfg["port"], cfg["token"])
        if not up:
            time.sleep(0.3)
    st = server_state(root)
    out = {"ok": up, "installed": True, "label": label(root), "plist": path, "port": st["port"], "pid": st["pid"]}
    if not up:
        out["error"] = "the job is loaded but its server does not answer; see %s" % log
    return say(out, 0 if up else 2)


def cmd_remove(args):
    root = serve.real_root(args.root)
    if sys.platform != "darwin":
        return refuse_platform()
    path = plist_path(root)
    if args.dry_run:
        return say({"ok": True, "dry_run": True, "label": label(root), "plist": path, "installed": os.path.exists(path)})
    was = loaded(root)
    if was:
        gone, out = unload(root)   # stops the job's server too
        if not gone:
            return say({"ok": False, "error": "launchctl bootout failed: %s" % out}, 2)
    existed = os.path.exists(path)
    if existed:
        os.unlink(path)
    return say({"ok": True, "removed": was or existed, "label": label(root)})


def cmd_status(args):
    root = serve.real_root(args.root)
    if sys.platform != "darwin":
        return say({"ok": True, "installed": False, "note": "no login job on this system yet", **server_state(root)})
    st = server_state(root)
    return say({"ok": True, "installed": os.path.exists(plist_path(root)), "loaded": loaded(root),
                "label": label(root), **st})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    for name, fn in (("install", cmd_install), ("remove", cmd_remove), ("status", cmd_status)):
        p = sub.add_parser(name)
        p.add_argument("--server", action="store_true", required=True, help="the goal server's login job")
        p.add_argument("--root", required=True)
        if name == "install":
            p.add_argument("--python", help="the Python command for the job (default: the profile's)")
        if name != "status":
            p.add_argument("--dry-run", action="store_true")
        p.set_defaults(fn=fn)
    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_help(sys.stderr)
        return 2
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
