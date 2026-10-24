(() => {
    const activeControllers = new Set();

    function cancelPendingRequests() {
        for (const controller of activeControllers) {
            controller.abort();
        }
        activeControllers.clear();
    }

    function begin() {
        const controller = new AbortController();
        activeControllers.add(controller);
        let finished = false;

        return {
            signal: controller.signal,
            finish() {
                if (!finished) {
                    activeControllers.delete(controller);
                    finished = true;
                }
            },
        };
    }

    document.addEventListener('click', (event) => {
        if (event.target instanceof Element && event.target.closest('[data-cancel-pending-requests]')) {
            cancelPendingRequests();
        }
    }, true);
    window.addEventListener('pagehide', cancelPendingRequests);

    window.mathLlmRequests = {
        begin,
        cancel: cancelPendingRequests,
    };
})();
