"use strict";

const STORAGE_KEY = "ritm-habits-v1";
const DATE_KEY = getDateKey(new Date());
const MAX_HABITS = 100;

const telegram = window.Telegram?.WebApp;
const listElement = document.querySelector("#habit-list");
const homeListElement = document.querySelector("#home-habit-list");
const emptyElement = document.querySelector("#empty-state");
const homeEmptyElement = document.querySelector("#home-empty-state");
const dialog = document.querySelector("#habit-dialog");
const confirmDialog = document.querySelector("#confirm-dialog");
const titleInput = document.querySelector("#habit-title");
const timeInput = document.querySelector("#habit-time");
const toastElement = document.querySelector("#toast");
const syncIndicator = document.querySelector("#sync-indicator");
let selectedIcon = "✳";
let selectedHistoryDate = DATE_KEY;
let toastTimer;
let cloudRefreshPromise;
let localRevision = 0;
let lastLocalMutationAt = 0;
let cloudConnected = false;
let cloudLoading = false;

function readState() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
    if (saved && Array.isArray(saved.habits)) {
      const history = saved.history && typeof saved.history === "object" ? saved.history : {};
      if (saved.date === DATE_KEY && !history[DATE_KEY] && Array.isArray(saved.done)) {
        history[DATE_KEY] = saved.done;
      }
      return {
        ...saved,
        date: DATE_KEY,
        done: saved.date === DATE_KEY && Array.isArray(saved.done) ? saved.done : [],
        history,
      };
    }
  } catch (error) {
    console.warn("Не удалось прочитать сохранённые привычки.", error);
  }
  return { date: DATE_KEY, habits: [], done: [], history: {} };
}

let state = readState();

