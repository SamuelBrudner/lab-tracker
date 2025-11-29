"""
Django admin configuration for the Neuroscience Experiment Orchestration App.

Provides admin interfaces for all core domain models with appropriate
list displays, filters, and search capabilities.
"""

from django.contrib import admin
from django.utils.html import format_html

from .models import (
    Question, Claim, Cohort, Run, Session, Component,
    Dataset, Analysis, Visualization, Panel, ClaimEvidence
)


# =============================================================================
# Inline Admin Classes
# =============================================================================

class SessionInline(admin.TabularInline):
    """Inline for Sessions within Run admin."""
    model = Session
    extra = 0
    fields = ['name', 'status', 'scheduled_start', 'scheduled_end', 'rig_identifier']
    readonly_fields = ['created_at']
    show_change_link = True


class ComponentInline(admin.TabularInline):
    """Inline for Components within Session admin."""
    model = Component
    extra = 0
    fields = ['name', 'role', 'modality', 'status']
    readonly_fields = ['created_at']
    show_change_link = True


class ClaimEvidenceInline(admin.TabularInline):
    """Inline for ClaimEvidence within Claim admin."""
    model = ClaimEvidence
    extra = 0
    fields = ['panel', 'analysis', 'dataset', 'evidence_type', 'description']
    readonly_fields = ['created_at']
    fk_name = 'claim'


class ChildQuestionInline(admin.TabularInline):
    """Inline for child Questions within Question admin."""
    model = Question
    fk_name = 'parent'
    extra = 0
    fields = ['title', 'status', 'is_pilot']
    readonly_fields = ['created_at']
    show_change_link = True
    verbose_name = "Child Question"
    verbose_name_plural = "Child Questions"


class ChildClaimInline(admin.TabularInline):
    """Inline for child Claims within Claim admin."""
    model = Claim
    fk_name = 'parent'
    extra = 0
    fields = ['title', 'status']
    readonly_fields = ['created_at']
    show_change_link = True
    verbose_name = "Sub-Claim"
    verbose_name_plural = "Sub-Claims"


class AnalysisInline(admin.TabularInline):
    """Inline for Analyses within Dataset admin."""
    model = Analysis
    extra = 0
    fields = ['name', 'recipe_identifier', 'status', 'started_at', 'completed_at']
    readonly_fields = ['created_at', 'started_at', 'completed_at']
    show_change_link = True


class VisualizationInline(admin.TabularInline):
    """Inline for Visualizations within Analysis admin."""
    model = Visualization
    extra = 0
    fields = ['name', 'asset_type', 'status']
    readonly_fields = ['created_at']
    show_change_link = True


class PanelInline(admin.TabularInline):
    """Inline for Panels within Visualization admin."""
    model = Panel
    extra = 0
    fields = ['label', 'figure_identifier', 'figure_role', 'status']
    readonly_fields = ['created_at']
    show_change_link = True


# =============================================================================
# Admin Classes
# =============================================================================

