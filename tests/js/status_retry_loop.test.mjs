/**
 * GUI regression tests for the two QA findings on v1.5.2-test.134.
 *
 * BUG A — the "Send Error Report?" modal re-fired on every retry cycle.
 * BUG B — the main status line oscillated during registration retries.
 *
 * These drive the real `updateStatus()` out of `gui/assets/app.js` in jsdom,
 * with the payload sequence generated from the real `app.state.NodeStateMachine`
 * (see tests/js/fixtures/gen_status_sequence.py).
 *
 * Run with:  npm test        (or: node --test tests/js/)
 */

import test from "node:test";
import assert from "node:assert/strict";

import { bootGui, sequence, renderSequence } from "./harness.mjs";

/** Distinct values in order of first appearance. */
function distinct(values) {
  return [...new Set(values)];
}

/** The frames that are actually mid-retry-loop (retry_count has moved off 0). */
function midLoop(rows) {
  return rows.filter((r) => (r.retry_count || 0) >= 1);
}

// ─────────────────────────────────────────────────────────────────────────────
// Sanity: the harness is really exercising the production render path.
// ─────────────────────────────────────────────────────────────────────────────

test("harness drives the real render path (guards against silently swallowed errors)", async () => {
  const gui = await bootGui();
  try {
    const frames = sequence("registration_retry_loop");
    const first = await gui.render(frames[0]);
    // updateStatus() has a bare `catch (e) {}`; if the render had thrown, the
    // DOM would still hold index.html's initial copy.
    assert.notEqual(first.text, "");
    assert.notEqual(first.text, "Checking status...");
    assert.equal(first.state, "initializing");
    // And the payload really is the backend's shape.
    for (const key of [
      "state", "detail", "error_message", "error_code", "is_transient",
      "retry_count", "next_retry_at", "staking_status", "error_report_available",
    ]) {
      assert.ok(key in frames[0], "payload is missing NodeStatus key: " + key);
    }
  } finally {
    gui.close();
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// BUG A — error-report modal must fire once per distinct error
// ─────────────────────────────────────────────────────────────────────────────

test("BUG A: the error-report modal fires exactly once across many retry cycles", async () => {
  const gui = await bootGui();
  try {
    const frames = sequence("registration_retry_loop");
    // Six full failed attempts, i.e. six passes through
    // initializing -> binding -> registering -> error_transient.
    assert.equal(frames.filter((f) => f.state === "error_transient").length, 6);
    assert.ok(
      frames.some((f) => (f.retry_count || 0) >= 3 && f.error_report_available),
      "fixture must reach the retry_count >= 3 threshold",
    );

    await renderSequence(gui, frames);

    assert.equal(
      gui.modalCalls.length,
      1,
      "modal should be offered once for one persistent error, not once per " +
        "retry cycle (got " + gui.modalCalls.length + ")",
    );
  } finally {
    gui.close();
  }
});

test("BUG A: the offer stays at one under realistic multi-poll-per-state polling", async () => {
  // The GUI polls every 3s (showStatus's setInterval) while the backoff grows
  // towards the 120s cap, so several polls land inside each state rather than
  // one poll per transition. The fire rate is one per *retry cycle* (the guard
  // does hold within a single state), which is why over an 8h outage at the
  // capped backoff an operator saw the modal a couple of hundred times.
  const gui = await bootGui();
  try {
    const frames = [];
    for (const frame of sequence("registration_retry_loop")) {
      for (let i = 0; i < 4; i++) frames.push(frame);
    }
    await renderSequence(gui, frames);
    assert.equal(
      gui.modalCalls.length,
      1,
      "the offer must stay at one across a realistic poll density, got " +
        gui.modalCalls.length,
    );
  } finally {
    gui.close();
  }
});

test("BUG A: the modal fires again for a genuinely different error_code", async () => {
  const gui = await bootGui();
  try {
    const frames = sequence("code_change_after_retries");
    const codes = distinct(
      frames.filter((f) => f.error_code).map((f) => f.error_code),
    );
    assert.deepEqual(codes, ["endpoint_unreachable", "rate_limited"]);

    await renderSequence(gui, frames);

    assert.equal(
      gui.modalCalls.length,
      2,
      "one offer per distinct error code (endpoint_unreachable, then " +
        "rate_limited), got " + gui.modalCalls.length,
    );
  } finally {
    gui.close();
  }
});

test("BUG A: the modal fires again after the node recovers to running and fails anew", async () => {
  const gui = await bootGui();
  try {
    const frames = sequence("recover_then_fail_again");
    assert.ok(frames.some((f) => f.state === "running"), "fixture must reach running");

    const rows = await renderSequence(gui, frames);

    // Two separate incidents: the registration loop, then the post-running
    // reconnect loop. Each gets exactly one offer.
    assert.equal(
      gui.modalCalls.length,
      2,
      "reaching running must re-arm the offer, got " + gui.modalCalls.length,
    );
    // retry_count really was zeroed by the running transition, which is the
    // signal the fix relies on.
    const runningRow = rows.find((r) => r.state === "running");
    assert.equal(runningRow.retry_count, 0);
  } finally {
    gui.close();
  }
});

test("BUG A: an operator stop/start re-arms the offer", async () => {
  const gui = await bootGui();
  try {
    const frames = sequence("operator_restart_after_retries");
    assert.ok(frames.some((f) => f.state === "idle"), "fixture must pass through idle");

    await renderSequence(gui, frames);

    assert.equal(
      gui.modalCalls.length,
      2,
      "one offer before the restart and one after, got " + gui.modalCalls.length,
    );
  } finally {
    gui.close();
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// BUG B — the status line must settle, not oscillate
// ─────────────────────────────────────────────────────────────────────────────

test("BUG B: the replayed cycle really is four states, not three", async () => {
  // The daemon does not resume at a phase after the backoff:
  // NodeManager._schedule_retry spawns a fresh thread on _run_loop, which
  // re-enters app.main._run() from the top. So every attempt replays
  // initializing -> binding -> registering before failing again, and
  // retry_count climbs monotonically because NodeStateMachine.transition()
  // only zeroes it on idle/running/passphrase_required.
  const frames = sequence("registration_retry_loop");
  const cycle = frames.slice(4, 8).map((f) => f.state);
  assert.deepEqual(cycle, ["initializing", "binding", "registering", "error_transient"]);

  const counts = frames.map((f) => f.retry_count);
  for (let i = 1; i < counts.length; i++) {
    assert.ok(counts[i] >= counts[i - 1], "retry_count went backwards mid-loop");
  }
  assert.ok(Math.max(...counts) >= 6, "retry_count should climb past the >= 3 gate");
});

test("BUG B: the main status line does not oscillate during a registration retry loop", async () => {
  const gui = await bootGui();
  try {
    const rows = await renderSequence(gui, sequence("registration_retry_loop"));
    const loop = midLoop(rows);
    assert.ok(loop.length >= 15, "expected a long loop to sample");

    const texts = distinct(loop.map((r) => r.text));
    assert.equal(
      texts.length,
      1,
      "#status-text cycled through " + JSON.stringify(texts) +
        " while the node sat in one retry loop",
    );

    // And the one phrase must be honest about what the node is doing.
    assert.match(texts[0], /retry/i);

    // The status dot must not flip between the "starting" and "reconnecting"
    // styles either — that is the same flicker, in colour.
    const dots = distinct(loop.map((r) => r.dot));
    assert.equal(dots.length, 1, "status dot class churned: " + JSON.stringify(dots));
  } finally {
    gui.close();
  }
});

test("BUG B: the staking-status label does not flip between Initializing and Coordination offline", async () => {
  const gui = await bootGui();
  try {
    const rows = await renderSequence(gui, sequence("registration_retry_loop"));
    const labels = distinct(midLoop(rows).map((r) => r.staking));
    // This is QA's literal report: "status kept switching between
    // Initializing and Coordination offline instead of settling".
    assert.deepEqual(
      labels,
      ["Coordination offline"],
      "#staking-status cycled through " + JSON.stringify(labels),
    );
  } finally {
    gui.close();
  }
});

test("BUG B: both labels settle for a retry loop on a NON-connectivity error too", async () => {
  const gui = await bootGui();
  try {
    // The second half of this fixture is a rate_limited loop, which is not in
    // COORD_OFFLINE_CODES. The two branches that render "retrying, no staking
    // status yet" must still agree with each other.
    const rows = await renderSequence(gui, sequence("code_change_after_retries"));
    const rateLimited = rows.slice(rows.findIndex((r) => r.error_code === "rate_limited"));
    assert.ok(rateLimited.length >= 5, "expected a rate_limited tail to sample");

    assert.deepEqual(distinct(rateLimited.map((r) => r.text)), ["Retrying..."]);
    assert.deepEqual(distinct(rateLimited.map((r) => r.staking)), ["Reconnecting…"]);
  } finally {
    gui.close();
  }
});

test("BUG B: the retry countdown information survives (a ticking countdown is not a flicker)", async () => {
  const gui = await bootGui();
  try {
    const rows = midLoop(await renderSequence(gui, sequence("registration_retry_loop")));
    // Every mid-loop frame still tells the operator which attempt this is.
    for (const row of rows) {
      assert.match(
        row.detail,
        /Attempt \d+/,
        "lost the attempt counter on a " + row.state + " frame: " + row.detail,
      );
    }
    // The attempt number climbs, so the detail line is live rather than frozen.
    const attempts = rows.map((r) => Number(/Attempt (\d+)/.exec(r.detail)[1]));
    assert.ok(
      attempts[attempts.length - 1] > attempts[0],
      "attempt counter never advanced: " + JSON.stringify(attempts),
    );
    // And a real countdown is still rendered while the node waits out the backoff.
    assert.ok(
      rows.some((r) => r.state === "error_transient" && /\d+s/.test(r.detail)),
      "no countdown rendered on any error_transient frame",
    );
  } finally {
    gui.close();
  }
});

test("BUG B: opening the GUI part-way through an outage still gets a stable line", async () => {
  const gui = await bootGui();
  try {
    // Start rendering from an initializing poll deep inside the loop, i.e. the
    // GUI never saw the error_transient frame that carries the error code.
    const frames = sequence("registration_retry_loop");
    const start = frames.findIndex((f) => f.state === "initializing" && f.retry_count >= 3);
    assert.ok(start > 0, "fixture must contain a mid-loop initializing frame");

    const rows = await renderSequence(gui, frames.slice(start));
    assert.deepEqual(distinct(rows.map((r) => r.text)), ["Retrying..."]);
    // The specific staking label can only sharpen once (unknown -> Coordination
    // offline) as the first error_transient poll arrives; it must not then flip
    // back to "Initializing…".
    const labels = distinct(rows.map((r) => r.staking));
    assert.deepEqual(labels, ["Reconnecting…", "Coordination offline"]);
  } finally {
    gui.close();
  }
});

test("BUG B: a genuine cold start still shows real per-phase progress", async () => {
  const gui = await bootGui();
  try {
    const frames = sequence("registration_retry_loop");
    // The first three frames are the very first attempt: retry_count is 0, so
    // there is no retry loop to stabilise and the phases must stay visible.
    const rows = await renderSequence(gui, frames.slice(0, 3));
    assert.deepEqual(rows.map((r) => r.state), ["initializing", "binding", "registering"]);
    assert.deepEqual(
      rows.map((r) => r.text),
      ["Initializing...", "Starting server...", "Registering..."],
      "the fix must not flatten a normal first-time startup",
    );
  } finally {
    gui.close();
  }
});
