/**
 * Timeline UI Module
 * Handles system activity timeline display and interactions
 */

class TimelineUI {
    constructor() {
        this.entries = [];
        this.filteredEntries = [];
        this.currentFilter = '';
        this.loading = false;
        
        this.init();
    }

    init() {
        // Bind event listeners
        document.getElementById('timelineRefresh').addEventListener('click', () => this.loadTimeline());
        document.getElementById('timelineFilter').addEventListener('change', (e) => {
            this.currentFilter = e.target.value;
            this.applyFilter();
        });

        // Auto-refresh every 30 seconds
        setInterval(() => this.loadTimeline(), 30000);
        
        // Initial load
        this.loadTimeline();
    }

    async loadTimeline() {
        if (this.loading) return;
        
        this.loading = true;
        this.showLoading(true);
        
        try {
            const response = await fetch('/api/ui/timeline?limit=100');
            const data = await response.json();
            
            if (data.ok) {
                this.entries = data.entries || [];
                this.applyFilter();
                this.updateCount();
                
                // Show demo indicator if active
                if (data.demo_active) {
                    this.showDemoIndicator(true);
                } else {
                    this.showDemoIndicator(false);
                }
            } else {
                this.showError(data.error || 'Failed to load timeline');
            }
        } catch (error) {
            this.showError('Network error: ' + error.message);
        } finally {
            this.loading = false;
            this.showLoading(false);
        }
    }

    applyFilter() {
        if (!this.currentFilter) {
            this.filteredEntries = [...this.entries];
        } else {
            this.filteredEntries = this.entries.filter(entry => 
                entry.type === this.currentFilter
            );
        }
        this.render();
    }

    render() {
        const container = document.getElementById('timelineContainer');
        const emptyState = document.getElementById('timelineEmpty');
        
        if (this.filteredEntries.length === 0) {
            container.innerHTML = '';
            emptyState.style.display = 'block';
            return;
        }
        
        emptyState.style.display = 'none';
        
        const timelineHTML = this.filteredEntries.map(entry => this.renderEntry(entry)).join('');
        container.innerHTML = timelineHTML;
    }

    renderEntry(entry) {
        const time = new Date(entry.ts_ms).toLocaleString();
        const typeColor = this.getTypeColor(entry.type);
        const typeIcon = this.getTypeIcon(entry.type);
        
        // Add demo indicator if this is a demo entry
        const demoIndicator = entry.demo ? 
            '<span class="pill crit" style="font-size: 10px; animation: pulse 2s infinite;">🎭 DEMO</span>' : '';
        
        return `
            <div class="timeline-entry" style="
                display: flex;
                align-items: flex-start;
                gap: 12px;
                padding: 12px;
                border-bottom: 1px solid #1c2129;
                cursor: pointer;
                transition: background-color 0.2s;
            " onclick="timelineUI.handleEntryClick('${entry.type}', '${entry.reference_id}')"
            onmouseover="this.style.backgroundColor='#0d1117'"
            onmouseout="this.style.backgroundColor='transparent'">
                
                <!-- Type Icon -->
                <div style="
                    width: 32px;
                    height: 32px;
                    border-radius: 6px;
                    background: ${typeColor};
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    font-size: 14px;
                    flex-shrink: 0;
                ">
                    ${typeIcon}
                </div>
                
                <!-- Content -->
                <div style="flex: 1; min-width: 0;">
                    <!-- Header -->
                    <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 4px; flex-wrap: wrap;">
                        <span style="
                            font-size: 12px;
                            font-weight: 600;
                            color: ${typeColor};
                            background: ${typeColor}20;
                            padding: 2px 6px;
                            border-radius: 4px;
                        ">${entry.type}</span>
                        <span style="font-size: 13px; font-weight: 500; color: #c9d1d9;">
                            ${entry.label}
                        </span>
                        ${demoIndicator}
                        <span style="font-size: 11px; color: #9da7b3; margin-left: auto;">
                            ${time}
                        </span>
                    </div>
                    
                    <!-- Description -->
                    <div style="
                        font-size: 12px;
                        color: #9da7b3;
                        line-height: 1.4;
                        margin-top: 2px;
                    ">
                        ${entry.description}
                    </div>
                </div>
            </div>
        `;
    }

    getTypeColor(type) {
        const colors = {
            'INGEST': '#58a6ff',
            'MODEL': '#7ee787', 
            'RISK': '#d29922',
            'DECISION': '#f85149',
            'EXECUTION': '#a371f7'
        };
        return colors[type] || '#9da7b3';
    }

    getTypeIcon(type) {
        const icons = {
            'INGEST': '📥',
            'MODEL': '🧠',
            'RISK': '⚠️',
            'DECISION': '🎯',
            'EXECUTION': '⚡'
        };
        return icons[type] || '📄';
    }

    handleEntryClick(type, referenceId) {
        // Navigate to relevant view based on entry type
        switch (type) {
            case 'DECISION':
                this.openDecision(referenceId);
                break;
            case 'RISK':
                this.openAlert(referenceId);
                break;
            case 'MODEL':
                this.openModel(referenceId);
                break;
            case 'EXECUTION':
                this.openExecution(referenceId);
                break;
            case 'INGEST':
                this.openJobLog(referenceId);
                break;
            default:
                console.log('Clicked entry:', type, referenceId);
        }
    }

    async openDecision(decisionId) {
        try {
            const response = await fetch(`/api/ui/decision?decision_id=${decisionId}`);
            const data = await response.json();
            
            if (data.ok) {
                // Open decision detail modal or navigate to decision view
                this.showDecisionModal(data.decision);
            } else {
                this.showError('Failed to load decision details');
            }
        } catch (error) {
            this.showError('Error loading decision: ' + error.message);
        }
    }

