/**
 * Live Architecture Visualization Module
 * Renders system architecture graph with health status and real-time updates
 */

// Architecture visualization state
let architectureState = {
    nodes: [],
    edges: [],
    svg: null,
    container: null,
    autoRefreshInterval: null,
    isAutoRefresh: false,
    nodeElements: new Map(),
    edgeElements: new Map(),
    pulseAnimations: new Map()
};

// Health status colors
const HEALTH_COLORS = {
    healthy: '#2ea043',
    degraded: '#d29922', 
    critical: '#ff6b6b',
    inactive: '#6e7681',
    unknown: '#58a6ff',
    stale: '#8b949e'
};

// Node positions for layout (horizontal pipeline)
const NODE_POSITIONS = {
    ingestion: { x: 100, y: 200 },
    features: { x: 250, y: 200 },
    models: { x: 400, y: 200 },
    risk_gates: { x: 550, y: 200 },
    portfolio: { x: 700, y: 200 },
    execution: { x: 850, y: 200 }
};

/**
 * Initialize architecture visualization
 */
function initArchitecture() {
    architectureState.container = document.getElementById('architectureContainer');
    architectureState.svg = document.getElementById('architectureSvg');
    
    if (!architectureState.container || !architectureState.svg) {
        console.warn('Architecture container or SVG not found');
        return;
    }
    
    // Set up event listeners
    const refreshBtn = document.getElementById('architectureRefresh');
    const autoRefreshBtn = document.getElementById('architectureAutoRefresh');
    
    if (refreshBtn) {
        refreshBtn.addEventListener('click', loadArchitecture);
    }
    
    if (autoRefreshBtn) {
        autoRefreshBtn.addEventListener('click', toggleAutoRefresh);
    }
    
    // Initial load
    loadArchitecture();
}

/**
 * Load architecture data from API
 */
async function loadArchitecture() {
    const statusEl = document.getElementById('architectureStatus');
    const loadingEl = document.getElementById('architectureLoading');
    const errorEl = document.getElementById('architectureError');
    
    try {
        // Show loading state
        if (loadingEl) loadingEl.style.display = 'block';
        if (errorEl) errorEl.style.display = 'none';
        if (statusEl) statusEl.textContent = 'Loading...';
        
        const response = await fetch('/api/ui/graph_snapshot');
        const data = await response.json();
        
        if (!data.ok) {
            throw new Error(data.error || 'Failed to load architecture data');
        }
        
        // Update state
        architectureState.nodes = data.nodes || [];
        architectureState.edges = data.edges || [];
        
        // Render the graph
        renderArchitecture();
        
        // Update status
        if (statusEl) {
            statusEl.textContent = `${data.node_count} nodes, ${data.edge_count} edges`;
            statusEl.className = 'pill ok';
        }
        
    } catch (error) {
        console.error('Failed to load architecture:', error);
        
        // Show error state
        if (loadingEl) loadingEl.style.display = 'none';
        if (errorEl) errorEl.style.display = 'block';
        if (statusEl) {
            statusEl.textContent = 'Error';
            statusEl.className = 'pill crit';
        }
    } finally {
        if (loadingEl) loadingEl.style.display = 'none';
    }
}

/**
 * Render the architecture graph
 */
function renderArchitecture() {
    if (!architectureState.svg) return;
    
    // Clear existing elements
    clearSVG();
    
    // Create edge definitions (markers for arrows)
    createArrowMarkers();
    
    // Render edges first (so they appear behind nodes)
    renderEdges();
    
    // Render nodes
    renderNodes();
    
    // Start pulse animations for active nodes
    startPulseAnimations();
}

/**
 * Clear SVG elements
 */
function clearSVG() {
    if (!architectureState.svg) return;
    
    // Clear all elements except definitions
    const elements = architectureState.svg.querySelectorAll(':not(defs)');
    elements.forEach(el => el.remove());
    
    // Clear state maps
    architectureState.nodeElements.clear();
    architectureState.edgeElements.clear();
    architectureState.pulseAnimations.clear();
}

