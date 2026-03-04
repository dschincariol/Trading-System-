"""
Explainable Reporting and Visualization System

Comprehensive reporting system providing explainable insights, visualizations,
and detailed analysis of research budget allocation decisions.
"""

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
import logging
from datetime import datetime, timedelta
import json
from pathlib import Path
import base64
from io import BytesIO
from research_budget_allocator import ResearchAsset, AllocationDecision, AllocationStrategy
from exploration_exploitation_manager import ExplorationStrategy, ExplorationMetrics
from stop_criteria_manager import StopReason, StopDecision
from budget_reallocator import ReallocationEvent, ReallocationStrategy
from safety_constraints import ConstraintViolation, ConstraintSeverity

logger = logging.getLogger(__name__)

class ReportType(Enum):
    SUMMARY = "summary"
    DETAILED = "detailed"
    EXECUTIVE = "executive"
    TECHNICAL = "technical"
    COMPLIANCE = "compliance"

class VisualizationType(Enum):
    ALLOCATION_PIE = "allocation_pie"
    PERFORMANCE_TREND = "performance_trend"
    BUDGET_UTILIZATION = "budget_utilization"
    EXPLORATION_EXPLOITATION = "exploration_exploitation"
    RISK_HEATMAP = "risk_heatmap"
    ROI_SCATTER = "roi_scatter"
    DECISION_TREE = "decision_tree"
    TIMELINE = "timeline"

@dataclass
class ReportSection:
    """Represents a section of the report"""
    title: str
    content: str
    visualizations: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    insights: List[str] = field(default_factory=list)

@dataclass
class Explanation:
    """Represents an explanation for a decision or metric"""
    decision_type: str
    input_data: Dict[str, Any]
    reasoning: str
    confidence: float
    factors: List[Tuple[str, float, str]]  # (factor, weight, description)
    alternatives_considered: List[str]
    timestamp: datetime = field(default_factory=datetime.now)

