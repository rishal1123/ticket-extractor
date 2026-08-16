/**
 * Dark mode toggle. base.html's inline <head> script already set the initial
 * data-bs-theme (stored choice, else OS preference) before first paint; this
 * just wires up the button and persists an explicit choice from here on.
 */
(function () {
    function currentTheme() {
        return document.documentElement.getAttribute('data-bs-theme') || 'light';
    }

    function applyTheme(theme) {
        document.documentElement.setAttribute('data-bs-theme', theme);
        localStorage.setItem('theme', theme);
        var icon = document.getElementById('themeToggleIcon');
        if (icon) {
            icon.classList.toggle('bi-moon-stars', theme === 'light');
            icon.classList.toggle('bi-sun', theme === 'dark');
        }
    }

    document.addEventListener('DOMContentLoaded', function () {
        // Sync the icon to whatever theme the flash-avoidance script already set.
        applyTheme(currentTheme());

        var btn = document.getElementById('themeToggleBtn');
        if (btn) {
            btn.addEventListener('click', function () {
                applyTheme(currentTheme() === 'dark' ? 'light' : 'dark');
            });
        }
    });
})();
