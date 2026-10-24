// Cache the form, endpoint, and page elements used throughout the solver workflow.
const form = document.getElementById('solver-form');
const solverEndpoint = form ? form.action : '';
const formPanel = document.getElementById('feature-form-panel') || document.getElementById('solver-form-panel');
const loadingScreen = document.getElementById('feature-loading-screen') || document.getElementById('loading-screen');
const loadingTitle = document.getElementById('loading-title');
const loadingMessage = document.getElementById('loading-message');
const imageOutputPanel = document.getElementById('image-output-panel');
const imageOutput = document.getElementById('image-output');
const solutionOutputPanel = document.getElementById('solution-output-panel');
const solutionOutput = document.getElementById('solution-output');
const resultPanel = document.getElementById('feature-result-panel') || document.getElementById('result-panel');
const resultText = document.getElementById('result-text');
const resultTitle = document.getElementById('result-title');
const pdfViewer = document.getElementById('feature-pdf-viewer') || document.getElementById('pdf-viewer');
const statusPanel = document.getElementById('feature-status-panel') || document.getElementById('status-panel');
const statusLog = document.getElementById('status-log');
const statusPill = document.getElementById('status-pill');
const errorBanner = document.getElementById('feature-error-banner') || document.getElementById('error-banner');
const csrfToken = document.querySelector('[name=csrfmiddlewaretoken]')?.value || '';
let progressEntry = null;

const loadingStates = {
    reading: {
        title: 'Reading the image...',
        message: 'We are extracting the math problem from your image.',
    },
    solving: {
        title: 'Solving the problem...',
        message: 'The model is working through the solution step by step.',
    },
    generatingPdf: {
        title: 'Generating your PDF...',
        message: 'The solution is complete. We are formatting it into a PDF.',
    },
};

function setLoadingState(state) {
    const current = loadingStates[state] || loadingStates.reading;
    loadingTitle.textContent = current.title;
    loadingMessage.textContent = current.message;
}

function setHidden(element, isHidden) {
    element.classList.toggle('hidden', isHidden);
}

function clearStatusLog() {
    statusLog.innerHTML = '';
    progressEntry = null;
}

function appendStatus(text) {
    if (!text || !text.trim()) {
        return;
    }

    createStatusEntry(text.trim());
}

function updateProgressStatus(text) {
    if (!text) {
        return;
    }

    if (!progressEntry) {
        progressEntry = createStatusEntry(text, 'status-progress');
    } else {
        progressEntry.lastChild.textContent = ` ${text}`;
    }
    statusLog.scrollTop = statusLog.scrollHeight;
    loadingMessage.textContent = text;
}

function createStatusEntry(label, className = '') {
    const entry = document.createElement('div');
    entry.className = `status-entry ${className}`.trim();
    const timestamp = document.createElement('time');
    const now = new Date();
    timestamp.className = 'status-time';
    timestamp.dateTime = now.toISOString();
    timestamp.textContent = now.toLocaleTimeString([], {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
    });
    entry.append(timestamp, document.createTextNode(` ${label}`));
    statusLog.appendChild(entry);
    statusLog.scrollTop = statusLog.scrollHeight;
    return entry;
}

function setStatusPill(label) {
    statusPill.textContent = label;
}

function showStatusPanel() {
    setHidden(statusPanel, false);
}

function hideStatusPanel() {
    setHidden(statusPanel, true);
}

function hideErrorBanner() {
    errorBanner.textContent = '';
    setHidden(errorBanner, true);
}

function showErrorBanner(message) {
    errorBanner.textContent = message;
    setHidden(errorBanner, false);
}

function resetResultDisplay() {
    resultText.textContent = '';
    setHidden(resultText, false);
    setHidden(pdfViewer, true);
    pdfViewer.removeAttribute('src');
    resultTitle.textContent = 'LLM response';
    clearStatusLog();
    imageOutput.textContent = '';
    solutionOutput.textContent = '';
    setHidden(imageOutputPanel, true);
    setHidden(solutionOutputPanel, true);
    hideStatusPanel();
    hideErrorBanner();
}

function showResult() {
    setHidden(loadingScreen, true);
    setHidden(resultPanel, false);
}

function showTextResult(title, text, restoreForm = false) {
    resultTitle.textContent = title;
    resultText.textContent = text;
    setHidden(resultText, false);
    setHidden(pdfViewer, true);
    pdfViewer.removeAttribute('src');
    setHidden(formPanel, !restoreForm);
    showStatusPanel();
    showResult();
}

function showPdfResult(blob) {
    if (!blob.size) {
        throw new Error('The solver returned an empty PDF.');
    }

    pdfViewer.src = URL.createObjectURL(blob);
    setHidden(pdfViewer, false);
    setHidden(resultText, true);
    resultTitle.textContent = 'Solution PDF';
    showStatusPanel();
    showResult();
}

async function postFormData(formData, signal) {
    return fetch(solverEndpoint, {
        method: 'POST',
        body: formData,
        headers: {
            'X-CSRFToken': csrfToken,
        },
        signal,
    });
}

