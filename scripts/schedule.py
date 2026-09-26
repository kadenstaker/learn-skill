#!/usr/bin/env python3
"""The learn skill's scheduler: installs, removes and reports the jobs that run without an agent session.

usage:
  schedule.py install --root DIR --time HH:MM [--agent claude | --command JSON] [--leave-notes] [--python CMD] [--dry-run]
                                    the prepare-ahead tick: once a day at HH:MM (or at the next wake), grade finished
                                    lessons and write the next one (tick.py); saves prepare_ahead in the profile
  schedule.py remove --root DIR [--dry-run]
                                    remove the tick and set prepare_ahead to null
  schedule.py status --root DIR     whether the tick is installed and loaded, and its last log lines
  schedule.py install --server --root DIR [--python CMD] [--dry-run]
                                    start DIR's goal server at login, and again if it crashes
  schedule.py remove --server --root DIR [--dry-run]
                                    remove that job and stop its server
  schedule.py status --server --root DIR
                                    whether the job is installed and loaded, and whether a server answers

The login job (--server) runs `serve.py run` in the foreground, not `ensure`: the system keeps it running. The tick
never starts the server. Both are LaunchAgents on macOS; other systems get a clear refusal until they are built.
The tick is a StartCalendarInterval job, which launchd runs once on wake when the Mac slept through the time.
install --agent claude writes the checked Claude Code command (claude -p --restricted, --permission-prompts none,
file tools and one serve.py merge Bash pattern); --command takes another agent's argv as a JSON list, with the
placeholders tick.py fills. install warns when the learning root or notes folder sits where macOS asks before a
background job may read it (Documents, Desktop, Downloads, iCloud Drive, ~/Library/CloudStorage). `serve.py stop` stops the job's server too, and it stays stopped until the next
login or `install`; `serve.py ensure` then starts a detached one as usual.

Never prompts. Each command prints one JSON line to stdout; diagnostics go to stderr. Safe to run again:
install replaces the job. Python 3.7+ standard library only.
"""
import argparse, hashlib, json, os, plistlib, shutil, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))   # not realpath: a skill folder symlink keeps working if the repo moves
SERVE = os.path.join(HERE, "serve.py")
sys.path.insert(0, HERE)
import serve   # noqa: E402  (whoami, the pid file, stop)
from tick import protected   # noqa: E402


def say(obj, code=0):
    print(json.dumps(obj), flush=True)
    return code


TICK = os.path.join(HERE, "tick.py")
TICK_ERR = "tick.err"


def label(root, kind="serve"):
    """One job of each kind per learning root: its name carries a short hash of the root's path."""
    return "learn.%s.%s" % (kind, hashlib.sha1(root.encode("utf-8")).hexdigest()[:8])


def plist_path(root, kind="serve"):
    return os.path.expanduser("~/Library/LaunchAgents/%s.plist" % label(root, kind))


def domain():
    return "gui/%d" % os.getuid()


