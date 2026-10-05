const rules = JSON.parse(document.getElementById("rules").textContent);

// Same as counterparty_key() in store.py: 'SBB EASYRIDE' and 'SBB  EasyRide' are one merchant.
const merchantKey = (name) => name.toLowerCase().split(/\s+/).filter(Boolean).join(" ");

const selectOf = (tr, kind) => tr.querySelector(`select[data-sync="${kind}"]`);

// Choosing a category (or "next time") on one row fills in the other rows of the same
// merchant, unless those were changed by hand. Categories aren't copied to "always ask"
// rows: one name can stand for different people there (e.g. "Revolut Bank UAB").
for (const select of document.querySelectorAll("select[data-sync]")) {
  select.addEventListener("change", () => {
    select.dataset.touched = "yes";
    for (const other of document.querySelectorAll(`select[data-sync="${select.dataset.sync}"]`)) {
      const tr = other.closest("tr");
      const alwaysAsk = selectOf(tr, "remember").value === "always_ask";
      if (other.dataset.key !== select.dataset.key || other.dataset.touched) continue;
      if (select.dataset.sync === "category" && alwaysAsk) continue;
      other.value = select.value;
    }
  });
}

// A changed name brings what is remembered for that name.
for (const input of document.querySelectorAll("input.name")) {
  input.addEventListener("change", () => {
    const tr = input.closest("tr");
    const key = merchantKey(input.value) || merchantKey(input.dataset.original);
    const category = selectOf(tr, "category");
    const remember = selectOf(tr, "remember");
    const rule = rules[key];
    category.dataset.key = remember.dataset.key = key;
    if (!category.dataset.touched) category.value = rule?.choice ?? "";
    if (!remember.dataset.touched) remember.value = rule?.always_ask ? "always_ask" : "remember";
    tr.querySelector(".remembered").hidden = !(rule?.choice && category.value === rule.choice);
    const original = tr.querySelector(".original");
    if (original) original.hidden = key === merchantKey(input.dataset.original);
  });
}

// The button says how many rows will be sent.
const send = document.getElementById("send");
const boxes = [...document.querySelectorAll('input[name="include"]')];

function updateSendButton() {
  const count = boxes.filter((box) => box.checked).length;
  send.textContent = count === 1 ? "Send 1 transaction to Firefly" : `Send ${count} transactions to Firefly`;
  send.disabled = count === 0 || "blocked" in send.dataset;
}

boxes.forEach((box) => box.addEventListener("change", updateSendButton));
updateSendButton();
