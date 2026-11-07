// A page that comes back with a problem (a form that was refused, an action that failed) puts the
// keyboard focus on the message, so a keyboard or screen-reader user starts reading right there
// instead of at the top of the page. No inline script: the content policy forbids it.
(function () {
  var summary = document.querySelector("[data-error-summary]");
  if (!summary) { return; }
  summary.setAttribute("tabindex", "-1");
  summary.focus();
})();
