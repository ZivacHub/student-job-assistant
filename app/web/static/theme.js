// Light or dark theme. Light is the default and does NOT follow the computer's own setting.
// The choice is kept in this browser only (localStorage). This file runs in <head>, before the
// page is drawn, so there is no flash of the wrong colours. No inline script: the CSP forbids it.
(function () {
  var KEY = "theme";
  var root = document.documentElement;

  function read() {
    try { return localStorage.getItem(KEY); } catch (e) { return null; }
  }
  function write(value) {
    try { localStorage.setItem(KEY, value); } catch (e) { /* private window: just not remembered */ }
  }
  function apply(value) {
    var dark = value === "dark";
    root.setAttribute("data-theme", dark ? "dark" : "light");
    // the phone's status bar / browser frame takes the colour of the top bar
    var bar = document.querySelector('meta[name="theme-color"]');
    if (bar) bar.setAttribute("content", dark ? "#171b23" : "#ffffff");
  }

  apply(read());

  document.addEventListener("DOMContentLoaded", function () {
    var button = document.querySelector("[data-theme-toggle]");
    if (!button) return;
    button.hidden = false;  // without this script the button would do nothing, so it starts hidden
    button.addEventListener("click", function () {
      var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
      // a short colour fade instead of a flash (the style sheet turns it off for reduced motion)
      root.classList.add("theme-changing");
      apply(next);
      write(next);
      setTimeout(function () { root.classList.remove("theme-changing"); }, 260);
    });
  });
})();
