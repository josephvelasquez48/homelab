// Shared by every page: API calls, the signed-in user, and building DOM
// without innerHTML - names and labels come from the database, so they go
// in as text, never as markup.

export async function api(path, { method = "GET", body, headers = {} } = {}) {
  const init = { method, headers: { "X-Requested-With": "cam", ...headers }, credentials: "same-origin" };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const res = await fetch(path, init);
  let data = null;
  try { data = await res.json(); } catch { /* empty or not JSON */ }
  if (!res.ok) {
    const err = new Error((data && data.detail) || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
}

export async function me() {
  try {
    return (await api("/api/me")).user;
  } catch (err) {
    if (err.status === 401) location.replace("/login");
    throw err;
  }
}

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === false || value == null) continue;
    if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "class") el.className = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

// The top bar's right side: who's signed in, links, sign out.
export function fillNav(user) {
  const nav = document.querySelector(".topbar nav");
  if (!nav) return;
  // replaceChildren would print a null as the text "null", so drop them.
  nav.replaceChildren(...[
    h("span", { class: "who" }, user.username),
    location.pathname !== "/" ? h("a", { href: "/" }, "Camera") : null,
    user.is_admin && location.pathname !== "/admin" ? h("a", { href: "/admin" }, "People") : null,
    location.pathname !== "/account" ? h("a", { href: "/account" }, "Account") : null,
    h("button", { type: "button", onclick: signOut }, "Sign out"),
  ].filter(Boolean));
}

export async function signOut() {
  try { await api("/api/logout", { method: "POST" }); } finally { location.replace("/login"); }
}

export function showMessage(el, text, kind = "error") {
  el.textContent = text;
  el.className = `msg ${kind}`;
  el.hidden = !text;
}

export function timeAgo(iso) {
  if (!iso) return "never";
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 60) return "just now";
  const units = [["minute", 60], ["hour", 3600], ["day", 86400], ["week", 604800]];
  let label = "";
  for (const [name, size] of units) {
    if (seconds >= size) {
      const n = Math.floor(seconds / size);
      label = `${n} ${name}${n === 1 ? "" : "s"} ago`;
    }
  }
  return seconds > 2592000 ? new Date(iso).toLocaleDateString() : label;
}
