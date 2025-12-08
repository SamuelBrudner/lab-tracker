"""
Django REST Framework serializers for the Neuroscience Experiment Orchestration App.

Provides serializers for all core domain models with support for nested
representations and status transitions.
"""

from rest_framework import serializers
from .models import (
    Question, Claim, Cohort, Run, Session, Component,
    Dataset, Analysis, Visualization, Panel, ClaimEvidence
)


# =============================================================================
# Base Serializer Mixins
# =============================================================================

class TimestampMixin(serializers.Serializer):
    """Mixin for timestamp fields."""
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)


class ELNLinkMixin(serializers.Serializer):
    """Mixin for ELN integration fields."""
    eln_url = serializers.URLField(required=False, allow_blank=True)
    eln_snapshot_path = serializers.CharField(required=False, allow_blank=True)
    eln_snapshot_hash = serializers.CharField(required=False, allow_blank=True)
    eln_snapshot_at = serializers.DateTimeField(required=False, allow_null=True)


# =============================================================================
# Question Serializers
# =============================================================================

class QuestionListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for Question lists."""
    children_count = serializers.SerializerMethodField()
    runs_count = serializers.SerializerMethodField()

    class Meta:
        model = Question
        fields = [
            'id', 'title', 'status', 'is_pilot', 'parent',
            'children_count', 'runs_count', 'created_at', 'updated_at'
        ]

    def get_children_count(self, obj):
        return obj.children.count()

    def get_runs_count(self, obj):
        return obj.runs.count()


class QuestionSerializer(serializers.ModelSerializer):
    """Full serializer for Question with all fields."""
    children = QuestionListSerializer(many=True, read_only=True)
    ancestors = serializers.SerializerMethodField()

    class Meta:
        model = Question
        fields = [
            'id', 'title', 'description', 'parent', 'children', 'ancestors',
            'hypothesis', 'success_criteria', 'status', 'is_pilot', 'notes',
            'eln_url', 'eln_snapshot_path', 'eln_snapshot_hash', 'eln_snapshot_at',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_ancestors(self, obj):
        return [{'id': str(a.id), 'title': a.title} for a in obj.get_ancestors()]


# =============================================================================
# Claim Serializers
# =============================================================================

class ClaimListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for Claim lists."""
    evidence_count = serializers.SerializerMethodField()
    questions_count = serializers.SerializerMethodField()

    class Meta:
        model = Claim
        fields = [
            'id', 'title', 'status', 'parent',
            'evidence_count', 'questions_count', 'created_at', 'updated_at'
        ]

    def get_evidence_count(self, obj):
        return obj.evidence_links.count()

    def get_questions_count(self, obj):
        return obj.questions.count()


