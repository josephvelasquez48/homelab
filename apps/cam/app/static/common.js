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

// Mustard, drawn in SVG. One drawing, three poses set by a class on the
// wrapper (app.css animates them): "hello" (nods), "walk" (steps), "sleep"
// (eyes shut, head drawn in, z's). Presentation attributes only - the
// page's CSP allows no inline style - and constant markup, so innerHTML is
// safe here. Faces right, standing on y=146 in a 260x160 box.
const MUSTARD_SVG = `
<svg viewBox="0 0 270 160" role="img" aria-label="Mustard the tortoise" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="m-shell" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#f7cf5a"/><stop offset="1" stop-color="#d8981c"/>
    </linearGradient>
    <linearGradient id="m-skin" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#a7bb68"/><stop offset="1" stop-color="#7f944a"/>
    </linearGradient>
    <clipPath id="m-dome"><path d="M42 114 C42 62 82 34 126 34 C170 34 210 62 210 114 Z"/></clipPath>
  </defs>
  <ellipse class="m-shadow" cx="128" cy="147" rx="98" ry="7" fill="#000" opacity="0.2"/>
  <g class="m-body">
    <g class="m-leg m-leg-far-back"><rect x="86" y="108" width="24" height="32" rx="11" fill="#6f8340" stroke="#4f5f2b" stroke-width="3"/></g>
    <g class="m-leg m-leg-far-front"><rect x="186" y="108" width="24" height="32" rx="11" fill="#6f8340" stroke="#4f5f2b" stroke-width="3"/></g>
    <path d="M44 112 L26 121 L45 121 Z" fill="url(#m-skin)" stroke="#5f7136" stroke-width="3" stroke-linejoin="round"/>
    <g class="m-head">
      <path d="M196 102 C208 98 216 88 222 80 L238 93 C228 102 216 111 202 116 Z" fill="url(#m-skin)" stroke="#5f7136" stroke-width="3" stroke-linejoin="round"/>
      <ellipse cx="238" cy="80" rx="18" ry="14.5" fill="url(#m-skin)" stroke="#5f7136" stroke-width="3"/>
      <circle cx="233" cy="86" r="3.6" fill="#e8866a" opacity="0.45"/>
      <g class="m-eye-open"><circle cx="243" cy="75" r="3.4" fill="#1d1b12"/><circle cx="244.3" cy="73.8" r="1.1" fill="#fff"/></g>
      <path class="m-eye-closed" d="M239 76 Q243 79.5 247 76" fill="none" stroke="#1d1b12" stroke-width="2.2" stroke-linecap="round"/>
      <path d="M242 86.5 Q248 89 252.5 84.5" fill="none" stroke="#1d1b12" stroke-width="2.2" stroke-linecap="round"/>
      <circle cx="253" cy="77" r="1.1" fill="#1d1b12"/>
    </g>
    <path d="M40 117 Q40 127 52 127 H200 Q212 127 212 117 V111 H40 Z" fill="#b9821a" stroke="#7a5210" stroke-width="3" stroke-linejoin="round"/>
    <path d="M63 112 V126 M86 112 V126 M109 112 V126 M132 112 V126 M155 112 V126 M178 112 V126 M199 112 V125" stroke="#7a5210" stroke-width="2"/>
    <path d="M42 114 C42 62 82 34 126 34 C170 34 210 62 210 114 Z" fill="url(#m-shell)"/>
    <g clip-path="url(#m-dome)" fill="#f2c449" stroke="#94650f" stroke-width="2.6" stroke-linejoin="round">
      <path d="M112 44 L140 44 L150 62 L140 80 L112 80 L102 62 Z"/>
      <path d="M102 62 L112 80 L100 108 L66 108 L56 86 L72 62 Z"/>
      <path d="M150 62 L140 80 L152 108 L186 108 L196 86 L180 62 Z"/>
      <path d="M112 80 L140 80 L152 108 L100 108 Z"/>
      <path d="M72 62 L102 62 L112 44 L96 34 L70 40 Z"/>
      <path d="M180 62 L150 62 L140 44 L156 34 L182 40 Z"/>
      <path d="M118 52 L134 52 L139 62 L134 72 L118 72 L113 62 Z" fill="none" stroke-width="1.6" opacity="0.6"/>
    </g>
    <path d="M42 114 C42 62 82 34 126 34 C170 34 210 62 210 114" fill="none" stroke="#7a5210" stroke-width="3.2"/>
    <ellipse cx="98" cy="52" rx="24" ry="8" fill="#fff" opacity="0.28" transform="rotate(-14 98 52)"/>
    <g class="m-leg m-leg-near-back"><rect x="60" y="110" width="27" height="33" rx="12" fill="url(#m-skin)" stroke="#5f7136" stroke-width="3"/>
      <circle cx="66" cy="140" r="2.3" fill="#efe7c8"/><circle cx="73.5" cy="141" r="2.3" fill="#efe7c8"/><circle cx="81" cy="140" r="2.3" fill="#efe7c8"/></g>
    <g class="m-leg m-leg-near-front"><rect x="166" y="110" width="27" height="33" rx="12" fill="url(#m-skin)" stroke="#5f7136" stroke-width="3"/>
      <circle cx="172" cy="140" r="2.3" fill="#efe7c8"/><circle cx="179.5" cy="141" r="2.3" fill="#efe7c8"/><circle cx="187" cy="140" r="2.3" fill="#efe7c8"/></g>
  </g>
  <g class="m-zzz" fill="currentColor" font-family="ui-rounded, 'SF Pro Rounded', system-ui, sans-serif" font-weight="800">
    <text class="m-z1" x="226" y="52" font-size="15">z</text>
    <text class="m-z2" x="240" y="36" font-size="19">z</text>
    <text class="m-z3" x="256" y="18" font-size="23">z</text>
  </g>
</svg>`;

