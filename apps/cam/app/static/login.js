import { api, passkeysSupported, showMessage, usePasskey } from "/static/common.js";

const msg = document.getElementById("msg");
const button = document.getElementById("signin");

if (!passkeysSupported()) {
  button.disabled = true;
  showMessage(msg, "This browser can't use passkeys. Try Safari, Chrome or Edge, up to date.");
}

button.addEventListener("click", async () => {
  showMessage(msg, "");
  button.disabled = true;
  try {
    const options = await api("/api/login/options", { method: "POST" });
    const credential = await usePasskey(options);
    await api("/api/login", { method: "POST", body: credential });
    location.replace("/");
  } catch (err) {
    showMessage(msg, err.message);
  } finally {
    button.disabled = !passkeysSupported();
  }
});
