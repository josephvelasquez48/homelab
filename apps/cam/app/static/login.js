import { api, showMessage } from "/static/common.js";

const form = document.getElementById("form");
const msg = document.getElementById("msg");
const submit = document.getElementById("submit");

document.getElementById("username").focus();

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  showMessage(msg, "");
  submit.disabled = true;
  try {
    await api("/api/login", {
      method: "POST",
      body: {
        username: document.getElementById("username").value,
        password: document.getElementById("password").value,
      },
    });
    location.replace("/");
  } catch (err) {
    showMessage(msg, err.message);
    document.getElementById("password").select();
  } finally {
    submit.disabled = false;
  }
});
