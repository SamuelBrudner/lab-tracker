"""
Core domain models for the Neuroscience Experiment Orchestration App.

This module defines the semantic model for tracking experiments from
question to claim, including execution entities (Runs, Sessions, Components)
and evidence assets (Datasets, Analyses, Visualizations, Panels).
"""

import uuid
from django.db import models
from django.core.exceptions import ValidationError


# =============================================================================
# Status Choice Classes
# =============================================================================

class QuestionStatus(models.TextChoices):
    """Status workflow for Questions."""
    DRAFT = "draft", "Draft"
    PILOT = "pilot", "Pilot"
    OPERATIONAL = "operational", "Operational"
    PAUSED = "paused", "Paused"
    COMPLETED = "completed", "Completed"
    ARCHIVED = "archived", "Archived"


class ClaimStatus(models.TextChoices):
    """Status workflow for Claims."""
    SKETCHED = "sketched", "Sketched"
    DEVELOPING = "developing", "Developing"
    EVIDENCE_GATHERING = "evidence_gathering", "Evidence Gathering"
    UNDER_REVIEW = "under_review", "Under Review"
    ASSESSED = "assessed", "Assessed"
    PUBLISHED = "published", "Published"
    RETRACTED = "retracted", "Retracted"


class CohortType(models.TextChoices):
    """Type of cohort: pooled (interchangeable) or enumerated (named individuals)."""
    POOLED = "pooled", "Pooled"
    ENUMERATED = "enumerated", "Enumerated"


class Sex(models.TextChoices):
    """Biological sex options."""
    MALE = "M", "Male"
    FEMALE = "F", "Female"
    MIXED = "mixed", "Mixed"
    UNKNOWN = "unknown", "Unknown"


class RunStatus(models.TextChoices):
    """Status workflow for Runs."""
    PLANNED = "planned", "Planned"
    SCHEDULED = "scheduled", "Scheduled"
    RUNNING = "running", "Running"
    QC = "qc", "Quality Control"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    ARCHIVED = "archived", "Archived"


class SessionStatus(models.TextChoices):
    """Status workflow for Sessions."""
    PLANNED = "planned", "Planned"
    SCHEDULED = "scheduled", "Scheduled"
    IN_PROGRESS = "in_progress", "In Progress"
    COMPLETE = "complete", "Complete"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


class ComponentRole(models.TextChoices):
    """Role type for Components within a Session."""
    SUBJECTS = "subjects", "Subjects"
    INTERVENTION = "intervention", "Intervention"
    RECORDING = "recording", "Recording"


class ComponentStatus(models.TextChoices):
    """Status workflow for Components."""
    PENDING = "pending", "Pending"
    READY = "ready", "Ready"
    ACTIVE = "active", "Active"
    COMPLETE = "complete", "Complete"
    FAILED = "failed", "Failed"


class DatasetStatus(models.TextChoices):
    """Status workflow for Datasets."""
    BUILDING = "building", "Building"
    QC_PENDING = "qc_pending", "QC Pending"
    FROZEN = "frozen", "Frozen"
    PUBLISHED = "published", "Published"
    DEPRECATED = "deprecated", "Deprecated"


class AnalysisStatus(models.TextChoices):
    """Status workflow for Analyses."""
    SCRATCH = "scratch", "Scratch"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    REVIEWED = "reviewed", "Reviewed"
    FROZEN = "frozen", "Frozen"
    PUBLISHED = "published", "Published"
    FAILED = "failed", "Failed"


class VisualizationStatus(models.TextChoices):
    """Status workflow for Visualizations."""
    DRAFT = "draft", "Draft"
    REVIEWED = "reviewed", "Reviewed"
    FROZEN = "frozen", "Frozen"


class PanelStatus(models.TextChoices):
    """Status workflow for Panels."""
    DRAFT = "draft", "Draft"
    REVIEWED = "reviewed", "Reviewed"
    FROZEN = "frozen", "Frozen"
    PUBLISHED = "published", "Published"


class FigureRole(models.TextChoices):
    """Role of panel in a figure."""
    MAIN = "main", "Main Figure"
    SUPPLEMENT = "supplement", "Supplementary Figure"
    EXTENDED = "extended", "Extended Data"


# =============================================================================
# Abstract Base Models
# =============================================================================

