async function loadServices() {
    currentView = 'services';
    renderLabelFilter(false); // <--- MOSTRA IL FILTRO
    
    const ns = window.currentNamespace;
    const resArea = document.getElementById('resultArea');
    
    // Spinner di caricamento iniziale
    resArea.innerHTML = '<div style="text-align:center; padding:20px;"><i class="fas fa-spinner fa-spin fa-2x"></i></div>';
    
    try {
        const data = await apiCall(`/namespaces/${ns}/services`);
        
        let html = `
            <h2>Services [${ns}]</h2>
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Name</th>
                        <th>Type</th>
                        <th>Cluster IP</th>
                        <th style="text-align:right">Actions</th>
                    </tr>
                </thead>
                <tbody>`;
        
        data.forEach(s => {
            html += `
                <tr>
                    <td><b>${s.name}</b></td>
                    <td><span class="badge" style="background:#f1f5f9; color:#475569;">${s.type}</span></td>
                    <td><code style="font-size:0.75rem">${s.cluster_ip || 'None'}</code></td>
                    <td style="text-align:right">
                        <button onclick="deleteResource('services', '${s.name}')" class="btn-small delete-btn">
                            <i class="fas fa-trash"></i>
                        </button>
                    </td>
                </tr>`;
        });

        // Controllo lunghezza dati per mostrare tabella o messaggio
        resArea.innerHTML = data.length > 0 
            ? html + '</tbody></table>' 
            : `<p style="text-align:center; margin-top:20px; color:var(--text-muted);">No Service found in namespace ${ns}.</p>`;
            
    } catch (err) { 
        if (err.message === "RESTRICTED") {
            renderRestrictedAccess(); 
        } else {
            showError(err.message);
        }
    }
}

async function loadConfigMaps() {
    currentView = 'configmaps';
    renderLabelFilter(false);
    const ns = window.currentNamespace;
    const resArea = document.getElementById('resultArea');
    resArea.innerHTML = '<div style="text-align:center"><i class="fas fa-spinner fa-spin fa-2x"></i></div>';
    try {
        const data = await apiCall(`/namespaces/${ns}/configmaps`);
        let html = `<h2>ConfigMaps [${ns}]</h2>
                    <table class="data-table">
                        <thead>
                            <tr>
                                <th>Name</th>
                                <th>Data Keys</th>
                                <th>Actions</th>
                            </tr>
                        </thead>
                        <tbody>`;
        
        data.forEach(cm => {
            // Creiamo dei piccoli badge grigi per ogni chiave trovata
            const keysHtml = cm.keys.length > 0 
                ? cm.keys.map(k => `<code style="background:#f1f5f9; padding:2px 6px; border-radius:4px; margin-right:4px; font-size:0.75rem;">${k}</code>`).join('')
                : '<span style="color:var(--text-muted); font-size:0.8rem;">No data</span>';

            html += `<tr>
                <td><b>${cm.name}</b></td>
                <td>${keysHtml}</td>
                <td><button onclick="deleteResource('configmaps', '${cm.name}')" class="btn-small delete-btn"><i class="fas fa-trash"></i></button></td>
            </tr>`;
        });
        resArea.innerHTML = data.length > 0 
            ? html + '</tbody></table>' 
            : `<p style="text-align:center; margin-top:20px; color:var(--text-muted);">No ConfigMap found in namespace ${ns}.</p>`;

    } catch (err) {
        if (err.message === "RESTRICTED") {
            renderRestrictedAccess(); 
        } else {
            showError(err.message);
        }
    }
}

