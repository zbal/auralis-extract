document.addEventListener("click", (e) => {
  const target = e.target;
  if (!(target instanceof Element)) return;

  if (target.closest(".actions form button")) {
    e.stopPropagation();
  }
  if (target.closest(".actions a.icon-btn")) {
    e.stopPropagation();
  }

  const toggle = target.closest(".fetch-toggle");
  if (toggle) {
    e.preventDefault();
    e.stopPropagation();
    const menu = toggle.closest(".fetch-menu");
    if (!menu) return;
    const wasOpen = menu.classList.contains("open");
    document.querySelectorAll(".fetch-menu.open").forEach((el) => el.classList.remove("open"));
    if (!wasOpen) menu.classList.add("open");
    return;
  }

  if (!target.closest(".fetch-menu")) {
    document.querySelectorAll(".fetch-menu.open").forEach((el) => el.classList.remove("open"));
  }
});

const updateSelectionCount = (form) => {
  const counter = form.querySelector("[data-selection-count]");
  const submit = form.querySelector("[data-batch-submit]");
  if (!counter) return;
  const selected = form.querySelectorAll("[data-stream-checkbox]:checked").length;
  counter.textContent = `${selected} selected`;
  if (submit instanceof HTMLButtonElement) {
    submit.disabled = selected === 0;
  }
};

const showToast = (message, isError = false) => {
  const stack = document.getElementById("toast-stack");
  if (!stack) return;
  const toast = document.createElement("div");
  toast.className = `toast${isError ? " error" : ""}`;
  toast.textContent = message;
  stack.appendChild(toast);
  window.setTimeout(() => {
    toast.remove();
  }, 2800);
};

const applyQueuedState = (row, stream) => {
  if (!(row instanceof HTMLElement)) return;
  row.dataset.state = "queued";
  row.dataset.new = "false";
  row.classList.remove("is-new", "is-fresh", "is-undownloaded");
  row.classList.add("is-queued");

  const badge = row.querySelector("[data-state-badge]");
  if (badge instanceof HTMLElement) {
    badge.textContent = stream.state_label;
    badge.className = `badge ${stream.badge_tone}`;
  } else {
    const meta = row.querySelector(".stream-meta");
    if (meta instanceof HTMLElement) {
      const newBadge = document.createElement("span");
      newBadge.className = `badge ${stream.badge_tone}`;
      newBadge.dataset.stateBadge = "true";
      newBadge.textContent = stream.state_label;
      meta.prepend(newBadge);
    }
  }

  let helper = row.querySelector("[data-helper-text]");
  if (stream.helper_text) {
    if (!(helper instanceof HTMLElement)) {
      helper = document.createElement("span");
      helper.className = "muted";
      helper.setAttribute("data-helper-text", "");
      const meta = row.querySelector(".stream-meta");
      if (meta instanceof HTMLElement) meta.appendChild(helper);
    }
    helper.textContent = stream.helper_text;
  } else if (helper instanceof HTMLElement) {
    helper.remove();
  }

  const checkbox = row.querySelector("[data-stream-checkbox]");
  if (checkbox instanceof HTMLInputElement) {
    checkbox.checked = false;
    checkbox.disabled = true;
  }

  const inlineForm = row.querySelector("[data-inline-queue-form]");
  if (inlineForm instanceof HTMLElement) {
    inlineForm.remove();
  }

  const selectionForm = row.closest("[data-selection-form]");
  if (selectionForm instanceof HTMLElement) {
    updateSelectionCount(selectionForm);
    const activeFilter = selectionForm.querySelector("[data-stream-filter].is-active");
    const mode = activeFilter instanceof HTMLElement ? activeFilter.getAttribute("data-stream-filter") || "all" : "all";
    row.classList.toggle("is-hidden", !rowMatchesFilter(row, mode));
  }
};

const rowMatchesFilter = (row, mode) => {
  const state = row.dataset.state || "";
  if (mode === "all") return true;
  if (mode === "new") return row.dataset.new === "true";
  if (mode === "undownloaded") return ["undownloaded", "new"].includes(state);
  return state === mode;
};

document.querySelectorAll("[data-selection-form]").forEach((form) => {
  updateSelectionCount(form);

  form.addEventListener("change", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)) return;
    if (!target.matches("[data-stream-checkbox]")) return;
    updateSelectionCount(form);
  });

  form.querySelectorAll("[data-select-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      const mode = button.getAttribute("data-select-filter");
      form.querySelectorAll("[data-stream-row]").forEach((row) => {
        if (!(row instanceof HTMLElement)) return;
        const checkbox = row.querySelector("[data-stream-checkbox]");
        if (!(checkbox instanceof HTMLInputElement) || checkbox.disabled) return;

        if (mode === "clear") {
          checkbox.checked = false;
        } else if (mode === "new") {
          checkbox.checked = row.dataset.new === "true";
        } else if (mode === "undownloaded") {
          checkbox.checked = ["undownloaded", "new", "failed"].includes(row.dataset.state || "");
        }
      });
      updateSelectionCount(form);
    });
  });

  form.querySelectorAll("[data-stream-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      const mode = button.getAttribute("data-stream-filter") || "all";
      form.querySelectorAll("[data-stream-filter]").forEach((chip) => {
        if (chip instanceof HTMLElement) {
          chip.classList.toggle("is-active", chip === button);
        }
      });
      form.querySelectorAll("[data-stream-row]").forEach((row) => {
        if (!(row instanceof HTMLElement)) return;
        row.classList.toggle("is-hidden", !rowMatchesFilter(row, mode));
      });
    });
  });
});

document.querySelectorAll("[data-inline-queue-form]").forEach((form) => {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector("[data-inline-queue-button]");
    if (button instanceof HTMLButtonElement) {
      button.disabled = true;
    }

    try {
      const response = await fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        headers: {
          "X-Requested-With": "fetch",
        },
      });

      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail || "Unable to queue stream");
      }

      const row = form.closest("[data-stream-row]");
      applyQueuedState(row, payload.stream || {});
      showToast(payload.message || "Stream queued.");
    } catch (error) {
      if (button instanceof HTMLButtonElement) {
        button.disabled = false;
      }
      showToast(error instanceof Error ? error.message : "Unable to queue stream", true);
    }
  });
});
