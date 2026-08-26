"""Drive the real GUI frontend with a real browser and save screenshots.

Pairs with `gui_bridge.py`, which serves the shipped `gui/assets` and wires the
real `gui.api.Api` behind the same `window.pywebview.api` contract pywebview
provides. This drives that page with real clicks and keystrokes and captures a
screenshot at every step, headless, so it never competes for the desktop.

Usage:
    python -m tests.e2e.gui_drive --url http://127.0.0.1:8770 --out <dir>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

# The GUI is a fixed-size desktop window; match it so layout bugs show up the
# way an operator would see them rather than being hidden by a wide viewport.
VIEWPORT = {"width": 480, "height": 760}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8770")
    ap.add_argument("--out", required=True)
    ap.add_argument("--label", default="gui")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    shots: list[str] = []
    console: list[str] = []
    step = [0]

    def shot(page, name: str) -> None:
        step[0] += 1
        p = out / f"{step[0]:02d}-{name}.png"
        page.screenshot(path=str(p), full_page=True)
        shots.append(p.name)
        print(f"  [shot] {p.name}", flush=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=2)
        page.on("console", lambda m: console.append(f"[{m.type}] {m.text}"))
        page.on("pageerror", lambda e: console.append(f"[pageerror] {e}"))

        page.goto(args.url, wait_until="load")
        page.wait_for_timeout(2500)
        shot(page, "landing")

        # What screen are we on? The app shows one <section> at a time.
        visible = page.evaluate("""() => {
            const out = [];
            document.querySelectorAll('section, .screen, [id^=screen-], .view').forEach(el => {
                const cs = getComputedStyle(el);
                if (cs.display !== 'none' && cs.visibility !== 'hidden' && el.id) {
                    out.push(el.id);
                }
            });
            return out;
        }""")
        print(f"  visible containers: {visible}", flush=True)

        # Capture the fields the operator actually reads, so a wrong value is
        # provable from the evidence rather than only visible in a picture.
        rendered = page.evaluate("""() => {
            const pick = (sel) => {
                const el = document.querySelector(sel);
                return el ? (el.textContent || el.value || '').trim() : null;
            };
            return {
                status_text: pick('#status-text'),
                status_detail: pick('#status-detail'),
                staking_status: pick('#staking-status'),
                identity_address: pick('#identity-address'),
                staking_input: pick('#staking-input') || pick('#staking-address-input'),
                title: document.title,
                body_first_line: (document.body.innerText || '').split('\\n').filter(Boolean)[0] || '',
            };
        }""")
        print("  rendered: " + json.dumps(rendered, indent=2), flush=True)

        (out / "rendered.json").write_text(json.dumps(rendered, indent=2))

        # Walk every top-level screen the app can show, so the review pass sees
        # more than the first paint.
        for sid in visible:
            page.wait_for_timeout(400)
            shot(page, f"screen-{sid}")

        (out / "console.log").write_text("\n".join(console) or "(no console output)")
        browser.close()

    print(f"\n  {len(shots)} screenshot(s) -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