// A dandelion - Mustard's favourite - for the sign-in pages.
const DANDELION_SVG = `
<svg viewBox="0 0 40 70" aria-hidden="true" xmlns="http://www.w3.org/2000/svg">
  <path d="M20 68 C19 52 22 40 20 26" fill="none" stroke="#6f8340" stroke-width="3" stroke-linecap="round"/>
  <path d="M20 54 C12 50 8 44 8 38 C14 42 18 46 20 52" fill="#7f944a"/>
  <path d="M21 46 C28 42 32 36 31 30 C26 35 22 39 21 44" fill="#7f944a"/>
  <g fill="#f5b81b" stroke="#c98a12" stroke-width="1.2">
    <circle cx="20" cy="18" r="13"/>
  </g>
  <g fill="none" stroke="#e09a0f" stroke-width="1.6" stroke-linecap="round">
    <path d="M20 8 V13 M28.5 11.5 L25 15 M31 19 H26 M11.5 11.5 L15 15 M9 19 H14 M14 26 L16.5 22.5 M26 26 L23.5 22.5"/>
  </g>
  <circle cx="20" cy="18" r="4.5" fill="#f9d45a"/>
</svg>`;

// Fill every <div class="mustard" data-pose="..."> on the page.
export function drawMustard(root = document) {
  root.querySelectorAll(".mustard").forEach((el) => {
    if (!el.firstElementChild) el.innerHTML = MUSTARD_SVG;
    el.classList.remove("hello", "walk", "sleep");
    el.classList.add(el.dataset.pose || "hello");
  });
  root.querySelectorAll(".dandelion").forEach((el) => {
    if (!el.firstElementChild) el.innerHTML = DANDELION_SVG;
  });
}

export function setMustardPose(el, pose) {
  el.dataset.pose = pose;
  el.classList.remove("hello", "walk", "sleep");
  el.classList.add(pose);
}
