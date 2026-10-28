"use strict";

function bindConfirmations(root = document) {
  root.querySelectorAll("form[data-confirm]").forEach((form) => {
    if (form.dataset.bound) return;
    form.dataset.bound = "1";
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });
}

function loadMore(sentinel) {
  if (!sentinel || sentinel.dataset.loading === "1") return;
  sentinel.dataset.loading = "1";
  const button = sentinel.querySelector("button");
  if (button) button.textContent = "Đang tải…";
  let url = sentinel.dataset.loadUrl;
  if (!url && sentinel.dataset.loadBase) {
    const params = new URLSearchParams(window.location.search);
    params.set("offset", sentinel.dataset.offset || "0");
    url = `${sentinel.dataset.loadBase}?${params.toString()}`;
  }
  fetch(url, {headers: {"X-Requested-With": "fetch"}})
    .then((response) => {
      if (!response.ok) throw new Error("load_failed");
      return response.text();
    })
    .then((html) => {
      sentinel.insertAdjacentHTML("beforebegin", html);
      sentinel.remove();
      bindLazyLoading();
      bindConfirmations();
    })
    .catch(() => {
      sentinel.dataset.loading = "0";
      if (button) button.textContent = "Thử tải lại";
    });
}

let observer;
function bindLazyLoading() {
  if (!observer) {
    observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => { if (entry.isIntersecting) loadMore(entry.target); });
    }, {rootMargin: "240px"});
  }
  document.querySelectorAll(".lazy-sentinel").forEach((sentinel) => {
    if (sentinel.dataset.observed) return;
    sentinel.dataset.observed = "1";
    observer.observe(sentinel);
    const button = sentinel.querySelector("button");
    if (button) button.addEventListener("click", () => loadMore(sentinel));
  });
}

function bindSidebar() {
  const toggle = document.querySelector("[data-sidebar-toggle]");
  const close = document.querySelector("[data-sidebar-close]");
  if (!toggle) return;
  const setOpen = (open) => {
    document.body.classList.toggle("sidebar-open", open);
    toggle.setAttribute("aria-expanded", String(open));
  };
  toggle.addEventListener("click", () => setOpen(!document.body.classList.contains("sidebar-open")));
  if (close) close.addEventListener("click", () => setOpen(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setOpen(false);
  });
}

