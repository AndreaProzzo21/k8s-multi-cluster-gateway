async function loadPods() {
    currentView = 'pods';
    const ns = window.currentNamespace;
    const resArea = document.getElementById('resultArea');
    
    // Recupero filtro dall'input globale
    const labelSelector = document.getElementById('labelFilter')?.value || '';
    let url = `/namespaces/${ns}/pods`;
    if (labelSelector) url += `?label_selector=${encodeURIComponent(labelSelector)}`;

    resArea.innerHTML = '<div style="text-align:center; padding:20px;"><i class="fas fa-spinner fa-spin fa-2x"></i></div>';

    try {
        const data = await apiCall(url);
        let html = `
            <h2>Pods [${ns}]</h2>
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Name</th>
                        <th>Node</th>
                        <th>Labels</th>
                        <th>Status</th>
                        <th>IP</th>
                        <th style="text-align:right">Actions</th>
                    </tr>
                </thead>
                <tbody>`;
        
        data.forEach(p => {
            const sClass = p.status.toLowerCase() === 'running' ? 'status-running' : 'status-pending';
            const nodeDisplay = p.node_name 
                ? `<span style="font-size:0.75rem; color:var(--accent); font-weight:600;">${p.node_name}</span>`
                : '<span style="color:var(--text-muted); font-size:0.75rem;">Unassigned</span>';
            html += `
                <tr>
                    <td><b>${p.name}</b></td>
                    <td>${nodeDisplay}</td>
                    <td>${renderLabels(p.labels)}</td>
                    <td><span class="badge ${sClass}">${p.status}</span></td>
                    <td><code style="font-size:0.75rem">${p.pod_ip || 'N/A'}</code></td>
                    <td style="text-align:right; white-space: nowrap;">
                        <button onclick="viewLogs('${p.name}', this)" class="btn-small table-btn" title="View Logs"><i class="fas fa-terminal"></i></button>
                        <button onclick="downloadLogs('${p.name}')" class="btn-small table-btn" title="Download Logs"><i class="fas fa-file-download"></i></button>
                        <button onclick="deleteResource('pods', '${p.name}')" class="btn-small delete-btn" title="Delete Pod"><i class="fas fa-trash"></i></button>
                    </td>
                </tr>`;
        });

        resArea.innerHTML = data.length > 0 
            ? html + '</tbody></table><div id="logConsoleArea"></div>' // <-- AGGIUNTO IL DIV QUI
            : `<p style="text-align:center; margin-top:20px; color:var(--text-muted);">No Pod found in namespace ${ns}.</p>`;


    } catch (err) { 
        if (err.message === "RESTRICTED") {
            renderRestrictedAccess(); 
        } else {
            showError(err.message);
        }
    }
}
async function loadDeployments() {
    currentView = 'deployments';
    const ns = window.currentNamespace;
    const resArea = document.getElementById('resultArea');
    
    const labelSelector = document.getElementById('labelFilter')?.value || '';
    let url = `/namespaces/${ns}/deployments`;
    if (labelSelector) url += `?label_selector=${encodeURIComponent(labelSelector)}`;

    resArea.innerHTML = '<div style="text-align:center; padding:20px;"><i class="fas fa-spinner fa-spin fa-2x"></i></div>';

    try {
        const data = await apiCall(url);
        let html = `
            <h2>Deployments [${ns}]</h2>
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Name</th>
                        <th>Labels</th>
                        <th>Replicas</th>
                        <th>Status</th>
                        <th style="text-align:right">Actions</th>
                    </tr>
                </thead>
                <tbody>`;

        data.forEach(d => {
            html += `
                <tr>
                    <td><b>${d.name}</b></td>
                    <td>${renderLabels(d.labels)}</td>
                    <td><b>${d.replicas_ready}</b>/${d.replicas_desired}</td>
                    <td><span class="badge status-running">${d.status}</span></td>
                    <td style="text-align:right; white-space: nowrap;">
                        <button onclick="scaleDeploy('${d.name}', ${d.replicas_desired})" class="btn-small scale-btn" title="Scale">Scale</button>
                        <button onclick="restartDeploy('${d.name}')" class="btn-small restart-btn" title="Restart Rollout"><i class="fas fa-sync"></i></button>
                        <button onclick="deleteResource('deployments', '${d.name}')" class="btn-small delete-btn" title="Delete Deployment"><i class="fas fa-trash"></i></button>
                    </td>
                </tr>`;
        });
        
        resArea.innerHTML = data.length > 0 
            ? html + '</tbody></table>' 
            : `<p style="text-align:center; margin-top:20px; color:var(--text-muted);">No Deployment found in namespace ${ns}.</p>`;
        
    } catch (err) { 
        if (err.message === "RESTRICTED") {
            renderRestrictedAccess(); 
        } else {
            showError(err.message);
        }
    }
}

