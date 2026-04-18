export const THEME_STORAGE_KEY = "yard_dashboard_theme";
const DEFAULT_THEME = "dark";

function validTheme(theme) {
  return theme === "dark" || theme === "light";
}

function applyTheme(theme) {
  const resolved = validTheme(theme) ? theme : DEFAULT_THEME;
  document.documentElement.setAttribute("data-theme", resolved);
  return resolved;
}

function nextTheme(theme) {
  return theme === "dark" ? "light" : "dark";
}

function toggleLabel(theme) {
  return theme === "dark" ? "Light Theme" : "Dark Theme";
}

export function getStoredTheme() {
  const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
  return validTheme(stored) ? stored : DEFAULT_THEME;
}

export function initThemeToggle(buttonElement) {
  let currentTheme = applyTheme(getStoredTheme());
  buttonElement.textContent = toggleLabel(currentTheme);

  buttonElement.addEventListener("click", () => {
    currentTheme = nextTheme(currentTheme);
    const applied = applyTheme(currentTheme);
    window.localStorage.setItem(THEME_STORAGE_KEY, applied);
    buttonElement.textContent = toggleLabel(applied);
  });
}
