import { createServer } from "node:http";
import { readFile, stat } from "node:fs/promises";
import { dirname, extname, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const DEFAULT_ROOT = resolve(here, "..", "..", "bundle");
const DEFAULT_STATIC_ROOT = resolve(here, "..", "src");
const DEFAULT_BASE_PATH = "/okf-govuk-content/";

const MEDIA_TYPES = new Map([
  [".css", "text/css; charset=utf-8"],
  [".gz", "application/gzip"],
  [".html", "text/html; charset=utf-8"],
  [".js", "text/javascript; charset=utf-8"],
  [".json", "application/json; charset=utf-8"],
  [".jsonld", "application/ld+json; charset=utf-8"],
  [".svg", "image/svg+xml"],
  [".ttl", "text/turtle; charset=utf-8"],
  [".webmanifest", "application/manifest+json; charset=utf-8"],
  [".yaml", "application/yaml; charset=utf-8"],
  [".yamlld", "application/yaml; charset=utf-8"]
]);

function normalizedBasePath(value) {
  const path = "/" + String(value || "").replace(/^\/+|\/+$/g, "") + "/";
  return path === "//" ? "/" : path;
}

function safeTarget(root, relativePath) {
  let decoded;
  try {
    decoded = decodeURIComponent(relativePath);
  } catch {
    return null;
  }
  if (!decoded || decoded.includes("\\") || decoded.split("/").includes("..")) return null;
  const target = resolve(root, decoded);
  return target === root || target.startsWith(root + sep) ? target : null;
}

async function isFile(path) {
  try {
    return (await stat(path)).isFile();
  } catch {
    return false;
  }
}

export async function startFixtureServer(options = {}) {
  const root = resolve(options.root || DEFAULT_ROOT);
  const staticRoot = resolve(options.staticRoot || DEFAULT_STATIC_ROOT);
  const basePath = normalizedBasePath(options.basePath || DEFAULT_BASE_PATH);
  const requests = [];
  const sockets = new Set();
  const server = createServer(async (request, response) => {
    const requestUrl = new URL(request.url || "/", "http://127.0.0.1");
    requests.push({
      method: request.method || "GET",
      pathname: requestUrl.pathname,
      search: requestUrl.search,
      range: String(request.headers.range || "")
    });
    if (basePath !== "/" && requestUrl.pathname === basePath.slice(0, -1)) {
      response.writeHead(308, { location: basePath + requestUrl.search });
      response.end();
      return;
    }
    const insideBase = basePath === "/" || requestUrl.pathname.startsWith(basePath);
    const relative = insideBase ? requestUrl.pathname.slice(basePath.length) || "index.html" : "";
    const staticTarget = insideBase ? safeTarget(staticRoot, relative) : null;
    let target = staticTarget && await isFile(staticTarget) ? staticTarget : insideBase ? safeTarget(root, relative) : null;
    let status = 200;
    if (!target || !await isFile(target)) {
      target = await isFile(resolve(staticRoot, "404.html")) ? resolve(staticRoot, "404.html") : resolve(root, "404.html");
      status = 404;
    }
    try {
      const body = await readFile(target);
      let payload = body;
      let responseStatus = status;
      const headers = {
        "accept-ranges": "bytes",
        "cache-control": "no-store",
        "content-type": MEDIA_TYPES.get(extname(target).toLowerCase()) || "application/octet-stream",
        "x-content-type-options": "nosniff"
      };
      const range = /^bytes=(\d+)-(\d+)$/.exec(String(request.headers.range || ""));
      if (status === 200 && range) {
        const start = Number(range[1]);
        const end = Number(range[2]);
        if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 0 || end < start || end >= body.byteLength) {
          response.writeHead(416, { ...headers, "content-range": `bytes */${body.byteLength}` });
          response.end();
          return;
        }
        payload = body.subarray(start, end + 1);
        responseStatus = 206;
        headers["content-range"] = `bytes ${start}-${end}/${body.byteLength}`;
      }
      headers["content-length"] = String(payload.byteLength);
      response.writeHead(responseStatus, headers);
      if (request.method !== "HEAD") response.end(payload);
      else response.end();
    } catch (error) {
      response.writeHead(500, { "content-type": "text/plain; charset=utf-8" });
      response.end(error instanceof Error ? error.message : String(error));
    }
  });
  server.on("connection", (socket) => {
    sockets.add(socket);
    socket.once("close", () => sockets.delete(socket));
  });
  await new Promise((resolveReady, reject) => {
    server.once("error", reject);
    server.listen(Number(options.port || 0), "127.0.0.1", resolveReady);
  });
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("Fixture server did not expose a TCP port");
  const origin = `http://127.0.0.1:${address.port}`;
  return {
    basePath,
    baseUrl: origin + basePath,
    origin,
    requests,
    async close() {
      if (!server.listening) return;
      await new Promise((resolveClose, reject) => {
        let settled = false;
        const finish = (error) => {
          if (settled) return;
          settled = true;
          clearTimeout(forceTimer);
          clearTimeout(hardTimer);
          if (error) reject(error);
          else resolveClose();
        };
        const destroyConnections = () => {
          server.closeAllConnections?.();
          for (const socket of sockets) socket.destroy();
        };
        const forceTimer = setTimeout(destroyConnections, 1000);
        const hardTimer = setTimeout(() => {
          destroyConnections();
          finish(new Error("Fixture server did not close within 5 seconds"));
        }, 5000);
        server.close((error) => finish(error));
        server.closeIdleConnections?.();
      });
    }
  };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const server = await startFixtureServer({ port: Number(process.env.PORT || 4173), basePath: process.env.BASE_PATH || DEFAULT_BASE_PATH });
  process.stdout.write(`READY ${server.baseUrl}\n`);
  const close = async () => {
    await server.close();
    process.exit(0);
  };
  process.once("SIGINT", close);
  process.once("SIGTERM", close);
}