function bindAiSearch() {
  const lookupSelect = document.querySelector("[data-ai-lookup-select]");
  if (lookupSelect) {
    lookupSelect.addEventListener("change", () => {
      const params = new URLSearchParams();
      if (lookupSelect.value) params.set("lan_tra_cuu_ai", lookupSelect.value);
      window.location.href = `/tra-cuu-ai${params.size ? `?${params}` : ""}`;
    });
  }
  const topicSelect = document.querySelector("[data-ai-topic-select]");
  const sourceSelect = document.querySelector("[data-ai-source-searches]");
  const sourceHint = document.querySelector("[data-ai-source-hint]");
  if (topicSelect && sourceSelect) {
    topicSelect.addEventListener("change", () => {
      sourceSelect.disabled = true;
      sourceSelect.replaceChildren(new Option("Đang tải danh sách…", ""));
      if (!topicSelect.value) {
        sourceSelect.replaceChildren(new Option("Hãy chọn đề tài trước", ""));
        return;
      }
      fetch(`/tra-cuu-ai/de-tai/${encodeURIComponent(topicSelect.value)}/lan-tim-kiem`, {headers: {"Accept": "application/json"}})
        .then((response) => { if (!response.ok) throw new Error("source_failed"); return response.json(); })
        .then((data) => {
          sourceSelect.replaceChildren();
          data.items.forEach((item) => {
            const created = new Date(item.created_at).toLocaleString("vi-VN");
            sourceSelect.add(new Option(`Lần ${item.number} · ${created} · ${item.total_results} kết quả · ${item.pdf_documents} có PDF`, item.id));
          });
          sourceSelect.disabled = data.items.length === 0;
          if (!data.items.length) sourceSelect.add(new Option("Đề tài chưa có lần tìm kiếm thông thường", ""));
          if (sourceHint) sourceHint.textContent = data.items.length ? `Có ${data.items.length} lần tìm kiếm; giữ Ctrl hoặc Command để chọn nhiều lần.` : "Chưa có lần tìm kiếm nguồn phù hợp.";
        })
        .catch(() => {
          sourceSelect.replaceChildren(new Option("Không thể tải danh sách", ""));
          if (sourceHint) sourceHint.textContent = "Vui lòng tải lại trang và thử lại.";
        });
    });
  }
  const panel = document.querySelector("[data-ai-progress-url]");
  if (!panel || ["hoan_thanh", "loi_mot_phan", "loi"].includes(panel.dataset.aiProgressStatus)) return;
  const elapsedNode = panel.querySelector("[data-ai-elapsed]");
  let serverElapsed = Math.max(0, Number(panel.dataset.elapsedMs || 0));
  let syncedAt = Date.now();
  const elapsedTimer = window.setInterval(() => {
    if (elapsedNode) elapsedNode.textContent = formatElapsed(serverElapsed + Date.now() - syncedAt);
  }, 1000);
  const poll = window.setInterval(() => {
    fetch(panel.dataset.aiProgressUrl, {headers: {"Accept": "application/json"}})
      .then((response) => { if (!response.ok) throw new Error("progress_failed"); return response.json(); })
      .then((data) => {
        const setText = (selector, value) => { const node = panel.querySelector(selector); if (node) node.textContent = value; };
        setText("[data-pdf-total-missing]", data.total_missing);
        setText("[data-progress-label]", `${data.finished}/${data.total} đã xử lý`);
        setText("[data-progress-completed]", data.completed);
        setText("[data-progress-waiting]", data.waiting + data.processing);
        setText("[data-progress-no-pdf]", data.without_pdf);
        setText("[data-progress-failed]", data.failed);
        const bar = panel.querySelector("[data-progress-bar]");
        if (bar) bar.style.width = `${data.percent}%`;
        serverElapsed = data.elapsed_ms;
        syncedAt = Date.now();
        if (["hoan_thanh", "loi_mot_phan", "loi"].includes(data.status)) {
          window.clearInterval(elapsedTimer);
          window.clearInterval(poll);
          window.location.reload();
        }
      })
      .catch(() => window.clearInterval(poll));
  }, 4000);
}

function formatElapsed(milliseconds) {
  const totalSeconds = Math.max(0, Math.floor(Number(milliseconds || 0) / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const twoDigits = (value) => String(value).padStart(2, "0");
  return `${twoDigits(hours)} giờ ${twoDigits(minutes)} phút ${twoDigits(seconds)} giây`;
}

function bindPdfDownloaderTabs() {
  document.querySelectorAll("[data-pdf-downloader]").forEach((panel) => {
    const tabs = [...panel.querySelectorAll("[data-pdf-tab]")];
    const panels = [...panel.querySelectorAll("[data-pdf-tab-panel]")];
    if (!tabs.length) return;
    panel.classList.add("pdf-tabs-enabled");
    const activate = (name, focus = false) => {
      tabs.forEach((tab) => {
        const selected = tab.dataset.pdfTab === name;
        tab.setAttribute("aria-selected", String(selected));
        tab.tabIndex = selected ? 0 : -1;
        if (selected && focus) tab.focus();
      });
      panels.forEach((content) => { content.hidden = content.dataset.pdfTabPanel !== name; });
      if (focus) window.history.replaceState(null, "", `#pdf-panel-${name}`);
    };
    tabs.forEach((tab, index) => {
      tab.addEventListener("click", () => activate(tab.dataset.pdfTab, true));
      tab.addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
        event.preventDefault();
        const offset = event.key === "ArrowRight" ? 1 : -1;
        const next = tabs[(index + offset + tabs.length) % tabs.length];
        activate(next.dataset.pdfTab, true);
      });
    });
    const requested = window.location.hash === "#pdf-panel-failed" ? "failed" : null;
    activate(requested || tabs.find((tab) => tab.getAttribute("aria-selected") === "true")?.dataset.pdfTab || tabs[0].dataset.pdfTab);
  });
}