async function loadSecrets() {
    currentView = 'secrets';
    renderLabelFilter(false);
    const ns = window.currentNamespace;
    const resArea = document.getElementById('resultArea');
    resArea.innerHTML = '<div style="text-align:center"><i class="fas fa-spinner fa-spin fa-2x"></i></div>';
    try {
        const data = await apiCall(`/namespaces/${ns}/secrets`);
        let html = `<h2>Secrets [${ns}]</h2>
                    <table class="data-table">
                        <thead>
                            <tr>
                                <th>Name</th>
                                <th>Type</th>
                                <th>Data Keys</th>
                                <th>Actions</th>
                            </tr>
                        </thead>
                        <tbody>`;
        
        data.forEach(s => {
            // Per i Secret usiamo uno stile leggermente diverso per le chiavi (lucchetto)
            const keysHtml = s.keys.length > 0 
                ? s.keys.map(k => `<span style="display:inline-block; border:1px solid #e2e8f0; padding:2px 6px; border-radius:4px; margin:2px; font-size:0.75rem;"><i class="fas fa-lock" style="font-size:0.6rem; margin-right:4px; color:#94a3b8;"></i>${k}</span>`).join('')
                : '<span style="color:var(--text-muted); font-size:0.8rem;">Empty</span>';

            html += `<tr>
                <td><b>${s.name}</b></td>
                <td><small>${s.type}</small></td>
                <td>${keysHtml}</td>
                <td><button onclick="deleteResource('secrets', '${s.name}')" class="btn-small delete-btn"><i class="fas fa-trash"></i></button></td>
            </tr>`;
        });
        
        resArea.innerHTML = data.length > 0 
            ? html + '</tbody></table>' 
            : `<p style="text-align:center; margin-top:20px; color:var(--text-muted);">No Secret found in namespace ${ns}.</p>`;

    } catch (err) {                 
        if (err.message === "RESTRICTED") {
            renderRestrictedAccess(); 
        } else {
            showError(err.message);
        } 
    }
}

async function loadIngress() {
    currentView = 'ingress';
    renderLabelFilter(false);
    const ns = window.currentNamespace;
    const resArea = document.getElementById('resultArea');
    
    resArea.innerHTML = '<div style="text-align:center; padding:20px;"><i class="fas fa-spinner fa-spin fa-2x"></i></div>';

    try {
        const data = await apiCall(`/namespaces/${ns}/ingress`);
        
        let html = `
            <h2>Ingress Resources [${ns}]</h2>
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Name</th>
                        <th>Hosts</th>
                        <th>Address (LoadBalancer)</th>
                        <th>Age</th>
                        <th style="text-align:right">Actions</th>
                    </tr>
                </thead>
                <tbody>`;

        data.forEach(ing => {
            // Renderizziamo gli host come piccoli codici blu
            const hostsHtml = ing.hosts.length > 0 
                ? ing.hosts.map(h => `<code style="color:var(--accent); margin-right:5px;">${h}</code>`).join(' ')
                : '<span style="color:var(--text-muted)">*</span>';

            // Renderizziamo gli IP/Hostname esterni
            const addrHtml = ing.address.length > 0
                ? ing.address.map(a => `<span style="font-size:0.75rem;">${a}</span>`).join(', ')
                : '<span style="color:var(--text-muted)">-</span>';

            html += `
                <tr>
                    <td><b>${ing.name}</b></td>
                    <td>${hostsHtml}</td>
                    <td>${addrHtml}</td>
                    <td><small>${new Date(ing.creation_timestamp).toLocaleDateString()}</small></td>
                    <td style="text-align:right">
                        <button onclick="deleteResource('ingress', '${ing.name}')" class="btn-small delete-btn" title="Delete Ingress">
                            <i class="fas fa-trash"></i>
                        </button>
                    </td>
                </tr>`;
        });

        resArea.innerHTML = data.length > 0 
            ? html + '</tbody></table>' 
            : `<p style="text-align:center; margin-top:20px; color:var(--text-muted);">No Ingress found in namespace ${ns}.</p>`;

    } catch (err) { 
        if (err.message === "RESTRICTED") {
            renderRestrictedAccess(); 
        } else {
            showError(err.message);
        } 
    }
}




