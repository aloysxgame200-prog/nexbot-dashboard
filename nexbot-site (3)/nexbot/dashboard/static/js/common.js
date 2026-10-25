// Nexbot — script commun à toutes les pages
(function () {
  "use strict";
  window.toast = function (message, type) {
    const box = document.getElementById("toasts");
    if (!box) return;
    const el = document.createElement("div");
    el.className = "toast" + (type === "error" ? " error" : "");
    el.textContent = message;
    box.appendChild(el);
    setTimeout(() => el.remove(), type === "error" ? 7000 : 3500);
  };
  const logout = document.getElementById("logout");
  if (logout) {
    logout.addEventListener("click", async () => {
      await fetch("/logout", { method: "POST", headers: { "X-CSRF-Token": logout.dataset.csrf }, credentials: "same-origin" });
      window.location.href = "/";
    });
  }
})();