class TimestampedModel(models.Model):
    """Abstract base model providing created/updated timestamps."""
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class ELNLinkMixin(models.Model):
    """
    Mixin providing ELN integration fields.

    Entities with reasoning or narrative context expose these fields for
    deep links back to the canonical ELN pages and snapshot metadata.
    """
    eln_url = models.URLField(
        blank=True,
        help_text="Deep link to the canonical ELN page"
    )
    eln_snapshot_path = models.CharField(
        max_length=500,
        blank=True,
        help_text="Path to ELN export snapshot"
    )
    eln_snapshot_hash = models.CharField(
        max_length=128,
        blank=True,
        help_text="Hash of ELN snapshot for integrity verification"
    )
    eln_snapshot_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when ELN snapshot was taken"
    )

    class Meta:
        abstract = True


# =============================================================================
# Core Domain Models
# =============================================================================

class Question(TimestampedModel, ELNLinkMixin):
    """
    A scientific question or sub-question that can drive experimental runs.

    Questions form a hierarchy (parent/child) and can be marked as pilot
    (exploratory) or operational (ready to drive gated runs).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    title = models.CharField(max_length=300)
    description = models.TextField(blank=True)

    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="children"
    )

    hypothesis = models.TextField(
        blank=True,
        help_text="The hypothesis to be tested"
    )
    success_criteria = models.TextField(
        blank=True,
        help_text="Criteria for evaluating whether the question is answered"
    )

    status = models.CharField(
        max_length=20,
        choices=QuestionStatus.choices,
        default=QuestionStatus.DRAFT
    )
    is_pilot = models.BooleanField(
        default=False,
        help_text="True if this is an exploratory/pilot question"
    )

    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title

    def get_ancestors(self):
        """Return list of ancestor questions from root to immediate parent."""
        ancestors = []
        current = self.parent
        while current:
            ancestors.insert(0, current)
            current = current.parent
        return ancestors


class Claim(TimestampedModel, ELNLinkMixin):
    """
    An explicit scientific statement that may be supported or refuted by evidence.

    Claims form a hierarchy and progress through statuses from sketchy ideas
    to fully assessed claims. They maintain explicit links to evidence:
    Panels and Analyses. Dataset provenance is derived through the evidence
    chain via get_source_datasets().
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    title = models.CharField(max_length=300)
    statement = models.TextField(
        help_text="The explicit scientific statement"
    )

    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="children"
    )

    questions = models.ManyToManyField(
        Question,
        blank=True,
        related_name="claims",
        help_text="Questions this claim is linked to"
    )

    status = models.CharField(
        max_length=20,
        choices=ClaimStatus.choices,
        default=ClaimStatus.SKETCHED
    )

    assessment_notes = models.TextField(
        blank=True,
        help_text="Notes from evidence assessment"
    )

    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title

    def get_source_datasets(self):
        """
        Return all datasets underlying this claim's evidence.

        Traces provenance through:
        - Panel -> Visualization -> datasets (direct M2M)
        - Panel -> Visualization -> Analysis -> Dataset
        - Analysis -> Dataset
        """
        datasets = set()
        for evidence in self.evidence_links.all():
            # Direct analysis evidence
            if evidence.analysis and evidence.analysis.dataset:
                datasets.add(evidence.analysis.dataset)
            # Panel evidence - trace through visualization
            if evidence.panel and evidence.panel.visualization:
                viz = evidence.panel.visualization
                # Visualization can link directly to datasets
                for ds in viz.datasets.all():
                    datasets.add(ds)
                # Or through its analysis
                if viz.analysis and viz.analysis.dataset:
                    datasets.add(viz.analysis.dataset)
        return datasets


