// Thin client for the typed-decision endpoint v2 used (OpenRouter's Decisions API, model family "Jev").
// One request: shared `state` plus a map of questions (choice | score | noul). Everything that touches
// the outside world (fetch, the key, the clock) is passed in, so tests need no network.
// It never throws and never puts the key, the state or a response body into an error.

export const DEFAULT_ENDPOINT = "https://openrouter.ai/api/alpha/decisions";

/**
 * @param deps.apiKey     bearer key for the endpoint (from the environment; never from a file)
 * @param deps.fetchImpl  fetch-compatible function
 * @param deps.endpoint   override for tests or a local decision server
 * @param deps.now        () => epoch ms
 */
export function createDecisionClient({ apiKey, fetchImpl, endpoint = DEFAULT_ENDPOINT, now = Date.now } = {}) {
  return {
    /**
     * @returns {Promise<{ ok: true, answers, model, costUsd, inputTokens, outputTokens, latencyMs }
     *                 | { ok: false, kind: "no-key"|"no-fetch"|"timeout"|"network"|"http"|"parse", status?, latencyMs }>}
     */
    async ask({ model, state, questions, timeoutMs = 10000 }) {
      if (!apiKey) return { ok: false, kind: "no-key", latencyMs: 0 };
      if (typeof fetchImpl !== "function") return { ok: false, kind: "no-fetch", latencyMs: 0 };
      const started = now();
      const elapsed = () => Math.max(0, now() - started);
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      try {
        let res;
        try {
          res = await fetchImpl(endpoint, {
            method: "POST",
            headers: { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json" },
            body: JSON.stringify({ model, state, questions }),
            signal: controller.signal,
          });
        } catch (e) {
          return { ok: false, kind: controller.signal.aborted || e?.name === "AbortError" ? "timeout" : "network", latencyMs: elapsed() };
        }
        if (!res.ok) return { ok: false, kind: "http", status: res.status, latencyMs: elapsed() };
        let data;
        try {
          data = await res.json();
        } catch {
          return { ok: false, kind: controller.signal.aborted ? "timeout" : "parse", latencyMs: elapsed() };
        }
        if (!data || typeof data.answers !== "object" || data.answers === null) return { ok: false, kind: "parse", latencyMs: elapsed() };
        const n = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
        return {
          ok: true,
          answers: data.answers,
          model: typeof data.model === "string" ? data.model : model,
          costUsd: n(data.usage?.cost),
          inputTokens: n(data.usage?.input_tokens),
          outputTokens: n(data.usage?.output_tokens),
          latencyMs: elapsed(),
        };
      } finally {
        clearTimeout(timer);
      }
    },
  };
}
