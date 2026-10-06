// The connections page: change their order by dragging the handle (or with the arrow keys on
// it), and rename them.

const list = document.querySelector("table.connections tbody");

// Saves the order shown and puts the menu in the same order.
async function saveOrder() {
  const ids = [...list.rows].map((tr) => tr.dataset.id);
  const menu = Object.fromEntries(
    [...document.querySelectorAll("[data-connection]")].map((li) => [li.dataset.connection, li]),
  );
  let previous = menu[ids[0]]?.parentNode.querySelector("[data-connection]").previousElementSibling;
  for (const id of ids) {
    if (!previous || !menu[id]) break;
    previous.after(menu[id]);
    previous = menu[id];
  }
  const form = new URLSearchParams({ csrf: list.dataset.csrf });
  for (const id of ids) form.append("order", id);
  const response = await fetch("/connections/order", { method: "POST", body: form }).catch(() => null);
  // Logged out meanwhile, page out of date or offline: show what's saved.
  if (!response || !response.ok || response.redirected) location.reload();
}

if (list && window.Sortable) {
  Sortable.create(list, { handle: ".drag-handle", animation: 150, onEnd: (event) => {
    if (event.oldIndex !== event.newIndex) saveOrder();
  } });

  list.addEventListener("keydown", (event) => {
    const handle = event.target.closest(".drag-handle");
    const tr = handle?.closest("tr");
    if (!tr || (event.key !== "ArrowUp" && event.key !== "ArrowDown")) return;
    event.preventDefault();
    const other = event.key === "ArrowUp" ? tr.previousElementSibling : tr.nextElementSibling;
    if (!other) return;
    if (event.key === "ArrowUp") other.before(tr);
    else other.after(tr);
    handle.focus();
    saveOrder();
  });
}

// Opening a rename form puts the cursor in its field.
for (const form of document.querySelectorAll("form.rename")) {
  form.addEventListener("shown.bs.collapse", () => form.querySelector('input[name="name"]').select());
}
