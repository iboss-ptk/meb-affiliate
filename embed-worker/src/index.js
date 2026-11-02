// Embeds book texts with bge-m3 on Workers AI, for mebreco's semantic index.
// POST /embed  {"texts": ["...", ...]}  ->  {"model": "...", "vectors": [[...], ...]}
// Requires "Authorization: Bearer <EMBED_TOKEN>" so others can't spend the quota.

const MODEL = "@cf/baai/bge-m3";
const MAX_TEXTS = 100;

function authorized(request, env) {
  const got = request.headers.get("Authorization") || "";
  const want = `Bearer ${env.EMBED_TOKEN || ""}`;
  if (!env.EMBED_TOKEN || got.length !== want.length) return false;
  let diff = 0; // constant-time compare
  for (let i = 0; i < want.length; i++) diff |= got.charCodeAt(i) ^ want.charCodeAt(i);
  return diff === 0;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== "/embed") return new Response("not found", { status: 404 });
    if (request.method !== "POST") return new Response("POST only", { status: 405 });
    if (!authorized(request, env)) return new Response("unauthorized", { status: 401 });

    let texts;
    try {
      ({ texts } = await request.json());
    } catch {
      return new Response("invalid JSON", { status: 400 });
    }
    if (!Array.isArray(texts) || !texts.length || texts.length > MAX_TEXTS || !texts.every((t) => typeof t === "string" && t)) {
      return new Response(`texts must be 1..${MAX_TEXTS} non-empty strings`, { status: 400 });
    }
    try {
      const out = await env.AI.run(MODEL, { text: texts });
      return Response.json({ model: MODEL, vectors: out.data });
    } catch (e) {
      return Response.json({ error: String(e?.message || e) }, { status: 502 });
    }
  },
};
