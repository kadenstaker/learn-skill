#!/usr/bin/env python3
"""The learn skill's prepare-ahead tick: grades finished lessons and writes the next one while nobody watches.

usage:
  tick.py --root DIR [--dry-run]

The scheduler (schedule.py install) runs this once a day at the profile's prepare_ahead time. It makes no model
calls itself. For each served goal under DIR (#app[data-served]) with a finished lesson waiting (a finish:<lesson>
record in state.json newer than that section's data-graded), it takes the goal's run lock, starts the agent command
from the profile in the goal folder with a prompt that points at SKILL.md's "Scheduled run" section, then checks
that the markers were cleared, commits the notes folder when it is a git repository that was clean before, and
tells the learner the lesson is ready: the ntfy daily nudge gets the ready text (same time), or a desktop
notification when ntfy is off. Only a cleared marker counts as graded, so a failed or partial run is tried again
next time. Offline (the agent's API does not answer), it does nothing and the next run retries. It never starts
serve.py: the login job or the next session's `ensure` does.

The profile's prepare_ahead is {"command": [argv], "time": "HH:MM", "leave_notes": bool}. In the command,
"{python}", "{skill}", "{root}" and "{notes}" are replaced inside each argument, and an argument that is exactly
"{add_dirs}" becomes --add-dir <folder> for the learning root, the skill folder, the notes folder (unless
leave_notes) and the folder of each local source file the goal's note names, except one in Documents, Desktop,
Downloads, iCloud Drive or ~/Library/CloudStorage, where the agent would wait on a macOS dialog nobody answers.
The prompt is added last.
The env file ~/.config/learn/tick.env (under $XDG_CONFIG_HOME when set; KEY=VALUE lines, mode 0600, outside the
learning root) is added to the agent's environment, for a credential when the agent has no keychain login.

Each goal it runs appends one JSON line to <root>/tick.log (0600): time, goal, result (graded, partial, failed,
timeout, locked, offline), the lessons, denied actions, the agent's result subtype and closing line. Nothing
waiting: no line.
Prints one JSON line to stdout; diagnostics go to stderr. Python 3.7+ standard library only.
"""
import argparse, json, os, re, shutil, signal, socket, subprocess, sys, time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import serve   # noqa: E402  (records, the run lock, the profile, nudges)

LOG = "tick.log"
LOG_MAX = 1 << 20
AGENT_TIMEOUT = 45 * 60           # seconds; well under the run lock's 2 hours, so no one takes it over mid-run
SECTION_RE = re.compile(r'<section\b[^>]*>', re.S)
PAGE_LINE = re.compile(r"^page:\s*(.+?)\s*$", re.M)
SOURCE_LINE = re.compile(r"^source:\s*(.+?)\s*$", re.M)
PATHISH = re.compile(r"(?:~|/)[^,;()\n]*")
# where macOS asks a person before a background job may read (checked in 5f for iCloud Drive and in 5g for
# Documents: claude waits on the dialog, so the run hangs until killed)
PROTECTED = ("~/Documents", "~/Desktop", "~/Downloads", "~/Library/Mobile Documents", "~/Library/CloudStorage")


def say(obj, code=0):
    print(json.dumps(obj), flush=True)
    return code


def expand(p):
    return os.path.realpath(os.path.expanduser(p)) if isinstance(p, str) and p else None


def env_file():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "learn", "tick.env")


def read_env_file():
    path, out = env_file(), {}
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return out
    if st.st_mode & 0o077:
        sys.stderr.write("%s is readable by others; not used (chmod 600 it)\n" % path)
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def served(goal):
    """True when the goal page's #app carries data-served."""
    try:
        with open(os.path.join(goal, "index.html"), "r", encoding="utf-8", errors="replace") as f:
            m = serve.GOAL_ATTR_RE.search(f.read())
    except OSError:
        return False
    return bool(m and re.search(r"\bdata-served\b", m.group(0)))


def graded_marks(goal):
    """{lesson id: its section's data-graded, or None}."""
    with open(os.path.join(goal, "index.html"), "r", encoding="utf-8", errors="replace") as f:
        html = f.read()
    out = {}
    for tag in SECTION_RE.findall(html):
        sid = re.search(r'\bid="([^"]*)"', tag)
        if sid and re.search(r'\bclass="[^"]*\blesson\b', tag):
            g = re.search(r'\bdata-graded="([^"]*)"', tag)
            out[sid.group(1)] = g.group(1) if g else None
    return out


def waiting(goal):
    """Lessons with a finish marker newer than their section's data-graded, oldest marker first (SKILL.md,
    Finished lessons first, step 2). A marker for a lesson with no section on the page is left alone."""
    try:
        recs = serve.read_records(goal, "state")
        marks = graded_marks(goal)
    except (serve.StoreError, OSError) as e:
        sys.stderr.write("%s: %s\n" % (goal, e))
        return []
    out = []
    for key, r in recs.items():
        if not key.startswith("finish:") or not isinstance(r, dict):
            continue
        lesson = key[len("finish:"):]
        if lesson in marks and serve.at_time(r.get("at")) > serve.at_time(marks[lesson]):
            out.append((serve.at_time(r.get("at")), lesson))
    return [lesson for _, lesson in sorted(out)]


