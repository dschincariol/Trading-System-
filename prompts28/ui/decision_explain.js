/* decision_explain.js - Animated backtrace and explainability UI */

import { fmtTime, fmtNum } from "./utils.js";

class DecisionExplainer {
    constructor() {
        this.currentDecision = null;
        this.animationStep = 0;
        this.isAnimating = false;
        this.expandedSections = new Set();
    }

    async showDecisionExplain(decisionId) {
        try {
            const response = await fetch(`/api/ui/decision?decision_id=${decisionId}`);
            const data = await response.json();
            
            if (!data.ok) {
                throw new Error(data.error || 'Failed to load decision');
            }

            this.currentDecision = data.decision;
            this.renderExplainer();
            this.startAnimation();
        } catch (error) {
            console.error('Error loading decision explanation:', error);
            this.showError('Failed to load decision explanation');
        }
    }

    renderExplainer() {
        const modal = document.createElement('div');
        modal.className = 'explainModal';
        modal.innerHTML = `
            <div class="explainOverlay" onclick="this.parentElement.remove()"></div>
            <div class="explainPanel">
                <div class="explainHeader">
                    <h2>🔍 Explain This Decision</h2>
                    <button class="btn btnSmall" onclick="this.closest('.explainModal').remove()">✕</button>
                </div>
                
                <div class="explainContent">
                    ${this.renderBacktraceFlow()}
                    ${this.renderPlainExplanation()}
                    ${this.renderTechnicalDetails()}
                </div>
            </div>
        `;
        
        document.body.appendChild(modal);
    }

    renderBacktraceFlow() {
        const decision = this.currentDecision;
        const steps = [
            {
                id: 'inputs',
                title: 'Inputs',
                icon: '📊',
                content: this.renderInputsStep(),
                delay: 0
            },
            {
                id: 'models',
                title: 'Models',
                icon: '🧠',
                content: this.renderModelsStep(),
                delay: 800
            },
            {
                id: 'risk',
                title: 'Risk Gates',
                icon: '🛡️',
                content: this.renderRiskStep(),
                delay: 1600
            },
            {
                id: 'allocation',
                title: 'Allocation',
                icon: '⚖️',
                content: this.renderAllocationStep(),
                delay: 2400
            },
            {
                id: 'action',
                title: 'Action',
                icon: '⚡',
                content: this.renderActionStep(),
                delay: 3200
            }
        ];

        return `
            <div class="backtraceSection">
                <h3>🔄 Decision Flow</h3>
                <div class="flowContainer">
                    ${steps.map((step, index) => `
                        <div class="flowStep" data-step="${step.id}" style="animation-delay: ${step.delay}ms">
                            <div class="stepConnector ${index < steps.length - 1 ? 'active' : ''}"></div>
                            <div class="stepBubble">
                                <span class="stepIcon">${step.icon}</span>
                                <span class="stepNumber">${index + 1}</span>
                            </div>
                            <div class="stepContent">
                                <h4>${step.title}</h4>
                                <div class="stepDetails">${step.content}</div>
                            </div>
                        </div>
                    `).join('')}
                </div>
            </div>
        `;
    }

    renderInputsStep() {
        const drivers = this.currentDecision.top_drivers || [];
        return `
            <div class="driverList">
                ${drivers.length > 0 ? drivers.map(driver => `
                    <div class="driverItem impact-${driver.impact}">
                        <span class="driverName">${driver.name}</span>
                        <span class="driverValue">${typeof driver.value === 'number' ? driver.value.toFixed(3) : driver.value}</span>
                        ${driver.model ? `<span class="driverModel">${driver.model}</span>` : ''}
                    </div>
                `).join('') : '<div class="noData">No input drivers available</div>'}
            </div>
        `;
    }