function saveState() {
  try {
    if (!Array.isArray(state.history[DATE_KEY])) state.history[DATE_KEY] = [...state.done];
    const oldestDate = new Date();
    oldestDate.setDate(oldestDate.getDate() - 59);
    const oldestKey = getDateKey(oldestDate);
    Object.keys(state.history).forEach((date) => {
      if (date < oldestKey) delete state.history[date];
    });
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch (error) {
    console.warn("Не удалось сохранить привычки в этом браузере.", error);
    showToast("Не получилось сохранить изменения на этом устройстве.");
  }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

function renderHabitList(target, habits) {
  const completed = new Set(state.done);
  target.innerHTML = habits.map((habit) => {
    const done = completed.has(habit.id);
    const id = escapeHtml(habit.id);
    return `
      <article class="habit-row${done ? " is-done" : ""}">
        <div class="habit-left">
          <span class="habit-icon" aria-hidden="true">${escapeHtml(habit.icon || "✳")}</span>
          <div class="habit-copy">
            <p class="habit-title">${escapeHtml(habit.title)}</p>
            <p class="habit-subtitle">${done ? "Отлично, так держать" : escapeHtml(habit.time || "Маленький шаг")}</p>
          </div>
        </div>
        <div class="habit-actions">
          <button class="check-button" type="button" data-action="toggle" data-id="${id}" aria-label="${done ? "Снять отметку" : "Отметить выполненной"}" aria-pressed="${done}">✓</button>
          <button class="delete-button" type="button" data-action="delete" data-id="${id}" aria-label="Удалить привычку «${escapeHtml(habit.title)}»">×</button>
        </div>
      </article>`;
  }).join("");
}

function renderHabits() {
  renderHabitList(listElement, state.habits);
  renderHabitList(homeListElement, state.habits.slice(0, 3));
  emptyElement.hidden = state.habits.length > 0 || cloudLoading;
  homeEmptyElement.hidden = state.habits.length > 0 || cloudLoading;
  document.querySelector("#loading-state").hidden = !cloudLoading;
  listElement.hidden = state.habits.length === 0;
  homeListElement.hidden = state.habits.length === 0;
  renderProgress();
  renderStats();
}

function renderProgress() {
  const total = state.habits.length;
  const done = state.habits.filter((habit) => state.done.includes(habit.id)).length;
  const percent = total ? Math.round((done / total) * 100) : 0;
  document.querySelector("#done-count").textContent = String(done);
  document.querySelector("#total-count").textContent = String(total);
  document.querySelector("#remaining-count").textContent = String(total - done);
  document.querySelector("#progress-percent").textContent = `${percent}%`;
  const ring = document.querySelector("#progress-ring");
  ring.style.setProperty("--progress", `${percent}%`);
  ring.setAttribute("aria-valuenow", String(percent));
  document.querySelector("#progress-message").textContent = total === 0
    ? "Добавь первую привычку, чтобы задать свой ритм."
    : done === total
      ? "Все дела на сегодня выполнены. Ты молодец!"
      : done === 0
        ? "Начни с одной привычки — этого достаточно."
        : "Хороший темп. Продолжай в своём ритме.";
}

function getActivityCount(dateKey) {
  return Array.isArray(state.history[dateKey]) ? state.history[dateKey].length : 0;
}

function pluralize(number, forms) {
  const value = Math.abs(number) % 100;
  const lastDigit = value % 10;
  if (value > 10 && value < 20) return forms[2];
  if (lastDigit > 1 && lastDigit < 5) return forms[1];
  if (lastDigit === 1) return forms[0];
  return forms[2];
}

function getStreak() {
  const date = new Date();
  if (!getActivityCount(getDateKey(date))) date.setDate(date.getDate() - 1);
  let streak = 0;
  while (getActivityCount(getDateKey(date))) {
    streak += 1;
    date.setDate(date.getDate() - 1);
  }
  return streak;
}

function renderWeek() {
  const names = new Intl.DateTimeFormat("ru-RU", { weekday: "short" });
  const days = Array.from({ length: 7 }, (_, index) => {
    const date = new Date();
    date.setDate(date.getDate() - (6 - index));
    const key = getDateKey(date);
    return { key, label: names.format(date).replace(".", ""), isToday: key === DATE_KEY };
  });
  document.querySelector("#week-grid").innerHTML = days.map((day) => {
    const count = getActivityCount(day.key);
    const countLabel = pluralize(count, ["выполнение", "выполнения", "выполнений"]);
    return `<div class="day-cell${count ? " has-done" : ""}${day.isToday ? " is-today" : ""}" aria-label="${day.label}: ${count} ${countLabel}">
      <span class="day-dot" aria-hidden="true">${count ? "✓" : "·"}</span><span>${day.label}</span>
    </div>`;
  }).join("");
  const weekTotal = days.reduce((total, day) => total + getActivityCount(day.key), 0);
  document.querySelector("#week-total").textContent = `${weekTotal} ${pluralize(weekTotal, ["выполнение", "выполнения", "выполнений"])}`;
  document.querySelector("#home-week-total").textContent = String(weekTotal);
}

function renderCalendar() {
  const firstDate = new Date();
  firstDate.setDate(firstDate.getDate() - 27);
  const leadingCells = (firstDate.getDay() + 6) % 7;
  const cells = Array.from({ length: leadingCells }, () => '<span aria-hidden="true"></span>');
  for (let offset = 0; offset < 28; offset += 1) {
    const date = new Date(firstDate);
    date.setDate(firstDate.getDate() + offset);
    const key = getDateKey(date);
    const count = getActivityCount(key);
    const classes = [
      "calendar-cell",
      count ? "has-done" : "",
      key === DATE_KEY ? "is-today" : "",
      key === selectedHistoryDate ? "is-selected" : "",
    ].filter(Boolean).join(" ");
    const countLabel = pluralize(count, ["выполнение", "выполнения", "выполнений"]);
    cells.push(`<button class="${classes}" type="button" data-history-date="${key}" aria-label="${escapeHtml(new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long" }).format(date))}: ${count} ${countLabel}">${date.getDate()}</button>`);
  }
  document.querySelector("#calendar-grid").innerHTML = cells.join("");
  renderHistoryDetail(selectedHistoryDate);
}

function renderHistoryDetail(dateKey) {
  const date = new Date(`${dateKey}T12:00:00`);
  const title = new Intl.DateTimeFormat("ru-RU", { weekday: "long", day: "numeric", month: "long" }).format(date);
  const ids = state.history[dateKey] || [];
  const habits = ids.map((id) => state.habits.find((habit) => habit.id === id));
  const labels = habits.map((habit) => habit ? habit.title : "Привычка удалена");
  const content = labels.length
    ? `<div class="history-items">${labels.map((label) => `<span class="history-chip">${escapeHtml(label)}</span>`).join("")}</div>`
    : '<p class="history-empty">В этот день пока нет отмеченных привычек.</p>';
  document.querySelector("#history-detail").innerHTML = `<p class="history-date">${escapeHtml(title)}</p>${content}`;
}

function renderStats() {
  const todayCount = state.habits.filter((habit) => state.done.includes(habit.id)).length;
  const weekCount = Array.from({ length: 7 }, (_, offset) => {
    const date = new Date();
    date.setDate(date.getDate() - offset);
    return getActivityCount(getDateKey(date));
  }).reduce((sum, count) => sum + count, 0);
  const totalCount = Object.values(state.history).reduce((sum, ids) => sum + (Array.isArray(ids) ? ids.length : 0), 0);
  const streak = getStreak();
  document.querySelector("#stats-today").textContent = String(todayCount);
  document.querySelector("#stats-today-note").textContent = `из ${state.habits.length} привычек`;
  document.querySelector("#stats-week").textContent = String(weekCount);
  document.querySelector("#stats-total").textContent = String(totalCount);
  document.querySelector("#streak-count").textContent = String(streak);
  document.querySelector("#streak-unit").textContent = pluralize(streak, [" день", " дня", " дней"]);
  document.querySelector("#home-streak").textContent = String(streak);
  document.querySelector("#home-streak-unit").textContent = `${pluralize(streak, ["день", "дня", "дней"])} ритма`;
  renderWeek();
  renderCalendar();
}

function getDateKey(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function renderDate() {
  const now = new Date();
  document.querySelector("#today-label").textContent = new Intl.DateTimeFormat("ru-RU", {
    weekday: "long", day: "numeric", month: "long",
  }).format(now).toUpperCase();
  const user = telegram?.initDataUnsafe?.user;
  const name = [user?.first_name, user?.last_name].filter(Boolean).join(" ").trim();
  const firstName = user?.first_name?.trim();
  document.querySelector("#greeting-name").textContent = firstName ? `, ${firstName}` : "";
  document.querySelector("#avatar-letter").textContent = firstName?.[0]?.toLocaleUpperCase("ru-RU") || "Р";
  document.querySelector("#profile-avatar").textContent = firstName?.[0]?.toLocaleUpperCase("ru-RU") || "Р";
  document.querySelector("#profile-name").textContent = name || "Личный профиль";
  document.querySelector("#profile-subtitle").textContent = telegram?.initData
    ? "Аккаунт Telegram"
    : "Локальный профиль";
}

function updateSyncIndicator(loading = false) {
  syncIndicator.classList.toggle("is-cloud", cloudConnected && !loading);
  syncIndicator.classList.toggle("is-loading", loading);
  document.querySelector("#sync-label").textContent = loading
    ? "Синхронизация…"
    : cloudConnected ? "Облако подключено" : "На устройстве";
  document.querySelector("#profile-storage").textContent = cloudConnected
    ? "Облако и устройство"
    : "На устройстве";
  document.querySelector("#profile-cloud").textContent = !telegram?.initData
    ? "Открой приложение в Telegram"
    : cloudConnected ? "Подключена" : loading ? "Синхронизация…" : "Нет соединения";
}

function showToast(message) {
  toastElement.textContent = message;
  toastElement.classList.add("is-visible");
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => toastElement.classList.remove("is-visible"), 2800);
}

async function requestApi(path, method = "GET", payload) {
  const options = { method, headers: {} };
  if (telegram?.initData) options.headers["X-Telegram-Init-Data"] = telegram.initData;
  if (payload !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(payload);
  }
  const response = await fetch(path, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.error || `Сервер ответил ${response.status}`);
    error.saved = body.saved === true;
    throw error;
  }
  return body;
}

function applyCloudState(cloudState) {
  state = {
    date: DATE_KEY,
    habits: cloudState.habits,
    done: cloudState.done,
    history: cloudState.history,
  };
  cloudConnected = true;
  saveState();
  renderHabits();
  updateSyncIndicator();
}

async function refreshCloudState(showError = false) {
  if (!telegram?.initData || cloudRefreshPromise || Date.now() - lastLocalMutationAt < 5000) return;
  const revision = localRevision;
  cloudLoading = true;
  renderHabits();
  updateSyncIndicator(true);
  cloudRefreshPromise = (async () => {
    try {
      let cloudState = await requestApi(`/api/habits?date=${encodeURIComponent(DATE_KEY)}`);
      if (!cloudState.initialized) {
        await requestApi("/api/bootstrap", "POST", {
          habits: state.habits,
          history: state.history,
        });
        cloudState = await requestApi(`/api/habits?date=${encodeURIComponent(DATE_KEY)}`);
      }
      if (revision !== localRevision) return;
      applyCloudState(cloudState);
    } catch (error) {
      console.warn("Не удалось синхронизировать привычки с сервером.", error);
      cloudConnected = false;
      updateSyncIndicator();
      if (showError) showToast("Не удалось загрузить привычки из облака. Пока показаны данные устройства.");
    }
  })();
  await cloudRefreshPromise;
  cloudRefreshPromise = null;
  cloudLoading = false;
  renderHabits();
  updateSyncIndicator();
}

async function syncHabitEvent(eventType, habit) {
  if (!telegram?.initData) return { localOnly: true };
  try {
    const result = await requestApi("/api/events", "POST", {
      event: eventType,
      habit,
      date: DATE_KEY,
    });
    cloudConnected = true;
    updateSyncIndicator();
    return {
      saved: true,
      makeConfigured: result.makeConfigured,
      queuedForMake: result.queuedForMake,
    };
  } catch (error) {
    console.warn("Не удалось синхронизировать изменение привычки.", error);
    cloudConnected = false;
    updateSyncIndicator();
    return { saved: error.saved, localOnly: !error.saved };
  }
}

function reportSyncResult(result) {
  if (result.localOnly && telegram?.initData) {
    showToast("Не удалось подтвердить синхронизацию. Данные на устройстве сохранены.");
  } else if (result.saved && !result.makeConfigured) {
    showToast("Привычки сохранены в облаке. Проверь настройку вебхука Make.com.");
  }
}

function openDialog() {
  if (state.habits.length >= MAX_HABITS) {
    showToast(`Можно создать не больше ${MAX_HABITS} привычек.`);
    return;
  }
  dialog.showModal();
  window.setTimeout(() => titleInput.focus(), 0);
}

function confirmDelete(habit) {
  document.querySelector("#confirm-copy").textContent = `«${habit.title}» будет удалена из твоего списка. История её выполнений сохранится.`;
  return new Promise((resolve) => {
    confirmDialog.addEventListener("close", () => resolve(confirmDialog.returnValue === "delete"), { once: true });
    confirmDialog.showModal();
  });
}

function setActiveTab(tab) {
  document.querySelectorAll("[data-page]").forEach((page) => {
    const active = page.dataset.page === tab;
    page.hidden = !active;
    page.classList.toggle("is-active", active);
  });
  document.querySelectorAll(".nav-item").forEach((item) => {
    const active = item.dataset.tab === tab;
    item.classList.toggle("is-active", active);
    if (active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  window.scrollTo({ top: 0, behavior: "smooth" });
}

document.querySelectorAll("[data-tab]").forEach((control) => {
  control.addEventListener("click", (event) => {
    event.preventDefault();
    setActiveTab(control.dataset.tab);
  });
});

document.querySelectorAll('[data-action="add"]').forEach((button) => button.addEventListener("click", openDialog));
document.querySelector("#add-habit-button").addEventListener("click", openDialog);
document.querySelector("#empty-add-button").addEventListener("click", openDialog);
document.querySelector("#close-dialog-button").addEventListener("click", () => dialog.close());
dialog.addEventListener("click", (event) => {
  if (event.target === dialog) dialog.close();
});

document.querySelector("#icon-options").addEventListener("click", (event) => {
  const button = event.target.closest("[data-icon]");
  if (!button) return;
  selectedIcon = button.dataset.icon;
  document.querySelectorAll(".icon-option").forEach((option) => {
    const selected = option === button;
    option.classList.toggle("is-selected", selected);
    option.setAttribute("aria-pressed", String(selected));
  });
});

titleInput.addEventListener("input", () => titleInput.setCustomValidity(""));
document.querySelector("#habit-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const title = titleInput.value.trim();
  if (!title) {
    titleInput.setCustomValidity("Введи название привычки.");
    titleInput.reportValidity();
    return;
  }
  if (state.habits.length >= MAX_HABITS) {
    showToast(`Можно создать не больше ${MAX_HABITS} привычек.`);
    return;
  }
  const habit = {
    id: crypto.randomUUID(),
    title,
    icon: selectedIcon,
    time: timeInput.value.trim() || "Маленький шаг",
  };
  localRevision += 1;
  lastLocalMutationAt = Date.now();
  state.habits.push(habit);
  saveState();
  renderHabits();
  dialog.close();
  document.querySelector("#habit-form").reset();
  timeInput.value = "";
  selectedIcon = "✳";
  document.querySelectorAll(".icon-option").forEach((option) => {
    const selected = option.dataset.icon === selectedIcon;
    option.classList.toggle("is-selected", selected);
    option.setAttribute("aria-pressed", String(selected));
  });
  showToast("Привычка добавлена. Ты справишься!");
  reportSyncResult(await syncHabitEvent("habit.created", habit));
});

document.querySelector("#calendar-grid").addEventListener("click", (event) => {
  const button = event.target.closest("[data-history-date]");
  if (!button) return;
  selectedHistoryDate = button.dataset.historyDate;
  renderCalendar();
});

async function handleHabitAction(event) {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const habit = state.habits.find((item) => item.id === button.dataset.id);
  if (!habit) return;
  if (button.dataset.action === "delete") {
    if (!await confirmDelete(habit)) return;
    localRevision += 1;
    lastLocalMutationAt = Date.now();
    state.habits = state.habits.filter((item) => item.id !== habit.id);
    state.done = state.done.filter((id) => id !== habit.id);
    saveState();
    renderHabits();
    showToast("Привычка удалена. История сохранена.");
    reportSyncResult(await syncHabitEvent("habit.deleted", habit));
    return;
  }

  const isDone = state.done.includes(habit.id);
  state.done = isDone
    ? state.done.filter((id) => id !== habit.id)
    : [...state.done, habit.id];
  const archivedCompletions = (state.history[DATE_KEY] || [])
    .filter((id) => !state.habits.some((item) => item.id === id));
  state.history[DATE_KEY] = [...new Set([...archivedCompletions, ...state.done])];
  const eventType = isDone ? "habit.uncompleted" : "habit.completed";
  localRevision += 1;
  lastLocalMutationAt = Date.now();
  saveState();
  renderHabits();
  showToast(isDone ? "Отметку можно изменить в любой момент." : "Отлично! Ещё один шаг сделан ✳");
  if (telegram?.isVersionAtLeast?.("6.1") && telegram.HapticFeedback?.impactOccurred) {
    telegram.HapticFeedback.impactOccurred("light");
  }
  reportSyncResult(await syncHabitEvent(eventType, habit));
}

listElement.addEventListener("click", handleHabitAction);
homeListElement.addEventListener("click", handleHabitAction);

async function refreshServiceStatus() {
  try {
    const status = await requestApi("/api/status");
    document.querySelector("#profile-make").textContent = status.makeConfigured ? "Подключена" : "Не настроена";
  } catch (error) {
    console.warn("Не удалось загрузить состояние интеграций.", error);
    document.querySelector("#profile-make").textContent = "Недоступно";
  }
}

renderDate();
renderHabits();
updateSyncIndicator();
refreshCloudState(true);
refreshServiceStatus();
window.setInterval(() => refreshCloudState(), 30_000);
window.addEventListener("focus", () => refreshCloudState());
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshCloudState();
});

if (telegram) {
  telegram.ready();
  telegram.expand();
  if (telegram.isVersionAtLeast?.("6.1")) {
    telegram.setHeaderColor("#f5f6fb");
    telegram.setBackgroundColor("#f5f6fb");
  }
}
