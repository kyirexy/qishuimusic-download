"use strict";
// Verify the actual native addon and Msgpackr codec using synthetic data only.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const runtime = path.resolve(process.argv[2]);
const { open, version } = require(path.join(runtime, "node_modules", "lmdb"));
assert.equal(process.platform, "win32");
assert.equal(process.arch, "x64");
assert.equal(version.patch, 90, "The cache reader requires the LMDB-JS v2 data format");
const tempRoot = fs.realpathSync(os.tmpdir());
const directory = fs.mkdtempSync(path.join(tempRoot, "soda-build-check-"));
(async () => {
  let db;
  try {
    const file = path.join(directory, "synthetic.db");
    db = open({ path: file });
    db.putSync("synthetic_F_high", { full: true, nested: { codec: "aac" }, optional: undefined });
    await db.close();
    db = open({ path: file, readOnly: true, create: false, noSubdir: true });
    assert.equal(db.get("synthetic_F_high").nested.codec, "aac");
    await db.close();
    db = null;
    process.stdout.write("Cache runtime native/codec smoke test passed.\n");
  } finally {
    if (db) await db.close();
    if (path.dirname(fs.realpathSync(directory)) !== tempRoot || !path.basename(directory).startsWith("soda-build-check-")) {
      throw new Error("Temporary cleanup path is outside the expected directory");
    }
    fs.rmSync(directory, { recursive: true });
  }
})().catch(error => { process.stderr.write(error.message + "\n"); process.exitCode = 1; });
