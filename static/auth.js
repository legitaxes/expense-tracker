/* Shared logic for /login and /signup. Kept separate from app.js so the
   login page does not pull the gated app bundle. */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  /* Unhide BEFORE writing the text. .hidden is display:none !important, which
     keeps the box out of the accessibility tree, and a role="alert" only
     announces content that changes while the node is IN the tree. Setting
     textContent first would announce nothing. */
  function fail(msg) {
    const box = $("auth-error");
    box.classList.remove("hidden");
    box.textContent = msg;
  }

  /* Without this, an error outlives the condition that caused it: fix a
     mismatched confirm-password, submit again, and the old message sits there
     for the whole round trip looking like the submit did nothing. */
  function clearError() {
    const box = $("auth-error");
    box.textContent = "";
    box.classList.add("hidden");
  }

  /* Guards against double-submit. scrypt at N=2**14 makes the round trip slow
     enough to invite a second tap, and on the login form each extra POST is a
     REAL throttle failure recorded against both the IP and the username keys --
     three double-tapped wrong passwords reach LOGIN_MAX_FAILURES and lock the
     owner out after what felt like three tries. Disabling the button also
     supplies the progress feedback whose absence prompts the second tap. */
  let busy = false;

  function setBusy(form, on) {
    busy = on;
    const btn = form.querySelector("button[type=submit]");
    if (btn) btn.disabled = on;
    form.setAttribute("aria-busy", on ? "true" : "false");
  }

  /* The routes raise HTTPException with a plain string, so detail is normally
     the message to show -- and the messages are deliberately distinct ("locked"
     vs "invalid credentials"), so passing them through matters. A 422 from
     request-body validation is the exception: FastAPI answers with a LIST of
     error objects there, and handing that to textContent prints
     "[object Object]". Only strings are trusted; anything else falls back. */
  function detailOf(data, fallback) {
    if (data && typeof data.detail === "string" && data.detail) return data.detail;
    return fallback;
  }

  async function post(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    return { ok: res.ok, data: data };
  }

  async function status() {
    try {
      const res = await fetch("/api/auth/status");
      return await res.json();
    } catch (e) {
      return { authed: false, signup_open: false };
    }
  }

  const loginForm = $("login-form");
  if (loginForm) {
    status().then(function (s) {
      // Already signed in (a bookmark, or a stale SW copy of this page): go
      // home instead of showing a form they don't need. replace(), not href,
      // so Back doesn't come straight back here. This cannot loop: "/" only
      // redirects to /login when the session is invalid, and in that case
      // authed was false and this branch never ran.
      if (s.authed) { location.replace("/"); return; }
      if (s.signup_open) $("signup-link").classList.remove("hidden");
    });
    loginForm.addEventListener("submit", async function (e) {
      e.preventDefault();
      if (busy) return;
      clearError();
      setBusy(loginForm, true);
      const out = await post("/api/auth/login", {
        username: $("username").value.trim(),
        password: $("password").value,
      });
      // Deliberately stays busy on success -- the navigation is the next thing
      // that happens, and re-enabling would flash the button back to live.
      if (out.ok) { location.href = "/"; return; }
      setBusy(loginForm, false);
      fail(detailOf(out.data, "Sign in failed"));
    });
  }

  const signupForm = $("signup-form");
  if (signupForm) {
    status().then(function (s) {
      if (!s.signup_open) { fail("Signup is closed."); return; }
      // Shown iff a SIGNUP_CODE is configured. needs_code is bool(SIGNUP_CODE)
      // and nothing else -- when one is set the server demands it for EVERY
      // signup, first run included. Account existence is irrelevant here.
      if (s.needs_code) {
        $("code-row").classList.remove("hidden");
        // Only required once it's actually on screen, or the hidden field
        // would block submit on the no-code install.
        $("code").required = true;
      }
    });
    signupForm.addEventListener("submit", async function (e) {
      e.preventDefault();
      if (busy) return;
      clearError();
      if ($("password").value !== $("confirm").value) {
        fail("Passwords do not match");
        return;
      }
      setBusy(signupForm, true);
      const out = await post("/api/auth/signup", {
        username: $("username").value.trim(),
        password: $("password").value,
        code: $("code").value,
      });
      if (out.ok) { location.href = "/"; return; }
      setBusy(signupForm, false);
      fail(detailOf(out.data, "Could not create the account"));
    });
  }
})();
