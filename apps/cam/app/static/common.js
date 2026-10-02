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

// Passkeys. The server sends WebAuthn options as JSON with binary fields in
// base64url; the browser API wants ArrayBuffers, and its answer has to go
// back as JSON. Converted by hand rather than with the newer
// PublicKeyCredential.parse*/toJSON helpers, which older iPhones lack.

const toBuffer = (s) => {
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (s.length % 4)) % 4);
  return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0)).buffer;
};
const toBase64url = (buf) =>
  btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

export const passkeysSupported = () => typeof window.PublicKeyCredential === "function";

function friendlyError(err) {
  if (err && err.name === "NotAllowedError") return new Error("Cancelled, or it timed out - try again.");
  if (err && err.name === "InvalidStateError") return new Error("This device already has a passkey for this account.");
  if (err && err.name === "SecurityError") return new Error("Passkeys only work on the camera's own address.");
  return err;
}

// Make a passkey from the server's creation options; returns what to POST back.
export async function createPasskey(options) {
  let cred;
  try {
    cred = await navigator.credentials.create({
      publicKey: {
        ...options,
        challenge: toBuffer(options.challenge),
        user: { ...options.user, id: toBuffer(options.user.id) },
        excludeCredentials: (options.excludeCredentials || []).map((c) => ({ ...c, id: toBuffer(c.id) })),
      },
    });
  } catch (err) {
    throw friendlyError(err);
  }
  return {
    id: cred.id,
    rawId: toBase64url(cred.rawId),
    type: cred.type,
    authenticatorAttachment: cred.authenticatorAttachment || undefined,
    clientExtensionResults: cred.getClientExtensionResults(),
    response: {
      clientDataJSON: toBase64url(cred.response.clientDataJSON),
      attestationObject: toBase64url(cred.response.attestationObject),
      transports: cred.response.getTransports ? cred.response.getTransports() : [],
    },
  };
}

// Sign in with a passkey from the server's request options.
export async function usePasskey(options) {
  let cred;
  try {
    cred = await navigator.credentials.get({
      publicKey: {
        ...options,
        challenge: toBuffer(options.challenge),
        allowCredentials: (options.allowCredentials || []).map((c) => ({ ...c, id: toBuffer(c.id) })),
      },
    });
  } catch (err) {
    throw friendlyError(err);
  }
  return {
    id: cred.id,
    rawId: toBase64url(cred.rawId),
    type: cred.type,
    authenticatorAttachment: cred.authenticatorAttachment || undefined,
    clientExtensionResults: cred.getClientExtensionResults(),
    response: {
      clientDataJSON: toBase64url(cred.response.clientDataJSON),
      authenticatorData: toBase64url(cred.response.authenticatorData),
      signature: toBase64url(cred.response.signature),
      userHandle: cred.response.userHandle ? toBase64url(cred.response.userHandle) : null,
    },
  };
}