class ExplainableReportingSystem:
    """Main explainable reporting and visualization system"""
    
    def __init__(self, output_dir: str = "reports"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        
        # Report configuration
        self.report_templates = self._initialize_templates()
        self.visualization_configs = self._initialize_visualization_configs()
        
        # Data storage
        self.explanations: List[Explanation] = []
        self.report_cache: Dict[str, Dict] = {}
        
        # Visualization settings
        plt.style.use('seaborn-v0_8')
        sns.set_palette("husl")
        
        logger.info(f"Initialized ExplainableReportingSystem with output directory: {self.output_dir}")
    
    def _initialize_templates(self) -> Dict[ReportType, Dict]:
        """Initialize report templates"""
        return {
            ReportType.SUMMARY: {
                "sections": ["overview", "key_metrics", "performance_summary", "recommendations"],
                "detail_level": "high",
                "include_visualizations": True
            },
            ReportType.DETAILED: {
                "sections": ["overview", "allocation_analysis", "performance_analysis", 
                           "risk_analysis", "exploration_analysis", "stop_decisions", 
                           "reallocation_history", "technical_details"],
                "detail_level": "comprehensive",
                "include_visualizations": True
            },
            ReportType.EXECUTIVE: {
                "sections": ["executive_summary", "key_insights", "roi_analysis", "strategic_recommendations"],
                "detail_level": "strategic",
                "include_visualizations": True
            },
            ReportType.TECHNICAL: {
                "sections": ["system_overview", "algorithm_analysis", "performance_metrics", 
                           "constraint_analysis", "determinism_analysis"],
                "detail_level": "technical",
                "include_visualizations": True
            },
            ReportType.COMPLIANCE: {
                "sections": ["compliance_summary", "audit_trail", "constraint_compliance", 
                           "decision_accountability", "risk_assessment"],
                "detail_level": "compliance",
                "include_visualizations": False
            }
        }
    
    def _initialize_visualization_configs(self) -> Dict[VisualizationType, Dict]:
        """Initialize visualization configurations"""
        return {
            VisualizationType.ALLOCATION_PIE: {
                "title": "Budget Allocation Distribution",
                "figsize": (10, 8),
                "save_format": "png"
            },
            VisualizationType.PERFORMANCE_TREND: {
                "title": "Performance Trends Over Time",
                "figsize": (12, 6),
                "save_format": "png"
            },
            VisualizationType.BUDGET_UTILIZATION: {
                "title": "Budget Utilization by Asset",
                "figsize": (10, 6),
                "save_format": "png"
            },
            VisualizationType.EXPLORATION_EXPLOITATION: {
                "title": "Exploration vs Exploitation Balance",
                "figsize": (8, 6),
                "save_format": "png"
            },
            VisualizationType.RISK_HEATMAP: {
                "title": "Risk Assessment Heatmap",
                "figsize": (12, 8),
                "save_format": "png"
            },
            VisualizationType.ROI_SCATTER: {
                "title": "ROI Analysis",
                "figsize": (10, 8),
                "save_format": "png"
            },
            VisualizationType.DECISION_TREE: {
                "title": "Decision Flow Analysis",
                "figsize": (12, 10),
                "save_format": "png"
            },
            VisualizationType.TIMELINE: {
                "title": "Allocation Timeline",
                "figsize": (14, 6),
                "save_format": "png"
            }
        }
    
    def generate_comprehensive_report(self, report_type: ReportType, 
                                    assets: Dict[str, ResearchAsset],
                                    allocation_decisions: List[AllocationDecision],
                                    exploration_metrics: Optional[ExplorationMetrics] = None,
                                    stop_decisions: Optional[Dict[str, List[StopDecision]]] = None,
                                    reallocation_events: Optional[List[ReallocationEvent]] = None,
                                    constraint_violations: Optional[List[ConstraintViolation]] = None,
                                    explanations: Optional[List[Explanation]] = None) -> str:
        """Generate comprehensive explainable report"""
        
        template = self.report_templates[report_type]
        sections = {}
        
        # Generate each section
        for section_name in template["sections"]:
            section = self._generate_section(section_name, assets, allocation_decisions, 
                                           exploration_metrics, stop_decisions, 
                                           reallocation_events, constraint_violations, explanations)
            sections[section_name] = section
        
        # Generate visualizations if requested
        visualizations = {}
        if template["include_visualizations"]:
            visualizations = self._generate_visualizations(assets, allocation_decisions, 
                                                         exploration_metrics, stop_decisions)
        
        # Compile report
        report = {
            "metadata": {
                "report_type": report_type.value,
                "generated_at": datetime.now().isoformat(),
                "detail_level": template["detail_level"],
                "total_assets": len(assets),
                "total_decisions": len(allocation_decisions)
            },
            "sections": sections,
            "visualizations": visualizations,
            "appendix": self._generate_appendix(assets, explanations)
        }
        
        # Save report
        filename = f"{report_type.value}_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        filepath = self.output_dir / filename
        
        with open(filepath, 'w') as f:
            json.dump(report, f, indent=2, default=str)
        
        logger.info(f"Generated {report_type.value} report: {filepath}")
        return str(filepath)
    
    def _generate_section(self, section_name: str, assets: Dict[str, ResearchAsset],
                         allocation_decisions: List[AllocationDecision],
                         exploration_metrics: Optional[ExplorationMetrics],
                         stop_decisions: Optional[Dict[str, List[StopDecision]]],
                         reallocation_events: Optional[List[ReallocationEvent]],
                         constraint_violations: Optional[List[ConstraintViolation]],
                         explanations: Optional[List[Explanation]]) -> ReportSection:
        """Generate a specific report section"""
        
        if section_name == "overview":
            return self._generate_overview_section(assets, allocation_decisions)
        elif section_name == "key_metrics":
            return self._generate_key_metrics_section(assets, allocation_decisions)
        elif section_name == "performance_summary":
            return self._generate_performance_summary_section(assets)
        elif section_name == "recommendations":
            return self._generate_recommendations_section(assets, allocation_decisions, 
                                                        exploration_metrics, stop_decisions)
        elif section_name == "allocation_analysis":
            return self._generate_allocation_analysis_section(assets, allocation_decisions)
        elif section_name == "risk_analysis":
            return self._generate_risk_analysis_section(assets, constraint_violations)
        elif section_name == "exploration_analysis":
            return self._generate_exploration_analysis_section(exploration_metrics)
        elif section_name == "executive_summary":
            return self._generate_executive_summary_section(assets, allocation_decisions)
        elif section_name == "technical_details":
            return self._generate_technical_details_section(assets, allocation_decisions)
        else:
            return ReportSection(title=section_name.replace("_", " ").title(), 
                               content="Section not implemented")
    
    def _generate_overview_section(self, assets: Dict[str, ResearchAsset], 
                                 allocation_decisions: List[AllocationDecision]) -> ReportSection:
        """Generate overview section"""
        total_budget = sum(asset.current_budget for asset in assets.values())
        total_spent = sum(asset.total_spent for asset in assets.values())
        active_assets = len([a for a in assets.values() if a.status.value == "active"])
        
        content = f"""
        # Research Budget Allocation Overview
        
        This report provides a comprehensive analysis of the research budget allocation system's performance 
        and decision-making process.
        
        ## Key Statistics
        - **Total Budget**: ${total_budget:,.2f}
        - **Total Spent**: ${total_spent:,.2f}
        - **Remaining Budget**: ${total_budget - total_spent:,.2f}
        - **Active Assets**: {active_assets} out of {len(assets)}
        - **Allocation Decisions**: {len(allocation_decisions)}
        
        ## System Performance
        The allocation system has processed {len(assets)} research assets across multiple categories, 
        with an average allocation confidence of {self._calculate_avg_confidence(assets):.1%}.
        """
        
        metrics = {
            "total_budget": total_budget,
            "total_spent": total_spent,
            "remaining_budget": total_budget - total_spent,
            "active_assets": active_assets,
            "total_assets": len(assets),
            "avg_confidence": self._calculate_avg_confidence(assets)
        }
        
        insights = [
            f"System is operating with {total_budget - total_spent:,.2f} remaining budget",
            f"{active_assets} assets are currently active and receiving allocations",
            f"Average confidence across all assets is {metrics['avg_confidence']:.1%}"
        ]
        
        return ReportSection(
            title="Overview",
            content=content,
            metrics=metrics,
            insights=insights
        )
    
    def _generate_key_metrics_section(self, assets: Dict[str, ResearchAsset], 
                                   allocation_decisions: List[AllocationDecision]) -> ReportSection:
        """Generate key metrics section"""
        # Calculate key metrics
        total_allocated = sum(decision.allocated_amount for decision in allocation_decisions)
        avg_performance = np.mean([asset.expected_payoff for asset in assets.values() if asset.expected_payoff > 0])
        total_compute_hours = sum(asset.compute_hours_used for asset in assets.values())
        
        # ROI calculations
        roi_values = []
        for asset in assets.values():
            if asset.total_spent > 0:
                roi = asset.expected_payoff / (asset.total_spent / 1000)  # Performance per $1000
                roi_values.append(roi)
        
        avg_roi = np.mean(roi_values) if roi_values else 0
        
        content = f"""
        # Key Performance Metrics
        
        ## Allocation Metrics
        - **Total Allocated**: ${total_allocated:,.2f}
        - **Average Performance**: {avg_performance:.3f}
        - **Average ROI**: {avg_roi:.2f} (performance per $1000 spent)
        
        ## Resource Utilization
        - **Total Compute Hours**: {total_compute_hours:,.0f}
        - **Compute Efficiency**: {self._calculate_compute_efficiency(assets):.3f}
        
        ## Distribution Analysis
        - **Allocation Concentration**: {self._calculate_concentration(assets):.1%}
        - **Diversification Score**: {self._calculate_diversification_score(assets):.3f}
        """
        
        metrics = {
            "total_allocated": total_allocated,
            "avg_performance": avg_performance,
            "avg_roi": avg_roi,
            "total_compute_hours": total_compute_hours,
            "compute_efficiency": self._calculate_compute_efficiency(assets),
            "allocation_concentration": self._calculate_concentration(assets),
            "diversification_score": self._calculate_diversification_score(assets)
        }
        
        return ReportSection(
            title="Key Metrics",
            content=content,
            metrics=metrics
        )
    
    def _generate_performance_summary_section(self, assets: Dict[str, ResearchAsset]) -> ReportSection:
        """Generate performance summary section"""
        # Performance analysis
        performances = [asset.expected_payoff for asset in assets.values() if asset.expected_payoff > 0]
        
        if performances:
            top_performer = max(assets.values(), key=lambda a: a.expected_payoff)
            worst_performer = min(assets.values(), key=lambda a: a.expected_payoff if a.expected_payoff > 0 else float('inf'))
            
            content = f"""
            # Performance Analysis
            
            ## Performance Distribution
            - **Average Performance**: {np.mean(performances):.3f}
            - **Performance Std Dev**: {np.std(performances):.3f}
            - **Top Performer**: {top_performer.name} ({top_performer.expected_payoff:.3f})
            - **Worst Performer**: {worst_performer.name} ({worst_performer.expected_payoff:.3f})
            
            ## Performance Categories
            - **High Performers (>0.7)**: {len([p for p in performances if p > 0.7])}
            - **Medium Performers (0.3-0.7)**: {len([p for p in performances if 0.3 <= p <= 0.7])}
            - **Low Performers (<0.3)**: {len([p for p in performances if p < 0.3])}
            
            ## Performance Trends
            The system shows {self._analyze_performance_trend(assets)} trend in overall performance.
            """
        else:
            content = "# Performance Analysis\n\nNo performance data available for analysis."
        
        return ReportSection(
            title="Performance Summary",
            content=content
        )
    
    def _generate_recommendations_section(self, assets: Dict[str, ResearchAsset],
                                        allocation_decisions: List[AllocationDecision],
                                        exploration_metrics: Optional[ExplorationMetrics],
                                        stop_decisions: Optional[Dict[str, List[StopDecision]]]) -> ReportSection:
        """Generate recommendations section"""
        recommendations = []
        
        # Analyze performance and generate recommendations
        low_performers = [asset for asset in assets.values() if asset.expected_payoff < 0.3]
        if low_performers:
            recommendations.append(f"Consider stopping or reallocating budget from {len(low_performers)} low-performing assets")
        
        # Analyze budget utilization
        underutilized = [asset for asset in assets.values() 
                        if asset.current_budget > asset.initial_budget * 0.8]
        if underutilized:
            recommendations.append(f"Reallocate budget from {len(underutilized)} underutilized assets")
        
        # Exploration recommendations
        if exploration_metrics:
            if exploration_metrics.exploration_rate < 0.2:
                recommendations.append("Increase exploration rate to discover new opportunities")
            elif exploration_metrics.exploration_rate > 0.5:
                recommendations.append("Consider reducing exploration in favor of exploitation")
        
        # Stop decision recommendations
        if stop_decisions:
            stopped_count = sum(1 for decisions in stop_decisions.values() 
                              if any(d.should_stop for d in decisions))
            if stopped_count > 0:
                recommendations.append(f"Review and process {stopped_count} assets recommended for stopping")
        
        content = "# Strategic Recommendations\n\n"
        if recommendations:
            for i, rec in enumerate(recommendations, 1):
                content += f"{i}. {rec}\n"
        else:
            content += "System is operating optimally. No immediate recommendations."
        
        return ReportSection(
            title="Recommendations",
            content=content,
            insights=recommendations
        )
    
    def _generate_visualizations(self, assets: Dict[str, ResearchAsset],
                               allocation_decisions: List[AllocationDecision],
                               exploration_metrics: Optional[ExplorationMetrics],
                               stop_decisions: Optional[Dict[str, List[StopDecision]]]) -> Dict[str, str]:
        """Generate all visualizations"""
        visualizations = {}
        
        try:
            # Allocation pie chart
            visualizations["allocation_pie"] = self._create_allocation_pie_chart(assets)
            
            # Performance trends
            visualizations["performance_trend"] = self._create_performance_trend_chart(assets)
            
            # Budget utilization
            visualizations["budget_utilization"] = self._create_budget_utilization_chart(assets)
            
            # Risk heatmap
            visualizations["risk_heatmap"] = self._create_risk_heatmap(assets)
            
            # ROI scatter plot
            visualizations["roi_scatter"] = self._create_roi_scatter_plot(assets)
            
        except Exception as e:
            logger.error(f"Error generating visualizations: {e}")
        
        return visualizations
    
    def _create_allocation_pie_chart(self, assets: Dict[str, ResearchAsset]) -> str:
        """Create allocation pie chart"""
        config = self.visualization_configs[VisualizationType.ALLOCATION_PIE]
        
        fig, ax = plt.subplots(figsize=config["figsize"])
        
        # Prepare data
        asset_names = [asset.name for asset in assets.values() if asset.current_budget > 0]
        budgets = [asset.current_budget for asset in assets.values() if asset.current_budget > 0]
        
        if budgets:
            wedges, texts, autotexts = ax.pie(budgets, labels=asset_names, autopct='%1.1f%%', 
                                             startangle=90, textprops={'fontsize': 10})
            ax.set_title(config["title"], fontsize=14, fontweight='bold')
            
            # Save to base64
            buffer = BytesIO()
            plt.savefig(buffer, format=config["save_format"], bbox_inches='tight', dpi=300)
            buffer.seek(0)
            image_base64 = base64.b64encode(buffer.getvalue()).decode()
            plt.close()
            
            return f"data:image/{config['save_format']};base64,{image_base64}"
        
        return ""
    
    def _create_performance_trend_chart(self, assets: Dict[str, ResearchAsset]) -> str:
        """Create performance trend chart"""
        config = self.visualization_configs[VisualizationType.PERFORMANCE_TREND]
        
        fig, ax = plt.subplots(figsize=config["figsize"])
        
        # Plot performance trends for assets with history
        for asset in assets.values():
            if asset.recent_performance:
                x = range(len(asset.recent_performance))
                ax.plot(x, asset.recent_performance, marker='o', label=asset.name, alpha=0.7)
        
        ax.set_title(config["title"], fontsize=14, fontweight='bold')
        ax.set_xlabel('Time Period')
        ax.set_ylabel('Performance')
        ax.legend(bbox_to_anchor=(1.05, 1), loc='upper left')
        ax.grid(True, alpha=0.3)
        
        # Save to base64
        buffer = BytesIO()
        plt.savefig(buffer, format=config["save_format"], bbox_inches='tight', dpi=300)
        buffer.seek(0)
        image_base64 = base64.b64encode(buffer.getvalue()).decode()
        plt.close()
        
        return f"data:image/{config['save_format']};base64,{image_base64}"
    
    def _create_budget_utilization_chart(self, assets: Dict[str, ResearchAsset]) -> str:
        """Create budget utilization chart"""
        config = self.visualization_configs[VisualizationType.BUDGET_UTILIZATION]
        
        fig, ax = plt.subplots(figsize=config["figsize"])
        
        asset_names = [asset.name for asset in assets.values()]
        utilization_ratios = [(asset.initial_budget - asset.current_budget) / asset.initial_budget 
                             if asset.initial_budget > 0 else 0 
                             for asset in assets.values()]
        
        bars = ax.bar(asset_names, utilization_ratios, alpha=0.7)
        ax.set_title(config["title"], fontsize=14, fontweight='bold')
        ax.set_ylabel('Utilization Ratio')
        ax.set_ylim(0, 1)
        
        # Add percentage labels
        for bar, ratio in zip(bars, utilization_ratios):
            height = bar.get_height()
            ax.text(bar.get_x() + bar.get_width()/2., height + 0.01,
                   f'{ratio:.1%}', ha='center', va='bottom')
        
        plt.xticks(rotation=45, ha='right')
        plt.tight_layout()
        
        # Save to base64
        buffer = BytesIO()
        plt.savefig(buffer, format=config["save_format"], bbox_inches='tight', dpi=300)
        buffer.seek(0)
        image_base64 = base64.b64encode(buffer.getvalue()).decode()
        plt.close()
        
        return f"data:image/{config['save_format']};base64,{image_base64}"
    
    def _create_risk_heatmap(self, assets: Dict[str, ResearchAsset]) -> str:
        """Create risk assessment heatmap"""
        config = self.visualization_configs[VisualizationType.RISK_HEATMAP]
        
        fig, ax = plt.subplots(figsize=config["figsize"])
        
        # Create risk matrix
        asset_names = [asset.name for asset in assets.values()]
        risk_factors = ['Budget Risk', 'Performance Risk', 'Confidence Risk', 'Exploration Risk']
        
        risk_matrix = np.zeros((len(risk_factors), len(assets)))
        
        for i, asset in enumerate(assets.values()):
            # Budget risk (low budget = high risk)
            budget_risk = 1 - (asset.current_budget / asset.initial_budget) if asset.initial_budget > 0 else 0
            risk_matrix[0, i] = budget_risk
            
            # Performance risk (low performance = high risk)
            perf_risk = 1 - asset.expected_payoff
            risk_matrix[1, i] = perf_risk
            
            # Confidence risk (low confidence = high risk)
            conf_risk = 1 - asset.confidence_score
            risk_matrix[2, i] = conf_risk
            
            # Exploration risk (high exploration = higher risk)
            expl_risk = asset.exploration_score / 2  # Normalize to 0-1
            risk_matrix[3, i] = expl_risk
        
        # Create heatmap
        im = ax.imshow(risk_matrix, cmap='RdYlBu_r', aspect='auto', vmin=0, vmax=1)
        ax.set_xticks(range(len(asset_names)))
        ax.set_yticks(range(len(risk_factors)))
        ax.set_xticklabels(asset_names, rotation=45, ha='right')
        ax.set_yticklabels(risk_factors)
        ax.set_title(config["title"], fontsize=14, fontweight='bold')
        
        # Add colorbar
        cbar = plt.colorbar(im, ax=ax)
        cbar.set_label('Risk Level')
        
        # Add text annotations
        for i in range(len(risk_factors)):
            for j in range(len(asset_names)):
                text = ax.text(j, i, f'{risk_matrix[i, j]:.2f}',
                             ha="center", va="center", color="black", fontsize=8)
        
        plt.tight_layout()
        
        # Save to base64
        buffer = BytesIO()
        plt.savefig(buffer, format=config["save_format"], bbox_inches='tight', dpi=300)
        buffer.seek(0)
        image_base64 = base64.b64encode(buffer.getvalue()).decode()
        plt.close()
        
        return f"data:image/{config['save_format']};base64,{image_base64}"
    
    def _create_roi_scatter_plot(self, assets: Dict[str, ResearchAsset]) -> str:
        """Create ROI scatter plot"""
        config = self.visualization_configs[VisualizationType.ROI_SCATTER]
        
        fig, ax = plt.subplots(figsize=config["figsize"])
        
        # Prepare data
        investments = []
        returns = []
        names = []
        sizes = []
        
        for asset in assets.values():
            if asset.total_spent > 0:
                investments.append(asset.total_spent / 1000)  # Convert to thousands
                returns.append(asset.expected_payoff)
                names.append(asset.name)
                sizes.append(asset.confidence_score * 100)  # Size based on confidence
        
        if investments:
            scatter = ax.scatter(investments, returns, s=sizes, alpha=0.6, c=returns, cmap='viridis')
            
            # Add labels for each point
            for i, name in enumerate(names):
                ax.annotate(name, (investments[i], returns[i]), fontsize=8, alpha=0.7)
            
            ax.set_xlabel('Investment ($1000s)')
            ax.set_ylabel('Expected Return')
            ax.set_title(config["title"], fontsize=14, fontweight='bold')
            ax.grid(True, alpha=0.3)
            
            # Add colorbar
            cbar = plt.colorbar(scatter, ax=ax)
            cbar.set_label('Expected Return')
        
        plt.tight_layout()
        
        # Save to base64
        buffer = BytesIO()
        plt.savefig(buffer, format=config["save_format"], bbox_inches='tight', dpi=300)
        buffer.seek(0)
        image_base64 = base64.b64encode(buffer.getvalue()).decode()
        plt.close()
        
        return f"data:image/{config['save_format']};base64,{image_base64}"
    
    def _calculate_avg_confidence(self, assets: Dict[str, ResearchAsset]) -> float:
        """Calculate average confidence across assets"""
        confidences = [asset.confidence_score for asset in assets.values()]
        return np.mean(confidences) if confidences else 0.0
    
    def _calculate_compute_efficiency(self, assets: Dict[str, ResearchAsset]) -> float:
        """Calculate compute efficiency"""
        total_performance = sum(asset.expected_payoff for asset in assets.values())
        total_compute = sum(asset.compute_hours_used for asset in assets.values())
        return total_performance / (total_compute + 1e-6)
    
    def _calculate_concentration(self, assets: Dict[str, ResearchAsset]) -> float:
        """Calculate allocation concentration (Herfindahl index)"""
        total_budget = sum(asset.current_budget for asset in assets.values())
        if total_budget == 0:
            return 0.0
        
        shares = [asset.current_budget / total_budget for asset in assets.values()]
        return sum(share ** 2 for share in shares)
    
    def _calculate_diversification_score(self, assets: Dict[str, ResearchAsset]) -> float:
        """Calculate diversification score"""
        concentration = self._calculate_concentration(assets)
        return 1 - concentration  # Higher diversification = lower concentration
    
    def _analyze_performance_trend(self, assets: Dict[str, ResearchAsset]) -> str:
        """Analyze overall performance trend"""
        all_performances = []
        for asset in assets.values():
            all_performances.extend(asset.recent_performance)
        
        if len(all_performances) < 10:
            return "insufficient data for"
        
        # Simple trend analysis
        recent_half = all_performances[-len(all_performances)//2:]
        early_half = all_performances[:len(all_performances)//2]
        
        recent_avg = np.mean(recent_half)
        early_avg = np.mean(early_half)
        
        if recent_avg > early_avg * 1.1:
            return "an improving"
        elif recent_avg < early_avg * 0.9:
            return "a declining"
        else:
            return "a stable"
    
    def _generate_appendix(self, assets: Dict[str, ResearchAsset], 
                         explanations: Optional[List[Explanation]]) -> Dict[str, Any]:
        """Generate appendix with technical details"""
        appendix = {
            "asset_details": {},
            "explanations": []
        }
        
        # Asset details
        for asset_id, asset in assets.items():
            appendix["asset_details"][asset_id] = {
                "name": asset.name,
                "category": asset.category,
                "status": asset.status.value,
                "initial_budget": asset.initial_budget,
                "current_budget": asset.current_budget,
                "total_spent": asset.total_spent,
                "expected_payoff": asset.expected_payoff,
                "confidence_score": asset.confidence_score,
                "exploration_score": asset.exploration_score,
                "compute_hours_used": asset.compute_hours_used,
                "data_processed": asset.data_processed,
                "recent_performance_count": len(asset.recent_performance)
            }
        
        # Explanations
        if explanations:
            for explanation in explanations:
                appendix["explanations"].append({
                    "decision_type": explanation.decision_type,
                    "reasoning": explanation.reasoning,
                    "confidence": explanation.confidence,
                    "factors": explanation.factors,
                    "timestamp": explanation.timestamp.isoformat()
                })
        
        return appendix
    
    def create_decision_explanation(self, decision_type: str, input_data: Dict[str, Any],
                                  reasoning: str, confidence: float,
                                  factors: List[Tuple[str, float, str]],
                                  alternatives_considered: List[str]) -> Explanation:
        """Create an explanation for a decision"""
        explanation = Explanation(
            decision_type=decision_type,
            input_data=input_data,
            reasoning=reasoning,
            confidence=confidence,
            factors=factors,
            alternatives_considered=alternatives_considered
        )
        
        self.explanations.append(explanation)
        return explanation
    
    def export_html_report(self, json_report_path: str) -> str:
        """Export JSON report as HTML with visualizations"""
        # Load JSON report
        with open(json_report_path, 'r') as f:
            report = json.load(f)
        
        # Generate HTML
        html_content = self._generate_html_report(report)
        
        # Save HTML
        html_path = json_report_path.replace('.json', '.html')
        with open(html_path, 'w') as f:
            f.write(html_content)
        
        logger.info(f"HTML report exported to: {html_path}")
        return html_path
    
    def _generate_html_report(self, report: Dict[str, Any]) -> str:
        """Generate HTML report from JSON data"""
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>{report['metadata']['report_type'].title()} Report</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 40px; }}
                .header {{ text-align: center; margin-bottom: 30px; }}
                .section {{ margin-bottom: 30px; }}
                .metrics {{ display: flex; flex-wrap: wrap; gap: 20px; }}
                .metric {{ background: #f5f5f5; padding: 15px; border-radius: 5px; }}
                .visualization {{ text-align: center; margin: 20px 0; }}
                img {{ max-width: 100%; height: auto; }}
                table {{ border-collapse: collapse; width: 100%; }}
                th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
                th {{ background-color: #f2f2f2; }}
            </style>
        </head>
        <body>
            <div class="header">
                <h1>{report['metadata']['report_type'].title()} Report</h1>
                <p>Generated: {report['metadata']['generated_at']}</p>
            </div>
        """
        
        # Add sections
        for section_name, section in report['sections'].items():
            html += f"""
            <div class="section">
                <h2>{section['title']}</h2>
                <div>{section['content'].replace(chr(10), '<br>')}</div>
            """
            
            # Add metrics if available
            if 'metrics' in section and section['metrics']:
                html += "<div class='metrics'>"
                for metric_name, metric_value in section['metrics'].items():
                    html += f"<div class='metric'><strong>{metric_name.replace('_', ' ').title()}:</strong> {metric_value}</div>"
                html += "</div>"
            
            # Add insights if available
            if 'insights' in section and section['insights']:
                html += "<h3>Key Insights</h3><ul>"
                for insight in section['insights']:
                    html += f"<li>{insight}</li>"
                html += "</ul>"
            
            html += "</div>"
        
        # Add visualizations
        if 'visualizations' in report and report['visualizations']:
            html += "<div class='section'><h2>Visualizations</h2>"
            for viz_name, viz_data in report['visualizations'].items():
                if viz_data:
                    html += f"<div class='visualization'><h3>{viz_name.replace('_', ' ').title()}</h3>"
                    html += f"<img src='{viz_data}' alt='{viz_name}'></div>"
            html += "</div>"
        
        html += """
        </body>
        </html>
        """
        
        return html

# Example usage
if __name__ == "__main__":
    from research_budget_allocator import ResearchAsset, AllocationDecision
    from exploration_exploitation_manager import ExplorationMetrics
    
    # Create reporting system
    reporter = ExplainableReportingSystem()
    
    # Create test data
    assets = {
        "asset1": ResearchAsset("asset1", "ML Model", "ml", 100000, 60000),
        "asset2": ResearchAsset("asset2", "CV Model", "cv", 100000, 40000),
        "asset3": ResearchAsset("asset3", "NLP Model", "nlp", 100000, 80000),
    }
    
    # Set performance data
    assets["asset1"].update_performance(0.8)
    assets["asset1"].confidence_score = 0.7
    assets["asset2"].update_performance(0.6)
    assets["asset2"].confidence_score = 0.6
    assets["asset3"].update_performance(0.9)
    assets["asset3"].confidence_score = 0.8
    
    # Create allocation decisions
    decisions = [
        AllocationDecision("asset1", 20000, "High performance", 0.8, 0.2, 0.1),
        AllocationDecision("asset2", 15000, "Moderate performance", 0.6, 0.3, 0.2),
        AllocationDecision("asset3", 25000, "Excellent performance", 0.9, 0.1, 0.1),
    ]
    
    # Create exploration metrics
    exploration_metrics = ExplorationMetrics(
        exploration_rate=0.3,
        exploitation_rate=0.7,
        total_trials=50,
        unique_assets_explored=3,
        convergence_score=0.8,
        diversity_index=0.7,
        timestamp=datetime.now().isoformat()
    )
    
    # Generate report
    report_path = reporter.generate_comprehensive_report(
        ReportType.SUMMARY,
        assets,
        decisions,
        exploration_metrics
    )
    
    print(f"Generated report: {report_path}")
    
    # Export HTML
    html_path = reporter.export_html_report(report_path)
    print(f"HTML report: {html_path}")
    
    # Create a decision explanation
    explanation = reporter.create_decision_explanation(
        decision_type="budget_allocation",
        input_data={"total_budget": 100000, "asset_performance": [0.8, 0.6, 0.9]},
        reasoning="Allocated based on performance-weighted distribution",
        confidence=0.85,
        factors=[
            ("performance_score", 0.7, "Expected payoff and confidence"),
            ("exploration_bonus", 0.2, "Uncertainty and discovery potential"),
            ("risk_adjustment", 0.1, "Risk mitigation factors")
        ],
        alternatives_considered=["equal_allocation", "performance_only", "exploration_only"]
    )
    
    print(f"Created explanation: {explanation.reasoning}")