    renderModelsStep() {
        const outputs = this.currentDecision.model_outputs || [];
        return `
            <div class="modelOutputs">
                ${outputs.length > 0 ? outputs.map(output => `
                    <div class="modelOutput">
                        <div class="modelInfo">
                            <span class="modelName">${output.model_name}</span>
                            <span class="modelKind">${output.model_kind}</span>
                        </div>
                        <div class="modelPrediction">
                            <span class="zScore">z=${output.predicted_z.toFixed(2)}</span>
                            <span class="interpretation">${output.interpretation}</span>
                            <span class="confidence">${(output.confidence * 100).toFixed(1)}% confidence</span>
                        </div>
                    </div>
                `).join('') : '<div class="noData">No model outputs available</div>'}
            </div>
        `;
    }

    renderRiskStep() {
        const gates = this.currentDecision.risk_gates_triggered || [];
        return `
            <div class="riskGates">
                ${gates.length > 0 ? gates.map(gate => `
                    <div class="riskGate triggered">
                        <span class="gateName">${gate.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase())}</span>
                        <span class="gateStatus">⚠️ Triggered</span>
                    </div>
                `).join('') : '<div class="noData">No risk gates triggered</div>'}
            </div>
        `;
    }

    renderAllocationStep() {
        const alloc = this.currentDecision.allocation_before_after || {};
        return `
            <div class="allocationChange">
                <div class="allocationBar">
                    <div class="allocSegment before" style="width: ${(alloc.before || 0) * 100}%">
                        Before: ${((alloc.before || 0) * 100).toFixed(1)}%
                    </div>
                    <div class="allocSegment after" style="width: ${(alloc.after || 0) * 100}%">
                        After: ${((alloc.after || 0) * 100).toFixed(1)}%
                    </div>
                </div>
                <div class="allocationDetails">
                    <span>Change: ${((alloc.change || 0) * 100).toFixed(2)}%</span>
                    <span>Size: ${this.currentDecision.size_delta.toFixed(4)}</span>
                </div>
            </div>
        `;
    }

    renderActionStep() {
        return `
            <div class="finalAction">
                <div class="actionBadge action-${this.currentDecision.action}">
                    ${this.currentDecision.action.toUpperCase()}
                </div>
                <div class="actionDetails">
                    <div>Symbol: ${this.currentDecision.symbol}</div>
                    <div>Risk Impact: ${this.currentDecision.risk_impact}</div>
                    <div>Confidence: ${(this.currentDecision.certainty * 100).toFixed(1)}%</div>
                </div>
            </div>
        `;
    }

    renderPlainExplanation() {
        return `
            <div class="plainExplanationSection">
                <h3>📝 Plain English Explanation</h3>
                <div class="explanationText">
                    ${this.currentDecision.plain_english_explanation || 'No explanation available'}
                </div>
            </div>
        `;
    }

    renderTechnicalDetails() {
        return `
            <div class="technicalSection">
                <h3>🔧 Technical Details</h3>
                <div class="expandableSection" data-section="inputs-summary">
                    <div class="sectionHeader" onclick="this.parentElement.classList.toggle('expanded')">
                        <span>📊 Inputs Summary</span>
                        <span class="expandIcon">▶</span>
                    </div>
                    <div class="sectionContent">
                        ${this.renderInputsSummary()}
                    </div>
                </div>
                
                <div class="expandableSection" data-section="decision-logs">
                    <div class="sectionHeader" onclick="this.parentElement.classList.toggle('expanded')">
                        <span>📋 Decision Logs</span>
                        <span class="expandIcon">▶</span>
                    </div>
                    <div class="sectionContent">
                        ${this.renderDecisionLogs()}
                    </div>
                </div>
                
                <div class="expandableSection" data-section="model-versions">
                    <div class="sectionHeader" onclick="this.parentElement.classList.toggle('expanded')">
                        <span>🏷️ Model Versions</span>
                        <span class="expandIcon">▶</span>
                    </div>
                    <div class="sectionContent">
                        ${this.renderModelVersions()}
                    </div>
                </div>
            </div>
        `;
    }

    renderInputsSummary() {
        const inputs = this.currentDecision.inputs_summary || {};
        return `
            <div class="inputsGrid">
                <div class="inputItem">
                    <label>From Weight:</label>
                    <span>${(inputs.from_weight || 0).toFixed(4)}</span>
                </div>
                <div class="inputItem">
                    <label>To Weight:</label>
                    <span>${(inputs.to_weight || 0).toFixed(4)}</span>
                </div>
                <div class="inputItem">
                    <label>Current Side:</label>
                    <span>${inputs.current_side || 'Unknown'}</span>
                </div>
                <div class="inputItem">
                    <label>Current Weight:</label>
                    <span>${(inputs.current_weight || 0).toFixed(4)}</span>
                </div>
            </div>
        `;
    }

    renderDecisionLogs() {
        const logs = this.currentDecision.decision_logs || [];
        if (logs.length === 0) {
            return '<div class="noData">No decision logs available</div>';
        }
        
        return `
            <div class="logsContainer">
                ${logs.map(log => `
                    <div class="logEntry">
                        <div class="logHeader">
                            <span class="logModel">${log.model_name}</span>
                            <span class="logTime">${fmtTime(log.model_ts_ms)}</span>
                        </div>
                        <div class="logDetails">
                            <div>Predicted Z: ${log.predicted_z.toFixed(3)}</div>
                            <div>Confidence: ${(log.confidence * 100).toFixed(1)}%</div>
                        </div>
                    </div>
                `).join('')}
            </div>
        `;
    }

    renderModelVersions() {
        const versions = this.currentDecision.model_versions || [];
        if (versions.length === 0) {
            return '<div class="noData">No model versions available</div>';
        }
        
        return `
            <div class="versionsList">
                ${versions.map(version => `
                    <div class="versionItem">${version}</div>
                `).join('')}
            </div>
        `;
    }

    startAnimation() {
        this.isAnimating = true;
        this.animationStep = 0;
        
        const steps = document.querySelectorAll('.flowStep');
        steps.forEach((step, index) => {
            step.style.opacity = '0';
            step.style.transform = 'translateY(20px)';
        });

        // Animate steps sequentially
        steps.forEach((step, index) => {
            setTimeout(() => {
                step.style.transition = 'all 0.6s cubic-bezier(0.4, 0, 0.2, 1)';
                step.style.opacity = '1';
                step.style.transform = 'translateY(0)';
                
                // Add pulse effect to current step
                const bubble = step.querySelector('.stepBubble');
                if (bubble) {
                    bubble.classList.add('pulse');
                    setTimeout(() => bubble.classList.remove('pulse'), 600);
                }
                
                this.animationStep = index + 1;
                
                // Complete animation when done
                if (index === steps.length - 1) {
                    setTimeout(() => {
                        this.isAnimating = false;
                    }, 600);
                }
            }, index * 400);
        });
    }

    showError(message) {
        const modal = document.createElement('div');
        modal.className = 'explainModal';
        modal.innerHTML = `
            <div class="explainOverlay" onclick="this.parentElement.remove()"></div>
            <div class="explainPanel">
                <div class="explainHeader">
                    <h2>❌ Error</h2>
                    <button class="btn btnSmall" onclick="this.closest('.explainModal').remove()">✕</button>
                </div>
                <div class="explainContent">
                    <div class="errorMessage">${message}</div>
                </div>
            </div>
        `;
        document.body.appendChild(modal);
    }
}