/**
 * Create arrow markers for edges
 */
function createArrowMarkers() {
    if (!architectureState.svg) return;
    
    const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
    
    // Create arrow marker
    const marker = document.createElementNS('http://www.w3.org/2000/svg', 'marker');
    marker.setAttribute('id', 'arrowhead');
    marker.setAttribute('markerWidth', '10');
    marker.setAttribute('markerHeight', '7');
    marker.setAttribute('refX', '9');
    marker.setAttribute('refY', '3.5');
    marker.setAttribute('orient', 'auto');
    
    const polygon = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
    polygon.setAttribute('points', '0 0, 10 3.5, 0 7');
    polygon.setAttribute('fill', '#58a6ff');
    
    marker.appendChild(polygon);
    defs.appendChild(marker);
    architectureState.svg.appendChild(defs);
}

/**
 * Render edges between nodes
 */
function renderEdges() {
    architectureState.edges.forEach(edge => {
        const sourcePos = NODE_POSITIONS[edge.source];
        const targetPos = NODE_POSITIONS[edge.target];
        
        if (!sourcePos || !targetPos) return;
        
        // Create edge line
        const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('x1', sourcePos.x + 40); // Node radius
        line.setAttribute('y1', sourcePos.y);
        line.setAttribute('x2', targetPos.x - 40); // Node radius
        line.setAttribute('y2', targetPos.y);
        line.setAttribute('stroke', '#58a6ff');
        line.setAttribute('stroke-width', '2');
        line.setAttribute('marker-end', 'url(#arrowhead)');
        line.setAttribute('opacity', '0.6');
        
        // Add animation for data flow
        const animate = document.createElementNS('http://www.w3.org/2000/svg', 'animate');
        animate.setAttribute('attributeName', 'opacity');
        animate.setAttribute('values', '0.6;1;0.6');
        animate.setAttribute('dur', '3s');
        animate.setAttribute('repeatCount', 'indefinite');
        
        line.appendChild(animate);
        architectureState.svg.appendChild(line);
        architectureState.edgeElements.set(edge.source + '-' + edge.target, line);
    });
}

/**
 * Render nodes
 */
function renderNodes() {
    architectureState.nodes.forEach(node => {
        const pos = NODE_POSITIONS[node.id];
        if (!pos) return;
        
        // Create node group
        const group = document.createElementNS('http://www.w3.org/2000/svg', 'g');
        group.setAttribute('transform', `translate(${pos.x}, ${pos.y})`);
        
        // Create node circle
        const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        circle.setAttribute('r', '40');
        circle.setAttribute('fill', HEALTH_COLORS[node.health] || HEALTH_COLORS.unknown);
        circle.setAttribute('stroke', '#30363d');
        circle.setAttribute('stroke-width', '2');
        
        // Create pulse circle (for animation)
        const pulseCircle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        pulseCircle.setAttribute('r', '40');
        pulseCircle.setAttribute('fill', 'none');
        pulseCircle.setAttribute('stroke', HEALTH_COLORS[node.health] || HEALTH_COLORS.unknown);
        pulseCircle.setAttribute('stroke-width', '2');
        pulseCircle.setAttribute('opacity', '0');
        
        // Create node text
        const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        text.setAttribute('text-anchor', 'middle');
        text.setAttribute('y', '5');
        text.setAttribute('fill', 'white');
        text.setAttribute('font-size', '12');
        text.setAttribute('font-weight', 'bold');
        text.textContent = node.label.replace(/\s+/g, '\n');
        
        // Create status text
        const statusText = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        statusText.setAttribute('text-anchor', 'middle');
        statusText.setAttribute('y', '60');
        statusText.setAttribute('fill', '#9da7b3');
        statusText.setAttribute('font-size', '10');
        statusText.textContent = node.health.toUpperCase();
        
        // Add hover tooltip
        const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
        title.textContent = `${node.label}\nHealth: ${node.health}\nLast activity: ${formatTime(node.last_heartbeat_ms)}\nActivity count: ${node.activity_count}`;
        
        // Assemble node
        group.appendChild(pulseCircle);
        group.appendChild(circle);
        group.appendChild(text);
        group.appendChild(statusText);
        group.appendChild(title);
        
        architectureState.svg.appendChild(group);
        architectureState.nodeElements.set(node.id, { group, circle, pulseCircle, statusText });
    });
}

