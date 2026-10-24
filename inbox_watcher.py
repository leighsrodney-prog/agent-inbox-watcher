#!/usr/bin/env python3
"""Agent inbox watcher (Python 3.10+, standard library only).

A small team of agents (bots) shares one folder. Instead of messaging the lead
agent directly, each agent drops a Markdown file whose FIRST line is

    URGENCY: fyi | decision | risk

This watcher scans the folder every couple of seconds and wakes the lead agent
with an HTTP POST to a webhook. The POST body carries file NAMES only, never
file contents:

  risk          -> POST right away (next pass, ~2-5 s)
  decision      -> short grouping window: decision files that land within a few
                   seconds of each other share one wake (default 3 s, max 6 s)
  fyi / unknown -> batched: at most one POST per FYI_MIN_GAP (default 2 h),
                   only inside the working window (default Mon-Fri 9:00-17:00),
                   plus one end-of-window flush so late FYIs are not left over

The lead agent reads the files, then moves them to a done/ subfolder (or
deletes them). Names that leave the folder are forgotten.

Configuration (environment variables):
  WAKE_URL            webhook endpoint (https; http only with --allow-insecure-http)
  WAKE_AUTH           exact Authorization header value, e.g. "Bearer <token>"
                      If either is missing the watcher runs in DRY-RUN mode:
                      it logs what it would POST and announces nothing.
  AIW_REQUIRE_LIVE    1 = refuse to start in DRY-RUN (exit 4) and make
                      --status exit 4 if the running watcher is in DRY-RUN.
                      Use it in production so lost credentials fail loudly.
  AIW_INBOX           folder to watch          (default ./inbox)
  AIW_STATE_DIR       state, log, pid, acks    (default ./state)
  AIW_TZ              IANA time zone           (default UTC)
  AIW_LOOP_SECONDS    scan interval            (default 2)
  AIW_FYI_MIN_GAP     seconds between batches  (default 7200)
  AIW_FYI_WINDOW      "start-end" local hours  (default 9-17)
  AIW_FYI_FLUSH_MIN   end-of-window flush, minutes before the end (default 15; 0 = off)
  AIW_DECISION_WAIT   decision grouping wait, seconds (default 3)
  AIW_DECISION_MAX    decision max hold, seconds      (default 6)
  AIW_SETTLE_SECONDS  skip files modified this recently (default 2)

The URL and the auth value are never printed or logged.

Each POST attempt appends one JSON line per file to <state>/acks.jsonl:
  {"ts", "epoch", "file", "reason", "ok", "http", "mode"}
ok is true only on HTTP 2xx, so a sender can confirm its own file was delivered.

Usage: inbox_watcher.py            # loop forever
       inbox_watcher.py --once     # single pass
       inbox_watcher.py --status   # running yes/no, pid, mode, announced/pending
                                   # exit 3 if not running, 4 if dry-run under AIW_REQUIRE_LIVE
       inbox_watcher.py --require-live  # same as AIW_REQUIRE_LIVE=1
"""
import argparse
import datetime as dt
import errno
import fcntl
import json
import logging
import logging.handlers
import os
import re
import signal
import sys
import tempfile
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_window(name, default):
    raw = os.environ.get(name, default)
    try:
        a, b = (int(x) for x in raw.split("-", 1))
        if 0 <= a < b <= 24:
            return a, b
    except ValueError:
        pass
    a, b = (int(x) for x in default.split("-", 1))
    return a, b


INBOX = os.path.abspath(os.environ.get("AIW_INBOX") or "inbox")
STATE_DIR = os.path.abspath(os.environ.get("AIW_STATE_DIR") or "state")
STATE_PATH = os.path.join(STATE_DIR, "watcher_state.json")
LOG_PATH = os.path.join(STATE_DIR, "watcher.log")
PID_PATH = os.path.join(STATE_DIR, "watcher.pid")
MODE_PATH = os.path.join(STATE_DIR, "watcher.mode")   # LIVE or DRY-RUN of the running process
REQUIRE_LIVE = (os.environ.get("AIW_REQUIRE_LIVE") or "").strip().lower() in ("1", "true", "yes", "on")
EXIT_NOT_LIVE = 4
ALLOW_INSECURE_HTTP = False  # set by --allow-insecure-http
ACK_PATH = os.path.join(STATE_DIR, "acks.jsonl")

