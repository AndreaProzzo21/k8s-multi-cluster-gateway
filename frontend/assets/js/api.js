const API_BASE = "http://localhost:8000/api/v1";

window.apiAbortController = new AbortController();

async function apiCall(endpoint, method = 'GET', isText = false, body = null) {
    // 2. Leggiamo il token SEMPRE all'interno della chiamata
    const currentToken = localStorage.getItem('k8s_jwt');
    const signal = window.apiAbortController.signal;

    const options = { 
        method, 
        headers: { 
            'Authorization': `Bearer ${currentToken}`,
            'Connection': 'close'
        },
        signal: signal // Permette l'interruzione immediata
    };
    
    if (body instanceof FormData) {
        options.body = body;
    } else if (body) {
        options.headers['Content-Type'] = 'application/json';
        options.body = JSON.stringify(body);
    }

    try {
        const response = await fetch(`${API_BASE}${endpoint}`, options);
        
        if (response.status === 401 || response.status === 403) {
            throw new Error("RESTRICTED");
        }

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.message || errorData.detail || "API ERROR");
        }

        return isText ? await response.text() : await response.json();

    } catch (error) {
        if (error.name === 'AbortError') {
            console.warn("Request aborted to clear connection queue.");
            // Restituiamo una promise che non si risolve mai per fermare il caricamento UI
            return new Promise(() => {}); 
        }
        throw error;
    }
}


function handleFileUpload(event) {
    const file = event.target.files[0];
    if (!file) return;
    document.getElementById('fileNameDisplay').innerText = `File: ${file.name}`;
    const reader = new FileReader();
    reader.onload = (e) => document.getElementById('yamlEditor').value = e.target.result;
    reader.readAsText(file);
}