// Global instance
window.decisionExplainer = new DecisionExplainer();

// CSS for the explainer UI
const explainerCSS = `
.explainModal {
    position: fixed;
    inset: 0;
    z-index: 3000;
    display: flex;
    align-items: center;
    justify-content: center;
}

.explainOverlay {
    position: absolute;
    inset: 0;
    background: rgba(0, 0, 0, 0.7);
    backdrop-filter: blur(4px);
}

.explainPanel {
    position: relative;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 14px;
    box-shadow: 0 20px 40px rgba(0, 0, 0, 0.4);
    max-width: min(900px, 90vw);
    max-height: 85vh;
    width: 90vw;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    animation: explainPanelIn 0.3s cubic-bezier(0.4, 0, 0.2, 1);
}

@keyframes explainPanelIn {
    from {
        opacity: 0;
        transform: scale(0.95) translateY(10px);
    }
    to {
        opacity: 1;
        transform: scale(1) translateY(0);
    }
}

.explainHeader {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 16px 20px;
    border-bottom: 1px solid var(--border);
    background: linear-gradient(to bottom, var(--panel), rgba(11, 15, 21, 0.8));
}

.explainHeader h2 {
    margin: 0;
    font-size: 18px;
    font-weight: 700;
}

.explainContent {
    flex: 1;
    overflow-y: auto;
    padding: 20px;
}

.backtraceSection {
    margin-bottom: 24px;
}

.backtraceSection h3 {
    margin: 0 0 16px;
    font-size: 16px;
    font-weight: 600;
}

.flowContainer {
    display: flex;
    flex-direction: column;
    gap: 20px;
}

.flowStep {
    display: flex;
    align-items: flex-start;
    gap: 16px;
    position: relative;
}

.stepConnector {
    position: absolute;
    left: 20px;
    top: 40px;
    width: 2px;
    height: 20px;
    background: var(--border);
    transition: background 0.3s ease;
}

.stepConnector.active {
    background: var(--info);
}

.stepBubble {
    position: relative;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 40px;
    height: 40px;
    border-radius: 50%;
    background: var(--btn);
    border: 2px solid var(--border);
    flex-shrink: 0;
    transition: all 0.3s ease;
}

.stepBubble.pulse {
    animation: stepPulse 0.6s ease;
}

@keyframes stepPulse {
    0%, 100% { transform: scale(1); }
    50% { transform: scale(1.1); box-shadow: 0 0 20px rgba(88, 166, 255, 0.4); }
}

.stepIcon {
    font-size: 16px;
}

.stepNumber {
    position: absolute;
    top: -8px;
    right: -8px;
    background: var(--info);
    color: white;
    font-size: 10px;
    font-weight: 700;
    padding: 2px 4px;
    border-radius: 10px;
}

.stepContent {
    flex: 1;
    min-width: 0;
}

.stepContent h4 {
    margin: 0 0 8px;
    font-size: 14px;
    font-weight: 600;
}

.stepDetails {
    font-size: 13px;
}

.driverList {
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.driverItem {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 12px;
    border-radius: 8px;
    background: var(--btn);
    border: 1px solid var(--border);
}

.driverItem.impact-high {
    border-color: var(--crit);
    background: rgba(255, 107, 107, 0.1);
}

.driverItem.impact-medium {
    border-color: var(--warn);
    background: rgba(210, 153, 34, 0.1);
}

.driverName {
    font-weight: 600;
}

.driverValue {
    margin-left: auto;
    font-family: monospace;
}

.driverModel {
    font-size: 11px;
    color: var(--muted);
}

.modelOutputs {
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.modelOutput {
    padding: 8px 12px;
    border-radius: 8px;
    background: var(--btn);
    border: 1px solid var(--border);
}

.modelInfo {
    display: flex;
    gap: 8px;
    margin-bottom: 4px;
}

.modelName {
    font-weight: 600;
}

.modelKind {
    font-size: 11px;
    color: var(--muted);
}

.modelPrediction {
    display: flex;
    gap: 12px;
    font-size: 12px;
}

.zScore {
    font-family: monospace;
    font-weight: 600;
}

.interpretation {
    color: var(--muted);
}

.confidence {
    margin-left: auto;
}

.riskGates {
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.riskGate {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 12px;
    border-radius: 8px;
    background: var(--btn);
    border: 1px solid var(--border);
}

.riskGate.triggered {
    border-color: var(--crit);
    background: rgba(255, 107, 107, 0.1);
}

.gateName {
    font-weight: 600;
}

.gateStatus {
    margin-left: auto;
}

.allocationChange {
    padding: 12px;
    border-radius: 8px;
    background: var(--btn);
    border: 1px solid var(--border);
}

.allocationBar {
    display: flex;
    height: 24px;
    border-radius: 4px;
    overflow: hidden;
    margin-bottom: 8px;
}

.allocSegment {
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 11px;
    font-weight: 600;
    color: white;
}

.allocSegment.before {
    background: var(--muted);
}

.allocSegment.after {
    background: var(--info);
}

.allocationDetails {
    display: flex;
    gap: 16px;
    font-size: 12px;
}

.finalAction {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 12px;
    border-radius: 8px;
    background: var(--btn);
    border: 1px solid var(--border);
}

.actionBadge {
    padding: 8px 16px;
    border-radius: 20px;
    font-weight: 700;
    text-transform: uppercase;
    font-size: 12px;
}

.actionBadge.action-increase {
    background: var(--ok);
    color: white;
}

.actionBadge.action-reduce {
    background: var(--warn);
    color: black;
}

.actionBadge.action-hold {
    background: var(--muted);
    color: white;
}

.actionDetails {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 12px;
}

.plainExplanationSection {
    margin-bottom: 24px;
}

.explanationText {
    padding: 16px;
    border-radius: 8px;
    background: linear-gradient(135deg, rgba(88, 166, 255, 0.1), rgba(88, 166, 255, 0.05));
    border: 1px solid rgba(88, 166, 255, 0.3);
    font-size: 14px;
    line-height: 1.5;
}

.technicalSection h3 {
    margin: 0 0 16px;
    font-size: 16px;
    font-weight: 600;
}

.expandableSection {
    margin-bottom: 12px;
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
}

.sectionHeader {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 16px;
    background: var(--btn);
    cursor: pointer;
    user-select: none;
    transition: background 0.2s ease;
}

.sectionHeader:hover {
    background: rgba(88, 166, 255, 0.1);
}

.expandIcon {
    transition: transform 0.2s ease;
}

.expandableSection.expanded .expandIcon {
    transform: rotate(90deg);
}

.sectionContent {
    max-height: 0;
    overflow: hidden;
    transition: max-height 0.3s ease;
}

.expandableSection.expanded .sectionContent {
    max-height: 500px;
    overflow-y: auto;
}

.inputsGrid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 12px;
    padding: 16px;
}

.inputItem {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 8px 12px;
    background: var(--panel);
    border-radius: 6px;
}

.inputItem label {
    font-size: 12px;
    color: var(--muted);
}

.logsContainer {
    max-height: 300px;
    overflow-y: auto;
    padding: 16px;
}

.logEntry {
    padding: 12px;
    border-radius: 6px;
    background: var(--panel);
    margin-bottom: 8px;
}

.logHeader {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
}

.logModel {
    font-weight: 600;
}

.logTime {
    font-size: 11px;
    color: var(--muted);
}

.logDetails {
    display: flex;
    gap: 16px;
    font-size: 12px;
}

.versionsList {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    padding: 16px;
}

.versionItem {
    padding: 6px 12px;
    background: var(--panel);
    border-radius: 16px;
    font-size: 12px;
    font-family: monospace;
}

.noData {
    padding: 20px;
    text-align: center;
    color: var(--muted);
    font-style: italic;
}

.errorMessage {
    padding: 20px;
    text-align: center;
    color: var(--crit);
    font-weight: 600;
}
`;

// Inject CSS
if (!document.querySelector('#decision-explain-css')) {
    const style = document.createElement('style');
    style.id = 'decision-explain-css';
    style.textContent = explainerCSS;
    document.head.appendChild(style);
}

export { DecisionExplainer };
