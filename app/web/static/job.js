// "Kopiraj tekst" button on the job page. Without this script the button stays hidden and the
// text can still be selected by hand. No inline script: the content policy forbids it.
(function () {
  document.addEventListener("DOMContentLoaded", function () {
    var timer = null;
    document.querySelectorAll("[data-copy-from]").forEach(function (button) {
      var id = button.getAttribute("data-copy-from");
      var source = document.getElementById(id);
      var status = document.querySelector('[data-copy-status="' + id + '"]');
      if (!source) return;
      button.hidden = false;

      function say(ok) {
        if (!status) return;
        status.textContent = ok ? "Kopirano." : "Kopiranje nije uspjelo. Označite tekst i pritisnite Ctrl+C.";
        clearTimeout(timer);
        timer = setTimeout(function () { status.textContent = ""; }, 4000);
      }
      function fallback() {
        var ok = false;
        source.focus();
        source.select();
        try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
        say(ok);
      }

      button.addEventListener("click", function () {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(source.value).then(function () { say(true); }, fallback);
        } else {
          fallback();
        }
      });
    });
  });
})();