class Cohort(TimestampedModel):
    """
    A collection of specimens that can be drawn upon for Runs and Sessions.

    Can be pooled (interchangeable animals) or enumerated (named individuals).
    Tracks genotype, sex, rearing conditions, age ranges, and availability.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)

    cohort_type = models.CharField(
        max_length=20,
        choices=CohortType.choices,
        default=CohortType.POOLED
    )

    genotype = models.CharField(
        max_length=200,
        blank=True,
        help_text="Genetic background or transgenic line"
    )
    sex = models.CharField(
        max_length=10,
        choices=Sex.choices,
        default=Sex.MIXED
    )
    rearing_conditions = models.TextField(
        blank=True,
        help_text="Housing, diet, light cycle, and other rearing details"
    )

    # Age range in days
    min_age_days = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Minimum age in days"
    )
    max_age_days = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Maximum age in days"
    )

    # Availability tracking
    total_count = models.PositiveIntegerField(
        default=0,
        help_text="Total number of specimens in cohort"
    )
    available_count = models.PositiveIntegerField(
        default=0,
        help_text="Number of specimens currently available"
    )

    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def clean(self):
        if self.available_count > self.total_count:
            raise ValidationError(
                {"available_count": "Available count cannot exceed total count."}
            )
        if self.min_age_days and self.max_age_days:
            if self.min_age_days > self.max_age_days:
                raise ValidationError(
                    {"min_age_days": "Minimum age cannot exceed maximum age."}
                )


class Run(TimestampedModel, ELNLinkMixin):
    """
    A planned or executed experimental run, potentially covering many
    subjects and Sessions.

    Always linked to at least one Question. Holds design intent and has
    a status workflow. Serves as the parent for Sessions.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)

    questions = models.ManyToManyField(
        Question,
        related_name="runs",
        help_text="Questions this run addresses"
    )

    cohort = models.ForeignKey(
        Cohort,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="runs",
        help_text="Cohort for subject sourcing"
    )

    # Design intent
    data_sink = models.CharField(
        max_length=500,
        blank=True,
        help_text="Path or identifier for where data will be stored"
    )
    planned_start = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Planned start date/time"
    )
    planned_end = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Planned end date/time"
    )
    success_criteria = models.TextField(
        blank=True,
        help_text="Criteria for run success"
    )

    # Actual execution times
    actual_start = models.DateTimeField(null=True, blank=True)
    actual_end = models.DateTimeField(null=True, blank=True)

    status = models.CharField(
        max_length=20,
        choices=RunStatus.choices,
        default=RunStatus.PLANNED
    )

    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name


class Session(TimestampedModel):
    """
    A co-temporal slice of a Run (e.g., a particular time window on a
    specific rig or room).

    Child of a Run, can be scheduled manually or relative to other sessions.
    Serves as the parent for Components.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    run = models.ForeignKey(
        Run,
        on_delete=models.CASCADE,
        related_name="sessions"
    )

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)

    # Scheduling
    scheduled_start = models.DateTimeField(null=True, blank=True)
    scheduled_end = models.DateTimeField(null=True, blank=True)
    actual_start = models.DateTimeField(null=True, blank=True)
    actual_end = models.DateTimeField(null=True, blank=True)

    # Relative scheduling reference
    relative_to = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="dependent_sessions",
        help_text="Another session this one is scheduled relative to"
    )
    relative_offset_minutes = models.IntegerField(
        null=True,
        blank=True,
        help_text="Offset in minutes from the referenced session"
    )

    # Location/equipment
    rig_identifier = models.CharField(
        max_length=100,
        blank=True,
        help_text="Identifier for the rig, room, or equipment"
    )

    status = models.CharField(
        max_length=20,
        choices=SessionStatus.choices,
        default=SessionStatus.PLANNED
    )

    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["scheduled_start", "name"]

    def __str__(self):
        return f"{self.run.name} - {self.name}"


class Component(TimestampedModel):
    """
    A role-specific element of a Session (Subjects, Intervention, or Recording).

    Components carry a structured but extensible metadata payload for
    role- and modality-specific details.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    session = models.ForeignKey(
        Session,
        on_delete=models.CASCADE,
        related_name="components"
    )

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)

    role = models.CharField(
        max_length=20,
        choices=ComponentRole.choices
    )

    # Modality and equipment
    modality = models.CharField(
        max_length=100,
        blank=True,
        help_text="E.g., 2-photon, ephys, behavior_video, optogenetics"
    )
    equipment_identifier = models.CharField(
        max_length=200,
        blank=True,
        help_text="Identifier for specific equipment used"
    )

    # Timing within session
    start_offset_seconds = models.IntegerField(
        null=True,
        blank=True,
        help_text="Start offset from session start in seconds"
    )
    duration_seconds = models.IntegerField(
        null=True,
        blank=True,
        help_text="Expected duration in seconds"
    )

    # Subject-specific fields (for role=SUBJECTS)
    cohort = models.ForeignKey(
        Cohort,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="components",
        help_text="Cohort for subject components"
    )
    requested_count = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Requested number of subjects"
    )
    consumed_count = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Actually consumed/used subjects"
    )
    sampling_notes = models.TextField(
        blank=True,
        help_text="Notes on subject selection and sampling"
    )

    # Recording-specific fields (for role=RECORDING)
    trial_count = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Number of trials for recording components"
    )
    trial_log_path = models.CharField(
        max_length=500,
        blank=True,
        help_text="Path to trial log file"
    )

    # Flexible metadata payload for role/modality-specific details
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Extensible metadata for role/modality-specific configuration"
    )

    status = models.CharField(
        max_length=20,
        choices=ComponentStatus.choices,
        default=ComponentStatus.PENDING
    )

    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["session", "role", "name"]

    def __str__(self):
        return f"{self.session.name} - {self.get_role_display()}: {self.name}"