TZ = ZoneInfo(os.environ.get("AIW_TZ") or "UTC")
LOOP_SECONDS = max(1, _env_int("AIW_LOOP_SECONDS", 2))
FYI_MIN_GAP = _env_int("AIW_FYI_MIN_GAP", 2 * 3600)
FYI_WINDOW = _env_window("AIW_FYI_WINDOW", "9-17")
FYI_FLUSH_MIN = max(0, _env_int("AIW_FYI_FLUSH_MIN", 15))
SETTLE_SECONDS = max(0, _env_int("AIW_SETTLE_SECONDS", 2))  # ignore files modified this recently (still being written)
DECISION_GROUP_WAIT = _env_int("AIW_DECISION_WAIT", 3)
DECISION_MAX_HOLD = max(DECISION_GROUP_WAIT, _env_int("AIW_DECISION_MAX", 6))
BACKOFF_START, BACKOFF_MAX = 60, 1800
HTTP_TIMEOUT = 15
URGENCY_RE = re.compile(r"^\s*URGENCY:\s*(fyi|decision|risk)\s*$", re.IGNORECASE)
PROC_MARKER = b"inbox_watcher"

log = logging.getLogger("inbox_watcher")
_backoff = {}        # reason -> {"fails": n, "next": epoch}; in memory only
_dry_logged = {}     # reason -> tuple(files) already logged (dry-run de-dupe)
_held_logged = None  # last (why, files) hold message, to avoid repeating every pass
_dry_acked = set()   # files already given a dry-run ack line


def setup_logging():
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    h = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S %z"))
    log.addHandler(h)


def redact(text):
    """Strip secret material from a message before it is logged."""
    text = str(text)
    for env in ("WAKE_URL", "WAKE_AUTH"):
        v = os.environ.get(env)
        if v:
            text = text.replace(v, "[redacted]")
            if env == "WAKE_AUTH" and " " in v:   # also the bare token after "Bearer "
                text = text.replace(v.split(" ", 1)[1], "[redacted]")
    return re.sub(r"https?://\S+", "[url-redacted]", text)


# ---------- state ----------
def load_state():
    try:
        with open(STATE_PATH) as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("announced", [])
    s.setdefault("last_fyi_post", None)
    s.setdefault("pid", None)
    return s


