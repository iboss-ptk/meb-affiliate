// Embeds book texts with bge-m3 on Workers AI, for mebreco's semantic index.
// POST /embed  {"texts": ["...", ...]}  ->  {"model": "...", "vectors": [[...], ...]}
// PUT/GET /data/<name>  ->  store/fetch mebreco's DB + index in R2 (binding DATA)
// Requires "Authorization: Bearer <EMBED_TOKEN>" so others can't spend the quota or read the data.

const MODEL = "@cf/baai/bge-m3";
const MAX_TEXTS = 100;
const MAX_OBJECT = 95 * 1024 * 1024; // request bodies are capped at 100 MB on the free plan
const NAME = /^[a-z0-9._-]{1,64}$/;

async function data(request, env, name) {
  if (!NAME.test(name)) return new Response("bad name", { status: 400 });
  if (request.method === "PUT") {
    const size = Number(request.headers.get("Content-Length") || 0);
    if (!size) return new Response("Content-Length required", { status: 411 });
    if (size > MAX_OBJECT) return new Response("too large", { status: 413 });
    const obj = await env.DATA.put(name, request.body);
    return Response.json({ name, size: obj.size });
  }
  if (request.method === "GET") {
    const obj = await env.DATA.get(name);
    if (!obj) return new Response("not found", { status: 404 });
    return new Response(obj.body, { headers: { "Content-Type": "application/octet-stream", "Content-Length": String(obj.size) } });
  }
  return new Response("PUT or GET only", { status: 405 });
}

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
    if (url.pathname.startsWith("/data/")) {
      if (!authorized(request, env)) return new Response("unauthorized", { status: 401 });
      return data(request, env, url.pathname.slice("/data/".length));
    }
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