class Dataset(TimestampedModel, ELNLinkMixin):
    """
    An aggregate, QC-aware collection of data, often spanning multiple Runs.

    May be linked to one or more Runs and their Sessions. Encodes inclusion
    criteria, replicate targets, and tracks readiness via a status workflow.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    version = models.CharField(
        max_length=50,
        default="1.0",
        help_text="Version identifier for the dataset"
    )

    # Links to Runs (primary and contributing)
    primary_run = models.ForeignKey(
        Run,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="primary_datasets",
        help_text="Primary run this dataset is associated with"
    )
    contributing_runs = models.ManyToManyField(
        Run,
        blank=True,
        related_name="contributing_datasets",
        help_text="Additional runs contributing data"
    )
    sessions = models.ManyToManyField(
        Session,
        blank=True,
        related_name="datasets",
        help_text="Specific sessions included in dataset"
    )

    # Inclusion criteria and replication
    inclusion_criteria = models.TextField(
        blank=True,
        help_text="Criteria for including data in this dataset"
    )
    replicate_targets = models.JSONField(
        default=dict,
        blank=True,
        help_text="Target replicate counts per condition cell"
    )

    # Data location and processing
    output_path = models.CharField(
        max_length=500,
        blank=True,
        help_text="Canonical path to dataset outputs"
    )
    analysis_job_ids = models.JSONField(
        default=list,
        blank=True,
        help_text="List of job identifiers for analysis processing"
    )

    status = models.CharField(
        max_length=20,
        choices=DatasetStatus.choices,
        default=DatasetStatus.BUILDING
    )

    frozen_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when dataset was frozen"
    )

    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} v{self.version}"


class Analysis(TimestampedModel, ELNLinkMixin):
    """
    A transformation of a Dataset or raw data using a specified recipe.

    Identified by a recipe identifier and parameter set. Tracks execution
    via job identifiers and has a status workflow.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)

    dataset = models.ForeignKey(
        Dataset,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="analyses",
        help_text="Dataset being analyzed"
    )

    # Recipe identification
    recipe_identifier = models.CharField(
        max_length=200,
        help_text="Identifier for the analysis recipe/pipeline"
    )
    recipe_version = models.CharField(
        max_length=50,
        blank=True,
        help_text="Version of the recipe"
    )

    # Parameters - extensible payload
    parameters = models.JSONField(
        default=dict,
        blank=True,
        help_text="Analysis parameters and configuration"
    )

    # Execution tracking
    job_identifier = models.CharField(
        max_length=200,
        blank=True,
        help_text="Cluster job ID or execution identifier"
    )
    job_dashboard_url = models.URLField(
        blank=True,
        help_text="URL to job monitoring dashboard"
    )

    # Outputs
    output_path = models.CharField(
        max_length=500,
        blank=True,
        help_text="Path to analysis outputs"
    )
    outputs_manifest = models.JSONField(
        default=dict,
        blank=True,
        help_text="Manifest of output files and their locations"
    )

    status = models.CharField(
        max_length=20,
        choices=AnalysisStatus.choices,
        default=AnalysisStatus.SCRATCH
    )

    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    frozen_at = models.DateTimeField(null=True, blank=True)

    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "Analyses"

    def __str__(self):
        return self.name


