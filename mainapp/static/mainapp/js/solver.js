// Cache the form, endpoint, and page elements used throughout the solver workflow.
const form = document.getElementById('solver-form');
const solverEndpoint = form.action;
const formPanel = document.getElementById('solver-form-panel');
const loadingScreen = document.getElementById('loading-screen');
const loadingTitle = document.getElementById('loading-title');
const loadingMessage = document.getElementById('loading-message');
const resultPanel = document.getElementById('result-panel');
const resultText = document.getElementById('result-text');
const resultTitle = document.getElementById('result-title');
const pdfViewer = document.getElementById('pdf-viewer');
const csrfToken = document.querySelector('[name=csrfmiddlewaretoken]').value;

// Keep progress messages aligned with the transcription, solving, and PDF stages.
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

// Reuse the CSS utility class to switch page elements between visible and hidden.
function setHidden(element, isHidden) {
    element.classList.toggle('hidden', isHidden);
}

// Clear previous output before starting a new submission.
function resetResultDisplay() {
    resultText.textContent = '';
    setHidden(resultText, false);
    setHidden(pdfViewer, true);
    pdfViewer.removeAttribute('src');
    resultTitle.textContent = 'LLM response';
}

// Replace the progress view with the completed result panel.
function showResult() {
    setHidden(loadingScreen, true);
    setHidden(resultPanel, false);
}

// Render text or an error and optionally return the user to the upload form.
function showTextResult(title, text, restoreForm = false) {
    resultTitle.textContent = title;
    resultText.textContent = text;
    setHidden(resultText, false);
    setHidden(pdfViewer, true);
    pdfViewer.removeAttribute('src');
    setHidden(formPanel, !restoreForm);
    showResult();
}

// Display a non-empty PDF response in the embedded viewer.
function showPdfResult(blob) {
    if (!blob.size) {
        throw new Error('The solver returned an empty PDF.');
    }

    pdfViewer.src = URL.createObjectURL(blob);
    setHidden(pdfViewer, false);
    setHidden(resultText, true);
    resultTitle.textContent = 'Solution PDF';
    showResult();
}

// Send a stage request to Django with the session's CSRF token.
async function postFormData(formData) {
    return fetch(solverEndpoint, {
        method: 'POST',
        body: formData,
        headers: {
            'X-CSRFToken': csrfToken,
        },
    });
}

// Decode JSON responses and provide a readable fallback for plain-text errors.
async function getResponseData(response, contentType) {
    if (contentType.includes('application/json')) {
        return response.json();
    }

    if (response.status >= 400) {
        return { error: await response.text() };
    }

    return {};
}

// Run the upload workflow in order: transcribe the image, solve it, and request a PDF.
async function handleSubmit(event) {
    event.preventDefault();

    setHidden(formPanel, true);
    setHidden(resultPanel, true);
    setHidden(loadingScreen, false);
    resetResultDisplay();
    setLoadingState('reading');

    try {
        // Ask the server to extract the math problem from the selected image.
        const readFormData = new FormData(form);
        readFormData.append('action', 'read');
        const readResponse = await postFormData(readFormData);

        console.log('Read response status:', readResponse.status);

        const readData = await readResponse.json();
        if (!readResponse.ok) {
            throw new Error(readData.error || 'Could not read the uploaded image.');
        }

        // Submit the transcription and wait for the step-by-step solution.
        setLoadingState('solving');
        const solveFormData = new FormData();
        solveFormData.append('action', 'solve');
        solveFormData.append('problem', readData.problem);
        const solveResponse = await postFormData(solveFormData);

        console.log('Solve response status:', solveResponse.status);
        const solveData = await solveResponse.json();
        if (!solveResponse.ok) {
            throw new Error(solveData.error || 'Could not solve the problem.');
        }

        // Request a PDF and show it when the server returns a PDF response.
        setLoadingState('generatingPdf');
        const pdfFormData = new FormData();
        pdfFormData.append('action', 'pdf');
        pdfFormData.append('solution', solveData.solution);
        const response = await postFormData(pdfFormData);

        console.log('PDF response status:', response.status);

        const contentType = (response.headers.get('Content-Type') || '').toLowerCase();
        const contentDisposition = (response.headers.get('Content-Disposition') || '').toLowerCase();
        const isPdfResponse =
            contentType.includes('application/pdf') ||
            contentDisposition.includes('solution.pdf') ||
            contentDisposition.includes('.pdf');

        if (isPdfResponse) {
            showPdfResult(await response.blob());
            return;
        }

        // Display server errors or text output rather than assuming a PDF was returned.
        const data = await getResponseData(response, contentType);

        if (!response.ok) {
            showTextResult('Error', data.error || 'Something went wrong.');
            return;
        }

        showTextResult(
            'LLM response',
            data.text || data.message || 'No readable text detected.',
        );
    } catch (error) {
        const errorMessage = error instanceof TypeError
            ? 'Could not reach the solver server. Check that the Django server is running and try again.'
            : error.message || 'Something went wrong.';
        showTextResult('Error', errorMessage, true);
    }
}

// Intercept normal submission so each asynchronous stage can update the page.
form.addEventListener('submit', handleSubmit);