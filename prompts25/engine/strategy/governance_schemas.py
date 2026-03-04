"""
Governance Metadata Schemas
Defines comprehensive metadata schemas for model governance, audit, and compliance.
"""

import json
from typing import Dict, Any, List, Optional, Union
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timezone

class ComplianceLevel(Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"

class RiskCategory(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

class ModelType(Enum):
    PREDICTIVE = "predictive"
    CLASSIFICATION = "classification"
    REGRESSION = "regression"
    CLUSTERING = "clustering"
    ANOMALY_DETECTION = "anomaly_detection"
    TIME_SERIES = "time_series"
    REINFORCEMENT = "reinforcement"
    ENSEMBLE = "ensemble"

class ValidationStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PASSED = "passed"
    FAILED = "failed"
    EXEMPTED = "exempted"

@dataclass
class DataSchema:
    """Schema definition for model data"""
    schema_version: str
    features: List[Dict[str, Any]]
    target: Optional[Dict[str, Any]] = None
    preprocessing_steps: List[Dict[str, Any]] = field(default_factory=list)
    data_constraints: Dict[str, Any] = field(default_factory=dict)
    quality_requirements: Dict[str, Any] = field(default_factory=dict)

@dataclass
class PerformanceMetrics:
    """Standardized performance metrics"""
    accuracy: Optional[float] = None
    precision: Optional[float] = None
    recall: Optional[float] = None
    f1_score: Optional[float] = None
    auc_roc: Optional[float] = None
    rmse: Optional[float] = None
    mae: Optional[float] = None
    r2_score: Optional[float] = None
    sharpe_ratio: Optional[float] = None
    win_rate: Optional[float] = None
    profit_factor: Optional[float] = None
    max_drawdown: Optional[float] = None
    volatility: Optional[float] = None
    custom_metrics: Dict[str, float] = field(default_factory=dict)

@dataclass
class RiskMetrics:
    """Model risk assessment metrics"""
    model_risk_score: float  # 0-1 scale
    data_drift_score: float
    concept_drift_score: float
    prediction_confidence: float
    stability_score: float
    robustness_score: float
    fairness_metrics: Dict[str, float] = field(default_factory=dict)
    explainability_score: float
    uncertainty_quantification: float

@dataclass
class GovernanceConstraints:
    """Governance and compliance constraints"""
    max_model_age_days: int
    min_training_samples: int
    max_features: int
    required_validations: List[str] = field(default_factory=list)
    prohibited_features: List[str] = field(default_factory=list)
    compliance_requirements: List[str] = field(default_factory=list)
    audit_frequency_days: int
    approval_required: bool = True
    auto_promotion_allowed: bool = False

@dataclass
class ModelMetadata:
    """Core model metadata"""
    model_id: str
    model_name: str
    model_version: str
    model_type: ModelType
    description: str
    created_by: str
    created_at: datetime
    last_updated: datetime
    tags: List[str] = field(default_factory=list)
    compliance_level: ComplianceLevel = ComplianceLevel.INTERNAL
    risk_category: RiskCategory = RiskCategory.MEDIUM
    business_owner: Optional[str] = None
    technical_owner: Optional[str] = None

@dataclass
class TrainingMetadata:
    """Training process metadata"""
    training_id: str
    training_start: datetime
    training_end: datetime
    training_duration_seconds: int
    training_environment: str
    training_config: Dict[str, Any]
    hyperparameters: Dict[str, Any]
    random_seed: Optional[int] = None
    git_commit_hash: Optional[str] = None
    code_version: str
    dependencies: Dict[str, str] = field(default_factory=dict)
    compute_resources: Dict[str, Any] = field(default_factory=dict)

@dataclass
class DataMetadata:
    """Training data metadata"""
    data_id: str
    data_source: str
    data_version: str
    data_hash: str
    sample_count: int
    feature_count: int
    target_distribution: Dict[str, Any]
    feature_statistics: Dict[str, Any]
    missing_value_percentage: float
    outlier_percentage: float
    data_collection_start: datetime
    data_collection_end: datetime
    preprocessing_pipeline: Dict[str, Any]
    data_quality_score: float
    bias_assessment: Dict[str, Any] = field(default_factory=dict)

@dataclass
class ValidationMetadata:
    """Model validation metadata"""
    validation_id: str
    validation_type: str  # cross_validation, holdout, production_shadow, etc.
    validation_start: datetime
    validation_end: datetime
    validation_dataset: str
    performance_metrics: PerformanceMetrics
    risk_metrics: RiskMetrics
    validation_status: ValidationStatus
    validation_notes: str
    thresholds: Dict[str, float] = field(default_factory=dict)
    passed_checks: List[str] = field(default_factory=list)
    failed_checks: List[str] = field(default_factory=list)
    validator: str

@dataclass
class DeploymentMetadata:
    """Deployment metadata"""
    deployment_id: str
    deployment_environment: str  # staging, production, etc.
    deployment_start: datetime
    deployment_config: Dict[str, Any]
    endpoint_url: Optional[str] = None
    serving_infrastructure: Dict[str, Any] = field(default_factory=dict)
    monitoring_config: Dict[str, Any] = field(default_factory=dict)
    rollback_config: Dict[str, Any] = field(default_factory=dict)
    deployment_status: str = "active"
    deployed_by: str

@dataclass
class AuditMetadata:
    """Audit trail metadata"""
    audit_id: str
    audit_timestamp: datetime
    audit_type: str  # promotion, demotion, rollback, compliance_check, etc.
    actor: str
    action: str
    previous_state: Optional[Dict[str, Any]] = None
    new_state: Optional[Dict[str, Any]] = None
    reason: str
    evidence: List[str] = field(default_factory=list)
    approvals: List[Dict[str, Any]] = field(default_factory=list)
    compliance_checks: List[Dict[str, Any]] = field(default_factory=list)
    automated_decision: bool = False
    risk_assessment: Optional[Dict[str, Any]] = None

@dataclass
class ModelGovernanceRecord:
    """Complete governance record for a model"""
    model_metadata: ModelMetadata
    training_metadata: TrainingMetadata
    data_metadata: DataMetadata
    validation_results: List[ValidationMetadata]
    deployment_history: List[DeploymentMetadata]
    audit_trail: List[AuditMetadata]
    governance_constraints: GovernanceConstraints
    current_performance: PerformanceMetrics
    current_risk: RiskMetrics
    compliance_status: Dict[str, Any] = field(default_factory=dict)
    approval_chain: List[Dict[str, Any]] = field(default_factory=list)
    exemptions: List[Dict[str, Any]] = field(default_factory=list)

class GovernanceSchemaValidator:
    """Validates governance metadata against schemas"""
    
    @staticmethod
    def validate_model_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
        """Validate model metadata schema"""
        errors = []
        warnings = []
        
        required_fields = ['model_id', 'model_name', 'model_version', 'model_type', 
                          'description', 'created_by', 'created_at', 'last_updated']
        
        for field in required_fields:
            if field not in metadata:
                errors.append(f"Missing required field: {field}")
        
        if 'model_type' in metadata:
            if metadata['model_type'] not in [t.value for t in ModelType]:
                errors.append(f"Invalid model_type: {metadata['model_type']}")
        
        if 'compliance_level' in metadata:
            if metadata['compliance_level'] not in [c.value for c in ComplianceLevel]:
                errors.append(f"Invalid compliance_level: {metadata['compliance_level']}")
        
        if 'risk_category' in metadata:
            if metadata['risk_category'] not in [r.value for r in RiskCategory]:
                errors.append(f"Invalid risk_category: {metadata['risk_category']}")
        
        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings
        }
    
    @staticmethod
    def validate_performance_metrics(metrics: Dict[str, Any]) -> Dict[str, Any]:
        """Validate performance metrics"""
        errors = []
        warnings = []
        
        # Check metric ranges
        if 'accuracy' in metrics and not 0 <= metrics['accuracy'] <= 1:
            errors.append("Accuracy must be between 0 and 1")
        
        if 'precision' in metrics and not 0 <= metrics['precision'] <= 1:
            errors.append("Precision must be between 0 and 1")
        
        if 'recall' in metrics and not 0 <= metrics['recall'] <= 1:
            errors.append("Recall must be between 0 and 1")
        
        if 'f1_score' in metrics and not 0 <= metrics['f1_score'] <= 1:
            errors.append("F1 score must be between 0 and 1")
        
        if 'auc_roc' in metrics and not 0 <= metrics['auc_roc'] <= 1:
            errors.append("AUC-ROC must be between 0 and 1")
        
        if 'sharpe_ratio' in metrics and metrics['sharpe_ratio'] < 0:
            warnings.append("Negative Sharpe ratio detected")
        
        if 'win_rate' in metrics and not 0 <= metrics['win_rate'] <= 1:
            errors.append("Win rate must be between 0 and 1")
        
        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings
        }
    
    @staticmethod
    def validate_risk_metrics(metrics: Dict[str, Any]) -> Dict[str, Any]:
        """Validate risk metrics"""
        errors = []
        warnings = []
        
        required_fields = ['model_risk_score', 'data_drift_score', 'concept_drift_score',
                          'prediction_confidence', 'stability_score', 'robustness_score']
        
        for field in required_fields:
            if field not in metrics:
                errors.append(f"Missing required risk metric: {field}")
            elif field in metrics and not 0 <= metrics[field] <= 1:
                errors.append(f"{field} must be between 0 and 1")
        
        # Risk score warnings
        if 'model_risk_score' in metrics:
            if metrics['model_risk_score'] > 0.8:
                warnings.append("Very high model risk score detected")
            elif metrics['model_risk_score'] > 0.6:
                warnings.append("High model risk score detected")
        
        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings
        }