async function restartDeploy(name) {
    if(!confirm(`Riavviare il deployment ${name}?`)) return;
    try {
        await apiCall(`/namespaces/${window.currentNamespace}/deployments/${name}/restart`, 'POST');
        loadDeployments();
    } catch (err) { alert(err.message); }
}

async function scaleDeploy(name, current) {
    const n = prompt("Replicas:", current);
    if (n === null) return;
    try {
        await apiCall(`/namespaces/${window.currentNamespace}/deployments/${name}/scale?replicas=${n}`, 'PATCH');
        loadDeployments();
    } catch (err) { alert(err.message); }
}

async function viewLogs(name, btn) {
    const row = btn.closest('tr');
    const existingLog = document.getElementById(`logs-${name}`);

    // Toggle: Se i log sono già aperti, li rimuoviamo
    if (existingLog) {
        existingLog.remove();
        return;
    }

    try {
        // Inseriamo una riga di caricamento immediata
        row.insertAdjacentHTML('afterend', `
            <tr id="logs-${name}" class="log-row">
                <td colspan="6">
                    <div class="log-container">
                        <div style="display:flex; justify-content:space-between; color:#94a3b8; margin-bottom:5px;">
                            <small>Streaming logs for <b>${name}</b>...</small>
                            <button onclick="this.closest('tr').remove()" style="background:none; border:none; color:#94a3b8; cursor:pointer;">&times;</button>
                        </div>
                        <pre id="pre-${name}">Loading...</pre>
                    </div>
                </td>
            </tr>`);

        const logs = await apiCall(`/namespaces/${window.currentNamespace}/pods/${name}/logs?tail=100`, 'GET', true);
        document.getElementById(`pre-${name}`).textContent = logs || "No logs available.";
        
    } catch (err) {
        alert("Error: " + err.message);
        document.getElementById(`logs-${name}`)?.remove();
    }
}

async function downloadLogs(name) {
    const logs = await apiCall(`/namespaces/${window.currentNamespace}/pods/${name}/logs`, 'GET', true);
    const blob = new Blob([logs], { type: 'text/plain' });
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = `${name}_logs.txt`;
    document.body.appendChild(a); a.click(); a.remove();
}

async function executeApply() {
    const yamlContent = document.getElementById('yamlEditor').value;
    if(!yamlContent.trim()) return;
    const reportDiv = document.getElementById('applyReport');
    reportDiv.innerHTML = "Processing...";
    try {
        const formData = new FormData();
        formData.append('file', new Blob([yamlContent], { type: 'text/yaml' }), 'resource.yaml');
        const res = await apiCall(`/namespaces/${window.currentNamespace}/apply`, 'POST', false, formData);
        reportDiv.innerHTML = `<div style="background:var(--accent-soft); padding:15px; border-radius:10px;"><b>Progress...</b><ul>${res.details.map(d => `<li>${d}</li>`).join('')}</ul></div>`;
    } catch (err) { showError(err.message);}
}

async function deleteResource(type, name) {
    if (!confirm(`Confermi l'eliminazione di ${type}: ${name}?`)) return;

    const ns = window.currentNamespace;
    const url = `/namespaces/${ns}/${type}/${name}`;

    try {
        await apiCall(url, 'DELETE'); 
        alert(`${type} '${name}' successfully deleted.`);

        refreshCurrentView(); 
        
    } catch (err) {
        if (err.message == "RESTRICTED") {
            alert(`Error in deleting ${name}: RESTRICTED ACCESS 403 `);
        }
        else{
            alert(`Error in deleting ${name}: ${err}`);
        }
    }
}