function bindSearchTimers() {
  document.querySelectorAll("form[data-search-form]").forEach((form) => {
    if (form.dataset.timerBound) return;
    form.dataset.timerBound = "1";
    form.addEventListener("submit", () => {
      const button = form.querySelector("[data-search-clock]");
      if (!button) return;
      const started = Date.now();
      const label = button.dataset.submitText || "Đang xử lý";
      button.textContent = `${label} · ${formatElapsed(0)}`;
      window.setInterval(() => { button.textContent = `${label} · ${formatElapsed(Date.now() - started)}`; }, 1000);
    });
  });

  const panel = document.querySelector("[data-search-progress-url]");
  if (!panel) return;
  const elapsedNodes = panel.querySelectorAll("[data-elapsed-seconds]");
  const initiallyPaused = panel.dataset.searchProgressStatus === "tam_dung";
  let paused = initiallyPaused;
  let serverElapsed = Math.max(0, Number(panel.dataset.elapsedMs || 0));
  let syncedAt = Date.now();
  const renderClock = () => {
    elapsedNodes.forEach((node) => { node.textContent = formatElapsed(serverElapsed + (paused ? 0 : Date.now() - syncedAt)); });
  };
  renderClock();
  if (["hoan_thanh", "loi_mot_phan", "loi"].includes(panel.dataset.searchProgressStatus)) return;
  const clock = window.setInterval(renderClock, 1000);
  const poll = window.setInterval(() => {
    fetch(panel.dataset.searchProgressUrl, {headers: {"Accept": "application/json"}})
      .then((response) => { if (!response.ok) throw new Error("progress_failed"); return response.json(); })
      .then((data) => {
        serverElapsed = data.elapsed_ms;
        syncedAt = Date.now();
        paused = data.status === "tam_dung";
        const setText = (selector, value) => { panel.querySelectorAll(selector).forEach((node) => { node.textContent = value; }); };
        setText("[data-pdf-total]", data.total);
        setText("[data-pdf-waiting]", data.waiting);
        setText("[data-pdf-processing]", `${data.processing}/${data.max_workers}`);
        setText("[data-pdf-not-found]", data.not_found);
        setText("[data-pdf-failed]", data.failed);
        setText("[data-pdf-failed-count]", data.failed);
        setText("[data-pdf-successful]", data.successful);
        setText("[data-pdf-progress-detail]", `Đã tải: ${data.successful} · Không tìm thấy: ${data.not_found} · Lỗi: ${data.failed} · Đang chờ: ${data.waiting}`);
        setText("[data-pdf-percent]", `${data.percent}%`);
        panel.querySelectorAll("[data-pdf-progress-bar]").forEach((bar) => { bar.style.width = `${data.percent}%`; });
        panel.querySelectorAll("[data-pdf-retry-button]").forEach((button) => { button.disabled = data.failed === 0; });
        panel.querySelectorAll("[data-pdf-no-failed]").forEach((message) => { message.hidden = data.failed > 0; });
        if (data.status === "tam_dung" && data.processing === 0) {
          window.clearInterval(clock);
          window.clearInterval(poll);
          elapsedNodes.forEach((node) => { node.textContent = formatElapsed(data.elapsed_ms); });
          if (!initiallyPaused) window.location.reload();
        } else if (["hoan_thanh", "loi_mot_phan", "loi"].includes(data.status)) {
          window.clearInterval(clock);
          window.clearInterval(poll);
          elapsedNodes.forEach((node) => { node.textContent = formatElapsed(data.elapsed_ms); });
          window.location.reload();
        }
      })
      .catch(() => window.clearInterval(poll));
  }, 4000);
}

document.addEventListener("DOMContentLoaded", () => {
  bindConfirmations();
  bindLazyLoading();
  bindSidebar();
  bindPdfDownloaderTabs();
  bindAiSearch();
  bindSearchTimers();
  document.querySelectorAll("form[data-omit-empty-uuid]").forEach((form) => {
    form.addEventListener("submit", () => {
      form.querySelectorAll('select[name="lan_tra_cuu_ai"]').forEach((select) => {
        if (!select.value) select.disabled = true;
      });
    });
  });
  document.querySelectorAll("form[data-search-form]").forEach((form) => {
    form.addEventListener("submit", () => {
      const button = form.querySelector("button[type=submit]");
      if (button) {
        button.disabled = true;
        if (!button.dataset.searchClock) button.textContent = button.dataset.submitText || "Đang xử lý…";
      }
    });
  });
});
