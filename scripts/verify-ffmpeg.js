const fs = require("fs");
const { spawnSync } = require("child_process");

const PE_MACHINES = new Map([
  [0x8664, "x64"],
  [0xaa64, "arm64"],
]);
const MACHO_CPUS = new Map([
  [0x01000007, "x64"],
  [0x0100000c, "arm64"],
]);

function detectExecutableArchitecture(file) {
  const header = fs.readFileSync(file).subarray(0, 4096);

  if (header.length >= 64 && header.toString("ascii", 0, 2) === "MZ") {
    const peOffset = header.readUInt32LE(0x3c);
    if (peOffset + 6 > header.length || header.toString("ascii", peOffset, peOffset + 4) !== "PE\0\0") {
      throw new Error(`Invalid PE executable: ${file}`);
    }
    return PE_MACHINES.get(header.readUInt16LE(peOffset + 4)) || "unknown";
  }

  if (header.length >= 8 && header.readUInt32LE(0) === 0xfeedfacf) {
    return MACHO_CPUS.get(header.readUInt32LE(4)) || "unknown";
  }

  throw new Error(`Unsupported executable format: ${file}`);
}

function runFfmpeg(binary, args) {
  const result = spawnSync(binary, args, {
    encoding: "utf8",
    maxBuffer: 4 * 1024 * 1024,
    windowsHide: true,
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(`${binary} ${args.join(" ")} exited with code ${result.status}`);
  }
  return `${result.stdout || ""}\n${result.stderr || ""}`;
}

function verifyFfmpeg(binary, expectedArch, expectedVersion = "8.1.2") {
  const actualArch = detectExecutableArchitecture(binary);
  if (actualArch !== expectedArch) {
    throw new Error(`FFmpeg architecture mismatch: expected ${expectedArch}, found ${actualArch}`);
  }

  const escapedVersion = expectedVersion.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const versionOutput = runFfmpeg(binary, ["-version"]);
  const versionPattern = new RegExp(`^ffmpeg version n?${escapedVersion}(?:[ .-]|$)`, "m");
  if (!versionPattern.test(versionOutput)) {
    throw new Error(`FFmpeg version mismatch: expected ${expectedVersion}`);
  }

  const encoderOutput = runFfmpeg(binary, ["-hide_banner", "-encoders"]);
  if (!encoderOutput.includes("libmp3lame")) {
    throw new Error("FFmpeg is missing the libmp3lame encoder");
  }

  console.log(`FFmpeg verification passed: ${expectedVersion}, ${actualArch}, libmp3lame`);
}

if (require.main === module) {
  const [binary, expectedArch, expectedVersion] = process.argv.slice(2);
  if (!binary || !expectedArch) {
    throw new Error("Usage: node scripts/verify-ffmpeg.js <binary> <x64|arm64> [version]");
  }
  verifyFfmpeg(binary, expectedArch, expectedVersion);
}

module.exports = { detectExecutableArchitecture, verifyFfmpeg };
