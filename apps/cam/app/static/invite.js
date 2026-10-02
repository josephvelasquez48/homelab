import { api, showMessage } from "/static/common.js";

const $ = (id) => document.getElementById(id);
const token = location.pathname.split("/").pop();

async function load() {
  try {
    const info = await api(`/api/invite/${encodeURIComponent(token)}`);
    $("title").textContent = info.reset ? "Choose a new password" : `Welcome, ${info.username}`;
    $("lede").textContent = info.reset
      ? `For ${info.username}. Signing in with the new password signs you out everywhere else.`
      : `Choose a password for ${info.username}. You'll sign in with this name and password from now on.`;
    $("username").value = info.username;  // so password managers save the right name
    $("form").hidden = false;
    $("password").focus();
  } catch (err) {
    $("invalid-why").textContent = err.message;
    $("invalid").hidden = false;
  } finally {
    $("loading").hidden = true;
  }
}

$("form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const password = $("password").value;
  if (password.length < 10) return showMessage($("msg"), "Use at least 10 characters");
  if (password !== $("confirm").value) return showMessage($("msg"), "The two passwords don't match");
  $("submit").disabled = true;
  try {
    await api(`/api/invite/${encodeURIComponent(token)}`, { method: "POST", body: { password } });
    location.replace("/");
  } catch (err) {
    showMessage($("msg"), err.message);
    $("submit").disabled = false;
  }
});

load();