/**
 * Start pulse animations for active nodes
 */
function startPulseAnimations() {
    architectureState.nodes.forEach(node => {
        if (node.health === 'healthy' || node.health === 'degraded') {
            const nodeEl = architectureState.nodeElements.get(node.id);
            if (nodeEl && nodeEl.pulseCircle) {
                startNodePulse(node.id, nodeEl.pulseCircle);
            }
        }
    });
}

/**
 * Start pulse animation for a specific node
 */
function startNodePulse(nodeId, pulseCircle) {
    const animate = document.createElementNS('http://www.w3.org/2000/svg', 'animate');
    animate.setAttribute('attributeName', 'r');
    animate.setAttribute('values', '40;55;40');
    animate.setAttribute('dur', '2s');
    animate.setAttribute('repeatCount', 'indefinite');
    
    const animateOpacity = document.createElementNS('http://www.w3.org/2000/svg', 'animate');
    animateOpacity.setAttribute('attributeName', 'opacity');
    animateOpacity.setAttribute('values', '0;0.6;0');
    animateOpacity.setAttribute('dur', '2s');
    animateOpacity.setAttribute('repeatCount', 'indefinite');
    
    pulseCircle.appendChild(animate);
    pulseCircle.appendChild(animateOpacity);
    
    architectureState.pulseAnimations.set(nodeId, { animate, animateOpacity });
}

/**
 * Toggle auto-refresh
 */
function toggleAutoRefresh() {
    const btn = document.getElementById('architectureAutoRefresh');
    if (!btn) return;
    
    architectureState.isAutoRefresh = !architectureState.isAutoRefresh;
    
    if (architectureState.isAutoRefresh) {
        btn.textContent = 'Auto: ON';
        btn.className = 'btn ok';
        
        // Start auto-refresh interval
        architectureState.autoRefreshInterval = setInterval(() => {
            loadArchitecture();
        }, 5000); // Refresh every 5 seconds
        
    } else {
        btn.textContent = 'Auto: OFF';
        btn.className = 'btn';
        
        // Clear auto-refresh interval
        if (architectureState.autoRefreshInterval) {
            clearInterval(architectureState.autoRefreshInterval);
            architectureState.autoRefreshInterval = null;
        }
    }
}

/**
 * Format timestamp for display
 */
function formatTime(timestamp) {
    if (!timestamp) return 'Never';
    
    const date = new Date(timestamp);
    const now = new Date();
    const diffMs = now - date;
    const diffSecs = Math.floor(diffMs / 1000);
    const diffMins = Math.floor(diffSecs / 60);
    const diffHours = Math.floor(diffMins / 60);
    
    if (diffSecs < 60) {
        return `${diffSecs}s ago`;
    } else if (diffMins < 60) {
        return `${diffMins}m ago`;
    } else if (diffHours < 24) {
        return `${diffHours}h ago`;
    } else {
        return date.toLocaleDateString();
    }
}

/**
 * Cleanup architecture visualization
 */
function cleanupArchitecture() {
    // Clear auto-refresh
    if (architectureState.autoRefreshInterval) {
        clearInterval(architectureState.autoRefreshInterval);
        architectureState.autoRefreshInterval = null;
    }
    
    // Clear state
    architectureState.nodes = [];
    architectureState.edges = [];
    architectureState.nodeElements.clear();
    architectureState.edgeElements.clear();
    architectureState.pulseAnimations.clear();
}

// Export for use in dashboard.js
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        initArchitecture,
        loadArchitecture,
        cleanupArchitecture
    };
}
