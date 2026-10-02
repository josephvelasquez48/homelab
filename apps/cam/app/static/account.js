import { api, fillNav, me, showMessage } from "/static/common.js";

const $ = (id) => document.getElementById(id);

me().then((user) => {
  fillNav(user);
  $("username").value = user.username;
}).catch(() => {});

$("form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const next = $("new").value;
  if (next.length < 10) return showMessage($("msg"), "Use at least 10 characters");
  if (next !== $("confirm").value) return showMessage($("msg"), "The two new passwords don't match");
  $("submit").disabled = true;
  try {
    await api("/api/account/password", { method: "POST", body: { current: $("current").value, new: next } });
    $("form").reset();
    showMessage($("msg"), "Password changed.", "ok");
  } catch (err) {
    showMessage($("msg"), err.message);
  } finally {
    $("submit").disabled = false;
  }
});
