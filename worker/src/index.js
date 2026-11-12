// The only server-side piece of the comparison page: GitHub's OAuth token endpoint needs the
// app's client secret and does not allow browser (CORS) calls, so the page sends the one-time
// `code` (or a refresh token) here and gets the user's access token back. Nothing is stored.
// The token acts as the signed-in user, limited to what the GitHub App may do (issues of one repo).

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin") || "";
    const allowed = env.ALLOWED_ORIGINS.split(",").map(s => s.trim());
    const cors = {
      "Access-Control-Allow-Origin": allowed.includes(origin) ? origin : allowed[0],
      "Access-Control-Allow-Methods": "POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
      "Vary": "Origin",
    };
    const json = (obj, status = 200) =>
      new Response(JSON.stringify(obj), { status, headers: { ...cors, "Content-Type": "application/json" } });

    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
    const url = new URL(request.url);
    if (url.pathname !== "/token" || request.method !== "POST") return json({ error: "not_found" }, 404);
    if (!allowed.includes(origin)) return json({ error: "origin_not_allowed" }, 403);
    if (!env.GH_CLIENT_SECRET) return json({ error: "server_not_configured" }, 500);

    let body;
    try { body = await request.json(); } catch { return json({ error: "bad_json" }, 400); }
    const params = { client_id: env.GH_CLIENT_ID, client_secret: env.GH_CLIENT_SECRET };
    if (typeof body.code === "string" && body.code.length < 100) params.code = body.code;
    else if (typeof body.refresh_token === "string" && body.refresh_token.length < 200) {
      params.grant_type = "refresh_token";
      params.refresh_token = body.refresh_token;
    } else return json({ error: "missing_code" }, 400);

    const r = await fetch("https://github.com/login/oauth/access_token", {
      method: "POST",
      headers: { "Accept": "application/json", "Content-Type": "application/json", "User-Agent": "hl7at-ig-comparison-auth" },
      body: JSON.stringify(params),
    });
    const t = await r.json().catch(() => ({ error: "bad_github_response" }));
    if (!r.ok || t.error || !t.access_token) {
      return json({ error: t.error || "exchange_failed", error_description: t.error_description || "" }, 400);
    }
    // pass through only what the page needs
    return json({
      access_token: t.access_token,
      expires_in: t.expires_in,
      refresh_token: t.refresh_token,
      refresh_token_expires_in: t.refresh_token_expires_in,
    });
  },
};
