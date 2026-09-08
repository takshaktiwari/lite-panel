/* "Open in Adminer" auto-login.
   Adminer's own login form requires a per-session CSRF token issued when
   its page is rendered -- there is no way to hand-build a valid POST to it
   from a static link. Instead this fetches Adminer's real login page
   (same-origin, so the browser's session cookie goes with it), takes the
   REAL <form> it rendered, fills in the username/password/database fields,
   and submits that same form -- every other field (token included) is
   whatever Adminer itself put there, untouched.
   If Adminer is already logged in (no login form in the response) or
   unreachable, this falls back to plain navigation -- never worse than the
   plain link this replaces. */
(function () {
  "use strict";

  const ADMINER_USER = "www-data";
  // unix_socket authentication ignores whatever password is sent -- MariaDB
  // approves the login by checking who is actually on the other end of the
  // socket, not this value. It only has to be non-empty: Adminer refuses an
  // actually-empty password with "does not support accessing a database
  // without a password" before the connection is even attempted.
  const ADMINER_PASSWORD_PLACEHOLDER = "unix-socket-auth-ignores-this";

  function fallback(dbName) {
    // Adminer looks up a stored login by the "username" query param (see
    // get_session() in adminer.php) -- without it, even an already-logged-in
    // session shows the login form again, so this has to be included for
    // the "already signed in" case this fallback also covers.
    window.open(
      "/adminer/?username=" + encodeURIComponent(ADMINER_USER) + "&db=" + encodeURIComponent(dbName),
      "_blank",
      "noopener"
    );
  }

  async function openAdminer(dbName) {
    let response;
    try {
      response = await fetch("/adminer/?username=" + encodeURIComponent(ADMINER_USER), {
        credentials: "same-origin",
      });
    } catch (err) {
      fallback(dbName);
      return;
    }
    if (!response.ok) {
      fallback(dbName);
      return;
    }

    const doc = new DOMParser().parseFromString(await response.text(), "text/html");
    const usernameField = doc.querySelector('[name="auth[username]"]');
    if (!usernameField || !usernameField.form) {
      // No login form -- already signed in (or an unexpected page shape).
      fallback(dbName);
      return;
    }

    const form = usernameField.form;
    const setField = (name, value) => {
      const el = form.querySelector('[name="' + name + '"]');
      if (el) el.value = value;
    };
    setField("auth[username]", ADMINER_USER);
    setField("auth[password]", ADMINER_PASSWORD_PLACEHOLDER);
    setField("auth[db]", dbName);

    form.action = "/adminer/";
    form.method = "post";
    form.target = "_blank";
    form.rel = "noopener";
    document.body.appendChild(form);
    form.submit();
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-adminer-db]");
    if (!button) return;
    event.preventDefault();
    openAdminer(button.getAttribute("data-adminer-db") || "");
  });
})();
