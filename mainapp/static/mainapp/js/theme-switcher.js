(function () {
    const STORAGE_KEY = 'mathllm-theme';
    const themeToggle = document.getElementById('theme-toggle');
    const themeMenu = document.getElementById('theme-menu');
    const themeCurrentLabel = document.getElementById('theme-current-label');
    const themeOptions = Array.from(document.querySelectorAll('[data-theme-option]'));
    const systemPrefersLight = window.matchMedia('(prefers-color-scheme: light)');

    function getStoredTheme() {
        try {
            return sessionStorage.getItem(STORAGE_KEY) || 'dark';
        } catch (error) {
            return 'dark';
        }
    }

    function labelForTheme(themeValue) {
        if (themeValue === 'system') {
            return 'System';
        }
        return themeValue.charAt(0).toUpperCase() + themeValue.slice(1);
    }

    function resolveTheme(themeValue) {
        if (themeValue === 'system') {
            return systemPrefersLight.matches ? 'light' : 'dark';
        }
        return themeValue === 'light' ? 'light' : 'dark';
    }

    function applyTheme(themeValue) {
        const normalizedTheme = ['dark', 'light', 'system'].includes(themeValue) ? themeValue : 'dark';
        const resolvedTheme = resolveTheme(normalizedTheme);

        document.body.classList.remove('theme-dark', 'theme-light');
        document.body.classList.add(resolvedTheme === 'light' ? 'theme-light' : 'theme-dark');

        if (themeCurrentLabel) {
            themeCurrentLabel.textContent = labelForTheme(normalizedTheme);
        }

        themeOptions.forEach((option) => {
            const isSelected = option.dataset.themeOption === normalizedTheme;
            option.classList.toggle('is-selected', isSelected);
            option.setAttribute('aria-checked', String(isSelected));
        });

        try {
            sessionStorage.setItem(STORAGE_KEY, normalizedTheme);
        } catch (error) {
            // Ignore session storage failures and keep the in-memory theme behavior.
        }
    }

    function toggleMenu(forceOpen) {
        if (!themeMenu || !themeToggle) {
            return;
        }

        const shouldOpen = typeof forceOpen === 'boolean'
            ? forceOpen
            : !themeMenu.classList.contains('is-open');
        themeMenu.classList.toggle('is-open', shouldOpen);
        themeToggle.setAttribute('aria-expanded', String(shouldOpen));
    }

    if (themeToggle) {
        themeToggle.addEventListener('click', (event) => {
            event.stopPropagation();
            toggleMenu();
        });
    }

    themeOptions.forEach((option) => {
        option.addEventListener('click', () => {
            applyTheme(option.dataset.themeOption);
            toggleMenu(false);
        });
    });

    document.addEventListener('click', (event) => {
        if (!themeMenu || !themeToggle) {
            return;
        }

        if (!themeMenu.contains(event.target) && !themeToggle.contains(event.target)) {
            toggleMenu(false);
        }
    });

    if (systemPrefersLight.addEventListener) {
        systemPrefersLight.addEventListener('change', () => {
            if (sessionStorage.getItem(STORAGE_KEY) === 'system') {
                applyTheme('system');
            }
        });
    } else if (systemPrefersLight.addListener) {
        systemPrefersLight.addListener(() => {
            if (sessionStorage.getItem(STORAGE_KEY) === 'system') {
                applyTheme('system');
            }
        });
    }

    applyTheme(getStoredTheme());
})();
