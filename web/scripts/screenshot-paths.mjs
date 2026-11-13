import { mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

// Resolve the default against this module, independent of the caller's cwd.
export async function screenshotDirectory(override) {
  const directory = resolve(override || process.env.WEB_SCREENSHOT_DIR || fileURLToPath(new URL("../../docs/screenshots/", import.meta.url)));
  await mkdir(directory, { recursive: true });
  return directory;
}

const directory = await screenshotDirectory();
export function screenshotPath(filename) {
  return resolve(directory, filename);
}
