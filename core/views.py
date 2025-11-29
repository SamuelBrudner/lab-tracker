"""
Django REST Framework viewsets for the Neuroscience Experiment Orchestration App.

Provides CRUD operations and filtering for all core domain models.
"""

from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone

from .models import (
    Question, Claim, Cohort, Run, Session, Component,
    Dataset, Analysis, Visualization, Panel, ClaimEvidence,
    QuestionStatus, ClaimStatus, RunStatus, SessionStatus,
    ComponentStatus, DatasetStatus, AnalysisStatus, VisualizationStatus, PanelStatus
)
from .serializers import (
    QuestionSerializer, QuestionListSerializer,
    ClaimSerializer, ClaimListSerializer,
    CohortSerializer, CohortListSerializer,
    RunSerializer, RunListSerializer,
    SessionSerializer, SessionListSerializer,
    ComponentSerializer, ComponentListSerializer,
    DatasetSerializer, DatasetListSerializer,
    AnalysisSerializer, AnalysisListSerializer,
    VisualizationSerializer, VisualizationListSerializer,
    PanelSerializer, PanelListSerializer,
    ClaimEvidenceSerializer, ClaimEvidenceListSerializer
)


class QuestionViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Question CRUD operations.

    Supports filtering by status, is_pilot, and parent.
    Provides hierarchy navigation through children and ancestors.
    """
    queryset = Question.objects.all()
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'is_pilot', 'parent']
    search_fields = ['title', 'description', 'hypothesis']
    ordering_fields = ['created_at', 'updated_at', 'title', 'status']
    ordering = ['-created_at']

    def get_serializer_class(self):
        if self.action == 'list':
            return QuestionListSerializer
        return QuestionSerializer

    @action(detail=False, methods=['get'])
    def roots(self, request):
        """Return all root questions (no parent)."""
        queryset = self.filter_queryset(self.get_queryset().filter(parent__isnull=True))
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def descendants(self, request, pk=None):
        """Return all descendants of a question."""
        question = self.get_object()
        descendants = []

        def collect_descendants(q):
            for child in q.children.all():
                descendants.append(child)
                collect_descendants(child)

        collect_descendants(question)
        serializer = QuestionListSerializer(descendants, many=True)
        return Response(serializer.data)


class ClaimViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Claim CRUD operations.

    Supports filtering by status and parent.
    Provides evidence graph queries.
    """
    queryset = Claim.objects.all()
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'parent']
    search_fields = ['title', 'statement', 'assessment_notes']
    ordering_fields = ['created_at', 'updated_at', 'title', 'status']
    ordering = ['-created_at']

    def get_serializer_class(self):
        if self.action == 'list':
            return ClaimListSerializer
        return ClaimSerializer

    @action(detail=True, methods=['get'])
    def evidence(self, request, pk=None):
        """Return all evidence links for a claim."""
        claim = self.get_object()
        evidence = claim.evidence_links.all()
        serializer = ClaimEvidenceListSerializer(evidence, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def by_status(self, request):
        """Return claims grouped by status with counts."""
        from django.db.models import Count
        status_counts = (
            Claim.objects
            .values('status')
            .annotate(count=Count('id'))
            .order_by('status')
        )
        return Response(list(status_counts))


class CohortViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Cohort CRUD operations.

    Supports filtering by cohort_type, genotype, and sex.
    Provides availability queries.
    """
    queryset = Cohort.objects.all()
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['cohort_type', 'sex', 'genotype']
    search_fields = ['name', 'description', 'genotype']
    ordering_fields = ['created_at', 'updated_at', 'name', 'available_count']
    ordering = ['name']

    def get_serializer_class(self):
        if self.action == 'list':
            return CohortListSerializer
        return CohortSerializer

    @action(detail=False, methods=['get'])
    def available(self, request):
        """Return cohorts with available specimens."""
        queryset = self.filter_queryset(
            self.get_queryset().filter(available_count__gt=0)
        )
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)


class RunViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Run CRUD operations.

    Supports filtering by status and cohort.
    Provides session management and status transitions.
    """
    queryset = Run.objects.all()
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'cohort']
    search_fields = ['name', 'description', 'data_sink']
    ordering_fields = ['created_at', 'updated_at', 'name', 'status', 'planned_start']
    ordering = ['-created_at']

    def get_serializer_class(self):
        if self.action == 'list':
            return RunListSerializer
        return RunSerializer

    @action(detail=True, methods=['get'])
    def sessions(self, request, pk=None):
        """Return all sessions for a run."""
        run = self.get_object()
        sessions = run.sessions.all()
        serializer = SessionListSerializer(sessions, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def start(self, request, pk=None):
        """Transition run to running status."""
        run = self.get_object()
        if run.status not in [RunStatus.PLANNED, RunStatus.SCHEDULED]:
            return Response(
                {'error': f'Cannot start run in {run.status} status'},
                status=status.HTTP_400_BAD_REQUEST
            )
        run.status = RunStatus.RUNNING
        run.actual_start = timezone.now()
        run.save()
        return Response(RunSerializer(run).data)

    @action(detail=True, methods=['post'])
    def complete(self, request, pk=None):
        """Transition run to completed status."""
        run = self.get_object()
        if run.status != RunStatus.RUNNING:
            return Response(
                {'error': f'Cannot complete run in {run.status} status'},
                status=status.HTTP_400_BAD_REQUEST
            )
        run.status = RunStatus.COMPLETED
        run.actual_end = timezone.now()
        run.save()
        return Response(RunSerializer(run).data)

    @action(detail=False, methods=['get'])
    def upcoming(self, request):
        """Return planned and scheduled runs."""
        queryset = self.filter_queryset(
            self.get_queryset().filter(
                status__in=[RunStatus.PLANNED, RunStatus.SCHEDULED]
            )
        )
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def active(self, request):
        """Return currently running runs."""
        queryset = self.filter_queryset(
            self.get_queryset().filter(status=RunStatus.RUNNING)
        )
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)


class SessionViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Session CRUD operations.

    Supports filtering by run and status.
    """
    queryset = Session.objects.all()
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['run', 'status', 'rig_identifier']
    search_fields = ['name', 'description', 'rig_identifier']
    ordering_fields = ['created_at', 'updated_at', 'scheduled_start', 'name', 'status']
    ordering = ['scheduled_start', 'name']

    def get_serializer_class(self):
        if self.action == 'list':
            return SessionListSerializer
        return SessionSerializer

    @action(detail=True, methods=['get'])
    def components(self, request, pk=None):
        """Return all components for a session."""
        session = self.get_object()
        components = session.components.all()
        serializer = ComponentListSerializer(components, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def start(self, request, pk=None):
        """Transition session to in_progress status."""
        session = self.get_object()
        if session.status not in [SessionStatus.PLANNED, SessionStatus.SCHEDULED]:
            return Response(
                {'error': f'Cannot start session in {session.status} status'},
                status=status.HTTP_400_BAD_REQUEST
            )
        session.status = SessionStatus.IN_PROGRESS
        session.actual_start = timezone.now()
        session.save()
        return Response(SessionSerializer(session).data)

    @action(detail=True, methods=['post'])
    def complete(self, request, pk=None):
        """Transition session to complete status."""
        session = self.get_object()
        if session.status != SessionStatus.IN_PROGRESS:
            return Response(
                {'error': f'Cannot complete session in {session.status} status'},
                status=status.HTTP_400_BAD_REQUEST
            )
        session.status = SessionStatus.COMPLETE
        session.actual_end = timezone.now()
        session.save()
        return Response(SessionSerializer(session).data)


class ComponentViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Component CRUD operations.

    Supports filtering by session, role, and status.
    """
    queryset = Component.objects.all()
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['session', 'role', 'status', 'modality']
    search_fields = ['name', 'description', 'modality', 'equipment_identifier']
    ordering_fields = ['created_at', 'updated_at', 'name', 'role', 'status']
    ordering = ['session', 'role', 'name']

    def get_serializer_class(self):
        if self.action == 'list':
            return ComponentListSerializer
        return ComponentSerializer

    @action(detail=False, methods=['get'])
    def by_role(self, request):
        """Return components filtered by role parameter."""
        role = request.query_params.get('role')
        if not role:
            return Response(
                {'error': 'role parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        queryset = self.filter_queryset(
            self.get_queryset().filter(role=role)
        )
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)


class DatasetViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Dataset CRUD operations.

    Supports filtering by status and primary_run.
    Provides freeze/publish actions.
    """
    queryset = Dataset.objects.all()
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'primary_run']
    search_fields = ['name', 'description', 'version']
    ordering_fields = ['created_at', 'updated_at', 'name', 'status', 'frozen_at']
    ordering = ['-created_at']

    def get_serializer_class(self):
        if self.action == 'list':
            return DatasetListSerializer
        return DatasetSerializer

    @action(detail=True, methods=['post'])
    def freeze(self, request, pk=None):
        """Freeze the dataset."""
        dataset = self.get_object()
        if dataset.status not in [DatasetStatus.BUILDING, DatasetStatus.QC_PENDING]:
            return Response(
                {'error': f'Cannot freeze dataset in {dataset.status} status'},
                status=status.HTTP_400_BAD_REQUEST
            )
        dataset.status = DatasetStatus.FROZEN
        dataset.frozen_at = timezone.now()
        dataset.save()
        return Response(DatasetSerializer(dataset).data)

    @action(detail=False, methods=['get'])
    def ready(self, request):
        """Return datasets ready for analysis (frozen or published)."""
        queryset = self.filter_queryset(
            self.get_queryset().filter(
                status__in=[DatasetStatus.FROZEN, DatasetStatus.PUBLISHED]
            )
        )
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)


class AnalysisViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Analysis CRUD operations.

    Supports filtering by dataset and status.
    Provides execution tracking actions.
    """
    queryset = Analysis.objects.all()
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['dataset', 'status', 'recipe_identifier']
    search_fields = ['name', 'description', 'recipe_identifier', 'job_identifier']
    ordering_fields = ['created_at', 'updated_at', 'name', 'status', 'started_at', 'completed_at']
    ordering = ['-created_at']

    def get_serializer_class(self):
        if self.action == 'list':
            return AnalysisListSerializer
        return AnalysisSerializer

    @action(detail=True, methods=['post'])
    def start_execution(self, request, pk=None):
        """Mark analysis as running."""
        analysis = self.get_object()
        if analysis.status != AnalysisStatus.SCRATCH:
            return Response(
                {'error': f'Cannot start analysis in {analysis.status} status'},
                status=status.HTTP_400_BAD_REQUEST
            )
        analysis.status = AnalysisStatus.RUNNING
        analysis.started_at = timezone.now()
        # Allow setting job_identifier from request
        if 'job_identifier' in request.data:
            analysis.job_identifier = request.data['job_identifier']
        if 'job_dashboard_url' in request.data:
            analysis.job_dashboard_url = request.data['job_dashboard_url']
        analysis.save()
        return Response(AnalysisSerializer(analysis).data)

    @action(detail=True, methods=['post'])
    def complete_execution(self, request, pk=None):
        """Mark analysis as completed."""
        analysis = self.get_object()
        if analysis.status != AnalysisStatus.RUNNING:
            return Response(
                {'error': f'Cannot complete analysis in {analysis.status} status'},
                status=status.HTTP_400_BAD_REQUEST
            )
        analysis.status = AnalysisStatus.COMPLETED
        analysis.completed_at = timezone.now()
        # Allow setting outputs from request
        if 'output_path' in request.data:
            analysis.output_path = request.data['output_path']
        if 'outputs_manifest' in request.data:
            analysis.outputs_manifest = request.data['outputs_manifest']
        analysis.save()
        return Response(AnalysisSerializer(analysis).data)

    @action(detail=True, methods=['post'])
    def freeze(self, request, pk=None):
        """Freeze the analysis."""
        analysis = self.get_object()
        if analysis.status not in [AnalysisStatus.COMPLETED, AnalysisStatus.REVIEWED]:
            return Response(
                {'error': f'Cannot freeze analysis in {analysis.status} status'},
                status=status.HTTP_400_BAD_REQUEST
            )
        analysis.status = AnalysisStatus.FROZEN
        analysis.frozen_at = timezone.now()
        analysis.save()
        return Response(AnalysisSerializer(analysis).data)


class VisualizationViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Visualization CRUD operations.

    Supports filtering by analysis and status.
    """
    queryset = Visualization.objects.all()
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['analysis', 'status', 'asset_type']
    search_fields = ['name', 'description', 'tags']
    ordering_fields = ['created_at', 'updated_at', 'name', 'status', 'frozen_at']
    ordering = ['-created_at']

    def get_serializer_class(self):
        if self.action == 'list':
            return VisualizationListSerializer
        return VisualizationSerializer

    @action(detail=True, methods=['post'])
    def freeze(self, request, pk=None):
        """Freeze the visualization."""
        viz = self.get_object()
        if viz.status not in [VisualizationStatus.DRAFT, VisualizationStatus.REVIEWED]:
            return Response(
                {'error': f'Cannot freeze visualization in {viz.status} status'},
                status=status.HTTP_400_BAD_REQUEST
            )
        viz.status = VisualizationStatus.FROZEN
        viz.frozen_at = timezone.now()
        viz.save()
        return Response(VisualizationSerializer(viz).data)

    @action(detail=False, methods=['get'])
    def by_tag(self, request):
        """Return visualizations matching a tag."""
        tag = request.query_params.get('tag')
        if not tag:
            return Response(
                {'error': 'tag parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        queryset = self.filter_queryset(
            self.get_queryset().filter(tags__contains=[tag])
        )
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)


class PanelViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Panel CRUD operations.

    Supports filtering by figure and status.
    """
    queryset = Panel.objects.all()
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['visualization', 'status', 'figure_role', 'figure_identifier']
    search_fields = ['label', 'caption', 'figure_identifier']
    ordering_fields = ['created_at', 'updated_at', 'figure_identifier', 'label', 'status']
    ordering = ['figure_identifier', 'label']

    def get_serializer_class(self):
        if self.action == 'list':
            return PanelListSerializer
        return PanelSerializer

    @action(detail=True, methods=['post'])
    def freeze(self, request, pk=None):
        """Freeze the panel."""
        panel = self.get_object()
        if panel.status not in [PanelStatus.DRAFT, PanelStatus.REVIEWED]:
            return Response(
                {'error': f'Cannot freeze panel in {panel.status} status'},
                status=status.HTTP_400_BAD_REQUEST
            )
        panel.status = PanelStatus.FROZEN
        panel.frozen_at = timezone.now()
        # Allow setting frozen file info from request
        if 'frozen_file_path' in request.data:
            panel.frozen_file_path = request.data['frozen_file_path']
        if 'frozen_file_hash' in request.data:
            panel.frozen_file_hash = request.data['frozen_file_hash']
        panel.save()
        return Response(PanelSerializer(panel).data)

    @action(detail=False, methods=['get'])
    def by_figure(self, request):
        """Return panels for a specific figure identifier."""
        figure = request.query_params.get('figure')
        if not figure:
            return Response(
                {'error': 'figure parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        queryset = self.filter_queryset(
            self.get_queryset().filter(figure_identifier=figure)
        )
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)


class ClaimEvidenceViewSet(viewsets.ModelViewSet):
    """
    ViewSet for ClaimEvidence CRUD operations.

    Links claims to their supporting evidence (panels, analyses, datasets).
    """
    queryset = ClaimEvidence.objects.all()
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['claim', 'panel', 'analysis', 'dataset', 'evidence_type']
    search_fields = ['description', 'evidence_type']
    ordering_fields = ['created_at', 'updated_at']
    ordering = ['-created_at']

    def get_serializer_class(self):
        if self.action == 'list':
            return ClaimEvidenceListSerializer
        return ClaimEvidenceSerializer
