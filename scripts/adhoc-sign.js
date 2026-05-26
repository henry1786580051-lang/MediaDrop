// Ad-hoc codesign the app bundle after electron-builder assembles it.
// This prevents macOS Gatekeeper from showing "app is damaged" for unsigned apps.
exports.default = async function (context) {
  const { electronPlatformName, appOutDir } = context;
  if (electronPlatformName !== 'darwin') return;

  const { execSync } = require('child_process');
  const path = require('path');
  const appName = context.packager.appInfo.productFilename;
  const appPath = path.join(appOutDir, `${appName}.app`);

  console.log(`[sign] Ad-hoc signing ${appPath}`);
  try {
    execSync(`codesign --force --deep --sign - "${appPath}" 2>&1`, { stdio: 'inherit' });
    console.log('[sign] Ad-hoc signing complete');
  } catch (err) {
    console.warn('[sign] Ad-hoc signing failed (non-fatal):', err.message);
  }
};
