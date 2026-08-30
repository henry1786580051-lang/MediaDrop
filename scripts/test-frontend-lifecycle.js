const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "app", "static", "app.js"), "utf8");
function section(start, end) {
  const from = source.indexOf(start);
  const to = source.indexOf(end, from);
  assert(from >= 0 && to > from, `Missing application functions: ${start}`);
  return source.slice(from, to);
}
const code = [
  section("function buildDownloadRequest", "function fmtDur"),
  section("function stopCardPolling", "function renderCard"),
  section("async function togglePause", "function renderDownloadAll"),
  section("function pickFormat", "const taskStatusLabels"),
].join("\n");

function response(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

function createHarness(handler) {
  const timers = new Map();
  const requests = [];
  const renders = [];
  let timerId = 0;
  const elements = { urls: { value: "https://example.com/new" }, goBtn: {}, cards: {} };
  const context = {
    AbortController, Promise, Set, console,
    cardData: [{ url: "https://example.com/old", title: "Old", format: "video", status: "ready" }],
    parseController: null, notifiedJobs: new Set(), window: {},
    document: { getElementById: id => elements[id], querySelector: () => null },
    currentCardFormat: idx => context.cardData[idx].format || "video",
    renderCard: idx => renders.push({ idx, title: context.cardData[idx]?.title, status: context.cardData[idx]?.status }),
    renderDownloadAll: () => {}, friendlyError: value => String(value),
    setTimeout: callback => { timers.set(++timerId, callback); return timerId; },
    clearTimeout: id => timers.delete(id),
    fetch: async (url, options) => {
      requests.push({ url, options });
      if (url === "/api/info") return response({ title: "New", formats: [] });
      return handler(url, options);
    },
  };
  vm.createContext(context);
  vm.runInContext(code, context);
  return {
    context, timers, requests, renders,
    async tick() {
      const next = timers.entries().next().value;
      assert(next, "No poll was scheduled");
      timers.delete(next[0]);
      await next[1]();
    },
  };
}

async function test(name, run) {
  await run();
  console.log(`OK ${name}`);
}

(async () => {
  await test("late download response cannot attach to a new card", async () => {
    const pending = deferred();
    const h = createHarness(() => pending.promise);
    const downloading = h.context.dlCard(0);
    await h.context.go();
    const renderCount = h.renders.length;
    pending.resolve(response({ job_id: "old-job" }));
    await downloading;
    assert.equal(h.context.cardData[0].title, "New");
    assert.equal(h.context.cardData[0].status, "ready");
    assert.equal(h.context.cardData[0].jobId, undefined);
    assert.equal(h.timers.size, 0);
    assert.equal(h.renders.length, renderCount);
    assert(!h.requests.some(request => request.url.includes("undefined")));
  });

  await test("a pending old poll cannot update a replacement card", async () => {
    const pending = deferred();
    const h = createHarness(url => url === "/api/download" ? response({ job_id: "old-job" }) : pending.promise);
    await h.context.dlCard(0);
    const polling = h.tick();
    await h.context.go();
    pending.resolve(response({ status: "done", filename: "old.mp4" }));
    await polling;
    assert.equal(h.context.cardData[0].status, "ready");
    assert.equal(Object.keys(h.context.cardData[0].completedDownloads).length, 0);
    assert.equal(h.timers.size, 0);
  });

  await test("missing jobs stop polling instead of showing perpetual progress", async () => {
    const h = createHarness(url => url === "/api/download"
      ? response({ job_id: "missing" }) : response({ error: "Job not found" }, 404));
    await h.context.dlCard(0);
    await h.tick();
    assert.equal(h.context.cardData[0].status, "error");
    assert.equal(h.timers.size, 0);
  });

  await test("cancel then retry ignores completion from the previous attempt", async () => {
    const oldPoll = deferred();
    let downloads = 0;
    const h = createHarness(url => {
      if (url === "/api/download") return response({ job_id: ++downloads === 1 ? "old-job" : "new-job" });
      if (url.startsWith("/api/cancel/")) return response({ status: "cancelled" });
      return oldPoll.promise;
    });
    await h.context.dlCard(0);
    const polling = h.tick();
    await h.context.cancelDl(0);
    await h.context.dlCard(0);
    oldPoll.resolve(response({ status: "done", filename: "old.mp4" }));
    await polling;
    assert.equal(h.context.cardData[0].jobId, "new-job");
    assert.equal(h.context.cardData[0].status, "downloading");
    assert.equal(h.context.cardData[0].filename, undefined);
    assert.equal(h.timers.size, 1);
  });

  await test("cancel before a job ID arrives cancels the eventual job", async () => {
    const pending = deferred();
    const h = createHarness(url => url === "/api/download" ? pending.promise : response({ status: "cancelled" }));
    const downloading = h.context.dlCard(0);
    await h.context.cancelDl(0);
    assert.equal(h.context.cardData[0].status, "cancelling");
    pending.resolve(response({ job_id: "late-job" }));
    await downloading;
    assert(h.requests.some(request => request.url === "/api/cancel/late-job"));
    assert.equal(h.context.cardData[0].status, "cancelled");
    assert.equal(h.timers.size, 0);
  });

  await test("failed cancellation keeps the task observable", async () => {
    const h = createHarness(url => {
      if (url === "/api/download") return response({ job_id: "active" });
      if (url.startsWith("/api/cancel/")) return response({ error: "Cancel failed" }, 500);
      return response({ status: "paused", progress: {} });
    });
    await h.context.dlCard(0);
    await h.context.cancelDl(0);
    assert.equal(h.context.cardData[0].status, "downloading");
    assert.equal(h.context.cardData[0].cancelRequested, false);
    assert.equal(h.context.cardData[0].actionError, "Cancel failed");
    assert.equal(h.timers.size, 1);
    await h.tick();
    assert.equal(h.context.cardData[0].status, "paused");
  });

  await test("WebM clears unsupported options without changing MP3 behavior", async () => {
    const h = createHarness(() => response({}));
    const card = h.context.cardData[0];
    card.options = { container: "mp4", embed_thumbnail: true };
    card.preset = "custom";
    card.selectedFormatId = "avc";
    card.formats = [{ id: "avc", vcodec: "avc1.640028", acodec: "none" }];
    h.context.setOption(0, "container", "webm");
    assert.equal(card.options.embed_thumbnail, false);
    assert.equal(card.selectedFormatId, null);
    assert.equal(card.preset, "recommended");
    assert.equal(h.renders.length, 1);
    h.context.setPreset(0, "compatible");
    assert.equal(card.preset, "recommended");
    card.options.embed_thumbnail = true;
    assert.equal(h.context.buildDownloadRequest(card).options.embed_thumbnail, false);
    assert.equal(h.context.buildDownloadRequest(card, "audio").options.embed_thumbnail, true);
  });

  await test("advanced option changes refresh completion state", async () => {
    const h = createHarness(() => response({}));
    const card = h.context.cardData[0];
    card.status = "done";
    const key = h.context.downloadRequestKey(h.context.buildDownloadRequest(card));
    card.completedDownloads = { [key]: { filename: "old.mp4" } };
    h.context.setOption(0, "container", "mkv");
    assert.equal(h.context.completedDownloadFor(card), null);
    assert.equal(h.renders.length, 1);
  });

  await test("download-all stays with its original parse batch", async () => {
    const pending = deferred();
    const h = createHarness(() => pending.promise);
    h.context.cardData.push({ url: "https://example.com/second", status: "ready", format: "video" });
    const downloading = h.context.dlAll();
    await h.context.go();
    pending.resolve(response({ job_id: "old-job" }));
    await downloading;
    assert.equal(h.requests.filter(request => request.url === "/api/download").length, 1);
    assert.equal(h.context.cardData[0].status, "ready");
  });

  await test("late request errors cannot replace the new card state", async () => {
    const pending = deferred();
    const h = createHarness(() => pending.promise);
    const downloading = h.context.dlCard(0);
    await h.context.go();
    pending.resolve(response({ error: "Old request failed" }, 500));
    await downloading;
    assert.equal(h.context.cardData[0].status, "ready");
    assert.equal(h.context.cardData[0].error, undefined);
  });
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
