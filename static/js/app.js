// DALI front-end helpers (plain JavaScript, no libraries)
(function () {
  "use strict";

  const peso = (n) =>
    "\u20b1" + (Number(n) || 0).toLocaleString("en-PH", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  // Live order computation on the "Place an Order" page
  const form = document.getElementById("order-form");
  if (form) {
    const get = (id) => document.getElementById(id);
    const fees = {};
    get("barangay").querySelectorAll("option[data-fee]").forEach((o) => (fees[o.value] = Number(o.dataset.fee)));

    const update = () => {
      const price = parseFloat(get("price").value) || 0;
      const qty = parseInt(get("quantity").value, 10) || 0;
      const sel = get("barangay");
      const fee = fees[sel.value];
      const hasFee = fee !== undefined;
      const subtotal = price * qty;

      get("c-brgy").textContent = hasFee ? sel.options[sel.selectedIndex].dataset.name : "-";
      get("c-info").textContent = get("additional_info").value.trim() || "-";
      get("c-rest").textContent = get("restaurant").value.trim() || "-";
      get("c-item").textContent = get("item").value.trim() || "-";
      get("c-price").textContent = peso(price);
      get("c-qty").textContent = qty || 0;
      get("c-sub").textContent = peso(subtotal);
      get("c-fee").textContent = hasFee ? peso(fee) : "-";
      get("c-total").textContent = peso(subtotal + (hasFee ? fee : 0));
    };
    form.addEventListener("input", update);
    form.addEventListener("change", update);
    update();
  }

  // Live search filter for tables (data-filter="<table id>")
  document.querySelectorAll("[data-filter]").forEach((input) => {
    const table = document.getElementById(input.dataset.filter);
    if (!table) return;
    input.addEventListener("input", () => {
      const q = input.value.toLowerCase();
      table.querySelectorAll("tbody tr").forEach((tr) => {
        tr.style.display = tr.textContent.toLowerCase().includes(q) ? "" : "none";
      });
    });
  });

  // Confirm before risky actions
  document.querySelectorAll("form[data-confirm]").forEach((f) => {
    f.addEventListener("submit", (e) => {
      if (!window.confirm(f.dataset.confirm)) e.preventDefault();
    });
  });

  // Fade out success messages
  document.querySelectorAll(".flash.ok").forEach((el) => {
    setTimeout(() => {
      el.style.transition = "opacity .5s";
      el.style.opacity = "0";
      setTimeout(() => el.remove(), 600);
    }, 4500);
  });
})();