def launchctl(*args):
    p = subprocess.run(["launchctl"] + list(args), capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def loaded(root, kind="serve"):
    return launchctl("print", "%s/%s" % (domain(), label(root, kind)))[0] == 0


def unload(root, kind="serve"):
    """bootout, then wait until the job is gone: a bootstrap while it is still unloading fails."""
    code, out = launchctl("bootout", "%s/%s" % (domain(), label(root, kind)))
    end = time.monotonic() + 10
    while loaded(root, kind):
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


def refuse_platform(tick=False):
    if tick:
        return say({"ok": False, "error": "the prepare-ahead tick is built for macOS (launchd) only so far; on this "
                    "system finished lessons are graded at the next session"}, 2)
    return say({"ok": False, "error": "the login job is built for macOS (launchd) only so far; on this system "
                "the server runs while `serve.py ensure` started it, until a restart"}, 2)


def claude_command():
    """The unattended Claude Code command, checked in 5f (SPEC Implementation notes), or (None, why not).
    --restricted ignores the learner's own settings and allow rules and confines the file tools to the working
    folders; --permission-prompts none denies what is not allowed instead of waiting for a person."""
    found = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    if not os.access(found, os.X_OK):
        return None, "claude is not on PATH or in ~/.local/bin"
    try:
        helptext = subprocess.run([found, "--help"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, "claude --help failed: %s" % e
    missing = [f for f in ("--restricted", "--permission-prompts") if f not in helptext]
    if missing:
        return None, "this Claude Code lacks %s; update it (claude update) and install again" % " and ".join(missing)
    return [os.path.abspath(found), "-p", "--restricted", "--strict-mcp-config",
            "--tools", "Read", "Write", "Edit", "Glob", "Grep", "Bash",
            "--permission-mode", "acceptEdits", "--permission-prompts", "none",
            "--allowedTools", "Read", "Write", "Edit", "Glob", "Grep", "Bash({python} {skill}/scripts/serve.py merge:*)",
            "{add_dirs}", "--output-format", "json"], None


def tick_job(root, python, hhmm):
    return {
        "Label": label(root, "tick"),
        "ProgramArguments": [python, TICK, "--root", root],
        "WorkingDirectory": root,
        "StartCalendarInterval": {"Hour": int(hhmm[:2]), "Minute": int(hhmm[3:])},
        "StandardOutPath": "/dev/null",           # tick.log holds the result
        "StandardErrorPath": os.path.join(root, TICK_ERR),
    }


def install_tick(args, root):
    if not args.time or not serve.TIME_RE.fullmatch(args.time):
        return say({"ok": False, "error": "--time HH:MM (24-hour) is required for the tick"}, 2)
    if args.command:
        try:
            cmd = json.loads(args.command)
        except ValueError:
            cmd = None
        if not (isinstance(cmd, list) and cmd and all(isinstance(a, str) for a in cmd) and os.path.isabs(cmd[0])):
            return say({"ok": False, "error": "--command is a JSON list of strings whose first item is an absolute path"}, 2)
    elif (args.agent or "claude") == "claude":
        cmd, why = claude_command()
        if not cmd:
            return say({"ok": False, "error": why}, 2)
    else:
        return say({"ok": False, "error": "only --agent claude is built; give another agent's argv with --command"}, 2)
    prof = serve.read_profile(root)
    notes = os.path.realpath(os.path.expanduser(prof["notes"])) if isinstance(prof.get("notes"), str) and prof["notes"] else None
    warn = []
    for what, path in (("learning root", root), ("notes folder", None if args.leave_notes else notes)):
        where = protected(path)
        if where:
            warn.append("the %s is under %s: macOS may block or ask before the job reads it (Full Disk Access for "
                        "the job's programs, or an Allow a person gives once)" % (what, where))
    if protected(notes) and not args.leave_notes:
        warn.append("install again with --leave-notes to leave the note for the next interactive run")
    spec = tick_job(root, python_path(root, args.python), args.time)
    pa = {"command": cmd, "time": args.time, "leave_notes": bool(args.leave_notes)}
    path = plist_path(root, "tick")
    out = {"ok": True, "label": spec["Label"], "plist": path, "time": args.time, "command": cmd[0]}
    if warn:
        out["warning"] = "; ".join(warn)
    if args.dry_run:
        return say(dict(out, dry_run=True, job=spec, prepare_ahead=pa))
    if loaded(root, "tick"):
        gone, msg = unload(root, "tick")
        if not gone:
            return say({"ok": False, "error": "launchctl bootout failed: %s" % msg}, 2)
    err = os.path.join(root, TICK_ERR)
    os.close(os.open(err, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))
    os.chmod(err, 0o600)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    serve.write_atomic(path, plistlib.dumps(spec), 0o644)
    code, msg = launchctl("bootstrap", domain(), path)
    if code != 0:
        return say({"ok": False, "error": "launchctl bootstrap failed: %s" % msg, "plist": path}, 2)
    prof["prepare_ahead"] = pa
    serve.save_profile(root, prof)
    return say(dict(out, installed=True))


def cmd_install(args):
    root = serve.real_root(args.root)
    if sys.platform != "darwin":
        return refuse_platform(not args.server)
    if not args.server:
        return install_tick(args, root)
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
        return refuse_platform(not args.server)
    if not args.server:
        return remove_tick(args, root)
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


def remove_tick(args, root):
    path = plist_path(root, "tick")
    if args.dry_run:
        return say({"ok": True, "dry_run": True, "label": label(root, "tick"), "plist": path, "installed": os.path.exists(path)})
    was = loaded(root, "tick")
    if was:
        gone, msg = unload(root, "tick")   # a tick already running goes on; its lock and log stay correct
        if not gone:
            return say({"ok": False, "error": "launchctl bootout failed: %s" % msg}, 2)
    existed = os.path.exists(path)
    if existed:
        os.unlink(path)
    prof = serve.read_profile(root)
    if prof.get("prepare_ahead") is not None:
        prof["prepare_ahead"] = None
        serve.save_profile(root, prof)
    return say({"ok": True, "removed": was or existed, "label": label(root, "tick")})


def last_ticks(root, n=5):
    try:
        with open(os.path.join(root, "tick.log"), "r", encoding="utf-8") as f:
            lines = f.readlines()[-n:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def cmd_status(args):
    root = serve.real_root(args.root)
    if not args.server:
        pa = serve.read_profile(root).get("prepare_ahead")
        base = {"ok": True, "time": pa.get("time") if isinstance(pa, dict) else None, "last": last_ticks(root)}
        if sys.platform != "darwin":
            return say(dict(base, installed=False, note="no tick on this system yet"))
        return say(dict(base, installed=os.path.exists(plist_path(root, "tick")), loaded=loaded(root, "tick"),
                        label=label(root, "tick")))
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
        p.add_argument("--server", action="store_true", help="the goal server's login job (default: the tick)")
        p.add_argument("--root", required=True)
        if name == "install":
            p.add_argument("--python", help="the Python command for the job (default: the profile's)")
            p.add_argument("--time", help="tick: HH:MM, 24-hour, local time")
            p.add_argument("--agent", choices=["claude"], help="tick: the agent to run (default: claude)")
            p.add_argument("--command", help="tick: another agent's argv as a JSON list (tick.py lists the placeholders)")
            p.add_argument("--leave-notes", action="store_true", help="tick: leave the note for the next interactive run")
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