async function streamTextResponse(
    response,
    onChunk,
    onStatus = () => {},
    onProgress = () => {},
) {
    if (!response.ok || !response.body) {
        const errorText = await response.text();
        throw new Error(errorText || 'The live model response failed.');
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let text = '';

    while (true) {
        const { value, done } = await reader.read();
        if (done) {
            break;
        }

        buffer += decoder.decode(value, { stream: true });
        const events = buffer.split('\n\n');
        buffer = events.pop() || '';

        for (const eventText of events) {
            const line = eventText.trim();
            if (!line.startsWith('data:')) {
                continue;
            }

            const payload = JSON.parse(line.replace(/^data:\s*/, ''));
            if (payload.type === 'error') {
                throw new Error(payload.error || 'The model reported an error.');
            }

            if (payload.type === 'content') {
                text += payload.text || '';
                onChunk(payload.text || '');
            } else if (payload.type === 'status') {
                onStatus(payload.text || '');
            } else if (payload.type === 'progress') {
                onProgress(payload.text || '');
            }
        }
    }

    if (buffer.trim().startsWith('data:')) {
        const payload = JSON.parse(buffer.trim().replace(/^data:\s*/, ''));
        if (payload.type === 'error') {
            throw new Error(payload.error || 'The model reported an error.');
        }
        if (payload.type === 'content') {
            text += payload.text || '';
            onChunk(payload.text || '');
        } else if (payload.type === 'status') {
            onStatus(payload.text || '');
        } else if (payload.type === 'progress') {
            onProgress(payload.text || '');
        }
    }

    return text;
}

async function readProblemWithStream(fileInput, signal) {
    const readFormData = new FormData(form);
    readFormData.append('action', 'read');
    readFormData.set('fileInput', fileInput);

    imageOutput.textContent = '';
    setHidden(imageOutputPanel, false);
    const response = await postFormData(readFormData, signal);
    return streamTextResponse(response, (chunk) => {
        imageOutput.textContent += chunk;
        imageOutput.scrollTop = imageOutput.scrollHeight;
    }, appendStatus);
}

async function solveProblemWithStream(problemText, signal) {
    const solveFormData = new FormData();
    solveFormData.append('action', 'solve');
    solveFormData.append('problem', problemText);

    solutionOutput.textContent = '';
    setHidden(solutionOutputPanel, false);
    const response = await postFormData(solveFormData, signal);

    return streamTextResponse(response, (chunk) => {
        solutionOutput.textContent += chunk;
        solutionOutput.scrollTop = solutionOutput.scrollHeight;
        loadingMessage.textContent = 'DeepSeek is streaming the solution.';
    }, appendStatus, updateProgressStatus);
}

async function handleSubmit(event) {
    event.preventDefault();

    setHidden(formPanel, true);
    setHidden(resultPanel, true);
    setHidden(loadingScreen, false);
    resetResultDisplay();
    setLoadingState('reading');
    showStatusPanel();
    setStatusPill('Reading');

    const requestScope = window.mathLlmRequests.begin();
    try {
        const fileInput = form.querySelector('#fileInput').files[0];
        if (!fileInput) {
            throw new Error('Please upload an image before submitting.');
        }

        const readProblem = await readProblemWithStream(fileInput, requestScope.signal);
        const trimmedProblem = (readProblem || '').trim();
        if (!trimmedProblem) {
            throw new Error('The image reader returned no problem text.');
        }

        setLoadingState('solving');
        setStatusPill('Solving');
        showStatusPanel();
        const solution = await solveProblemWithStream(trimmedProblem, requestScope.signal);

        if (!solution || !solution.trim()) {
            throw new Error('The model returned an empty solution.');
        }

        setLoadingState('generatingPdf');
        setStatusPill('PDF');
        appendStatus('Formatting the final solution into a PDF document...');

        const pdfFormData = new FormData();
        pdfFormData.append('action', 'pdf');
        pdfFormData.append('solution', solution.trim());
        const response = await postFormData(pdfFormData, requestScope.signal);

        const contentType = (response.headers.get('Content-Type') || '').toLowerCase();
        const contentDisposition = (response.headers.get('Content-Disposition') || '').toLowerCase();
        const isPdfResponse =
            contentType.includes('application/pdf') ||
            contentDisposition.includes('solution.pdf') ||
            contentDisposition.includes('.pdf');

        if (isPdfResponse) {
            setStatusPill('Complete');
            showPdfResult(await response.blob());
            return;
        }

        const data = await response.json();
        if (!response.ok) {
            throw new Error(data.error || 'Something went wrong while creating the PDF.');
        }

        setStatusPill('Complete');
        showTextResult('LLM response', data.text || data.message || 'No readable text detected.');
    } catch (error) {
        if (error.name === 'AbortError') {
            return;
        }
        const errorMessage = error instanceof TypeError
            ? 'Could not reach the solver server. Check that the Django server is running and try again.'
            : error.message || 'Something went wrong.';
        showErrorBanner(errorMessage);
        setHidden(formPanel, false);
        setHidden(resultPanel, true);
        setHidden(loadingScreen, true);
        setStatusPill('Error');
        showStatusPanel();
    } finally {
        requestScope.finish();
    }
}

form.addEventListener('submit', handleSubmit);