class GovernanceMetadataSerializer:
    """Serializes and deserializes governance metadata"""
    
    @staticmethod
    def serialize_governance_record(record: ModelGovernanceRecord) -> Dict[str, Any]:
        """Serialize governance record to dictionary"""
        return {
            "model_metadata": {
                **record.model_metadata.__dict__,
                "model_type": record.model_metadata.model_type.value,
                "compliance_level": record.model_metadata.compliance_level.value,
                "risk_category": record.model_metadata.risk_category.value,
                "created_at": record.model_metadata.created_at.isoformat(),
                "last_updated": record.model_metadata.last_updated.isoformat()
            },
            "training_metadata": {
                **record.training_metadata.__dict__,
                "training_start": record.training_metadata.training_start.isoformat(),
                "training_end": record.training_metadata.training_end.isoformat()
            },
            "data_metadata": {
                **record.data_metadata.__dict__,
                "data_collection_start": record.data_metadata.data_collection_start.isoformat(),
                "data_collection_end": record.data_metadata.data_collection_end.isoformat()
            },
            "validation_results": [
                {
                    **v.__dict__,
                    "validation_start": v.validation_start.isoformat(),
                    "validation_end": v.validation_end.isoformat(),
                    "validation_status": v.validation_status.value,
                    "performance_metrics": v.performance_metrics.__dict__,
                    "risk_metrics": v.risk_metrics.__dict__
                }
                for v in record.validation_results
            ],
            "deployment_history": [
                {
                    **d.__dict__,
                    "deployment_start": d.deployment_start.isoformat()
                }
                for d in record.deployment_history
            ],
            "audit_trail": [
                {
                    **a.__dict__,
                    "audit_timestamp": a.audit_timestamp.isoformat()
                }
                for a in record.audit_trail
            ],
            "governance_constraints": record.governance_constraints.__dict__,
            "current_performance": record.current_performance.__dict__,
            "current_risk": record.current_risk.__dict__,
            "compliance_status": record.compliance_status,
            "approval_chain": record.approval_chain,
            "exemptions": record.exemptions
        }
    
    @staticmethod
    def deserialize_governance_record(data: Dict[str, Any]) -> ModelGovernanceRecord:
        """Deserialize dictionary to governance record"""
        # Deserialize model metadata
        model_data = data["model_metadata"]
        model_metadata = ModelMetadata(
            model_id=model_data["model_id"],
            model_name=model_data["model_name"],
            model_version=model_data["model_version"],
            model_type=ModelType(model_data["model_type"]),
            description=model_data["description"],
            created_by=model_data["created_by"],
            created_at=datetime.fromisoformat(model_data["created_at"]),
            last_updated=datetime.fromisoformat(model_data["last_updated"]),
            tags=model_data.get("tags", []),
            compliance_level=ComplianceLevel(model_data.get("compliance_level", "internal")),
            risk_category=RiskCategory(model_data.get("risk_category", "medium")),
            business_owner=model_data.get("business_owner"),
            technical_owner=model_data.get("technical_owner")
        )
        
        # Deserialize training metadata
        training_data = data["training_metadata"]
        training_metadata = TrainingMetadata(
            training_id=training_data["training_id"],
            training_start=datetime.fromisoformat(training_data["training_start"]),
            training_end=datetime.fromisoformat(training_data["training_end"]),
            training_duration_seconds=training_data["training_duration_seconds"],
            training_environment=training_data["training_environment"],
            training_config=training_data["training_config"],
            hyperparameters=training_data["hyperparameters"],
            random_seed=training_data.get("random_seed"),
            git_commit_hash=training_data.get("git_commit_hash"),
            code_version=training_data["code_version"],
            dependencies=training_data.get("dependencies", {}),
            compute_resources=training_data.get("compute_resources", {})
        )
        
        # Deserialize data metadata
        data_meta = data["data_metadata"]
        data_metadata = DataMetadata(
            data_id=data_meta["data_id"],
            data_source=data_meta["data_source"],
            data_version=data_meta["data_version"],
            data_hash=data_meta["data_hash"],
            sample_count=data_meta["sample_count"],
            feature_count=data_meta["feature_count"],
            target_distribution=data_meta["target_distribution"],
            feature_statistics=data_meta["feature_statistics"],
            missing_value_percentage=data_meta["missing_value_percentage"],
            outlier_percentage=data_meta["outlier_percentage"],
            data_collection_start=datetime.fromisoformat(data_meta["data_collection_start"]),
            data_collection_end=datetime.fromisoformat(data_meta["data_collection_end"]),
            preprocessing_pipeline=data_meta["preprocessing_pipeline"],
            data_quality_score=data_meta["data_quality_score"],
            bias_assessment=data_meta.get("bias_assessment", {})
        )
        
        # Deserialize validation results
        validation_results = []
        for v_data in data["validation_results"]:
            performance_metrics = PerformanceMetrics(**v_data["performance_metrics"])
            risk_metrics = RiskMetrics(**v_data["risk_metrics"])
            
            validation_results.append(ValidationMetadata(
                validation_id=v_data["validation_id"],
                validation_type=v_data["validation_type"],
                validation_start=datetime.fromisoformat(v_data["validation_start"]),
                validation_end=datetime.fromisoformat(v_data["validation_end"]),
                validation_dataset=v_data["validation_dataset"],
                performance_metrics=performance_metrics,
                risk_metrics=risk_metrics,
                validation_status=ValidationStatus(v_data["validation_status"]),
                validation_notes=v_data["validation_notes"],
                thresholds=v_data.get("thresholds", {}),
                passed_checks=v_data.get("passed_checks", []),
                failed_checks=v_data.get("failed_checks", []),
                validator=v_data["validator"]
            ))
        
        # Deserialize deployment history
        deployment_history = []
        for d_data in data["deployment_history"]:
            deployment_history.append(DeploymentMetadata(
                deployment_id=d_data["deployment_id"],
                deployment_environment=d_data["deployment_environment"],
                deployment_start=datetime.fromisoformat(d_data["deployment_start"]),
                deployment_config=d_data["deployment_config"],
                endpoint_url=d_data.get("endpoint_url"),
                serving_infrastructure=d_data.get("serving_infrastructure", {}),
                monitoring_config=d_data.get("monitoring_config", {}),
                rollback_config=d_data.get("rollback_config", {}),
                deployment_status=d_data.get("deployment_status", "active"),
                deployed_by=d_data["deployed_by"]
            ))
        
        # Deserialize audit trail
        audit_trail = []
        for a_data in data["audit_trail"]:
            audit_trail.append(AuditMetadata(
                audit_id=a_data["audit_id"],
                audit_timestamp=datetime.fromisoformat(a_data["audit_timestamp"]),
                audit_type=a_data["audit_type"],
                actor=a_data["actor"],
                action=a_data["action"],
                previous_state=a_data.get("previous_state"),
                new_state=a_data.get("new_state"),
                reason=a_data["reason"],
                evidence=a_data.get("evidence", []),
                approvals=a_data.get("approvals", []),
                compliance_checks=a_data.get("compliance_checks", []),
                automated_decision=a_data.get("automated_decision", False),
                risk_assessment=a_data.get("risk_assessment")
            ))
        
        # Deserialize remaining fields
        governance_constraints = GovernanceConstraints(**data["governance_constraints"])
        current_performance = PerformanceMetrics(**data["current_performance"])
        current_risk = RiskMetrics(**data["current_risk"])
        
        return ModelGovernanceRecord(
            model_metadata=model_metadata,
            training_metadata=training_metadata,
            data_metadata=data_metadata,
            validation_results=validation_results,
            deployment_history=deployment_history,
            audit_trail=audit_trail,
            governance_constraints=governance_constraints,
            current_performance=current_performance,
            current_risk=current_risk,
            compliance_status=data.get("compliance_status", {}),
            approval_chain=data.get("approval_chain", []),
            exemptions=data.get("exemptions", [])
        )
