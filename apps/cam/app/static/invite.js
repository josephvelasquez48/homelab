import { api, createPasskey, passkeysSupported, showMessage } from "/static/common.js";

const $ = (id) => document.getElementById(id);
const token = location.pathname.split("/").pop();

async function load() {
  try {
    const info = await api(`/api/invite/${encodeURIComponent(token)}`);
    $("title").textContent = info.reset ? `New passkey for ${info.username}` : `Welcome, ${info.username}`;
    $("lede").textContent = info.reset
      ? "Make a passkey on this device. Your old passkeys stop working, and you're signed out everywhere else."
      : "Make a passkey on this device and you're in. You'll use it to sign in from now on.";
    $("form").hidden = false;
    if (!passkeysSupported()) {
      $("create").disabled = true;
      showMessage($("msg"), "This browser can't make passkeys. Open the link in Safari, Chrome or Edge, up to date.");
    }
  } catch (err) {
    $("invalid-why").textContent = err.message;
    $("invalid").hidden = false;
  } finally {
    $("loading").hidden = true;
  }
}

$("create").addEventListener("click", async () => {
  showMessage($("msg"), "");
  $("create").disabled = true;
  try {
    const path = `/api/invite/${encodeURIComponent(token)}`;
    const options = await api(`${path}/options`, { method: "POST" });
    const credential = await createPasskey(options);
    await api(path, { method: "POST", body: credential });
    location.replace("/");
  } catch (err) {
    showMessage($("msg"), err.message);
    $("create").disabled = false;
  }
});

load();
