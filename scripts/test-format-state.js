const fs = require("fs");
const path = require("path");

const template = fs.readFileSync(
  path.join(__dirname, "..", "app", "templates", "index.html"),
  "utf8"
);
const scriptMatch = template.match(/<script>([\s\S]*?)<\/script>/);
if (!scriptMatch) throw new Error("Inline application script was not found");

const source = scriptMatch[1];
const start = source.indexOf("function buildDownloadRequest");
const end = source.indexOf("function parseUrls", start);
if (start < 0 || end < 0) throw new Error("Download state helpers were not found");

const helpers = new Function(
  `${source.slice(start, end)}; return { buildDownloadRequest, downloadRequestKey, completedDownloadFor };`
)();

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

const card = {
  format: "video",
  selectedFormatId: "337",
  videoRangeMode: "hdr",
  completedDownloads: {},
};
const videoRequest = helpers.buildDownloadRequest(card);
card.completedDownloads[helpers.downloadRequestKey(videoRequest)] = { filename: "video.mp4" };

assert(helpers.completedDownloadFor(card)?.filename === "video.mp4", "Completed MP4 was not recognized");
assert(helpers.completedDownloadFor(card, "image") === null, "JPG incorrectly inherited MP4 completion");
assert(helpers.completedDownloadFor(card, "audio") === null, "MP3 incorrectly inherited MP4 completion");

const imageRequest = helpers.buildDownloadRequest(card, "image");
card.completedDownloads[helpers.downloadRequestKey(imageRequest)] = { filename: "cover.jpg" };
assert(helpers.completedDownloadFor(card, "image")?.filename === "cover.jpg", "Completed JPG was not recognized");
assert(helpers.completedDownloadFor(card, "video")?.filename === "video.mp4", "MP4 history was lost after JPG completion");

card.selectedFormatId = "315";
assert(helpers.completedDownloadFor(card, "video") === null, "A different video quality inherited completion");

console.log("format completion state OK");
