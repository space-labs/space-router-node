# Known issue: Windows GUI reports "Coordination offline" after a UPnP failure

Status: **open, needs a Windows tester to confirm the cause**
Reported: QA v1.5.2-test.136, Windows GUI, 2026-08-25 (Jenna Lee), raised as a blocker
Applies to: Windows GUI only. Windows CLI, macOS GUI/CLI and Linux CLI are unaffected.

## Symptom

The node shows "Coordination offline". The coordination server's probe gets
`connection_refused` against the operator's public IP on port 9090.

The log contains:

```
miniupnpc not installed — UPnP unavailable
UPnP enabled but mapping failed — falling back to direct public IP mode
```

On the same machine the Windows **CLI** maps the port through UPnP without
trouble. When the CLI exits it releases the mapping, and launching only the GUI
reproduces the failure.

## What we have ruled out

QA's stated cause was that `miniupnpc` is missing from the GUI build. It is not.
Verified against the shipped artifacts, not the source tree:

- `requirements.txt` pins `miniupnpc>=2.2,<3`; both `homenode.spec` and
  `spacerouter_gui.spec` list it in `hiddenimports`.
- The Windows CI logs for the GUI job show
  `miniupnpc-2.3.3-cp312-cp312-win_amd64.whl` installing successfully, and
  PyInstaller emits no missing-hidden-import warning for it.
- Parsing the PyInstaller archive of the shipped Windows GUI `.exe` shows
  `miniupnpc.cp312-win_amd64.pyd` and `miniupnpc-<hash>.dll` present, with
  sha256 identical to the Windows CLI `.exe`. The GUI bundle is a strict
  superset of the CLI bundle (418 entries vs 292).
- Every DLL the `.pyd` imports is bundled alongside it.
- UPX is not applied to either binary.

So the module and its dependencies ship in the GUI. The import still failed at
runtime on the operator's machine.

## What the message actually meant

`app/upnp.py` reported *every* `ImportError` as "miniupnpc not installed" and
discarded the exception text. On Windows a failed dependent-DLL load raises
`ImportError: DLL load failed while importing miniupnpc: ...`, which is an
`ImportError` subclass. A missing module and an unloadable one were therefore
indistinguishable in the log, which is why this could not be diagnosed.

Fixed in v1.5.2-test.139: the log line now names the exception type and message.

## The remaining harm

When UPnP is unavailable the node continues and registers
`https://{public_ip}:{NODE_PORT}` without verifying that anything forwards to
that port. The coordination probe then fails and the operator sees
"Coordination offline", which does not point at the real problem. Since
v1.5.2-test.137 the GUI does surface the server's real reason
(`connection_refused`) rather than a canned string, so the symptom is at least
legible now.

## What we need from a Windows tester

Run the current build and send the log line. It will now read either

- `UPnP unavailable — miniupnpc could not be imported: ImportError: DLL load failed ...`
  → a real loader problem, and the message will name the missing dependency, or
- no such line at all → UPnP worked and the blocker was environmental
  (the CLI still holding a mapping, the router refusing UPnP, Windows Defender
  Firewall blocking SSDP discovery).

Please also confirm the timestamp on the line. The GUI writes to a rotating log
at `~/.spacerouter/logs/spacerouter-node.log` that survives version upgrades, so
the line QA quoted may predate the build under test.

If UPnP genuinely cannot map the port, forwarding TCP 9090 to the machine
manually is the workaround, and the node should then register and go online.

## Follow-up work not yet done

- `app/node_logging.py` documents the GUI log directory as
  `%LOCALAPPDATA%/SpaceRouter/logs/`, but `_gui_log_dir()` resolves to
  `~/.spacerouter/logs/`. Support and QA have been pointed at a path that does
  not exist.
- Consider carrying the UPnP failure into the `endpoint_unreachable` message the
  operator sees, so a node that advertised an unforwarded port says so instead of
  reporting only that the coordination server could not reach it.
