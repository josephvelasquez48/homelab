import { api, fillNav, h, me, showMessage, timeAgo } from "/static/common.js";

const $ = (id) => document.getElementById(id);
let self = null;

function showLink(url) {
  $("invite-url").textContent = url;
  $("invite-result").hidden = false;
  $("invite-copy").textContent = "Copy";
}

async function act(promise, okText) {
  showMessage($("users-msg"), "");
  try {
    const result = await promise;
    if (okText) showMessage($("users-msg"), okText, "ok");
    await load();
    return result;
  } catch (err) {
    showMessage($("users-msg"), err.message);
  }
}

function userRow(user) {
  const isSelf = user.id === self.id;
  return h("tr", {},
    h("td", {},
      user.username,
      isSelf ? h("span", { class: "tag" }, "you") : null,
      user.is_admin ? h("span", { class: "tag admin" }, "admin") : null,
      user.disabled ? h("span", { class: "tag off" }, "turned off") : null,
    ),
    h("td", { class: "muted" }, timeAgo(user.last_seen)),
    h("td", { class: "actions" },
      h("button", {
        class: "btn small", type: "button",
        onclick: async () => {
          const result = await act(api(`/api/admin/users/${user.id}/reset`, { method: "POST" }));
          if (result) {
            showLink(result.url);
            $("invite-form").scrollIntoView({ behavior: "smooth" });
            showMessage($("invite-msg"), `Password reset link for ${user.username}:`, "ok");
          }
        },
      }, "Reset password"),
      isSelf ? null : h("button", {
        class: "btn small", type: "button",
        onclick: () => act(api(`/api/admin/users/${user.id}/${user.disabled ? "enable" : "disable"}`, { method: "POST" }),
          user.disabled ? `${user.username} can sign in again.` : `${user.username} is signed out and can't sign in.`),
      }, user.disabled ? "Turn on" : "Turn off"),
      isSelf ? null : h("button", {
        class: "btn small danger", type: "button",
        onclick: () => {
          if (confirm(`Delete ${user.username}'s account? They'll be signed out, and this can't be undone.`)) {
            act(api(`/api/admin/users/${user.id}`, { method: "DELETE" }), `Deleted ${user.username}.`);
          }
        },
      }, "Delete"),
    ),
  );
}

function inviteRow(invite) {
  return h("tr", {},
    h("td", {}, invite.username, invite.is_admin ? h("span", { class: "tag admin" }, "admin") : null),
    h("td", { class: "muted" }, new Date(invite.expires_at).toLocaleString()),
    h("td", { class: "actions" },
      h("button", {
        class: "btn small danger", type: "button",
        onclick: () => act(api(`/api/admin/invites/${invite.id}`, { method: "DELETE" }), "Link revoked."),
      }, "Revoke"),
    ),
  );
}

async function load() {
  const [{ users, invites }, { views }] = await Promise.all([api("/api/admin/users"), api("/api/admin/views")]);
  $("users").replaceChildren(...users.map(userRow));
  $("invites").replaceChildren(...invites.map(inviteRow));
  $("invites-card").hidden = invites.length === 0;
  $("views").replaceChildren(...(views.length
    ? views.map((v) => h("tr", {},
        h("td", {}, v.username),
        h("td", { class: "muted", title: new Date(v.started_at).toLocaleString() }, timeAgo(v.started_at)),
        h("td", { class: "muted" }, v.ip || "")))
    : [h("tr", {}, h("td", { class: "empty", colspan: "3" }, "Nobody has watched yet."))]));
}

$("invite-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  showMessage($("invite-msg"), "");
  $("invite-result").hidden = true;
  try {
    const result = await api("/api/admin/invites", {
      method: "POST",
      body: { username: $("invite-name").value, is_admin: $("invite-admin").checked },
    });
    showMessage($("invite-msg"), `Send this link to ${result.invite.username}:`, "ok");
    showLink(result.url);
    $("invite-form").reset();
    await load();
  } catch (err) {
    showMessage($("invite-msg"), err.message);
  }
});

$("invite-copy").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("invite-url").textContent);
    $("invite-copy").textContent = "Copied";
  } catch {
    // Clipboard needs a secure context; select the text so it can be copied by hand.
    getSelection().selectAllChildren($("invite-url"));
  }
});

me().then((user) => {
  self = user;
  fillNav(user);
  return load();
}).catch(() => {});