    async openAlert(alertId) {
        // Scroll to alerts section and highlight the alert
        const alertsPanel = document.querySelector('#alertsPanel, .card:has(h2:contains("Alerts"))');
        if (alertsPanel) {
            alertsPanel.scrollIntoView({ behavior: 'smooth' });
            // Highlight the specific alert if it exists
            setTimeout(() => {
                const alertElement = document.querySelector(`[data-alert-id="${alertId}"]`);
                if (alertElement) {
                    alertElement.style.backgroundColor = '#f8514920';
                    setTimeout(() => {
                        alertElement.style.backgroundColor = '';
                    }, 2000);
                }
            }, 500);
        }
    }

    openModel(modelName) {
        // Scroll to model metrics section
        const modelPanel = document.querySelector('#modelMetrics, .card:has(h2:contains("Model"))');
        if (modelPanel) {
            modelPanel.scrollIntoView({ behavior: 'smooth' });
        }
    }

    openExecution(referenceId) {
        // Scroll to broker or execution section
        const brokerPanel = document.querySelector('#brokerPanel, .card:has(h2:contains("Broker"))');
        if (brokerPanel) {
            brokerPanel.scrollIntoView({ behavior: 'smooth' });
        }
    }

    openJobLog(jobId) {
        // Open job history panel and filter for this job
        const jobHistoryPanel = document.getElementById('jobHistoryPanel');
        if (jobHistoryPanel) {
            jobHistoryPanel.style.display = 'block';
            jobHistoryPanel.scrollIntoView({ behavior: 'smooth' });
            
            // Filter job history for this specific job if possible
            setTimeout(() => {
                const jobHistory = document.getElementById('jobHistory');
                if (jobHistory && jobId) {
                    // Highlight relevant job entries
                    const lines = jobHistory.textContent.split('\n');
                    const filteredLines = lines.filter(line => line.includes(jobId));
                    if (filteredLines.length > 0) {
                        jobHistory.textContent = filteredLines.join('\n');
                    }
                }
            }, 500);
        }
    }

    showDecisionModal(decision) {
        // Create and show decision detail modal
        const modal = document.createElement('div');
        modal.style.cssText = `
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0,0,0,0.8);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 1000;
        `;
        
        modal.innerHTML = `
            <div style="
                background: #0d1117;
                border: 1px solid #30363d;
                border-radius: 12px;
                padding: 24px;
                max-width: 600px;
                max-height: 80vh;
                overflow-y: auto;
            ">
                <h3 style="margin: 0 0 16px 0; color: #c9d1d9;">Decision Details</h3>
                <div style="display: grid; gap: 12px;">
                    <div><strong>Action:</strong> ${decision.action} ${decision.symbol}</div>
                    <div><strong>Size Delta:</strong> ${(decision.size_delta * 100).toFixed(2)}%</div>
                    <div><strong>Certainty:</strong> ${(decision.certainty * 100).toFixed(1)}%</div>
                    <div><strong>Risk Impact:</strong> ${decision.risk_impact}</div>
                    <div><strong>Why:</strong> ${decision.why}</div>
                    <div><strong>Time:</strong> ${new Date(decision.ts_ms).toLocaleString()}</div>
                </div>
                <button onclick="this.parentElement.parentElement.remove()" style="
                    margin-top: 16px;
                    padding: 8px 16px;
                    background: #238636;
                    color: white;
                    border: none;
                    border-radius: 6px;
                    cursor: pointer;
                ">Close</button>
            </div>
        `;
        
        document.body.appendChild(modal);
        modal.addEventListener('click', (e) => {
            if (e.target === modal) {
                modal.remove();
            }
        });
    }

    updateCount() {
        const countElement = document.getElementById('timelineCount');
        countElement.textContent = `${this.filteredEntries.length} entries`;
    }

    showLoading(show) {
        const loadingElement = document.getElementById('timelineLoading');
        const containerElement = document.getElementById('timelineContainer');
        
        if (show) {
            loadingElement.style.display = 'block';
            containerElement.style.display = 'none';
        } else {
            loadingElement.style.display = 'none';
            containerElement.style.display = 'block';
        }
    }

    showError(message) {
        const container = document.getElementById('timelineContainer');
        container.innerHTML = `
            <div style="
                text-align: center;
                padding: 20px;
                color: #ff6b6b;
                font-size: 12px;
            ">
                ⚠️ ${message}
            </div>
        `;
    }

    showDemoIndicator(show) {
        const container = document.getElementById('timelineContainer');
        const existingIndicator = document.getElementById('demoIndicator');
        
        if (existingIndicator) {
            existingIndicator.remove();
        }
        
        if (show) {
            const indicator = document.createElement('div');
            indicator.id = 'demoIndicator';
            indicator.style.cssText = `
                background: linear-gradient(135deg, #ff6b6b, #d29922);
                color: white;
                padding: 8px 12px;
                margin: 8px;
                border-radius: 8px;
                font-size: 12px;
                font-weight: 600;
                text-align: center;
                animation: pulse 2s infinite;
            `;
            indicator.innerHTML = '🎭 DEMO MODE ACTIVE - Synthetic Data Displayed';
            
            // Insert at the beginning of container
            container.insertBefore(indicator, container.firstChild);
        }
    }
}

// Initialize timeline UI when DOM is ready
let timelineUI;
document.addEventListener('DOMContentLoaded', () => {
    timelineUI = new TimelineUI();
    // Make globally accessible for demo mode
    window.timelineUI = timelineUI;
});