def save_state(s):
    fd, tmp = tempfile.mkstemp(prefix=".watcher_state.", dir=STATE_DIR)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(s, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------- inbox ----------
def _is_queue_name(name):
    return not name.startswith(".") and name.endswith(".md") and name.lower() != "readme.md"


def urgency_of(path):
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            first = f.readline()
    except OSError:
        return "fyi"
    m = URGENCY_RE.match(first)
    return m.group(1).lower() if m else "fyi"   # missing or unknown urgency -> fyi batch


def scan_inbox(now_epoch):
    """Return {name: urgency} for queue items (skips README, dotfiles, dirs, symlinks, files still being written)."""
    items = {}
    try:
        names = os.listdir(INBOX)
    except FileNotFoundError:
        return items
    for name in sorted(names):
        if not _is_queue_name(name):
            continue
        p = os.path.join(INBOX, name)
        try:
            st = os.lstat(p)
        except OSError:
            continue
        if not os.path.isfile(p) or os.path.islink(p):
            continue
        if now_epoch - st.st_mtime < SETTLE_SECONDS:
            continue
        items[name] = urgency_of(p)
    return items


def in_fyi_window(now_local):
    return now_local.weekday() < 5 and FYI_WINDOW[0] <= now_local.hour < FYI_WINDOW[1]


def in_flush_slot(now_local):
    """True in the last FYI_FLUSH_MIN minutes of the working window."""
    if not FYI_FLUSH_MIN or not in_fyi_window(now_local):
        return False
    end = now_local.replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(hours=FYI_WINDOW[1])
    return (end - now_local) <= dt.timedelta(minutes=FYI_FLUSH_MIN)


# ---------- per-file ack ----------
def write_acks(reason, files, ok, code, mode):
    """Append one JSON line per file. Never raises; never contains secrets."""
    now = time.time()
    ts = dt.datetime.fromtimestamp(now, TZ).isoformat(timespec="seconds")
    lines = "".join(json.dumps({"ts": ts, "epoch": round(now, 3), "file": f, "reason": reason,
                                "ok": bool(ok), "http": code, "mode": mode}) + "\n" for f in files)
    try:
        fd = os.open(ACK_PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, lines.encode())
        finally:
            os.close(fd)
    except OSError as e:
        log.warning("ack write failed: %s", redact(e))


def pending_unsettled_decision(now_epoch):
    """True if a decision file is still being written (mtime within SETTLE_SECONDS)."""
    try:
        names = os.listdir(INBOX)
    except FileNotFoundError:
        return False
    for name in names:
        if not _is_queue_name(name):
            continue
        p = os.path.join(INBOX, name)
        try:
            if now_epoch - os.lstat(p).st_mtime < SETTLE_SECONDS and urgency_of(p) == "decision":
                return True
        except OSError:
            continue
    return False


def oldest_age(files, now_epoch):
    ages = []
    for n in files:
        try:
            ages.append(now_epoch - os.lstat(os.path.join(INBOX, n)).st_mtime)
        except OSError:
            pass
    return max(ages) if ages else 0


# ---------- POST ----------
def creds():
    url = os.environ.get("WAKE_URL", "").strip()
    auth = os.environ.get("WAKE_AUTH", "").strip()
    if auth.lower().startswith("authorization:"):   # pasted as a full header line
        auth = auth.split(":", 1)[1].strip()
    if not url or not auth:
        return None
    return url, auth


def post(reason, files, now_epoch):
    """Return True only when the wake was really delivered (HTTP 2xx). Dry-run returns False."""
    body = {"reason": reason, "files": list(files), "count": len(files)}
    c = creds()
    if c is None:
        key = tuple(files)
        if _dry_logged.get(reason) != key:
            log.info("DRY-RUN (WAKE_URL/WAKE_AUTH not set): would POST %s", json.dumps(body))
            _dry_logged[reason] = key
        new = [f for f in files if f not in _dry_acked]
        if new:
            write_acks(reason, new, False, None, "dry-run")
            _dry_acked.update(new)
        return False
    b = _backoff.get(reason)
    if b and now_epoch < b["next"]:
        return False
    url, auth = c
    if not url.lower().startswith(("https://", "http://")):
        log.error("WAKE_URL is not an http(s) URL; not posting")
        write_acks(reason, files, False, None, "bad-url")
        return False
    if url.lower().startswith("http://") and not ALLOW_INSECURE_HTTP:
        log.error("WAKE_URL uses plain http://; refusing to send the auth header in cleartext "
                  "(use https:// or pass --allow-insecure-http)")
        write_acks(reason, files, False, None, "insecure-url")
        return False
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": auth,
                                          "User-Agent": "agent-inbox-watcher/1"})
    err = None
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code, err = e.code, f"HTTP {e.code}"
    except Exception as e:  # network error, timeout, TLS, ...
        code, err = None, f"{type(e).__name__}: {redact(e)}"
    if code is not None and 200 <= code < 300:
        _backoff.pop(reason, None)
        log.info("POST ok (HTTP %s) reason=%s count=%d files=%s", code, reason, len(files), list(files))
        write_acks(reason, files, True, code, "live")
        return True
    fails = (b["fails"] if b else 0) + 1
    delay = min(BACKOFF_START * 2 ** (fails - 1), BACKOFF_MAX)
    _backoff[reason] = {"fails": fails, "next": now_epoch + delay}
    log.warning("POST failed reason=%s count=%d (%s); attempt %d, retry in %ds",
                reason, len(files), redact(err or f"HTTP {code}"), fails, delay)
    write_acks(reason, files, False, code, "live")
    return False


