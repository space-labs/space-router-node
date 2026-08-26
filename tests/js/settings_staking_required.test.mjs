/**
 * GUI regression test for the QA v1.5.2-test.136 finding reported FAIL on both
 * macOS GUI and Windows GUI, item 2 ("empty staking address rejection").
 *
 * Clearing the staking address in Settings disabled Save with no explanation of
 * any kind: the error span was blanked, the input kept its normal border, and
 * clicking the dead button did nothing. The gate lives in refreshSaveEnabled()
 * but the only writer of the message was the blur handler, so the two could
 * disagree and on the empty value they did.
 *
 * Drives the real initSettings() out of gui/assets/app.js in jsdom.
 *
 * Run with:  npm test        (or: node --test tests/js/)
 */

import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM } from "jsdom";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ASSETS = path.resolve(HERE, "..", "..", "gui", "assets");

const VALID = "0x1234567890abcdef1234567890abcdef12345678";
const REQUIRED_MESSAGE =
  "Staking address is required — Save stays disabled until you enter one.";
const FORMAT_MESSAGE =
  "Invalid address — expected 0x followed by 40 hex characters";

async function bootSettings() {
  const html = fs.readFileSync(path.join(ASSETS, "index.html"), "utf8");
  const appJs = fs.readFileSync(path.join(ASSETS, "app.js"), "utf8");

  const dom = new JSDOM(html, {
    runScripts: "outside-only",
    pretendToBeVisual: true,
    url: "http://localhost/",
  });
  const window = dom.window;

  window.pywebview = {
    api: {
      get_status: async () => ({ state: "idle" }),
      get_build_version: async () => "1.5.2-test.137",
      get_settings: async () => ({ coordination_api_url: "", mtls_enabled: true }),
      get_staking_address: async () => VALID,
      get_collection_address: async () => "",
    },
  };

  window.eval(appJs);

  if (typeof window.initSettings !== "function") {
    throw new Error("app.js did not expose initSettings");
  }
  window.initSettings();

  const doc = window.document;
  const input = doc.querySelector("#settings-staking-input");
  const error = doc.querySelector("#settings-staking-error");
  const save = doc.querySelector("#btn-save-settings");
  if (!input || !error || !save) {
    throw new Error("index.html is missing the settings elements under test");
  }

  function type(value) {
    input.value = value;
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
  }
  function blur() {
    input.dispatchEvent(new window.Event("blur", { bubbles: true }));
  }

  return { window, input, error, save, type, blur, close: () => window.close() };
}

test("clearing the staking address explains why Save is disabled", async () => {
  const gui = await bootSettings();
  try {
    gui.type("");
    assert.equal(
      gui.error.textContent,
      REQUIRED_MESSAGE,
      "an empty staking address must say why Save is dead, not blank the message",
    );
    assert.ok(
      gui.input.classList.contains("invalid"),
      "the empty field must also be marked invalid, not left looking normal",
    );
    assert.equal(gui.save.disabled, true, "Save must stay disabled while empty");
  } finally {
    gui.close();
  }
});

test("the message survives blurring out of the empty field", async () => {
  const gui = await bootSettings();
  try {
    gui.type("");
    gui.blur();
    assert.equal(
      gui.error.textContent,
      REQUIRED_MESSAGE,
      "the blur handler used to blank the message it should be showing",
    );
  } finally {
    gui.close();
  }
});

test("the required message does not stomp the format message", async () => {
  const gui = await bootSettings();
  try {
    gui.type("not-an-address");
    gui.blur();
    assert.equal(gui.error.textContent, FORMAT_MESSAGE);
    assert.equal(gui.save.disabled, true);
  } finally {
    gui.close();
  }
});

test("a bare 40-hex address is still accepted and clears the message", async () => {
  const gui = await bootSettings();
  try {
    gui.type("");
    assert.equal(gui.error.textContent, REQUIRED_MESSAGE);

    gui.type("1234567890abcdef1234567890abcdef12345678");
    gui.blur();
    assert.equal(
      gui.error.textContent,
      "",
      "BUG-06 accepts bare 40-hex; the required message must clear with it",
    );
    assert.equal(
      gui.input.classList.contains("invalid"),
      false,
      "a valid bare-hex address must not stay marked invalid",
    );
  } finally {
    gui.close();
  }
});
