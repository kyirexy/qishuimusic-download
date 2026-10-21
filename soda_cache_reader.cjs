"use strict";

// Private IPC helper. stdout is consumed only by soda_cache.py. It never makes
// network requests, reads cookies, or changes an application database.
const fs = require("node:fs");
const path = require("node:path");

const MAX_DATABASE_BYTES = 512 * 1024 * 1024;
const MAX_ENTRIES = 25000;
const MAX_MATCHES = 64;

function fail(code) {
  const error = new Error(code);
  error.safeCode = code;
  throw error;
}

function object(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function pickTrack(value) {
  value = object(value);
  return {
    id: value.id,
    vid: value.vid,
    type: value.type,
    name: value.name,
    duration: value.duration,
    duration_ms: value.duration_ms,
    artists: value.artists,
    album: value.album,
  };
}

function limitedEntry(key, value) {
  const info = object(value.info);
  const detail = object(info.mediaDetail);
  let model = detail.video_model;
  if (typeof model === "string") {
    try { model = JSON.parse(model); } catch { model = null; }
  }
  model = object(model);
  let sources = model.video_list;
  if (sources && !Array.isArray(sources) && typeof sources === "object") {
    sources = Object.values(sources);
  }
  if (!Array.isArray(sources)) sources = [];
  const urls = Array.isArray(info.urls) ? info.urls.filter(url => typeof url === "string") : [];
  const sourceCandidates = sources
    .filter(source => source && typeof source === "object")
    .filter(source => object(source.video_meta).quality === info.quality)
    .filter(source => urls.some(url => url === source.main_url || url === source.backup_url))
    .map(source => ({
      video_meta: source.video_meta,
      codec_type: source.codec_type,
      spade: object(source.encrypt_info).spade_a || "",
      is_preview: source.is_preview,
      is_trial: source.is_trial,
      is_trial_play: source.is_trial_play,
      preview_only: source.preview_only,
    }));
  const response = object(detail.response);
  return {
    key,
    resource_id: value.resourceId,
    chunk_id: value.chunkId,
    size: value.size,
    cached_at: value.previousAccessTime,
    info: {
      track_id: info.trackId,
      quality: info.quality,
      bitrate: info.bitrate,
      is_preview: info.isPreview,
      spade: info.spade || "",
    },
    detail: {
      present: !!info.mediaDetail,
      video_model_type: detail.video_model_type,
      player_model_type: object(response.track_player).video_model_type,
      status_code: response.status_code,
      playable: pickTrack(detail.playable),
      response_track: pickTrack(response.track),
      model_is_preview: model.is_preview,
      model_is_trial: model.is_trial,
      model_is_trial_play: model.is_trial_play,
      model_preview_only: model.preview_only,
    },
    source_candidates: sourceCandidates,
  };
}

async function main() {
  let text = "";
  for await (const chunk of process.stdin) {
    text += chunk.toString("utf8");
    if (text.length > 16384) fail("input_invalid");
  }
  let input;
  try { input = JSON.parse(text); } catch { fail("input_invalid"); }
  if (!input || !["find", "inspect"].includes(input.action)) fail("input_invalid");
  if (input.action === "find" && !/^\d{10,25}$/.test(input.track_id || "")) fail("input_invalid");
  if (typeof input.cache_dir !== "string" || !path.isAbsolute(input.cache_dir)) fail("input_invalid");
  const databasePath = path.join(input.cache_dir, "entries.db");
  let stat;
  try { stat = fs.statSync(databasePath); } catch { fail("cache_missing"); }
  if (!stat.isFile() || stat.size <= 0) fail("cache_missing");
  if (stat.size > MAX_DATABASE_BYTES) fail("database_too_large");
  const runtime = process.env.SODA_CACHE_RUNTIME;
  if (!runtime || !path.isAbsolute(runtime)) fail("runtime_missing");
  let open;
  try { ({ open } = require(path.join(runtime, "node_modules", "lmdb"))); }
  catch { fail("runtime_missing"); }

  let database;
  try {
    // Normal LMDB reader locks coordinate with the running official client.
    // readOnly prevents DB writes; create:false prevents creating a new DB.
    database = open({
      path: databasePath,
      readOnly: true,
      noSubdir: true,
      create: false,
      compression: false,
    });
    const matches = [];
    const counts = { entries: 0, with_metadata: 0, full_marked: 0, preview_marked: 0 };
    for (const { key, value } of database.getRange()) {
      if (++counts.entries > MAX_ENTRIES) fail("database_too_large");
      if (!value || typeof value !== "object") continue;
      const info = object(value.info);
      if (info.mediaDetail) counts.with_metadata++;
      if (typeof key === "string" && key.includes("_F_") && info.isPreview === false) counts.full_marked++;
      if (info.isPreview === true || (typeof key === "string" && key.includes("_P_"))) counts.preview_marked++;
      if (input.action !== "find" || String(info.trackId) !== input.track_id) continue;
      if (matches.length >= MAX_MATCHES) fail("too_many_matches");
      matches.push(limitedEntry(key, value));
    }
    const result = JSON.stringify({ ok: true, counts, matches });
    if (Buffer.byteLength(result, "utf8") > 4 * 1024 * 1024) fail("output_too_large");
    process.stdout.write(result);
  } finally {
    if (database) await database.close();
  }
}

main().catch(error => {
  // Never print native error dumps, filesystem paths, URLs, or cached data.
  process.stdout.write(JSON.stringify({ ok: false, error: error.safeCode || "database_unreadable" }));
  process.exitCode = 1;
});
