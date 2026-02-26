// ui/refresh_scheduler.js

let _intervalId = null;

export function scheduleRefreshTasks(tasks = [], intervalMs = 2000) {
  if (_intervalId) {
    clearInterval(_intervalId);
    _intervalId = null;
  }

  if (!Array.isArray(tasks) || tasks.length === 0) return;

  _intervalId = setInterval(() => {
    for (const fn of tasks) {
      try {
        if (typeof fn === "function") {
          fn();
        }
      } catch (e) {
        console.error("refresh task error:", e);
      }
    }
  }, intervalMs);
}

export function stopRefreshTasks() {
  if (_intervalId) {
    clearInterval(_intervalId);
    _intervalId = null;
  }
}