class Visualization(TimestampedModel, ELNLinkMixin):
    """
    A concrete visual asset (plot, video, etc.) produced by an Analysis
    or other process.

    Records asset location and supports tags for discovery. Can be
    promoted into Panels.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)

    analysis = models.ForeignKey(
        Analysis,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="visualizations",
        help_text="Analysis that produced this visualization"
    )
    datasets = models.ManyToManyField(
        Dataset,
        blank=True,
        related_name="visualizations",
        help_text="Datasets referenced by this visualization"
    )

    # Asset location
    asset_path = models.CharField(
        max_length=500,
        blank=True,
        help_text="Path to the visualization file"
    )
    asset_url = models.URLField(
        blank=True,
        help_text="URL if visualization is hosted"
    )
    asset_type = models.CharField(
        max_length=50,
        blank=True,
        help_text="Type of asset: plot, video, interactive, etc."
    )

    # Rendering source
    rendering_notebook = models.CharField(
        max_length=500,
        blank=True,
        help_text="Path to notebook that generates this visualization"
    )
    rendering_script = models.CharField(
        max_length=500,
        blank=True,
        help_text="Path to script that generates this visualization"
    )

    # Discovery
    tags = models.JSONField(
        default=list,
        blank=True,
        help_text="Tags for discovery and filtering"
    )

    status = models.CharField(
        max_length=20,
        choices=VisualizationStatus.choices,
        default=VisualizationStatus.DRAFT
    )

    frozen_at = models.DateTimeField(null=True, blank=True)

    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name


class Panel(TimestampedModel, ELNLinkMixin):
    """
    A leaf figure element that wraps a Visualization for inclusion in a figure.

    Represents a single panel (e.g., "Figure 2A"), storing panel label,
    caption, and figure association. Links to underlying Visualization and
    Datasets, and participates in the evidence graph via links to Claims.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    label = models.CharField(
        max_length=50,
        help_text="Panel label, e.g., 'A', '2A', 'S1A'"
    )
    caption = models.TextField(
        blank=True,
        help_text="Panel caption text"
    )

    # Figure association
    figure_identifier = models.CharField(
        max_length=50,
        blank=True,
        help_text="Figure number or identifier, e.g., 'Figure 2', 'Figure S1'"
    )
    figure_role = models.CharField(
        max_length=20,
        choices=FigureRole.choices,
        default=FigureRole.MAIN
    )

    # Underlying assets
    visualization = models.ForeignKey(
        Visualization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="panels",
        help_text="Visualization this panel wraps"
    )
    datasets = models.ManyToManyField(
        Dataset,
        blank=True,
        related_name="panels",
        help_text="Datasets underlying this panel"
    )

    # File integrity when frozen
    frozen_file_path = models.CharField(
        max_length=500,
        blank=True,
        help_text="Path to frozen panel file"
    )
    frozen_file_hash = models.CharField(
        max_length=128,
        blank=True,
        help_text="Hash of frozen file for integrity verification"
    )

    status = models.CharField(
        max_length=20,
        choices=PanelStatus.choices,
        default=PanelStatus.DRAFT
    )

    frozen_at = models.DateTimeField(null=True, blank=True)

    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["figure_identifier", "label"]

    def __str__(self):
        if self.figure_identifier:
            return f"{self.figure_identifier} Panel {self.label}"
        return f"Panel {self.label}"


# =============================================================================
# Evidence Graph / Junction Tables
# =============================================================================

class ClaimEvidence(TimestampedModel):
    """
    Links Claims to their evidence (Panels or Analyses).

    This explicit junction table allows for tracking the type and strength
    of evidence relationships. Dataset provenance is derived through the
    evidence chain: Panel -> Visualization -> Analysis -> Dataset, or
    directly Analysis -> Dataset.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    claim = models.ForeignKey(
        Claim,
        on_delete=models.CASCADE,
        related_name="evidence_links"
    )

    # Evidence can be a Panel or Analysis
    # Datasets are accessed via get_source_datasets() on Claim
    panel = models.ForeignKey(
        Panel,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="claim_evidence_links"
    )
    analysis = models.ForeignKey(
        Analysis,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="claim_evidence_links"
    )

    # Evidence metadata
    evidence_type = models.CharField(
        max_length=50,
        blank=True,
        help_text="Type of evidence: supporting, refuting, contextual"
    )
    description = models.TextField(
        blank=True,
        help_text="Description of how this evidence relates to the claim"
    )

    class Meta:
        verbose_name = "Claim Evidence"
        verbose_name_plural = "Claim Evidence"

    def __str__(self):
        evidence_target = self.panel or self.analysis
        return f"{self.claim.title} <- {evidence_target}"

    def clean(self):
        # Ensure exactly one evidence type is linked
        links = [self.panel, self.analysis]
        linked_count = sum(1 for link in links if link is not None)
        if linked_count != 1:
            raise ValidationError(
                "Exactly one of panel or analysis must be specified."
            )
