"""RC regression suite for the SpaceRouter node CLI.

Portable across macOS / Linux / Windows. Runs the real frozen binary in an
isolated HOME for every case, so it never touches the operator's own config.

Usage:
    python3 regression.py --binary <path> [--staking 0x...] [--network]

--network enables the cases that talk to the real test coordination server
(registration, reaching qualifying/earning, reset-time deregister). Without it
only the offline cases run, which is what a CI runner behind a firewall can do.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

COORD = "https://spacerouter-coordination-api-test.fly.dev"
IS_WIN = sys.platform == "win32"

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "", output: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""),
          flush=True)
    if not ok and output:
        excerpt = "\n".join(f"      | {line}" for line in output.splitlines()[:15])
        print(excerpt, flush=True)


def fresh_home(**settings) -> str:
    home = tempfile.mkdtemp(prefix="sr-reg-")
    cfg = os.path.join(home, ".spacerouter")
    os.makedirs(cfg, exist_ok=True)
    if settings:
        with open(os.path.join(cfg, "settings.json"), "w", encoding=settings.pop("_encoding", "utf-8")) as fh:
            json.dump(settings, fh)
    return home


def run(binary, args, home, timeout=25, stdin_devnull=True):
    env = dict(os.environ, HOME=home, USERPROFILE=home)
    env.pop("SR_STAKING_ADDRESS", None)
    try:
        proc = subprocess.run(
            [binary, *args],
            env=env,
            stdin=subprocess.DEVNULL if stdin_devnull else None,
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"") + (exc.stderr or b"")
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return None, out


def run_until(binary, args, home, seconds, needle=None):
    """Start the daemon, let it run, then terminate it and return the log."""
    env = dict(os.environ, HOME=home, USERPROFILE=home)
    env.pop("SR_STAKING_ADDRESS", None)
    log = os.path.join(home, "run.log")
    with open(log, "w", encoding="utf-8", errors="replace") as fh:
        kwargs = {}
        if IS_WIN:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen([binary, *args], env=env, stdout=fh,
                                stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, **kwargs)
        deadline = time.time() + seconds
        while time.time() < deadline:
            time.sleep(2)
            if proc.poll() is not None:
                break
            if needle:
                with open(log, encoding="utf-8", errors="replace") as rh:
                    if needle in rh.read():
                        break
        if proc.poll() is None:
            proc.send_signal(signal.CTRL_BREAK_EVENT if IS_WIN else signal.SIGINT)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
    with open(log, encoding="utf-8", errors="replace") as rh:
        return rh.read()


def coord_nodes(staking):
    url = f"{COORD}/nodes?staking_address={staking}"
    with urllib.request.urlopen(url, timeout=20) as resp:
        payload = json.loads(resp.read().decode())
    return payload if isinstance(payload, list) else payload.get("nodes", [])


# ── offline cases ───────────────────────────────────────────────────────────

def case_version(b):
    _, out = run(b, ["--version"], fresh_home())
    record("version reports 1.5.2-test", "1.5.2-test" in out, out.strip()[:60])


def case_help_prefix_optional(b):
    _, out = run(b, ["--help"], fresh_home())
    ok = "0x prefix\n                        optional" in out or "prefix optional" in out
    bad = "0x followed by 40 hex chars" in out
    record("--help says the 0x prefix is optional", ok and not bad)


def case_setup_no_tty(b):
    code, out = run(b, ["--setup"], fresh_home())
    ok = ("requires a TTY" in out and code == 2 and "Traceback" not in out
          and "Failed to execute script" not in out)
    record("--setup without a TTY exits cleanly", ok, f"exit={code}", out)


def case_no_stdin_staking_prompt(b, staking):
    home = fresh_home(wallet={"staking_address": staking},
                      coordination={"url": COORD}, node={"port": 9099})
    out = run_until(b, ["--no-upnp"], home, 30, needle="Staking address:")
    ok = ("EOFError" not in out and "Failed to execute script" not in out)
    record("no stdin at the staking prompt does not crash", ok)


def case_bom(b, staking):
    home = tempfile.mkdtemp(prefix="sr-reg-")
    cfg = os.path.join(home, ".spacerouter")
    os.makedirs(cfg)
    with open(os.path.join(cfg, "settings.json"), "w", encoding="utf-8-sig") as fh:
        json.dump({"wallet": {"staking_address": staking},
                   "coordination": {"url": COORD}, "node": {"port": 9099}}, fh)
    out = run_until(b, ["--no-upnp"], home, 25, needle="Staking address:")
    record("settings.json with a UTF-8 BOM loads", "JSONDecodeError" not in out)


def case_bare_hex_normalised(b):
    bare = "6bb7d70bae8d51fc304212b43207f9e80ed680b8"
    home = fresh_home(coordination={"url": COORD}, node={"port": 9099})
    run_until(b, ["--no-upnp", "--staking-address", bare], home, 25,
              needle="Staking address:")
    path = os.path.join(home, ".spacerouter", "settings.json")
    stored = ""
    if os.path.exists(path):
        with open(path) as fh:
            stored = (json.load(fh).get("wallet") or {}).get("staking_address") or ""
    record("bare 40-hex is accepted and normalised to 0x",
           stored.lower() == "0x" + bare, f"stored={stored!r}")


def case_flag_overrides_settings(b):
    a = "0x" + "aa" * 20
    want = "0x" + "bb" * 20
    home = fresh_home(wallet={"staking_address": a},
                      coordination={"url": COORD}, node={"port": 9099})
    out = run_until(b, ["--no-upnp", "--staking-address", want], home, 25,
                    needle="Staking address:")
    record("--staking-address overrides settings.json",
           want[:10] in out and a[:10] not in out.split("Staking address:")[-1][:80])


def case_missing_staking_refused(b):
    home = fresh_home(coordination={"url": COORD}, node={"port": 9099})
    code, out = run(b, ["--no-upnp"], home, timeout=40)
    ok = ("staking" in out.lower() and code == 1 and "Traceback" not in out
          and "Failed to execute script" not in out)
    record("missing staking address is refused cleanly", ok, f"exit={code}", out)


# ── network cases ───────────────────────────────────────────────────────────

def case_register_and_earn(b, staking, port):
    home = fresh_home(wallet={"staking_address": staking},
                      coordination={"url": COORD}, node={"port": port})
    out = run_until(b, [], home, 200, needle="registering -> running")
    registered = '/nodes/register "HTTP/1.1 200 OK"' in out
    running = "-> running" in out
    used_right_wallet = staking.lower() in out.lower()
    record("registers with the coordination server", registered)
    record("reaches running", running)
    record("registers with the operator's staking wallet", used_right_wallet)
    time.sleep(5)
    rows = coord_nodes(staking)
    record("coordination server shows the node offline after SIGINT",
           bool(rows) and rows[0].get("status") == "offline",
           f"status={rows[0].get('status') if rows else 'no row'}")
    return home


def case_reset_deregisters(b, staking, port):
    """The bug: reset-time deregister sent the identity address, not the UUID."""
    home = fresh_home(wallet={"staking_address": staking},
                      coordination={"url": COORD}, node={"port": port})
    run_until(b, [], home, 150, needle="State: registering -> running")
    time.sleep(3)
    code, out = run(b, ["--reset"], home, timeout=90, stdin_devnull=True)
    failed_msg = "Coord deregister failed" in out
    record("--reset does not report a failed coord deregister", not failed_msg,
           "" if not failed_msg else "still reporting failure")
    record("--reset completes without a traceback",
           "Traceback" not in out and "Failed to execute script" not in out)



def case_wrong_password_file(b, staking):
    """The interactive retry loop needs a pty; --password-file does not."""
    home = fresh_home(wallet={"staking_address": staking},
                      coordination={"url": COORD}, node={"port": 9099})
    certs = os.path.join(home, ".spacerouter", "certs")
    os.makedirs(certs, exist_ok=True)
    try:
        from eth_account import Account
    except ImportError:
        record("wrong --password-file exits cleanly", True, "skipped: no eth_account")
        return
    ks = Account.encrypt("0x" + "11" * 32, "correct-horse")
    with open(os.path.join(certs, "node-identity.key"), "w") as fh:
        json.dump(ks, fh)
    pw = os.path.join(home, "pw.txt")
    with open(pw, "w") as fh:
        fh.write("WRONG-PASSPHRASE")
    code, out = run(b, ["--no-upnp", "--password-file", pw], home, timeout=60)
    ok = ("Traceback" not in out and "Failed to execute script" not in out
          and "eth_keyfile" not in out and "passphrase" in out.lower())
    record("wrong --password-file exits cleanly, no traceback", ok, f"exit={code}", out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", required=True)
    ap.add_argument("--staking", default="0x" + "ab" * 20)
    ap.add_argument("--port", type=int, default=9090)
    ap.add_argument("--network", action="store_true")
    args = ap.parse_args()

    b = os.path.abspath(args.binary)
    if not IS_WIN:
        os.chmod(b, 0o755)
    print(f"regression: {b}\nplatform: {sys.platform}\n", flush=True)

    case_version(b)
    case_help_prefix_optional(b)
    case_setup_no_tty(b)
    case_no_stdin_staking_prompt(b, args.staking)
    case_bom(b, args.staking)
    case_bare_hex_normalised(b)
    case_flag_overrides_settings(b)
    case_missing_staking_refused(b)
    case_wrong_password_file(b, args.staking)

    if args.network:
        case_register_and_earn(b, args.staking, args.port)
        case_reset_deregisters(b, args.staking, args.port)

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} passed", flush=True)
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name} {detail}", flush=True)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
