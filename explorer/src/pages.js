export function pagesBasePath(pathname, projectName) {
  const segments = String(pathname || "").split("/").filter(Boolean);
  const project = String(projectName || "").trim();
  const index = project ? segments.indexOf(project) : -1;
  return index >= 0 ? "/" + segments.slice(0, index + 1).join("/") + "/" : "/";
}

export function pagesFallbackUrl(input, projectName) {
  const current = input instanceof URL ? input : new URL(String(input));
  const target = new URL(pagesBasePath(current.pathname, projectName), current.origin);
  target.search = current.search;
  target.hash = current.hash;
  return target;
}

if (typeof document !== "undefined" && typeof window !== "undefined") {
  const projectName = document.documentElement.dataset.pagesProject || "";
  const target = pagesFallbackUrl(window.location.href, projectName);
  if (target.href !== window.location.href) window.location.replace(target.href);
}
