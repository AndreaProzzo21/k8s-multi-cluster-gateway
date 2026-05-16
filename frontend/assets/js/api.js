const API_BASE = "/api/v1";
window.apiAbortController = new AbortController();

// Flag: true durante l'health check iniziale, false dopo che la dashboard è caricata.
// Serve a distinguere un 504 "pre-dashboard" (mostra overlay) da uno "operativo" (redirect).
window._dashboardReady = false;

async function apiCall(endpoint, method = 'GET', isText = false, body = null) {
    const currentToken = localStorage.getItem('k8s_jwt');
    const signal = window.apiAbortController.signal;

    const options = {
        method,
        headers: {
            'Authorization': `Bearer ${currentToken}`,
            'Connection': 'close'
        },
        signal
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

        if (response.status === 504 || response.status === 503) {
            if (window._dashboardReady) {
                // Dashboard già caricata: redirect al login con messaggio
                _handleClusterUnreachable();
                return new Promise(() => {});
            } else {
                // Siamo ancora nell'health check: lascia che initDashboard gestisca
                throw new Error("CLUSTER_UNREACHABLE");
            }
        }

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.message || errorData.detail || "API ERROR");
        }

        return isText ? await response.text() : await response.json();

    } catch (error) {
        if (error.name === 'AbortError') {
            console.warn("Request aborted.");
            return new Promise(() => {});
        }
        throw error;
    }
}

function _handleClusterUnreachable() {
    sessionStorage.setItem('login_error', 'Cluster unreachable or timed out. Please check the cluster status and try again.');
    localStorage.removeItem('k8s_jwt');
    window.location.replace('index.html');
}

function handleFileUpload(event) {
    const file = event.target.files[0];
    if (!file) return;
    document.getElementById('fileNameDisplay').innerText = `File: ${file.name}`;
    const reader = new FileReader();
    reader.onload = (e) => document.getElementById('yamlEditor').value = e.target.result;
    reader.readAsText(file);
}