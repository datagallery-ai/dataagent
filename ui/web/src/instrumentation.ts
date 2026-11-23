/** Next.js loads this for independent `next start` as well as managed launches. */
export async function register() {
  if (process.env.NEXT_RUNTIME !== "nodejs") return;
  const { installConsoleLogging } = await import("../../shared/logging.mjs");
  installConsoleLogging({ file: "web", component: "web" });
}
