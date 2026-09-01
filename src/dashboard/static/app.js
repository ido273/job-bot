// Small vanilla-JS tag-input widget: no build step, no dependency.
// Markup contract: a `[data-tag-input]` container holding one
// `textarea[data-tag-storage]` (hidden, newline-separated values, this is
// what actually submits with the form) plus a visible text field the user
// types into.

function initTagInput(container) {
  const storage = container.querySelector("[data-tag-storage]");
  const field = document.createElement("input");
  field.type = "text";
  field.className = "tag-input__field";
  field.placeholder = container.dataset.placeholder || "הקלד וגם Enter";

  let values = storage.value.split("\n").map((v) => v.trim()).filter(Boolean);

  function render() {
    container.querySelectorAll(".tag-input__chip").forEach((chip) => chip.remove());
    values.forEach((value, index) => {
      const chip = document.createElement("span");
      chip.className = "tag-input__chip";
      chip.textContent = value;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.setAttribute("aria-label", "הסר");
      remove.textContent = "×";
      remove.addEventListener("click", () => {
        values.splice(index, 1);
        sync();
      });
      chip.appendChild(remove);
      container.insertBefore(chip, field);
    });
    storage.value = values.join("\n");
  }

  function sync() {
    render();
  }

  function addValue(raw) {
    const value = raw.trim();
    if (value && !values.includes(value)) {
      values.push(value);
      sync();
    }
  }

  field.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === ",") {
      event.preventDefault();
      addValue(field.value);
      field.value = "";
    } else if (event.key === "Backspace" && field.value === "" && values.length) {
      values.pop();
      sync();
    }
  });

  field.addEventListener("blur", () => {
    if (field.value.trim()) {
      addValue(field.value);
      field.value = "";
    }
  });

  container.appendChild(field);
  render();
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-tag-input]").forEach(initTagInput);
});
