// The vault backend: the Bitwarden CLI (`bw`) against Potter's own Vaultwarden account, which sees only the
// collection the owner shared with it. Values are held in memory for the call that needs them, never written.
import { execFile } from "node:child_process";

const run = (bin, args, env, timeoutMs = 20000) => new Promise((res) => {
  execFile(bin, args, { env, timeout: timeoutMs, maxBuffer: 8 * 1024 * 1024 }, (err, stdout, stderr) =>
    res({ ok: !err, out: String(stdout ?? ""), err: err ? String(stderr || err.message).slice(0, 200) : "" }));
});

/** env: BW_CLIENTID, BW_CLIENTSECRET, BW_PASSWORD, BW_SERVER, BITWARDENCLI_APPDATA_DIR (root-only). */
export function createBackend({ bin = "bw", env, runImpl = run, now = () => Date.now(), ttlMs = 30 * 60 * 1000 }) {
  let session = null, sessionAt = 0, cache = null, cacheAt = 0;
  const base = { ...process.env, ...env, BW_NOINTERACTION: "true" };
  async function unlock() {
    if (session && now() - sessionAt < ttlMs) return { ok: true };
    const st = await runImpl(bin, ["status"], base);
    let status = "unauthenticated";
    try { status = JSON.parse(st.out).status ?? status; } catch { /* keep */ }
    if (status === "unauthenticated") {
      if (env.BW_SERVER) await runImpl(bin, ["config", "server", env.BW_SERVER], base);
      const li = await runImpl(bin, ["login", "--apikey"], base);
      if (!li.ok && !/already logged in/i.test(li.err)) return { ok: false, reason: "login failed" };
    }
    const u = await runImpl(bin, ["unlock", "--passwordenv", "BW_PASSWORD", "--raw"], base);
    if (!u.ok || !u.out.trim()) return { ok: false, reason: "unlock failed" };
    session = u.out.trim(); sessionAt = now();
    return { ok: true };
  }
  async function items(fresh = false) {
    const ok = await unlock();
    if (!ok.ok) return ok;
    if (cache && !fresh && now() - cacheAt < 5 * 60 * 1000) return { ok: true, items: cache };
    await runImpl(bin, ["sync", "--session", session], base);
    const r = await runImpl(bin, ["list", "items", "--session", session], base);
    if (!r.ok) { session = null; return { ok: false, reason: "list failed" }; }
    try { cache = JSON.parse(r.out); cacheAt = now(); return { ok: true, items: cache }; } catch { return { ok: false, reason: "unreadable list" }; }
  }
  async function totp(id) {
    const r = await runImpl(bin, ["get", "totp", id, "--session", session], base);
    return r.ok ? r.out.trim() : null;
  }
  return { unlock, items, totp, lock: () => { session = null; cache = null; } };
}

// Bitwarden identity items (type 4): plain field names -> Bitwarden's keys.
export const IDENTITY = {
  title: "title", first_name: "firstName", middle_name: "middleName", last_name: "lastName", address1: "address1",
  address2: "address2", address3: "address3", city: "city", state: "state", postal_code: "postalCode", country: "country",
  company: "company", email: "email", phone: "phone", ssn: "ssn", username: "username",
  passport_number: "passportNumber", license_number: "licenseNumber",
};
// Fields that are asked for one use at a time, even inside an item the owner already approved.
export const SENSITIVE = /^(ssn|social_security(_number)?|passport(_number|_no)?|license_number|drivers?_license(_number)?|dob|date_of_birth|birth_?date|tax_id|itin|national_id)$/;
export const isSensitive = (field) => SENSITIVE.test(String(field ?? ""));

/** What may be said about an item: name, kind, which fields exist, never a value. */
export function describe(it) {
  const kind = it.type === 3 ? "card" : it.type === 1 ? "login" : it.type === 2 ? "note" : it.type === 4 ? "identity" : "other";
  const fields = [];
  if (it.login?.username) fields.push("username");
  if (it.login?.password) fields.push("password");
  if (it.login?.totp) fields.push("totp");
  if (it.login?.uris?.length) fields.push("uri");
  if (it.card?.number) fields.push("number", "expiry", "exp_month", "exp_year", "code", "cardholder");
  if (it.identity) {
    for (const [k, bk] of Object.entries(IDENTITY)) if (it.identity[bk]) fields.push(k);
    if (it.identity.firstName || it.identity.lastName) fields.push("full_name");
  }
  for (const f of it.fields ?? []) if (f?.name && /^[a-z_]{2,30}$/.test(f.name)) fields.push(f.name);
  const auto = /\bpotter:auto\b/i.test(it.notes ?? "") && kind === "login";
  return { name: it.name, kind, fields: [...new Set(fields)], sensitive: [...new Set(fields)].filter(isSensitive), site: it.login?.uris?.[0]?.uri ? new URL(it.login.uris[0].uri, "https://x").hostname : null, auto };
}

export async function fieldValue(backend, it, field) {
  const c = it.card ?? {};
  if (it.identity) {
    const id = it.identity;
    if (field === "full_name") return [id.firstName, id.middleName, id.lastName].filter(Boolean).join(" ") || null;
    if (IDENTITY[field] && id[IDENTITY[field]]) return String(id[IDENTITY[field]]);
  }
  switch (field) {
    case "username": return it.login?.username ?? null;
    case "password": return it.login?.password ?? null;
    case "uri": return it.login?.uris?.[0]?.uri ?? null;
    case "totp": return it.login?.totp ? backend.totp(it.id) : null;
    case "number": return c.number ?? null;
    case "code": return c.code ?? null;
    case "cardholder": return c.cardholderName ?? null;
    case "exp_month": return c.expMonth ? String(c.expMonth).padStart(2, "0") : null;
    case "exp_year": return c.expYear ? String(c.expYear) : null;
    case "expiry": return c.expMonth && c.expYear ? `${String(c.expMonth).padStart(2, "0")}/${String(c.expYear).slice(-2)}` : null;
    default: return (it.fields ?? []).find((f) => f.name === field)?.value ?? null;
  }
}
