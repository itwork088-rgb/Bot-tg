"use strict";

const STORAGE_KEY = "ritm-habits-v1";
const DATE_KEY = getDateKey(new Date());
const DEFAULT_HABITS = [
  { id: "water", title: "Выпить стакан воды", icon: "◌", time: "Утренний ритуал" },
  { id: "stretch", title: "Немного размяться", icon: "↗", time: "Забота о себе" },
  { id: "read", title: "Почитать 10 минут", icon: "✳", time: "Время для себя" },
];

const telegram = window.Telegram?.WebApp;
const listElement = document.querySelector("#habit-list");
const emptyElement = document.querySelector("#empty-state");
const dialog = document.querySelector("#habit-dialog");
const titleInput = document.querySelector("#habit-title");
const toastElement = document.querySelector("#toast");
let selectedIcon = "✳";
let toastTimer;
let integrationStatusPromise;

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
  return { date: DATE_KEY, habits: DEFAULT_HABITS, done: [], history: {} };
}

let state = readState();

function saveState() {
  try {
    state.history[DATE_KEY] = [...state.done];
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

function renderHabits() {
  const completed = new Set(state.done);
  listElement.innerHTML = state.habits.map((habit) => {
    const done = completed.has(habit.id);
    return `
      <article class="habit-row${done ? " is-done" : ""}">
        <div class="habit-left">
          <span class="habit-icon" aria-hidden="true">${escapeHtml(habit.icon)}</span>
          <div class="habit-copy">
            <p class="habit-title">${escapeHtml(habit.title)}</p>
            <p class="habit-subtitle">${done ? "Отлично, так держать" : escapeHtml(habit.time || "Маленький шаг")}</p>
          </div>
        </div>
        <div class="habit-actions">
          <button class="check-button" type="button" data-action="toggle" data-id="${escapeHtml(habit.id)}" aria-label="${done ? "Снять отметку" : "Отметить выполненной"}" aria-pressed="${done}">${done ? "✓" : "✓"}</button>
          <button class="delete-button" type="button" data-action="delete" data-id="${escapeHtml(habit.id)}" aria-label="Удалить привычку">×</button>
        </div>
      </article>`;
  }).join("");
  emptyElement.hidden = state.habits.length > 0;
  listElement.hidden = state.habits.length === 0;
  renderProgress();
}

function renderProgress() {
  const total = state.habits.length;
  const done = state.habits.filter((habit) => state.done.includes(habit.id)).length;
  const percent = total ? Math.round((done / total) * 100) : 0;
  document.querySelector("#done-count").textContent = String(done);
  document.querySelector("#total-count").textContent = String(total);
  document.querySelector("#progress-percent").textContent = `${percent}%`;
  document.querySelector("#progress-fill").style.width = `${percent}%`;
  const progressTrack = document.querySelector("#progress-track");
  progressTrack.setAttribute("aria-valuenow", String(percent));
  document.querySelector("#progress-message").textContent = total === 0
    ? "Добавь первую привычку, чтобы задать свой ритм."
    : done === total
      ? "Все дела на сегодня выполнены. Ты молодец!"
      : done === 0
        ? "Начни с одной привычки — этого достаточно."
        : "Хороший темп. Продолжай в своём ритме.";
  renderStreak();
}

function renderWeek() {
  const names = new Intl.DateTimeFormat("ru-RU", { weekday: "short" });
  const days = Array.from({ length: 7 }, (_, index) => {
    const date = new Date();
    date.setDate(date.getDate() - (6 - index));
    const key = getDateKey(date);
    return { key, label: names.format(date).replace(".", ""), isToday: index === 6 };
  });
  document.querySelector("#week-grid").innerHTML = days.map((day) => {
    const count = (state.history[day.key] || []).length;
    return `<div class="day-cell${count ? " has-done" : ""}${day.isToday ? " is-today" : ""}" aria-label="${day.label}: ${count} выполнено">
      <span class="day-dot" aria-hidden="true">${count ? "✓" : "·"}</span><span>${day.label}</span>
    </div>`;
  }).join("");
  const weekTotal = days.reduce((total, day) => total + (state.history[day.key] || []).length, 0);
  document.querySelector("#week-total").textContent = `${weekTotal} выполнено`;
}

function renderStreak() {
  let streak = 0;
  const date = new Date();
  while (state.history[getDateKey(date)]?.length) {
    streak += 1;
    date.setDate(date.getDate() - 1);
  }
  document.querySelector("#streak-count").textContent = String(streak);
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
  const greetingName = telegram?.initDataUnsafe?.user?.first_name;
  document.querySelector("#avatar-letter").textContent = greetingName?.[0]?.toLocaleUpperCase("ru-RU") || "А";
}

function showToast(message) {
  toastElement.textContent = message;
  toastElement.classList.add("is-visible");
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => toastElement.classList.remove("is-visible"), 2800);
}

async function sendToMake(eventType, habit) {
  try {
    if (!integrationStatusPromise) {
      integrationStatusPromise = fetch("/api/status")
        .then((response) => response.ok ? response.json() : null)
        .catch(() => null);
    }
    const integrationStatus = await integrationStatusPromise;
    if (!integrationStatus?.telegramAuthConfigured || !integrationStatus.makeConfigured) return false;

    const response = await fetch("/api/events", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Telegram-Init-Data": telegram?.initData || "",
      },
      body: JSON.stringify({ event: eventType, habit }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.error || `Сервер ответил ${response.status}`);
    }
    return true;
  } catch (error) {
    console.info("Событие сохранено локально; Make.com пока не подключён.", error);
    return false;
  }
}

