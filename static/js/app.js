/* ExamManager — shared page behaviour: theme, mobile sidebar, toasts, menus.
   The theme is applied before first paint by the inline snippet in base.html;
   this file only handles switching it. */
(function () {
  "use strict";
  var THEME_KEY = "em_theme";

  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
  }

  function setTheme(theme) {
    var t = theme === "dark" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", t);
    try { localStorage.setItem(THEME_KEY, t); } catch (e) {}
    document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
      btn.setAttribute("aria-label", t === "dark" ? "Switch to light mode" : "Switch to dark mode");
      btn.setAttribute("title", t === "dark" ? "Light mode" : "Dark mode");
      var i = btn.querySelector("i");
      if (i) i.className = t === "dark" ? "fa-solid fa-sun" : "fa-solid fa-moon";
    });
    document.querySelectorAll("[data-theme-choice]").forEach(function (b) {
      b.setAttribute("aria-pressed", b.getAttribute("data-theme-choice") === t ? "true" : "false");
    });
    document.dispatchEvent(new CustomEvent("em:theme", { detail: t }));
  }

  function toggleTheme() { setTheme(currentTheme() === "dark" ? "light" : "dark"); }

  /* Mobile sidebar */
  function openNav() {
    var sb = document.getElementById("sidebar"), ov = document.getElementById("navOverlay");
    if (sb) sb.classList.add("open");
    if (ov) ov.classList.add("show");
  }
  function closeNav() {
    var sb = document.getElementById("sidebar"), ov = document.getElementById("navOverlay");
    if (sb) sb.classList.remove("open");
    if (ov) ov.classList.remove("show");
  }

  /* Toasts — type: "ok" | "bad" | "warn" | "info" */
  var ICONS = { ok: "fa-circle-check", bad: "fa-circle-exclamation", warn: "fa-triangle-exclamation", info: "fa-circle-info" };
  function toast(message, type, ms) {
    type = type || "ok";
    var box = document.getElementById("toasts");
    if (!box) return;
    var t = document.createElement("div");
    t.className = "toast " + type;
    t.setAttribute("role", type === "bad" ? "alert" : "status");
    var i = document.createElement("i");
    i.className = "fa-solid " + (ICONS[type] || ICONS.info);
    var span = document.createElement("span");
    span.textContent = message;
    t.appendChild(i); t.appendChild(span);
    box.appendChild(t);
    setTimeout(function () { t.classList.add("out"); setTimeout(function () { t.remove(); }, 220); }, ms || 3600);
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /* Modals: .modal elements; close on backdrop click and Escape */
  function openModal(id) { var m = document.getElementById(id); if (m) { m.classList.add("show"); var f = m.querySelector("button, input, select, a"); if (f) setTimeout(function () { f.focus(); }, 30); } }
  function closeModal(id) { var m = document.getElementById(id); if (m) m.classList.remove("show"); }

  document.addEventListener("click", function (e) {
    if (e.target.classList && e.target.classList.contains("modal") && !e.target.hasAttribute("data-static")) {
      e.target.classList.remove("show");
    }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      document.querySelectorAll(".modal.show:not([data-static])").forEach(function (m) { m.classList.remove("show"); });
      document.querySelectorAll(".menu.show").forEach(function (m) { m.classList.remove("show"); });
      closeNav();
    }
  });

  document.addEventListener("DOMContentLoaded", function () {
    setTheme(currentTheme());
    document.querySelectorAll("[data-theme-toggle]").forEach(function (b) { b.addEventListener("click", toggleTheme); });
    document.querySelectorAll("[data-theme-choice]").forEach(function (b) {
      b.addEventListener("click", function () { setTheme(b.getAttribute("data-theme-choice")); });
    });
    var mb = document.getElementById("menuBtn"); if (mb) mb.addEventListener("click", openNav);
    var ov = document.getElementById("navOverlay"); if (ov) ov.addEventListener("click", closeNav);
  });

  window.EM = { toast: toast, esc: esc, setTheme: setTheme, toggleTheme: toggleTheme,
                openModal: openModal, closeModal: closeModal, openNav: openNav, closeNav: closeNav };
  // Names older page scripts call.
  window.openModal = openModal;
  window.closeModal = closeModal;
  window.openMobile = window.openMobileSidebar = openNav;
  window.closeMobile = window.closeMobileSidebar = closeNav;
})();