# ---------- one pass ----------
def run_pass(now_epoch=None):
    global _held_logged
    now_epoch = time.time() if now_epoch is None else now_epoch
    now_local = dt.datetime.fromtimestamp(now_epoch, TZ)
    s = load_state()
    items = scan_inbox(now_epoch)
    announced = set(s["announced"]) & set(items)       # forget names that left the inbox
    pending = {n: u for n, u in items.items() if n not in announced}
    changed = announced != set(s["announced"])

    for reason in ("risk", "decision"):                 # risk first and immediate; decision grouped
        files = sorted(n for n, u in pending.items() if u == reason)
        if not files:
            continue
        if reason == "decision":
            age = oldest_age(files, now_epoch)
            if age < DECISION_GROUP_WAIT or (age < DECISION_MAX_HOLD and pending_unsettled_decision(now_epoch)):
                if _held_logged != ("decision-group", tuple(files)):
                    log.info("decision group wait (%d files, oldest %.1fs)", len(files), age)
                    _held_logged = ("decision-group", tuple(files))
                continue
        if post(reason, files, now_epoch):
            announced |= set(files)
            changed = True

    fyi = sorted(n for n, u in pending.items() if u == "fyi")
    if fyi:
        last = s["last_fyi_post"]
        gap_ok = last is None or now_epoch - float(last) >= FYI_MIN_GAP
        flush = in_flush_slot(now_local) and not s.get("flushed_on") == now_local.date().isoformat()
        why = None if in_fyi_window(now_local) else "outside the fyi window"
        if why is None and not gap_ok and not flush:
            why = "last fyi POST too recent"
        if why:
            if _held_logged != (why, tuple(fyi)):
                log.info("fyi batch held (%d files): %s", len(fyi), why)
                _held_logged = (why, tuple(fyi))
        elif post("fyi-batch", fyi, now_epoch):
            announced |= set(fyi)
            s["last_fyi_post"] = now_epoch
            if in_flush_slot(now_local):
                s["flushed_on"] = now_local.date().isoformat()
            changed = True

    if changed or s.get("pid") != os.getpid():
        s["announced"] = sorted(announced)
        s["pid"] = os.getpid()
        save_state(s)
    return {"announced": sorted(announced), "pending": {n: u for n, u in items.items() if n not in announced}}


# ---------- single instance ----------
def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return PROC_MARKER in f.read()
    except OSError:
        return True   # no /proc (e.g. macOS): trust the signal check


def read_pid():
    try:
        with open(PID_PATH) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


_lock_fd = None


