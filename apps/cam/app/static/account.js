import { api, createPasskey, fillNav, h, me, passkeysSupported, showMessage, timeAgo } from "/static/common.js";

const $ = (id) => document.getElementById(id);

async function load() {
  const { passkeys } = await api("/api/account/passkeys");
  $("passkeys").replaceChildren(...passkeys.map((p) => h("tr", {},
    h("td", {}, p.name, p.synced ? h("span", { class: "tag" }, "synced") : null),
    h("td", { class: "muted" }, new Date(p.created_at).toLocaleDateString()),
    h("td", { class: "muted" }, timeAgo(p.last_used)),
    h("td", { class: "actions" },
      passkeys.length > 1 ? h("button", {
        class: "btn small danger", type: "button",
        onclick: async () => {
          if (!confirm(`Remove the ${p.name} passkey? That device won't be able to sign in with it.`)) return;
          try {
            await api(`/api/account/passkeys/${encodeURIComponent(p.id)}`, { method: "DELETE" });
            showMessage($("msg"), "Passkey removed.", "ok");
            await load();
          } catch (err) {
            showMessage($("msg"), err.message);
          }
        },
      }, "Remove") : null),
  )));
}

$("add").addEventListener("click", async () => {
  showMessage($("msg"), "");
  $("add").disabled = true;
  try {
    const options = await api("/api/account/passkeys/options", { method: "POST" });
    const credential = await createPasskey(options);
    await api("/api/account/passkeys", { method: "POST", body: credential });
    showMessage($("msg"), "Passkey added.", "ok");
    await load();
  } catch (err) {
    showMessage($("msg"), err.message);
  } finally {
    $("add").disabled = !passkeysSupported();
  }
});

if (!passkeysSupported()) $("add").disabled = true;
me().then((user) => { fillNav(user); return load(); }).catch(() => {});