class ClaimSerializer(serializers.ModelSerializer):
    """Full serializer for Claim with all fields."""
    children = ClaimListSerializer(many=True, read_only=True)
    questions = QuestionListSerializer(many=True, read_only=True)
    question_ids = serializers.PrimaryKeyRelatedField(
        source='questions',
        queryset=Question.objects.all(),
        many=True,
        write_only=True,
        required=False
    )

    class Meta:
        model = Claim
        fields = [
            'id', 'title', 'statement', 'parent', 'children',
            'questions', 'question_ids', 'status', 'assessment_notes', 'notes',
            'eln_url', 'eln_snapshot_path', 'eln_snapshot_hash', 'eln_snapshot_at',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# =============================================================================
# Cohort Serializers
# =============================================================================

class CohortListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for Cohort lists."""
    availability_ratio = serializers.SerializerMethodField()

    class Meta:
        model = Cohort
        fields = [
            'id', 'name', 'cohort_type', 'genotype', 'sex',
            'total_count', 'available_count', 'availability_ratio',
            'created_at', 'updated_at'
        ]

    def get_availability_ratio(self, obj):
        if obj.total_count == 0:
            return None
        return round(obj.available_count / obj.total_count, 2)


class CohortSerializer(serializers.ModelSerializer):
    """Full serializer for Cohort with all fields."""

    class Meta:
        model = Cohort
        fields = [
            'id', 'name', 'description', 'cohort_type',
            'genotype', 'sex', 'rearing_conditions',
            'min_age_days', 'max_age_days',
            'total_count', 'available_count', 'notes',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# =============================================================================
# Run Serializers
# =============================================================================

class RunListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for Run lists."""
    sessions_count = serializers.SerializerMethodField()
    cohort_name = serializers.CharField(source='cohort.name', read_only=True)

    class Meta:
        model = Run
        fields = [
            'id', 'name', 'status', 'cohort', 'cohort_name',
            'planned_start', 'planned_end', 'actual_start', 'actual_end',
            'sessions_count', 'created_at', 'updated_at'
        ]

    def get_sessions_count(self, obj):
        return obj.sessions.count()


class RunSerializer(serializers.ModelSerializer):
    """Full serializer for Run with all fields."""
    questions = QuestionListSerializer(many=True, read_only=True)
    question_ids = serializers.PrimaryKeyRelatedField(
        source='questions',
        queryset=Question.objects.all(),
        many=True,
        write_only=True,
        required=False
    )
    cohort_detail = CohortListSerializer(source='cohort', read_only=True)

    class Meta:
        model = Run
        fields = [
            'id', 'name', 'description',
            'questions', 'question_ids', 'cohort', 'cohort_detail',
            'data_sink', 'planned_start', 'planned_end', 'success_criteria',
            'actual_start', 'actual_end', 'status', 'notes',
            'eln_url', 'eln_snapshot_path', 'eln_snapshot_hash', 'eln_snapshot_at',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# =============================================================================
# Session Serializers
# =============================================================================

class SessionListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for Session lists."""
    run_name = serializers.CharField(source='run.name', read_only=True)
    components_count = serializers.SerializerMethodField()

    class Meta:
        model = Session
        fields = [
            'id', 'name', 'run', 'run_name', 'status',
            'scheduled_start', 'scheduled_end', 'actual_start', 'actual_end',
            'rig_identifier', 'components_count', 'created_at', 'updated_at'
        ]

    def get_components_count(self, obj):
        return obj.components.count()


class SessionSerializer(serializers.ModelSerializer):
    """Full serializer for Session with all fields."""
    run_detail = RunListSerializer(source='run', read_only=True)

    class Meta:
        model = Session
        fields = [
            'id', 'run', 'run_detail', 'name', 'description',
            'scheduled_start', 'scheduled_end', 'actual_start', 'actual_end',
            'relative_to', 'relative_offset_minutes', 'rig_identifier',
            'status', 'notes', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# =============================================================================
# Component Serializers
# =============================================================================

class ComponentListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for Component lists."""
    session_name = serializers.CharField(source='session.name', read_only=True)
    role_display = serializers.CharField(source='get_role_display', read_only=True)

    class Meta:
        model = Component
        fields = [
            'id', 'name', 'session', 'session_name', 'role', 'role_display',
            'modality', 'status', 'created_at', 'updated_at'
        ]


class ComponentSerializer(serializers.ModelSerializer):
    """Full serializer for Component with all fields."""
    session_detail = SessionListSerializer(source='session', read_only=True)
    cohort_detail = CohortListSerializer(source='cohort', read_only=True)
    role_display = serializers.CharField(source='get_role_display', read_only=True)

    class Meta:
        model = Component
        fields = [
            'id', 'session', 'session_detail', 'name', 'description',
            'role', 'role_display', 'modality', 'equipment_identifier',
            'start_offset_seconds', 'duration_seconds',
            'cohort', 'cohort_detail', 'requested_count', 'consumed_count', 'sampling_notes',
            'trial_count', 'trial_log_path', 'metadata', 'status', 'notes',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# =============================================================================
# Dataset Serializers
# =============================================================================

class DatasetListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for Dataset lists."""
    analyses_count = serializers.SerializerMethodField()
    primary_run_name = serializers.CharField(source='primary_run.name', read_only=True)

    class Meta:
        model = Dataset
        fields = [
            'id', 'name', 'version', 'status', 'primary_run', 'primary_run_name',
            'analyses_count', 'frozen_at', 'created_at', 'updated_at'
        ]

    def get_analyses_count(self, obj):
        return obj.analyses.count()


class DatasetSerializer(serializers.ModelSerializer):
    """Full serializer for Dataset with all fields."""
    primary_run_detail = RunListSerializer(source='primary_run', read_only=True)
    contributing_runs_detail = RunListSerializer(source='contributing_runs', many=True, read_only=True)
    contributing_run_ids = serializers.PrimaryKeyRelatedField(
        source='contributing_runs',
        queryset=Run.objects.all(),
        many=True,
        write_only=True,
        required=False
    )
    session_ids = serializers.PrimaryKeyRelatedField(
        source='sessions',
        queryset=Session.objects.all(),
        many=True,
        write_only=True,
        required=False
    )

    class Meta:
        model = Dataset
        fields = [
            'id', 'name', 'description', 'version',
            'primary_run', 'primary_run_detail',
            'contributing_runs_detail', 'contributing_run_ids',
            'sessions', 'session_ids',
            'inclusion_criteria', 'replicate_targets',
            'output_path', 'analysis_job_ids', 'status', 'frozen_at', 'notes',
            'eln_url', 'eln_snapshot_path', 'eln_snapshot_hash', 'eln_snapshot_at',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# =============================================================================
# Analysis Serializers
# =============================================================================

class AnalysisListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for Analysis lists."""
    dataset_name = serializers.CharField(source='dataset.name', read_only=True)
    visualizations_count = serializers.SerializerMethodField()

    class Meta:
        model = Analysis
        fields = [
            'id', 'name', 'dataset', 'dataset_name',
            'recipe_identifier', 'recipe_version', 'status',
            'visualizations_count', 'started_at', 'completed_at', 'frozen_at',
            'created_at', 'updated_at'
        ]

    def get_visualizations_count(self, obj):
        return obj.visualizations.count()


class AnalysisSerializer(serializers.ModelSerializer):
    """Full serializer for Analysis with all fields."""
    dataset_detail = DatasetListSerializer(source='dataset', read_only=True)

    class Meta:
        model = Analysis
        fields = [
            'id', 'name', 'description', 'dataset', 'dataset_detail',
            'recipe_identifier', 'recipe_version', 'parameters',
            'job_identifier', 'job_dashboard_url',
            'output_path', 'outputs_manifest', 'status',
            'started_at', 'completed_at', 'frozen_at', 'notes',
            'eln_url', 'eln_snapshot_path', 'eln_snapshot_hash', 'eln_snapshot_at',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# =============================================================================
# Visualization Serializers
# =============================================================================

class VisualizationListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for Visualization lists."""
    analysis_name = serializers.CharField(source='analysis.name', read_only=True)
    panels_count = serializers.SerializerMethodField()

    class Meta:
        model = Visualization
        fields = [
            'id', 'name', 'analysis', 'analysis_name',
            'asset_type', 'status', 'tags', 'panels_count',
            'frozen_at', 'created_at', 'updated_at'
        ]

    def get_panels_count(self, obj):
        return obj.panels.count()


class VisualizationSerializer(serializers.ModelSerializer):
    """Full serializer for Visualization with all fields."""
    analysis_detail = AnalysisListSerializer(source='analysis', read_only=True)
    datasets_detail = DatasetListSerializer(source='datasets', many=True, read_only=True)
    dataset_ids = serializers.PrimaryKeyRelatedField(
        source='datasets',
        queryset=Dataset.objects.all(),
        many=True,
        write_only=True,
        required=False
    )

    class Meta:
        model = Visualization
        fields = [
            'id', 'name', 'description',
            'analysis', 'analysis_detail', 'datasets_detail', 'dataset_ids',
            'asset_path', 'asset_url', 'asset_type',
            'rendering_notebook', 'rendering_script', 'tags',
            'status', 'frozen_at', 'notes',
            'eln_url', 'eln_snapshot_path', 'eln_snapshot_hash', 'eln_snapshot_at',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# =============================================================================
# Panel Serializers
# =============================================================================

class PanelListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for Panel lists."""
    visualization_name = serializers.CharField(source='visualization.name', read_only=True)
    figure_role_display = serializers.CharField(source='get_figure_role_display', read_only=True)

    class Meta:
        model = Panel
        fields = [
            'id', 'label', 'figure_identifier', 'figure_role', 'figure_role_display',
            'visualization', 'visualization_name', 'status',
            'frozen_at', 'created_at', 'updated_at'
        ]


class PanelSerializer(serializers.ModelSerializer):
    """Full serializer for Panel with all fields."""
    visualization_detail = VisualizationListSerializer(source='visualization', read_only=True)
    datasets_detail = DatasetListSerializer(source='datasets', many=True, read_only=True)
    dataset_ids = serializers.PrimaryKeyRelatedField(
        source='datasets',
        queryset=Dataset.objects.all(),
        many=True,
        write_only=True,
        required=False
    )
    figure_role_display = serializers.CharField(source='get_figure_role_display', read_only=True)

    class Meta:
        model = Panel
        fields = [
            'id', 'label', 'caption',
            'figure_identifier', 'figure_role', 'figure_role_display',
            'visualization', 'visualization_detail', 'datasets_detail', 'dataset_ids',
            'frozen_file_path', 'frozen_file_hash', 'status', 'frozen_at', 'notes',
            'eln_url', 'eln_snapshot_path', 'eln_snapshot_hash', 'eln_snapshot_at',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# =============================================================================
# ClaimEvidence Serializers
# =============================================================================

class ClaimEvidenceListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for ClaimEvidence lists."""
    claim_title = serializers.CharField(source='claim.title', read_only=True)
    evidence_target = serializers.SerializerMethodField()

    class Meta:
        model = ClaimEvidence
        fields = [
            'id', 'claim', 'claim_title',
            'panel', 'analysis', 'evidence_target',
            'evidence_type', 'created_at', 'updated_at'
        ]

    def get_evidence_target(self, obj):
        if obj.panel:
            return {'type': 'panel', 'id': str(obj.panel.id), 'name': str(obj.panel)}
        if obj.analysis:
            return {'type': 'analysis', 'id': str(obj.analysis.id), 'name': obj.analysis.name}
        return None


class ClaimEvidenceSerializer(serializers.ModelSerializer):
    """Full serializer for ClaimEvidence with all fields."""
    claim_detail = ClaimListSerializer(source='claim', read_only=True)
    panel_detail = PanelListSerializer(source='panel', read_only=True)
    analysis_detail = AnalysisListSerializer(source='analysis', read_only=True)

    class Meta:
        model = ClaimEvidence
        fields = [
            'id', 'claim', 'claim_detail',
            'panel', 'panel_detail',
            'analysis', 'analysis_detail',
            'evidence_type', 'description',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate(self, data):
        """Ensure exactly one evidence type is specified."""
        panel = data.get('panel')
        analysis = data.get('analysis')
        links = [panel, analysis]
        linked_count = sum(1 for link in links if link is not None)
        if linked_count != 1:
            raise serializers.ValidationError(
                "Exactly one of panel or analysis must be specified."
            )
        return data