def acquire_lock():
    global _lock_fd
    fd = os.open(PID_PATH, os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(fd, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    os.fsync(fd)
    _lock_fd = fd
    return True


def release_lock():
    global _lock_fd
    if _lock_fd is not None:
        try:
            os.ftruncate(_lock_fd, 0)
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            os.close(_lock_fd)
        except OSError:
            pass
        _lock_fd = None


def write_mode(mode):
    """Record the running process's mode so --status (another shell) can report it."""
    tmp = MODE_PATH + ".tmp"
    with open(tmp, "w") as f:
        f.write(mode + "\n")
    os.replace(tmp, MODE_PATH)


def read_mode():
    try:
        with open(MODE_PATH) as f:
            return f.read().strip() or None
    except OSError:
        return None


def status():
    pid = read_pid()
    running = pid_alive(pid)
    run_mode = read_mode() if running else None
    s = load_state()
    items = scan_inbox(time.time() + SETTLE_SECONDS)
    ann = set(s["announced"]) & set(items)
    pend = {n: u for n, u in items.items() if n not in ann}
    by = {k: sum(1 for u in pend.values() if u == k) for k in ("risk", "decision", "fyi")}
    print(f"running: {'yes' if running else 'no'}")
    print(f"pid: {pid if running else '-'}")
    if running and run_mode:
        print(f"mode: {run_mode} (running watcher)")
    elif running:
        print("mode: unknown (running watcher has no watcher.mode; restart it to record its mode)")
    else:
        print(f"mode: {'LIVE' if creds() else 'DRY-RUN (WAKE_URL/WAKE_AUTH not set in this shell)'}")
    print(f"inbox: {INBOX}")
    print(f"announced: {len(ann)}")
    print(f"pending: {len(pend)} (risk {by['risk']}, decision {by['decision']}, fyi {by['fyi']})")
    lf = s.get("last_fyi_post")
    print("last fyi POST: " + (dt.datetime.fromtimestamp(float(lf), TZ).strftime("%Y-%m-%d %H:%M %Z") if lf else "never"))
    if not running:
        return 3
    if REQUIRE_LIVE and run_mode is None:
        print("ALERT: AIW_REQUIRE_LIVE is set but the running watcher's mode is unknown; restart it")
        return EXIT_NOT_LIVE
    if REQUIRE_LIVE and run_mode != "LIVE":
        print("ALERT: watcher is running in DRY-RUN but AIW_REQUIRE_LIVE is set; nothing is being delivered")
        return EXIT_NOT_LIVE
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true", help="single pass, then exit")
    g.add_argument("--status", action="store_true", help="print status and exit (exit code 3 if not running)")
    ap.add_argument("--require-live", action="store_true",
                    help="refuse to start without WAKE_URL/WAKE_AUTH (exit 4); same as AIW_REQUIRE_LIVE=1")
    ap.add_argument("--allow-insecure-http", action="store_true",
                    help="permit a plain http:// WAKE_URL (auth header is sent unencrypted)")
    a = ap.parse_args()
    global ALLOW_INSECURE_HTTP, REQUIRE_LIVE
    ALLOW_INSECURE_HTTP = a.allow_insecure_http
    REQUIRE_LIVE = REQUIRE_LIVE or a.require_live
    if a.status:
        return status()
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(INBOX, exist_ok=True)
    setup_logging()
    if REQUIRE_LIVE and creds() is None:
        msg = ("refusing to start: AIW_REQUIRE_LIVE is set but WAKE_URL/WAKE_AUTH are missing "
               "(the watcher would run in DRY-RUN and deliver nothing)")
        print(msg, file=sys.stderr)
        log.error(msg)
        return EXIT_NOT_LIVE
    if not acquire_lock():
        msg = f"another inbox_watcher is running (pid {read_pid()}); exiting"
        print(msg, file=sys.stderr)
        log.info(msg)
        return 1
    stop = {"flag": False}

    def _stop(signum, _frm):
        stop["flag"] = True
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    mode = "LIVE" if creds() else "DRY-RUN"
    write_mode(mode)
    log.info("start pid=%d mode=%s once=%s", os.getpid(), mode, a.once)
    if mode == "DRY-RUN":
        log.warning("running in DRY-RUN: no wakes will be delivered until WAKE_URL/WAKE_AUTH are set and the watcher restarts")
    try:
        while True:
            try:
                r = run_pass()
                if a.once:
                    log.info("pass done: announced=%d pending=%d", len(r["announced"]), len(r["pending"]))
            except Exception as e:
                log.error("pass error: %s: %s", type(e).__name__, redact(e))
                if a.once:
                    return 2
            if a.once or stop["flag"]:
                break
            for _ in range(LOOP_SECONDS):
                if stop["flag"]:
                    break
                time.sleep(1)
            if stop["flag"]:
                break
    finally:
        log.info("stop pid=%d", os.getpid())
        release_lock()
    return 0


if __name__ == "__main__":
    sys.exit(main())
