// Ad-hoc codesign the app bundle after electron-builder assembles it.
// A valid ad-hoc signature still requires the documented Gatekeeper override,
// but avoids the misleading "app is damaged" error caused by invalid signatures.
const { execFileSync } = require("child_process");
const path = require("path");

function runCommand(executable, args) {
  execFileSync(executable, args, { stdio: "inherit" });
}

function removeProhibitedAttribute(appPath, attribute, commandRunner) {
  try {
    commandRunner("/usr/bin/xattr", ["-dr", attribute, appPath]);
  } catch (error) {
    // xattr exits with status 1 when the attribute is absent. Any attributes
    // left behind are also caught by the strict codesign verification below.
    if (!error || error.status !== 1) throw error;
  }
}

function adHocSignApp(appPath, commandRunner = runCommand) {
  console.log(`[sign] Clearing extended attributes from ${appPath}`);
  commandRunner("/usr/bin/xattr", ["-cr", appPath]);
  removeProhibitedAttribute(appPath, "com.apple.FinderInfo", commandRunner);
  removeProhibitedAttribute(appPath, "com.apple.ResourceFork", commandRunner);

  console.log(`[sign] Ad-hoc signing ${appPath}`);
  commandRunner("/usr/bin/codesign", ["--force", "--deep", "--sign", "-", appPath]);
  commandRunner("/usr/bin/codesign", ["--verify", "--deep", "--strict", "--verbose=2", appPath]);
  console.log("[sign] Ad-hoc signing and verification complete");
}

exports.adHocSignApp = adHocSignApp;
exports.default = async function (context) {
  const { electronPlatformName, appOutDir } = context;
  if (electronPlatformName !== "darwin") return;

  const appName = context.packager.appInfo.productFilename;
  adHocSignApp(path.join(appOutDir, `${appName}.app`));
};
