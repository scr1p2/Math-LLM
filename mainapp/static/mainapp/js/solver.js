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

function setLoadingState(state) {
const states = {
    reading: {
        title: 'Reading the image...',
        message: 'We are extracting the math problem from your image.'
    },
    solving: {
        title: 'Solving the problem...',
        message: 'The model is working through the solution step by step.'
    }
};

const current = states[state] || states.reading;
loadingTitle.textContent = current.title;
loadingMessage.textContent = current.message;
}

function resetResultDisplay() {
resultText.textContent = '';
resultText.classList.remove('hidden');
pdfViewer.classList.add('hidden');
pdfViewer.removeAttribute('src');
resultTitle.textContent = 'LLM response';
}

form.addEventListener('submit', async function (event) {
event.preventDefault();

formPanel.classList.add('hidden');
resultPanel.classList.add('hidden');
loadingScreen.classList.remove('hidden');
resetResultDisplay();
setLoadingState('reading');

try {
    const readFormData = new FormData(form);
    readFormData.append('action', 'read');
    const readResponse = await fetch(solverEndpoint, {
        method: 'POST',
        body: readFormData,
        headers: {
            'X-CSRFToken': csrfToken,
        }
    });

    console.log("Read response status:", readResponse.status);

    const readData = await readResponse.json();
    if (!readResponse.ok) {
        throw new Error(readData.error || 'Could not read the uploaded image.');
    }

    setLoadingState('solving');
    const solveFormData = new FormData();
    solveFormData.append('action', 'solve');
    solveFormData.append('problem', readData.problem);
    const response = await fetch(solverEndpoint, {
        method: 'POST',
        body: solveFormData,
        headers: {
            'X-CSRFToken': csrfToken,
        }
    });

    console.log("Solve response status:", response.status);
    
    const contentType = (response.headers.get('Content-Type') || '').toLowerCase();
    const contentDisposition = (response.headers.get('Content-Disposition') || '').toLowerCase();
    const isPdfResponse = contentType.includes('application/pdf') || contentDisposition.includes('solution.pdf') || contentDisposition.includes('.pdf');

    if (isPdfResponse) {
        const blob = await response.blob();
        if (!blob.size) {
            throw new Error('The solver returned an empty PDF.');
        }

        const pdfUrl = URL.createObjectURL(blob);
        pdfViewer.src = pdfUrl;
        pdfViewer.classList.remove('hidden');
        resultText.classList.add('hidden');
        resultTitle.textContent = 'Solution PDF';
        loadingScreen.classList.add('hidden');
        resultPanel.classList.remove('hidden');
        return;
    }

    let data = {};
    if (contentType.includes('application/json')) {
        data = await response.json();
    } else if (response.status >= 400) {
        data = { error: await response.text() };
    }

    if (!response.ok) {
        const errorMessage = data.error || 'Something went wrong.';
        resultTitle.textContent = 'Error';
        resultText.textContent = errorMessage;
        resultText.classList.remove('hidden');
        pdfViewer.classList.add('hidden');
        pdfViewer.removeAttribute('src');
        loadingScreen.classList.add('hidden');
        resultPanel.classList.remove('hidden');
        return;
    }

    resultText.textContent = data.text || data.message || 'No readable text detected.';
    loadingScreen.classList.add('hidden');
    resultPanel.classList.remove('hidden');
} catch (error) {
    loadingScreen.classList.add('hidden');
    formPanel.classList.remove('hidden');
    resultPanel.classList.remove('hidden');
    resultTitle.textContent = 'Error';
    resultText.textContent = error instanceof TypeError
        ? 'Could not reach the solver server. Check that the Django server is running and try again.'
        : error.message || 'Something went wrong.';
    resultText.classList.remove('hidden');
    pdfViewer.classList.add('hidden');
    pdfViewer.removeAttribute('src');
}
});