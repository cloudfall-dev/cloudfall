// cloudfall.tech is the primary site. www.cloudfall.tech and the old
// cloudfall.dev hosts answer with a 301 to the same path and query on it;
// any other host (wrangler dev on localhost) is served as is
const PRIMARY_HOST = "cloudfall.tech";
const REDIRECT_HOSTS = new Set([
  "www.cloudfall.tech",
  "cloudfall.dev",
  "www.cloudfall.dev",
]);

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (REDIRECT_HOSTS.has(url.hostname)) {
      url.hostname = PRIMARY_HOST;
      return Response.redirect(url.toString(), 301);
    }
    return env.ASSETS.fetch(request);
  },
};
