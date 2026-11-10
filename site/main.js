/* AirLinx: strona produktu. Bez zależności.
   1) adres repozytorium z <html data-repo-url>, 2) przełącznik motywu, 3) kopiowanie bloków kodu,
   4) romb przelatuje przez szczelinę separatora raz, gdy separator wejdzie w kadr. */
(function () {
  "use strict";

  var root = document.documentElement;

  /* ---------- 1. Repozytorium ----------
     Jedyne źródło adresu: atrybut data-repo-url w index.html. Placeholder „{{REPO_URL}}” zostaje widoczny
     w poleceniu git clone, a żaden link nie prowadzi pod niego (drugie CTA wskazuje #jak-to-dziala). */
  var REPO_URL = root.getAttribute("data-repo-url") || "";
  var repoIsReal = /^https?:\/\/\S+$/.test(REPO_URL) && REPO_URL.indexOf("{{") === -1;

  document.querySelectorAll("[data-repo-text]").forEach(function (el) { el.textContent = REPO_URL; });

  if (repoIsReal) {
    document.querySelectorAll("[data-repo-cta]").forEach(function (a) {
      a.href = REPO_URL;
      a.textContent = "Kod źródłowy";
    });
    document.querySelectorAll("[data-repo-link]").forEach(function (a) { a.href = REPO_URL; });
    document.querySelectorAll("[data-repo-item]").forEach(function (li) { li.hidden = false; });
  }

  /* ---------- 2. Motyw ---------- */
  var STORE_KEY = "airlinx-theme";
  var prefersLight = window.matchMedia ? window.matchMedia("(prefers-color-scheme: light)") : null;

  function currentTheme() {
    var t = root.getAttribute("data-theme");
    if (t === "light" || t === "dark") return t;
    return prefersLight && prefersLight.matches ? "light" : "dark";
  }
  function syncToggle() {
    var next = currentTheme() === "dark" ? "jasny" : "ciemny";
    document.querySelectorAll("[data-theme-toggle]").forEach(function (b) {
      b.setAttribute("aria-label", "Włącz motyw " + next);
      b.setAttribute("title", "Motyw " + next);
    });
  }
  document.querySelectorAll("[data-theme-toggle]").forEach(function (b) {
    b.addEventListener("click", function () {
      var next = currentTheme() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem(STORE_KEY, next); } catch (e) { /* tryb prywatny: motyw działa do odświeżenia */ }
      syncToggle();
    });
  });
  if (prefersLight && prefersLight.addEventListener) prefersLight.addEventListener("change", syncToggle);
  syncToggle();

  /* ---------- 3. Kopiowanie kodu ---------- */
  var status = document.querySelector("[data-copy-status]");

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise(function (resolve, reject) {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed"; ta.style.opacity = "0"; ta.style.top = "0";
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
      document.body.removeChild(ta);
      ok ? resolve() : reject(new Error("copy failed"));
    });
  }

  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    var label = btn.querySelector(".copy-label");
    var fig = btn.closest(".code");
    var code = fig && fig.querySelector("pre code");
    var timer = null;
    if (!code) return;
    var name = fig.querySelector("figcaption > span");
    var title = name ? name.textContent.replace(/\s+/g, " ").trim() : "";
    btn.setAttribute("aria-label", "Kopiuj polecenie: " + title);
    fig.querySelector("pre").setAttribute("aria-label", "Polecenie: " + title);

    btn.addEventListener("click", function () {
      copyText(code.textContent).then(function () {
        btn.classList.add("is-done");
        label.textContent = "Skopiowano";
        if (status) status.textContent = "Skopiowano do schowka.";
        clearTimeout(timer);
        timer = setTimeout(function () {
          btn.classList.remove("is-done");
          label.textContent = "Kopiuj";
          if (status) status.textContent = "";
        }, 2000);
      }, function () {
        label.textContent = "Zaznacz i skopiuj";
        if (status) status.textContent = "Schowek niedostępny. Zaznacz polecenie i skopiuj je ręcznie.";
        var range = document.createRange();
        range.selectNodeContents(code);
        var sel = window.getSelection();
        sel.removeAllRanges(); sel.addRange(range);
      });
    });
  });

  /* ---------- 4. Przelot rombu w separatorach ---------- */
  var targets = document.querySelectorAll(".gap-rule, [data-live-on-view]");
  if (!("IntersectionObserver" in window)) {
    targets.forEach(function (el) { el.classList.add("is-live"); });
  } else {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) { en.target.classList.add("is-live"); io.unobserve(en.target); }
      });
    }, { rootMargin: "0px 0px -18% 0px", threshold: 1 });
    targets.forEach(function (el) { io.observe(el); });
  }
})();