def goals(root):
    for name in sorted(os.listdir(root)):
        goal = os.path.join(root, name)
        if serve.SLUG_RE.fullmatch(name) and not os.path.islink(goal) and os.path.isfile(os.path.join(goal, "index.html")) and served(goal):
            yield name, goal


def goal_note(notes, goal):
    """The note in the notes folder whose page: line names this goal's index.html."""
    page = os.path.realpath(os.path.join(goal, "index.html"))
    for dirpath, dirnames, filenames in os.walk(notes):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if not fn.endswith(".md"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    head = f.read(4096)
            except OSError:
                continue
            m = PAGE_LINE.search(head)
            if m and expand(m.group(1)) == page:
                return path, head
    return None, ""


def source_dirs(head):
    """The folders of the local files the note's source: line names (a URL or a book title adds none). One in a
    PROTECTED folder is left out, so the agent is refused at once instead of
    waiting on a dialog; it then grades and leaves the next lesson for the interactive run."""
    m = SOURCE_LINE.search(head)
    out = []
    for cand in PATHISH.findall(m.group(1) if m else ""):
        cand = cand.strip().rstrip(".")
        path = expand(cand)
        if path and not protected(path) and os.path.exists(path):
            d = path if os.path.isdir(path) else os.path.dirname(path)
            if d not in out:
                out.append(d)
    return out


def protected(path):
    """The PROTECTED folder path is in, or None."""
    for p in PROTECTED:
        d = os.path.realpath(os.path.expanduser(p))
        if path and (path == d or path.startswith(d + os.sep)):
            return p
    return None


def online(cmd):
    """False only when a Claude agent's API does not answer; other agents are not checked."""
    if os.path.basename(cmd[0]) != "claude":
        return True
    try:
        socket.create_connection(("api.anthropic.com", 443), timeout=5).close()
        return True
    except OSError:
        return False


def build_argv(pa, fill, dirs):
    out = []
    for a in pa["command"]:
        if a == "{add_dirs}":
            for d in dirs:
                out += ["--add-dir", d]
        else:
            for k, v in fill.items():
                a = a.replace("{%s}" % k, v)
            out.append(a)
    return out


def prompt(skill, goal, root, notes, python, lessons, tz):
    today = serve.local_today(time.time(), tz)
    lines = [
        "Scheduled prepare-ahead run of the learn skill. Nobody is watching and nothing can be approved.",
        "Read %s and follow its \"Scheduled run\" section." % os.path.join(skill, "SKILL.md"),
        "Goal folder (the current folder): %s" % goal,
        "Learning root: %s" % root,
        "Skill folder: %s" % skill,
        "Python: %s" % python,
        ("Notes folder: %s" % notes) if notes else "Notes folder: left for the next interactive run; do not look for or edit the note.",
        "Finished lessons waiting: %s" % ", ".join(lessons),
        "Today is %s (%s). Now is %s." % (today.isoformat(), tz or "local time", serve.now_at()),
    ]
    return "\n".join(lines)


def result_json(text):
    """The agent's JSON result (claude --output-format json), or {}."""
    for line in reversed((text or "").strip().splitlines()):
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict):
            return doc
    try:
        doc = json.loads(text)
        return doc if isinstance(doc, dict) else {}
    except (TypeError, ValueError):
        return {}


def denials(doc):
    out = []
    for d in doc.get("permission_denials") or []:
        if not isinstance(d, dict):
            continue
        inp = d.get("tool_input") if isinstance(d.get("tool_input"), dict) else {}
        what = inp.get("command") or inp.get("file_path") or inp.get("path") or inp.get("pattern") or ""
        out.append({"tool": d.get("tool_name"), "input": str(what)[:200]})   # never a Write's content
    return out


def git(notes, *args):
    return subprocess.run(["git", "-C", notes] + list(args), capture_output=True, text=True, timeout=60)


def git_ok(notes):
    """A git repository, and a git that runs: on macOS /usr/bin/git opens an installer without Command Line Tools."""
    if not notes or not os.path.isdir(os.path.join(notes, ".git")) or not shutil.which("git"):
        return False
    if sys.platform == "darwin" and subprocess.run(["xcode-select", "-p"], capture_output=True).returncode != 0:
        return False
    return True


def notes_clean(notes):
    if not git_ok(notes):
        return None
    p = git(notes, "status", "--porcelain")
    return p.returncode == 0 and not p.stdout.strip()


def commit_notes(notes, slug):
    if not git(notes, "status", "--porcelain").stdout.strip():
        return "unchanged"
    if git(notes, "add", "-A").returncode != 0:
        return "not committed"
    p = git(notes, "commit", "-q", "-m", "learn: scheduled run, %s" % slug)
    return "committed" if p.returncode == 0 else "not committed"