@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    """Admin for Question model."""
    list_display = [
        'title', 'status', 'is_pilot', 'parent',
        'runs_count', 'claims_count', 'created_at'
    ]
    list_filter = ['status', 'is_pilot', 'created_at']
    search_fields = ['title', 'description', 'hypothesis']
    readonly_fields = ['id', 'created_at', 'updated_at']
    raw_id_fields = ['parent']
    inlines = [ChildQuestionInline]

    fieldsets = (
        (None, {
            'fields': ('id', 'title', 'description', 'parent')
        }),
        ('Scientific Content', {
            'fields': ('hypothesis', 'success_criteria')
        }),
        ('Status', {
            'fields': ('status', 'is_pilot')
        }),
        ('ELN Integration', {
            'fields': ('eln_url', 'eln_snapshot_path', 'eln_snapshot_hash', 'eln_snapshot_at'),
            'classes': ('collapse',)
        }),
        ('Notes & Timestamps', {
            'fields': ('notes', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def runs_count(self, obj):
        return obj.runs.count()
    runs_count.short_description = 'Runs'

    def claims_count(self, obj):
        return obj.claims.count()
    claims_count.short_description = 'Claims'


@admin.register(Claim)
class ClaimAdmin(admin.ModelAdmin):
    """Admin for Claim model."""
    list_display = [
        'title', 'status', 'parent',
        'questions_count', 'evidence_count', 'created_at'
    ]
    list_filter = ['status', 'created_at']
    search_fields = ['title', 'statement', 'assessment_notes']
    readonly_fields = ['id', 'created_at', 'updated_at']
    raw_id_fields = ['parent']
    filter_horizontal = ['questions']
    inlines = [ChildClaimInline, ClaimEvidenceInline]

    fieldsets = (
        (None, {
            'fields': ('id', 'title', 'statement', 'parent')
        }),
        ('Linked Questions', {
            'fields': ('questions',)
        }),
        ('Status & Assessment', {
            'fields': ('status', 'assessment_notes')
        }),
        ('ELN Integration', {
            'fields': ('eln_url', 'eln_snapshot_path', 'eln_snapshot_hash', 'eln_snapshot_at'),
            'classes': ('collapse',)
        }),
        ('Notes & Timestamps', {
            'fields': ('notes', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def questions_count(self, obj):
        return obj.questions.count()
    questions_count.short_description = 'Questions'

    def evidence_count(self, obj):
        return obj.evidence_links.count()
    evidence_count.short_description = 'Evidence'


@admin.register(Cohort)
class CohortAdmin(admin.ModelAdmin):
    """Admin for Cohort model."""
    list_display = [
        'name', 'cohort_type', 'genotype', 'sex',
        'available_count', 'total_count', 'availability_display', 'created_at'
    ]
    list_filter = ['cohort_type', 'sex', 'created_at']
    search_fields = ['name', 'description', 'genotype']
    readonly_fields = ['id', 'created_at', 'updated_at']

    fieldsets = (
        (None, {
            'fields': ('id', 'name', 'description', 'cohort_type')
        }),
        ('Biological Characteristics', {
            'fields': ('genotype', 'sex', 'rearing_conditions', 'min_age_days', 'max_age_days')
        }),
        ('Availability', {
            'fields': ('total_count', 'available_count')
        }),
        ('Notes & Timestamps', {
            'fields': ('notes', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def availability_display(self, obj):
        if obj.total_count == 0:
            return '-'
        ratio = obj.available_count / obj.total_count
        color = 'green' if ratio > 0.5 else 'orange' if ratio > 0.2 else 'red'
        return format_html(
            '<span style="color: {}">{:.0%}</span>',
            color, ratio
        )
    availability_display.short_description = 'Availability'


@admin.register(Run)
class RunAdmin(admin.ModelAdmin):
    """Admin for Run model."""
    list_display = [
        'name', 'status', 'cohort',
        'planned_start', 'actual_start', 'sessions_count', 'created_at'
    ]
    list_filter = ['status', 'created_at', 'cohort']
    search_fields = ['name', 'description', 'data_sink']
    readonly_fields = ['id', 'created_at', 'updated_at']
    raw_id_fields = ['cohort']
    filter_horizontal = ['questions']
    inlines = [SessionInline]
    date_hierarchy = 'created_at'

    fieldsets = (
        (None, {
            'fields': ('id', 'name', 'description')
        }),
        ('Linked Entities', {
            'fields': ('questions', 'cohort')
        }),
        ('Design Intent', {
            'fields': ('data_sink', 'planned_start', 'planned_end', 'success_criteria')
        }),
        ('Execution', {
            'fields': ('status', 'actual_start', 'actual_end')
        }),
        ('ELN Integration', {
            'fields': ('eln_url', 'eln_snapshot_path', 'eln_snapshot_hash', 'eln_snapshot_at'),
            'classes': ('collapse',)
        }),
        ('Notes & Timestamps', {
            'fields': ('notes', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def sessions_count(self, obj):
        return obj.sessions.count()
    sessions_count.short_description = 'Sessions'


@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    """Admin for Session model."""
    list_display = [
        'name', 'run', 'status', 'rig_identifier',
        'scheduled_start', 'actual_start', 'components_count', 'created_at'
    ]
    list_filter = ['status', 'rig_identifier', 'created_at']
    search_fields = ['name', 'description', 'rig_identifier', 'run__name']
    readonly_fields = ['id', 'created_at', 'updated_at']
    raw_id_fields = ['run', 'relative_to']
    inlines = [ComponentInline]

    fieldsets = (
        (None, {
            'fields': ('id', 'run', 'name', 'description')
        }),
        ('Scheduling', {
            'fields': ('scheduled_start', 'scheduled_end', 'relative_to', 'relative_offset_minutes')
        }),
        ('Execution', {
            'fields': ('status', 'rig_identifier', 'actual_start', 'actual_end')
        }),
        ('Notes & Timestamps', {
            'fields': ('notes', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def components_count(self, obj):
        return obj.components.count()
    components_count.short_description = 'Components'


@admin.register(Component)
class ComponentAdmin(admin.ModelAdmin):
    """Admin for Component model."""
    list_display = [
        'name', 'session', 'role', 'modality', 'status',
        'equipment_identifier', 'created_at'
    ]
    list_filter = ['role', 'status', 'modality', 'created_at']
    search_fields = ['name', 'description', 'modality', 'equipment_identifier', 'session__name']
    readonly_fields = ['id', 'created_at', 'updated_at']
    raw_id_fields = ['session', 'cohort']

    fieldsets = (
        (None, {
            'fields': ('id', 'session', 'name', 'description')
        }),
        ('Role & Equipment', {
            'fields': ('role', 'modality', 'equipment_identifier', 'status')
        }),
        ('Timing', {
            'fields': ('start_offset_seconds', 'duration_seconds')
        }),
        ('Subject Configuration (for Subject role)', {
            'fields': ('cohort', 'requested_count', 'consumed_count', 'sampling_notes'),
            'classes': ('collapse',)
        }),
        ('Recording Configuration (for Recording role)', {
            'fields': ('trial_count', 'trial_log_path'),
            'classes': ('collapse',)
        }),
        ('Metadata', {
            'fields': ('metadata',)
        }),
        ('Notes & Timestamps', {
            'fields': ('notes', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(Dataset)
class DatasetAdmin(admin.ModelAdmin):
    """Admin for Dataset model."""
    list_display = [
        'name', 'version', 'status', 'primary_run',
        'analyses_count', 'frozen_at', 'created_at'
    ]
    list_filter = ['status', 'created_at', 'frozen_at']
    search_fields = ['name', 'description', 'version']
    readonly_fields = ['id', 'created_at', 'updated_at', 'frozen_at']
    raw_id_fields = ['primary_run']
    filter_horizontal = ['contributing_runs', 'sessions']
    inlines = [AnalysisInline]

    fieldsets = (
        (None, {
            'fields': ('id', 'name', 'description', 'version')
        }),
        ('Data Sources', {
            'fields': ('primary_run', 'contributing_runs', 'sessions')
        }),
        ('Configuration', {
            'fields': ('inclusion_criteria', 'replicate_targets')
        }),
        ('Outputs', {
            'fields': ('output_path', 'analysis_job_ids')
        }),
        ('Status', {
            'fields': ('status', 'frozen_at')
        }),
        ('ELN Integration', {
            'fields': ('eln_url', 'eln_snapshot_path', 'eln_snapshot_hash', 'eln_snapshot_at'),
            'classes': ('collapse',)
        }),
        ('Notes & Timestamps', {
            'fields': ('notes', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def analyses_count(self, obj):
        return obj.analyses.count()
    analyses_count.short_description = 'Analyses'


@admin.register(Analysis)
class AnalysisAdmin(admin.ModelAdmin):
    """Admin for Analysis model."""
    list_display = [
        'name', 'dataset', 'recipe_identifier', 'recipe_version',
        'status', 'started_at', 'completed_at', 'created_at'
    ]
    list_filter = ['status', 'recipe_identifier', 'created_at']
    search_fields = ['name', 'description', 'recipe_identifier', 'job_identifier']
    readonly_fields = ['id', 'created_at', 'updated_at', 'started_at', 'completed_at', 'frozen_at']
    raw_id_fields = ['dataset']
    inlines = [VisualizationInline]

    fieldsets = (
        (None, {
            'fields': ('id', 'name', 'description', 'dataset')
        }),
        ('Recipe', {
            'fields': ('recipe_identifier', 'recipe_version', 'parameters')
        }),
        ('Execution', {
            'fields': ('job_identifier', 'job_dashboard_url', 'status', 'started_at', 'completed_at')
        }),
        ('Outputs', {
            'fields': ('output_path', 'outputs_manifest', 'frozen_at')
        }),
        ('ELN Integration', {
            'fields': ('eln_url', 'eln_snapshot_path', 'eln_snapshot_hash', 'eln_snapshot_at'),
            'classes': ('collapse',)
        }),
        ('Notes & Timestamps', {
            'fields': ('notes', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(Visualization)
class VisualizationAdmin(admin.ModelAdmin):
    """Admin for Visualization model."""
    list_display = [
        'name', 'analysis', 'asset_type', 'status',
        'panels_count', 'frozen_at', 'created_at'
    ]
    list_filter = ['status', 'asset_type', 'created_at']
    search_fields = ['name', 'description', 'tags']
    readonly_fields = ['id', 'created_at', 'updated_at', 'frozen_at']
    raw_id_fields = ['analysis']
    filter_horizontal = ['datasets']
    inlines = [PanelInline]

    fieldsets = (
        (None, {
            'fields': ('id', 'name', 'description')
        }),
        ('Source', {
            'fields': ('analysis', 'datasets')
        }),
        ('Asset', {
            'fields': ('asset_path', 'asset_url', 'asset_type')
        }),
        ('Rendering', {
            'fields': ('rendering_notebook', 'rendering_script')
        }),
        ('Discovery', {
            'fields': ('tags',)
        }),
        ('Status', {
            'fields': ('status', 'frozen_at')
        }),
        ('ELN Integration', {
            'fields': ('eln_url', 'eln_snapshot_path', 'eln_snapshot_hash', 'eln_snapshot_at'),
            'classes': ('collapse',)
        }),
        ('Notes & Timestamps', {
            'fields': ('notes', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def panels_count(self, obj):
        return obj.panels.count()
    panels_count.short_description = 'Panels'


@admin.register(Panel)
class PanelAdmin(admin.ModelAdmin):
    """Admin for Panel model."""
    list_display = [
        'label', 'figure_identifier', 'figure_role', 'visualization',
        'status', 'frozen_at', 'created_at'
    ]
    list_filter = ['status', 'figure_role', 'figure_identifier', 'created_at']
    search_fields = ['label', 'caption', 'figure_identifier']
    readonly_fields = ['id', 'created_at', 'updated_at', 'frozen_at']
    raw_id_fields = ['visualization']
    filter_horizontal = ['datasets']

    fieldsets = (
        (None, {
            'fields': ('id', 'label', 'caption')
        }),
        ('Figure', {
            'fields': ('figure_identifier', 'figure_role')
        }),
        ('Assets', {
            'fields': ('visualization', 'datasets')
        }),
        ('Frozen State', {
            'fields': ('status', 'frozen_at', 'frozen_file_path', 'frozen_file_hash')
        }),
        ('ELN Integration', {
            'fields': ('eln_url', 'eln_snapshot_path', 'eln_snapshot_hash', 'eln_snapshot_at'),
            'classes': ('collapse',)
        }),
        ('Notes & Timestamps', {
            'fields': ('notes', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(ClaimEvidence)
class ClaimEvidenceAdmin(admin.ModelAdmin):
    """Admin for ClaimEvidence model."""
    list_display = [
        'claim', 'evidence_type', 'panel', 'analysis', 'dataset', 'created_at'
    ]
    list_filter = ['evidence_type', 'created_at']
    search_fields = ['description', 'claim__title']
    readonly_fields = ['id', 'created_at', 'updated_at']
    raw_id_fields = ['claim', 'panel', 'analysis', 'dataset']

    fieldsets = (
        (None, {
            'fields': ('id', 'claim')
        }),
        ('Evidence Link', {
            'fields': ('panel', 'analysis', 'dataset'),
            'description': 'Select exactly one of: Panel, Analysis, or Dataset'
        }),
        ('Evidence Details', {
            'fields': ('evidence_type', 'description')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
