/**
 * Kiosk mode: hides the navbar for an unattended wall-mounted monitor.
 * base.html's inline <head> script already applied the 'kiosk-mode' class
 * (from a stored choice or a ?kiosk=1/0 URL override) before first paint;
 * this just wires up the hide button and the reveal tab that appears once
 * the navbar is hidden.
 */
(function () {
    function setKiosk(on) {
        document.documentElement.classList.toggle('kiosk-mode', on);
        localStorage.setItem('kioskMode', on ? 'true' : 'false');
    }

    document.addEventListener('DOMContentLoaded', function () {
        var hideBtn = document.getElementById('kioskToggleBtn');
        if (hideBtn) {
            hideBtn.addEventListener('click', function () { setKiosk(true); });
        }
        var revealBtn = document.getElementById('kioskRevealBtn');
        if (revealBtn) {
            revealBtn.addEventListener('click', function () { setKiosk(false); });
        }
    });
})();
