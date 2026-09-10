/* Firewall interactions. External file for strict CSP compatibility. */
(function() {
  "use strict";

  document.querySelectorAll(".btn-preset").forEach(function(btn) {
    btn.addEventListener("click", function() {
      var port = btn.getAttribute("data-port");
      var proto = btn.getAttribute("data-proto");
      var comment = btn.getAttribute("data-comment");

      var portInput = document.getElementById("fw-port");
      var protoSelect = document.getElementById("fw-proto");
      var commentInput = document.getElementById("fw-comment");
      var fromInput = document.getElementById("fw-from");

      if (portInput) portInput.value = port;
      if (protoSelect) protoSelect.value = proto;
      if (commentInput) commentInput.value = comment;
      if (fromInput) fromInput.value = "any";

      var form = document.getElementById("form-add-rule");
      if (form) form.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  });
})();
