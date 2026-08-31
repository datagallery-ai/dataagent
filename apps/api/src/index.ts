import { config as loadDotenv } from "dotenv";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { loadApiConfig } from "./config.js";
import { createServer } from "./server.js";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
loadDotenv({ path: resolve(repoRoot, ".env") });

const config = loadApiConfig();
const server = await createServer();

try {
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(config.port, config.host, () => {
      server.off("error", reject);
      resolve();
    });
  });
  console.log(`Agent runtime server listening at http://${config.host}:${config.port}`);
} catch (error) {
  console.error(error);
  process.exit(1);
}