function openDialog() {
  dialog.showModal();
  window.setTimeout(() => titleInput.focus(), 0);
}

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

document.querySelector("#habit-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const title = titleInput.value.trim();
  if (!title) return;
  const habit = { id: crypto.randomUUID(), title, icon: selectedIcon, time: "Новая привычка" };
  state.habits.push(habit);
  saveState();
  renderHabits();
  dialog.close();
  titleInput.value = "";
  showToast("Привычка добавлена. Ты справишься!");
  if (!(await sendToMake("habit.created", habit))) {
    showToast("Добавлено на устройстве. Подключи Make.com для синхронизации.");
  }
});

listElement.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const habit = state.habits.find((item) => item.id === button.dataset.id);
  if (!habit) return;
  if (button.dataset.action === "delete") {
    state.habits = state.habits.filter((item) => item.id !== habit.id);
    state.done = state.done.filter((id) => id !== habit.id);
    saveState();
    renderHabits();
    renderWeek();
    showToast("Привычка удалена.");
    if (!(await sendToMake("habit.deleted", habit))) {
      showToast("Удалено на устройстве. Make.com пока не подключён.");
    }
    return;
  }
  let eventType;
  if (state.done.includes(habit.id)) {
    state.done = state.done.filter((id) => id !== habit.id);
    eventType = "habit.uncompleted";
    showToast("Отметку можно изменить в любой момент.");
  } else {
    state.done.push(habit.id);
    eventType = "habit.completed";
    showToast("Отлично! Ещё один шаг сделан ✳");
  }
  saveState();
  renderHabits();
  renderWeek();
  if (!(await sendToMake(eventType, habit))) {
    showToast("Изменение сохранено. Make.com пока не подключён.");
  }
});

document.querySelector("#profile-button").addEventListener("click", () => {
  showToast("Твой профиль и настройки скоро появятся здесь.");
});

renderDate();
renderHabits();
renderWeek();

if (telegram) {
  telegram.ready();
  telegram.expand();
  if (telegram.isVersionAtLeast?.("6.1")) {
    telegram.setHeaderColor("#101114");
    telegram.setBackgroundColor("#101114");
  }
}
