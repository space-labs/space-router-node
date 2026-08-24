/**
 * jsdom harness for the real GUI frontend.
 *
 * Loads the actual `gui/assets/index.html` and the actual `gui/assets/app.js`
 * into a jsdom window and drives `updateStatus()` — the production render path
 * — with the payloads `gui/api.py:get_status()` returns.  Nothing in app.js is
 * reimplemented, transcribed or stubbed; the only thing faked is the pywebview
 * transport (`window.pywebview.api.get_status`), i.e. the boundary the browser
 * side does not own.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM } from "jsdom";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const REPO_ROOT = path.resolve(HERE, "..", "..");
const ASSETS = path.join(REPO_ROOT, "gui", "assets");

const FIXTURES = JSON.parse(
  fs.readFileSync(path.join(HERE, "fixtures", "status_sequence.json"), "utf8"),
);

/** A recorded status sequence, generated from the real NodeStateMachine. */
export function sequence(name) {
  const seq = FIXTURES.sequences[name];
  if (!seq) throw new Error("no such fixture sequence: " + name);
  // `next_retry_at` is an absolute unix timestamp captured when the fixture was
  // generated. Re-base it onto "now" so the countdown the GUI computes is live
  // rather than a long-expired one.
  const generatedAt = FIXTURES.generated_at;
  const now = Date.now() / 1000;
  return seq.map(function (frame) {
    const copy = Object.assign({}, frame);
    if (copy.next_retry_at !== null && copy.next_retry_at !== undefined) {
      copy.next_retry_at = now + (copy.next_retry_at - generatedAt);
    }
    return copy;
  });
}

/**
 * Boot the real GUI in jsdom.
 *
 * Returns `{ window, render, modalCalls, close }`.
 *  - `render(payload)` awaits one full production `updateStatus()` cycle.
 *  - `modalCalls` collects every `showErrorReportModal()` invocation.
 */
export async function bootGui() {
  const html = fs.readFileSync(path.join(ASSETS, "index.html"), "utf8");
  const appJs = fs.readFileSync(path.join(ASSETS, "app.js"), "utf8");

  // `outside-only` keeps index.html's own <script src="app.js"> inert so the
  // pywebview transport can be installed before app.js runs; app.js is then
  // evaluated verbatim in the window's global scope, which is what a <script>
  // tag would have done.
  const dom = new JSDOM(html, {
    runScripts: "outside-only",
    pretendToBeVisual: true,
    url: "http://localhost/",
  });
  const window = dom.window;

  let nextStatus = null;
  const modalCalls = [];

  window.pywebview = {
    api: {
      get_status: async function () {
        if (nextStatus === null) throw new Error("no status queued");
        return nextStatus;
      },
      // Called by the parts of updateStatus()/updateEarningsRow() the status
      // render touches; harmless no-ops for these tests.
      get_receipt_summary: async function () {
        return { ok: false };
      },
      get_build_version: async function () {
        return "1.5.2-test.134";
      },
    },
  };

  window.eval(appJs);

  // Spy on the modal. `showErrorReportModal` is a top-level function
  // declaration in a classic script, so it lives on the global object and the
  // call site inside updateStatus() resolves through it — replacing it here
  // really does intercept the production call.
  if (typeof window.showErrorReportModal !== "function") {
    throw new Error("app.js did not expose showErrorReportModal");
  }
  window.showErrorReportModal = function () {
    modalCalls.push({ at: modalCalls.length });
  };
  // Same for the version-check modal, which is irrelevant here and would
  // otherwise poke at timers.
  window.handleVersionCheck = function () {};

  const statusText = window.document.querySelector("#status-text");
  const statusDetail = window.document.querySelector("#status-detail");
  const stakingStatus = window.document.querySelector("#staking-status");
  const statusDot = window.document.querySelector("#status-dot");
  if (!statusText || !statusDetail || !stakingStatus || !statusDot) {
    throw new Error("index.html is missing the status elements under test");
  }

  async function render(payload) {
    nextStatus = payload;
    // updateStatus() swallows exceptions ("backend not ready yet"), which would
    // silently turn a broken harness into a green test. Assert the render
    // actually landed by checking the DOM moved off its initial value.
    const before = statusText.textContent;
    await window.updateStatus();
    return {
      state: payload.state,
      retry_count: payload.retry_count,
      error_code: payload.error_code,
      text: statusText.textContent,
      detail: statusDetail.textContent,
      staking: stakingStatus.textContent,
      dot: statusDot.className,
      changed: statusText.textContent !== before,
    };
  }

  return {
    window,
    render,
    modalCalls,
    close: function () {
      window.close();
    },
  };
}

/** Render a whole sequence and return one row per poll. */
export async function renderSequence(gui, frames) {
  const rows = [];
  for (const frame of frames) {
    rows.push(await gui.render(frame));
  }
  return rows;
}
