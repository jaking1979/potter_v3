// Where the graph file lives. Pure: the caller supplies config, environment and the state dir.
import { isAbsolute, join } from "node:path";

export const DB_FILE = join("potter-kg", "potter-kg.sqlite");

/**
 * Order: explicit path (plugin config `dbPath` or --db), POTTER_KG_DB, then the OpenClaw state dir
 * (given by the host, or OPENCLAW_STATE_DIR). No home directory is assumed here: if none of these is
 * known the answer is null and the caller must refuse to run.
 */
export function resolveDbPath({ explicit, env = {}, stateDir } = {}) {
  const pick = [explicit, env.POTTER_KG_DB].find((p) => typeof p === "string" && p.trim());
  if (pick) {
    if (pick !== ":memory:" && !isAbsolute(pick)) throw new Error(`potter-kg: dbPath must be absolute, got ${JSON.stringify(pick)}`);
    return pick;
  }
  const dir = [stateDir, env.OPENCLAW_STATE_DIR].find((p) => typeof p === "string" && p.trim());
  if (!dir) return null;
  if (!isAbsolute(dir)) throw new Error("potter-kg: the OpenClaw state dir must be an absolute path");
  return join(dir, DB_FILE);
}