def notify(root, prof):
    """ntfy on: the daily nudge says the lesson is ready, at the same time. Else a desktop notification."""
    if prof.get("reminders") == "ntfy":
        r = serve.nudge_send(root, ready=True)
        return "ntfy" if r.get("ok") and not r.get("failed") else "ntfy failed"
    text = "Next lesson is ready."
    if sys.platform == "darwin":
        p = subprocess.run(["osascript", "-e", 'display notification "%s" with title "Learning"' % text], capture_output=True)
        return "desktop" if p.returncode == 0 else "none"
    if shutil.which("notify-send"):
        p = subprocess.run(["notify-send", "Learning", text], capture_output=True)
        return "desktop" if p.returncode == 0 else "none"
    return "none"


def log(root, entry):
    path = os.path.join(root, LOG)
    try:
        if os.path.getsize(path) > LOG_MAX:
            os.replace(path, path + ".1")
    except OSError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(dict({"at": serve.now_at()}, **entry)) + "\n")


def run_agent(argv, cwd, env, timeout):
    """(stdout, exit code or None, timed out). The agent leads its own process group, so a timeout stops the
    commands it started too (an agent waiting on a macOS dialog nobody answers ends here)."""
    p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, start_new_session=True)
    try:
        stdout, stderr = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        for sig, wait in ((signal.SIGTERM, 10), (signal.SIGKILL, 10)):
            try:
                os.killpg(p.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                stdout, stderr = p.communicate(timeout=wait)
                break
            except subprocess.TimeoutExpired:
                continue
        else:
            stdout, stderr = "", ""
        return stdout or "", None, True
    if p.returncode != 0:
        sys.stderr.write((stderr or "")[-2000:])
    return stdout, p.returncode, False


def run_goal(root, prof, pa, slug, goal, lessons, notes, python, skill):
    out = {"goal": slug, "lessons": lessons}
    got, code = serve.take_lock(goal, "tick")
    if code != 0:
        return dict(out, result="locked")   # another run grades now; next time
    try:
        head = goal_note(notes, goal)[1] if notes else ""
        dirs = [root, skill] + ([notes] if notes else []) + [d for d in source_dirs(head) if d not in (root, skill, notes)]
        argv = build_argv(pa, {"python": python, "skill": skill, "root": root, "notes": notes or ""}, dirs)
        argv.append(prompt(skill, goal, root, notes, python, lessons, prof.get("tz")))
        env = dict(os.environ, **read_env_file())
        clean = notes_clean(notes) if notes else None
        start = time.monotonic()
        try:
            stdout, rc, timed_out = run_agent(argv, goal, env, pa.get("timeout_min", AGENT_TIMEOUT // 60) * 60)
        except OSError as e:
            return dict(out, result="failed", error=str(e)[:200])
        doc = result_json(stdout)
        left = set(waiting(goal)) & set(lessons)
        result = "timeout" if timed_out else "graded" if not left else "partial" if len(left) < len(lessons) else "failed"
        out.update(result=result, seconds=int(time.monotonic() - start), exit=rc, subtype=doc.get("subtype"),
                   denied=denials(doc))
        if isinstance(doc.get("result"), str):
            out["reply"] = " ".join(doc["result"].split())[:400]   # the agent's closing sentence
        if isinstance(doc.get("total_cost_usd"), (int, float)):
            out["cost_usd"] = round(doc["total_cost_usd"], 2)
        if clean:
            out["notes"] = commit_notes(notes, slug)
        elif clean is False:
            out["notes"] = "not committed: the notes folder had other changes"
        if result in ("graded", "partial"):
            out["notified"] = notify(root, prof)
        return out
    finally:
        try:
            os.unlink(os.path.join(goal, serve.RUN_LOCK))
        except FileNotFoundError:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", required=True)
    ap.add_argument("--dry-run", action="store_true", help="say what would run, run nothing")
    args = ap.parse_args(argv)
    root = serve.real_root(args.root)
    prof = serve.read_profile(root)
    pa = prof.get("prepare_ahead")
    if not isinstance(pa, dict) or not isinstance(pa.get("command"), list) or not pa["command"]:
        return say({"ok": False, "error": "prepare-ahead is off in %s" % os.path.join(root, serve.PROFILE)}, 2)
    notes = None if pa.get("leave_notes") else expand(prof.get("notes"))
    python, skill = sys.executable, os.path.realpath(os.path.dirname(HERE))
    todo = [(slug, goal, waiting(goal)) for slug, goal in goals(root)]
    todo = [t for t in todo if t[2]]
    if args.dry_run or not todo:
        return say({"ok": True, "dry_run": args.dry_run, "waiting": {s: l for s, _, l in todo}})
    if not online(pa["command"]):
        log(root, {"result": "offline", "goals": [s for s, _, _ in todo]})
        return say({"ok": True, "offline": True})
    runs = []
    for slug, goal, lessons in todo:
        entry = run_goal(root, prof, pa, slug, goal, lessons, notes, python, skill)
        log(root, entry)
        runs.append(entry)
    return say({"ok": True, "runs": runs})


if __name__ == "__main__":
    sys.exit(main())
