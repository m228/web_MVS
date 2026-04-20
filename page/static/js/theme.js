(function () {
  const STORAGE_KEY = 'ma-app-theme';
  const THEMES = [
    { id: 'light', label: 'Светлый' },
    { id: 'navy', label: 'Темно-синий' },
    { id: 'dark', label: 'Темный' },
  ];

  function normalizeTheme(theme) {
    return THEMES.some((item) => item.id === theme) ? theme : 'light';
  }

  function getSavedTheme() {
    try {
      return normalizeTheme(localStorage.getItem(STORAGE_KEY));
    } catch (error) {
      return 'light';
    }
  }

  function getThemeMeta(theme) {
    return THEMES.find((item) => item.id === theme) || THEMES[0];
  }

  function getCurrentTheme() {
    return normalizeTheme(document.documentElement.getAttribute('data-theme') || getSavedTheme());
  }

  function syncThemeSwitchers(theme = getCurrentTheme()) {
    document.querySelectorAll('[data-theme-switcher]').forEach((container) => {
      const buttons = container.querySelectorAll('.theme-option');

      if (!buttons.length) {
        buildThemeSwitcher(container);
        return;
      }

      buttons.forEach((button) => {
        const isActive = button.dataset.themeValue === theme;
        button.classList.toggle('is-active', isActive);
        button.setAttribute('aria-pressed', String(isActive));
      });
    });
  }

  function syncThemeLogos(theme = getCurrentTheme()) {
    const useDarkLogo = theme === 'navy' || theme === 'dark';

    document.querySelectorAll('[data-theme-logo]').forEach((img) => {
      const lightLogo = img.dataset.logoLight;
      const darkLogo = img.dataset.logoDark;

      if (!lightLogo || !darkLogo) return;

      img.src = useDarkLogo ? darkLogo : lightLogo;
    });
  }

  function applyTheme(theme) {
    const safeTheme = normalizeTheme(theme);

    document.documentElement.setAttribute('data-theme', safeTheme);
    document.documentElement.style.colorScheme = safeTheme === 'light' ? 'light' : 'dark';

    try {
      localStorage.setItem(STORAGE_KEY, safeTheme);
    } catch (error) {
      console.warn('Не удалось сохранить тему интерфейса', error);
    }

    document.dispatchEvent(
      new CustomEvent('app-theme-change', {
        detail: {
          theme: safeTheme,
          meta: getThemeMeta(safeTheme),
        },
      })
    );

    syncThemeSwitchers(safeTheme);
    syncThemeLogos(safeTheme);
    return safeTheme;
  }

  function createThemeButton(theme, currentTheme) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'theme-option';
    button.dataset.themeValue = theme.id;
    button.textContent = theme.label;
    button.setAttribute('aria-pressed', currentTheme === theme.id ? 'true' : 'false');

    if (currentTheme === theme.id) {
      button.classList.add('is-active');
    }

    button.addEventListener('click', () => {
      applyTheme(theme.id);
    });

    return button;
  }

  function buildThemeSwitcher(container) {
    if (!container) return;

    const activeTheme = getCurrentTheme();
    container.innerHTML = '';

    THEMES.forEach((theme) => {
      container.appendChild(createThemeButton(theme, activeTheme));
    });
  }

  function initThemeControls() {
    const currentTheme = applyTheme(getSavedTheme());

    document.querySelectorAll('[data-theme-switcher]').forEach((container) => {
      buildThemeSwitcher(container);
    });

    syncThemeSwitchers(currentTheme);
    syncThemeLogos(currentTheme);
  }

  applyTheme(getSavedTheme());

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initThemeControls);
  } else {
    initThemeControls();
  }

  window.AppTheme = {
    applyTheme,
    getCurrentTheme,
    themes: THEMES.slice(),
  };
})();
