"""
Behavior-driven tests for the Neuroscience Experiment Orchestration App.

Tests are organized around key behaviors:
- Model validation and constraints
- Status workflow transitions
- Hierarchy navigation (Questions)
- Evidence graph relationships
- API endpoints and custom actions
"""

from django.test import TestCase
from django.core.exceptions import ValidationError
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework import status
from datetime import timedelta

from .models import (
    Question, Claim, Cohort, Run, Session, Component, Dataset,
    Analysis, Visualization, Panel, ClaimEvidence,
    QuestionStatus, ClaimStatus, CohortType, Sex, RunStatus,
    SessionStatus, ComponentRole, ComponentStatus, DatasetStatus,
    AnalysisStatus, VisualizationStatus, PanelStatus, FigureRole
)


# =============================================================================
# Model Validation Tests
# =============================================================================

class CohortValidationTests(TestCase):
    """Test Cohort model validation rules."""

    def test_available_count_cannot_exceed_total_count(self):
        """Cohort available_count must not exceed total_count."""
        cohort = Cohort(
            name="Test Cohort",
            total_count=10,
            available_count=15  # Invalid: exceeds total
        )
        with self.assertRaises(ValidationError) as context:
            cohort.full_clean()
        self.assertIn('available_count', context.exception.message_dict)

    def test_valid_available_count(self):
        """Cohort with valid available_count should pass validation."""
        cohort = Cohort(
            name="Test Cohort",
            total_count=10,
            available_count=5
        )
        cohort.full_clean()  # Should not raise

    def test_min_age_cannot_exceed_max_age(self):
        """Cohort min_age_days must not exceed max_age_days."""
        cohort = Cohort(
            name="Test Cohort",
            min_age_days=100,
            max_age_days=50  # Invalid: min > max
        )
        with self.assertRaises(ValidationError) as context:
            cohort.full_clean()
        self.assertIn('min_age_days', context.exception.message_dict)

    def test_valid_age_range(self):
        """Cohort with valid age range should pass validation."""
        cohort = Cohort(
            name="Test Cohort",
            min_age_days=30,
            max_age_days=90
        )
        cohort.full_clean()  # Should not raise


class ClaimEvidenceValidationTests(TestCase):
    """Test ClaimEvidence model validation rules."""

    def setUp(self):
        self.claim = Claim.objects.create(
            title="Test Claim",
            statement="Test statement"
        )
        self.panel = Panel.objects.create(label="A")
        self.analysis = Analysis.objects.create(
            name="Test Analysis",
            recipe_identifier="test-recipe"
        )

    def test_exactly_one_evidence_type_required(self):
        """ClaimEvidence must link to exactly one evidence type."""
        # No evidence linked
        evidence = ClaimEvidence(claim=self.claim)
        with self.assertRaises(ValidationError):
            evidence.full_clean()

    def test_cannot_link_multiple_evidence_types(self):
        """ClaimEvidence cannot link to panel AND analysis."""
        evidence = ClaimEvidence(
            claim=self.claim,
            panel=self.panel,
            analysis=self.analysis  # Invalid: two evidence types
        )
        with self.assertRaises(ValidationError):
            evidence.full_clean()

    def test_valid_panel_evidence(self):
        """ClaimEvidence with only panel should be valid."""
        evidence = ClaimEvidence(
            claim=self.claim,
            panel=self.panel
        )
        evidence.full_clean()  # Should not raise

    def test_valid_analysis_evidence(self):
        """ClaimEvidence with only analysis should be valid."""
        evidence = ClaimEvidence(
            claim=self.claim,
            analysis=self.analysis
        )
        evidence.full_clean()  # Should not raise


# =============================================================================
# Status Workflow Tests
# =============================================================================

class RunStatusWorkflowTests(APITestCase):
    """Test Run status transitions via API actions."""

    def setUp(self):
        self.question = Question.objects.create(
            title="Test Question",
            status=QuestionStatus.OPERATIONAL
        )
        self.run = Run.objects.create(
            name="Test Run",
            status=RunStatus.PLANNED
        )
        self.run.questions.add(self.question)

    def test_start_run_from_planned(self):
        """Run can be started from PLANNED status."""
        response = self.client.post(f'/api/runs/{self.run.id}/start/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, RunStatus.RUNNING)
        self.assertIsNotNone(self.run.actual_start)

    def test_start_run_from_scheduled(self):
        """Run can be started from SCHEDULED status."""
        self.run.status = RunStatus.SCHEDULED
        self.run.save()
        response = self.client.post(f'/api/runs/{self.run.id}/start/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, RunStatus.RUNNING)

    def test_cannot_start_completed_run(self):
        """Run cannot be started from COMPLETED status."""
        self.run.status = RunStatus.COMPLETED
        self.run.save()
        response = self.client.post(f'/api/runs/{self.run.id}/start/')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', response.data)

    def test_complete_run_from_running(self):
        """Run can be completed from RUNNING status."""
        self.run.status = RunStatus.RUNNING
        self.run.actual_start = timezone.now()
        self.run.save()
        response = self.client.post(f'/api/runs/{self.run.id}/complete/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, RunStatus.COMPLETED)
        self.assertIsNotNone(self.run.actual_end)

    def test_cannot_complete_planned_run(self):
        """Run cannot be completed from PLANNED status."""
        response = self.client.post(f'/api/runs/{self.run.id}/complete/')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class SessionStatusWorkflowTests(APITestCase):
    """Test Session status transitions via API actions."""

    def setUp(self):
        self.question = Question.objects.create(title="Test Question")
        self.run = Run.objects.create(name="Test Run")
        self.run.questions.add(self.question)
        self.session = Session.objects.create(
            run=self.run,
            name="Test Session",
            status=SessionStatus.PLANNED
        )

    def test_start_session_from_planned(self):
        """Session can be started from PLANNED status."""
        response = self.client.post(f'/api/sessions/{self.session.id}/start/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, SessionStatus.IN_PROGRESS)
        self.assertIsNotNone(self.session.actual_start)

    def test_start_session_from_scheduled(self):
        """Session can be started from SCHEDULED status."""
        self.session.status = SessionStatus.SCHEDULED
        self.session.save()
        response = self.client.post(f'/api/sessions/{self.session.id}/start/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_cannot_start_completed_session(self):
        """Session cannot be started from COMPLETE status."""
        self.session.status = SessionStatus.COMPLETE
        self.session.save()
        response = self.client.post(f'/api/sessions/{self.session.id}/start/')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_complete_session_from_in_progress(self):
        """Session can be completed from IN_PROGRESS status."""
        self.session.status = SessionStatus.IN_PROGRESS
        self.session.actual_start = timezone.now()
        self.session.save()
        response = self.client.post(f'/api/sessions/{self.session.id}/complete/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, SessionStatus.COMPLETE)
        self.assertIsNotNone(self.session.actual_end)

    def test_cannot_complete_planned_session(self):
        """Session cannot be completed from PLANNED status."""
        response = self.client.post(f'/api/sessions/{self.session.id}/complete/')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class AnalysisStatusWorkflowTests(APITestCase):
    """Test Analysis status transitions via API actions."""

    def setUp(self):
        self.dataset = Dataset.objects.create(name="Test Dataset")
        self.analysis = Analysis.objects.create(
            name="Test Analysis",
            recipe_identifier="test-recipe",
            dataset=self.dataset,
            status=AnalysisStatus.SCRATCH
        )

    def test_start_execution_from_scratch(self):
        """Analysis can be started from SCRATCH status."""
        response = self.client.post(
            f'/api/analyses/{self.analysis.id}/start_execution/',
            {'job_identifier': 'job-123', 'job_dashboard_url': 'http://dashboard/123'}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.analysis.refresh_from_db()
        self.assertEqual(self.analysis.status, AnalysisStatus.RUNNING)
        self.assertEqual(self.analysis.job_identifier, 'job-123')
        self.assertIsNotNone(self.analysis.started_at)

    def test_cannot_start_running_analysis(self):
        """Analysis cannot be started from RUNNING status."""
        self.analysis.status = AnalysisStatus.RUNNING
        self.analysis.save()
        response = self.client.post(f'/api/analyses/{self.analysis.id}/start_execution/')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_complete_execution_from_running(self):
        """Analysis can be completed from RUNNING status."""
        self.analysis.status = AnalysisStatus.RUNNING
        self.analysis.started_at = timezone.now()
        self.analysis.save()
        response = self.client.post(
            f'/api/analyses/{self.analysis.id}/complete_execution/',
            {'output_path': '/data/outputs/analysis-1'}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.analysis.refresh_from_db()
        self.assertEqual(self.analysis.status, AnalysisStatus.COMPLETED)
        self.assertEqual(self.analysis.output_path, '/data/outputs/analysis-1')
        self.assertIsNotNone(self.analysis.completed_at)

    def test_freeze_from_completed(self):
        """Analysis can be frozen from COMPLETED status."""
        self.analysis.status = AnalysisStatus.COMPLETED
        self.analysis.save()
        response = self.client.post(f'/api/analyses/{self.analysis.id}/freeze/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.analysis.refresh_from_db()
        self.assertEqual(self.analysis.status, AnalysisStatus.FROZEN)
        self.assertIsNotNone(self.analysis.frozen_at)

    def test_freeze_from_reviewed(self):
        """Analysis can be frozen from REVIEWED status."""
        self.analysis.status = AnalysisStatus.REVIEWED
        self.analysis.save()
        response = self.client.post(f'/api/analyses/{self.analysis.id}/freeze/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_cannot_freeze_scratch_analysis(self):
        """Analysis cannot be frozen from SCRATCH status."""
        response = self.client.post(f'/api/analyses/{self.analysis.id}/freeze/')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class DatasetStatusWorkflowTests(APITestCase):
    """Test Dataset status transitions via API actions."""

    def setUp(self):
        self.dataset = Dataset.objects.create(
            name="Test Dataset",
            status=DatasetStatus.BUILDING
        )

    def test_freeze_from_building(self):
        """Dataset can be frozen from BUILDING status."""
        response = self.client.post(f'/api/datasets/{self.dataset.id}/freeze/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.dataset.refresh_from_db()
        self.assertEqual(self.dataset.status, DatasetStatus.FROZEN)
        self.assertIsNotNone(self.dataset.frozen_at)

    def test_freeze_from_qc_pending(self):
        """Dataset can be frozen from QC_PENDING status."""
        self.dataset.status = DatasetStatus.QC_PENDING
        self.dataset.save()
        response = self.client.post(f'/api/datasets/{self.dataset.id}/freeze/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_cannot_freeze_already_frozen(self):
        """Dataset cannot be frozen again from FROZEN status."""
        self.dataset.status = DatasetStatus.FROZEN
        self.dataset.save()
        response = self.client.post(f'/api/datasets/{self.dataset.id}/freeze/')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class VisualizationStatusWorkflowTests(APITestCase):
    """Test Visualization status transitions via API actions."""

    def setUp(self):
        self.viz = Visualization.objects.create(
            name="Test Viz",
            status=VisualizationStatus.DRAFT
        )

    def test_freeze_from_draft(self):
        """Visualization can be frozen from DRAFT status."""
        response = self.client.post(f'/api/visualizations/{self.viz.id}/freeze/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.viz.refresh_from_db()
        self.assertEqual(self.viz.status, VisualizationStatus.FROZEN)

    def test_freeze_from_reviewed(self):
        """Visualization can be frozen from REVIEWED status."""
        self.viz.status = VisualizationStatus.REVIEWED
        self.viz.save()
        response = self.client.post(f'/api/visualizations/{self.viz.id}/freeze/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_cannot_freeze_already_frozen(self):
        """Visualization cannot be frozen again."""
        self.viz.status = VisualizationStatus.FROZEN
        self.viz.save()
        response = self.client.post(f'/api/visualizations/{self.viz.id}/freeze/')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class PanelStatusWorkflowTests(APITestCase):
    """Test Panel status transitions via API actions."""

    def setUp(self):
        self.panel = Panel.objects.create(
            label="A",
            status=PanelStatus.DRAFT
        )

    def test_freeze_panel_with_file_info(self):
        """Panel can be frozen with file path and hash."""
        response = self.client.post(
            f'/api/panels/{self.panel.id}/freeze/',
            {
                'frozen_file_path': '/figures/fig1a.pdf',
                'frozen_file_hash': 'abc123hash'
            }
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.panel.refresh_from_db()
        self.assertEqual(self.panel.status, PanelStatus.FROZEN)
        self.assertEqual(self.panel.frozen_file_path, '/figures/fig1a.pdf')
        self.assertEqual(self.panel.frozen_file_hash, 'abc123hash')


# =============================================================================
# Question Hierarchy Tests
# =============================================================================

class QuestionHierarchyTests(TestCase):
    """Test Question hierarchy navigation."""

    def setUp(self):
        # Create a hierarchy: root -> child -> grandchild
        self.root = Question.objects.create(
            title="Root Question",
            status=QuestionStatus.OPERATIONAL
        )
        self.child = Question.objects.create(
            title="Child Question",
            parent=self.root,
            status=QuestionStatus.PILOT
        )
        self.grandchild = Question.objects.create(
            title="Grandchild Question",
            parent=self.child
        )
        self.sibling = Question.objects.create(
            title="Sibling Question",
            parent=self.root
        )

    def test_get_ancestors_from_grandchild(self):
        """Grandchild's ancestors should be [root, child] in order."""
        ancestors = self.grandchild.get_ancestors()
        self.assertEqual(len(ancestors), 2)
        self.assertEqual(ancestors[0], self.root)
        self.assertEqual(ancestors[1], self.child)

    def test_get_ancestors_from_child(self):
        """Child's ancestors should be [root]."""
        ancestors = self.child.get_ancestors()
        self.assertEqual(len(ancestors), 1)
        self.assertEqual(ancestors[0], self.root)

    def test_get_ancestors_from_root(self):
        """Root question should have no ancestors."""
        ancestors = self.root.get_ancestors()
        self.assertEqual(len(ancestors), 0)

    def test_children_relationship(self):
        """Parent should have access to children via related_name."""
        children = list(self.root.children.all())
        self.assertEqual(len(children), 2)
        self.assertIn(self.child, children)
        self.assertIn(self.sibling, children)


class QuestionHierarchyAPITests(APITestCase):
    """Test Question hierarchy API endpoints."""

    def setUp(self):
        self.root1 = Question.objects.create(title="Root 1")
        self.root2 = Question.objects.create(title="Root 2")
        self.child1 = Question.objects.create(title="Child 1", parent=self.root1)
        self.grandchild1 = Question.objects.create(title="Grandchild 1", parent=self.child1)

    def test_roots_endpoint_returns_only_root_questions(self):
        """The /roots/ endpoint should return only questions without parents."""
        response = self.client.get('/api/questions/roots/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        titles = [q['title'] for q in response.data]
        self.assertIn('Root 1', titles)
        self.assertIn('Root 2', titles)
        self.assertNotIn('Child 1', titles)
        self.assertNotIn('Grandchild 1', titles)

    def test_descendants_endpoint(self):
        """The /descendants/ endpoint should return all descendants."""
        response = self.client.get(f'/api/questions/{self.root1.id}/descendants/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        titles = [q['title'] for q in response.data]
        self.assertIn('Child 1', titles)
        self.assertIn('Grandchild 1', titles)
        self.assertEqual(len(response.data), 2)


# =============================================================================
# Evidence Graph Tests
# =============================================================================

class EvidenceGraphTests(TestCase):
    """Test evidence graph relationships between Claims and evidence."""

    def setUp(self):
        self.claim = Claim.objects.create(
            title="Main Claim",
            statement="Neurons fire in response to stimuli",
            status=ClaimStatus.EVIDENCE_GATHERING
        )
        self.dataset = Dataset.objects.create(name="Neural Dataset")
        self.analysis = Analysis.objects.create(
            name="Spike Analysis",
            recipe_identifier="spike-sorter",
            dataset=self.dataset
        )
        self.viz = Visualization.objects.create(
            name="Raster Plot",
            analysis=self.analysis
        )
        self.panel = Panel.objects.create(
            label="A",
            figure_identifier="Figure 1",
            visualization=self.viz
        )

    def test_claim_can_have_panel_evidence(self):
        """Claim can be linked to a Panel as evidence."""
        evidence = ClaimEvidence.objects.create(
            claim=self.claim,
            panel=self.panel,
            evidence_type="supporting",
            description="Shows clear spike response"
        )
        self.assertEqual(self.claim.evidence_links.count(), 1)
        self.assertEqual(self.claim.evidence_links.first().panel, self.panel)

    def test_claim_can_have_multiple_evidence_links(self):
        """Claim can have multiple evidence links (panel and analysis)."""
        ClaimEvidence.objects.create(
            claim=self.claim,
            panel=self.panel,
            evidence_type="supporting"
        )
        ClaimEvidence.objects.create(
            claim=self.claim,
            analysis=self.analysis,
            evidence_type="supporting"
        )
        self.assertEqual(self.claim.evidence_links.count(), 2)

    def test_evidence_chain_from_claim_to_raw_data(self):
        """Can trace from claim through panel, viz, analysis to dataset."""
        ClaimEvidence.objects.create(claim=self.claim, panel=self.panel)

        # Trace the chain
        evidence = self.claim.evidence_links.first()
        panel = evidence.panel
        viz = panel.visualization
        analysis = viz.analysis
        dataset = analysis.dataset

        self.assertEqual(panel.label, "A")
        self.assertEqual(viz.name, "Raster Plot")
        self.assertEqual(analysis.name, "Spike Analysis")
        self.assertEqual(dataset.name, "Neural Dataset")

    def test_get_source_datasets_from_panel_evidence(self):
        """get_source_datasets traces through panel -> viz -> analysis -> dataset."""
        ClaimEvidence.objects.create(claim=self.claim, panel=self.panel)

        datasets = self.claim.get_source_datasets()
        self.assertEqual(len(datasets), 1)
        self.assertIn(self.dataset, datasets)

    def test_get_source_datasets_from_analysis_evidence(self):
        """get_source_datasets traces through analysis -> dataset."""
        ClaimEvidence.objects.create(claim=self.claim, analysis=self.analysis)

        datasets = self.claim.get_source_datasets()
        self.assertEqual(len(datasets), 1)
        self.assertIn(self.dataset, datasets)

    def test_get_source_datasets_from_visualization_direct_link(self):
        """get_source_datasets includes datasets linked directly to visualization."""
        # Add a direct dataset link to visualization
        self.viz.datasets.add(self.dataset)
        ClaimEvidence.objects.create(claim=self.claim, panel=self.panel)

        datasets = self.claim.get_source_datasets()
        self.assertIn(self.dataset, datasets)


class EvidenceGraphAPITests(APITestCase):
    """Test evidence graph API endpoints."""

    def setUp(self):
        self.claim = Claim.objects.create(
            title="Test Claim",
            statement="Test statement"
        )
        self.panel = Panel.objects.create(label="A")
        self.analysis = Analysis.objects.create(
            name="Test Analysis",
            recipe_identifier="test"
        )
        ClaimEvidence.objects.create(
            claim=self.claim,
            panel=self.panel,
            evidence_type="supporting"
        )
        ClaimEvidence.objects.create(
            claim=self.claim,
            analysis=self.analysis,
            evidence_type="supporting"
        )

    def test_claim_evidence_endpoint(self):
        """The /evidence/ endpoint returns all evidence for a claim."""
        response = self.client.get(f'/api/claims/{self.claim.id}/evidence/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)


# =============================================================================
# API CRUD Tests
# =============================================================================

class QuestionCRUDTests(APITestCase):
    """Test Question CRUD operations."""

    def test_create_question(self):
        """Can create a question via API."""
        data = {
            'title': 'New Question',
            'description': 'A research question',
            'hypothesis': 'We hypothesize that...',
            'status': QuestionStatus.DRAFT
        }
        response = self.client.post('/api/questions/', data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Question.objects.count(), 1)
        self.assertEqual(Question.objects.first().title, 'New Question')

    def test_list_questions(self):
        """Can list questions via API."""
        Question.objects.create(title="Q1")
        Question.objects.create(title="Q2")
        response = self.client.get('/api/questions/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['results']), 2)

    def test_retrieve_question(self):
        """Can retrieve a single question via API."""
        q = Question.objects.create(title="Test Q", description="Details")
        response = self.client.get(f'/api/questions/{q.id}/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['title'], 'Test Q')

    def test_update_question(self):
        """Can update a question via API."""
        q = Question.objects.create(title="Old Title")
        response = self.client.patch(
            f'/api/questions/{q.id}/',
            {'title': 'New Title'},
            format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        q.refresh_from_db()
        self.assertEqual(q.title, 'New Title')

    def test_delete_question(self):
        """Can delete a question via API."""
        q = Question.objects.create(title="To Delete")
        response = self.client.delete(f'/api/questions/{q.id}/')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(Question.objects.count(), 0)


class CohortCRUDTests(APITestCase):
    """Test Cohort CRUD operations."""

    def test_create_cohort_with_full_details(self):
        """Can create a cohort with all tracking fields."""
        data = {
            'name': 'C57BL/6J Colony',
            'description': 'Standard black 6 mice',
            'cohort_type': CohortType.POOLED,
            'genotype': 'C57BL/6J',
            'sex': Sex.MIXED,
            'min_age_days': 60,
            'max_age_days': 90,
            'total_count': 50,
            'available_count': 35
        }
        response = self.client.post('/api/cohorts/', data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        cohort = Cohort.objects.first()
        self.assertEqual(cohort.genotype, 'C57BL/6J')
        self.assertEqual(cohort.available_count, 35)


class RunCRUDTests(APITestCase):
    """Test Run CRUD operations."""

    def test_create_run_with_question(self):
        """Can create a run associated with a question."""
        q = Question.objects.create(title="Research Question")
        data = {
            'name': 'Experiment Run 1',
            'description': 'First experimental run',
            'questions': [str(q.id)],
            'data_sink': '/data/runs/run-001',
            'status': RunStatus.PLANNED
        }
        response = self.client.post('/api/runs/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


class ComponentCRUDTests(APITestCase):
    """Test Component CRUD operations with different roles."""

    def setUp(self):
        self.question = Question.objects.create(title="Q")
        self.run = Run.objects.create(name="Run")
        self.run.questions.add(self.question)
        self.session = Session.objects.create(run=self.run, name="Session 1")
        self.cohort = Cohort.objects.create(
            name="Test Cohort",
            total_count=20,
            available_count=15
        )

    def test_create_subject_component(self):
        """Can create a SUBJECTS component with cohort link."""
        data = {
            'session': str(self.session.id),
            'name': 'Experimental Subjects',
            'role': ComponentRole.SUBJECTS,
            'cohort': str(self.cohort.id),
            'requested_count': 5
        }
        response = self.client.post('/api/components/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        comp = Component.objects.first()
        self.assertEqual(comp.role, ComponentRole.SUBJECTS)
        self.assertEqual(comp.cohort, self.cohort)

    def test_create_recording_component(self):
        """Can create a RECORDING component with modality."""
        data = {
            'session': str(self.session.id),
            'name': '2-Photon Recording',
            'role': ComponentRole.RECORDING,
            'modality': '2-photon',
            'trial_count': 100
        }
        response = self.client.post('/api/components/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        comp = Component.objects.first()
        self.assertEqual(comp.modality, '2-photon')

    def test_create_intervention_component(self):
        """Can create an INTERVENTION component."""
        data = {
            'session': str(self.session.id),
            'name': 'Optogenetic Stimulation',
            'role': ComponentRole.INTERVENTION,
            'modality': 'optogenetics',
            'metadata': {'wavelength': 470, 'power_mw': 5}
        }
        response = self.client.post('/api/components/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        comp = Component.objects.first()
        self.assertEqual(comp.metadata['wavelength'], 470)


# =============================================================================
# Custom Action Tests
# =============================================================================

class RunCustomActionTests(APITestCase):
    """Test Run viewset custom actions."""

    def setUp(self):
        self.question = Question.objects.create(title="Q")
        self.planned_run = Run.objects.create(name="Planned", status=RunStatus.PLANNED)
        self.scheduled_run = Run.objects.create(name="Scheduled", status=RunStatus.SCHEDULED)
        self.running_run = Run.objects.create(name="Running", status=RunStatus.RUNNING)
        self.completed_run = Run.objects.create(name="Completed", status=RunStatus.COMPLETED)
        for r in [self.planned_run, self.scheduled_run, self.running_run, self.completed_run]:
            r.questions.add(self.question)

    def test_upcoming_returns_planned_and_scheduled(self):
        """The /upcoming/ endpoint returns PLANNED and SCHEDULED runs."""
        response = self.client.get('/api/runs/upcoming/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [r['name'] for r in response.data]
        self.assertIn('Planned', names)
        self.assertIn('Scheduled', names)
        self.assertNotIn('Running', names)
        self.assertNotIn('Completed', names)

    def test_active_returns_running_runs(self):
        """The /active/ endpoint returns only RUNNING runs."""
        response = self.client.get('/api/runs/active/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [r['name'] for r in response.data]
        self.assertEqual(names, ['Running'])


class CohortCustomActionTests(APITestCase):
    """Test Cohort viewset custom actions."""

    def setUp(self):
        self.available_cohort = Cohort.objects.create(
            name="Available",
            total_count=10,
            available_count=5
        )
        self.empty_cohort = Cohort.objects.create(
            name="Empty",
            total_count=10,
            available_count=0
        )

    def test_available_returns_only_cohorts_with_specimens(self):
        """The /available/ endpoint returns only cohorts with available_count > 0."""
        response = self.client.get('/api/cohorts/available/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [c['name'] for c in response.data]
        self.assertIn('Available', names)
        self.assertNotIn('Empty', names)


class DatasetCustomActionTests(APITestCase):
    """Test Dataset viewset custom actions."""

    def setUp(self):
        self.building = Dataset.objects.create(name="Building", status=DatasetStatus.BUILDING)
        self.frozen = Dataset.objects.create(name="Frozen", status=DatasetStatus.FROZEN)
        self.published = Dataset.objects.create(name="Published", status=DatasetStatus.PUBLISHED)

    def test_ready_returns_frozen_and_published(self):
        """The /ready/ endpoint returns FROZEN and PUBLISHED datasets."""
        response = self.client.get('/api/datasets/ready/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [d['name'] for d in response.data]
        self.assertIn('Frozen', names)
        self.assertIn('Published', names)
        self.assertNotIn('Building', names)


class ComponentCustomActionTests(APITestCase):
    """Test Component viewset custom actions."""

    def setUp(self):
        self.question = Question.objects.create(title="Q")
        self.run = Run.objects.create(name="Run")
        self.run.questions.add(self.question)
        self.session = Session.objects.create(run=self.run, name="Session")
        self.subject = Component.objects.create(
            session=self.session,
            name="Subject",
            role=ComponentRole.SUBJECTS
        )
        self.recording = Component.objects.create(
            session=self.session,
            name="Recording",
            role=ComponentRole.RECORDING
        )

    def test_by_role_filters_components(self):
        """The /by_role/ endpoint filters by role parameter."""
        response = self.client.get('/api/components/by_role/', {'role': ComponentRole.SUBJECTS})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [c['name'] for c in response.data]
        self.assertIn('Subject', names)
        self.assertNotIn('Recording', names)

    def test_by_role_requires_parameter(self):
        """The /by_role/ endpoint requires role parameter."""
        response = self.client.get('/api/components/by_role/')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class VisualizationCustomActionTests(APITestCase):
    """Test Visualization viewset custom actions."""

    def setUp(self):
        self.viz_with_tag = Visualization.objects.create(
            name="Tagged Viz",
            tags=["neural", "calcium"]
        )
        self.viz_other_tag = Visualization.objects.create(
            name="Other Viz",
            tags=["behavior"]
        )

    def test_by_tag_filters_visualizations(self):
        """The /by_tag/ endpoint filters by tag.

        Note: This test is skipped on SQLite as JSON contains lookup
        is not supported. Use PostgreSQL in production for full JSON support.
        """
        from django.db import connection
        if connection.vendor == 'sqlite':
            self.skipTest("SQLite does not support JSON contains lookup")
        response = self.client.get('/api/visualizations/by_tag/', {'tag': 'neural'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [v['name'] for v in response.data]
        self.assertIn('Tagged Viz', names)
        self.assertNotIn('Other Viz', names)

    def test_by_tag_requires_parameter(self):
        """The /by_tag/ endpoint requires tag parameter."""
        response = self.client.get('/api/visualizations/by_tag/')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class PanelCustomActionTests(APITestCase):
    """Test Panel viewset custom actions."""

    def setUp(self):
        self.panel_fig1 = Panel.objects.create(label="A", figure_identifier="Figure 1")
        self.panel_fig1b = Panel.objects.create(label="B", figure_identifier="Figure 1")
        self.panel_fig2 = Panel.objects.create(label="A", figure_identifier="Figure 2")

    def test_by_figure_returns_panels_for_figure(self):
        """The /by_figure/ endpoint returns panels for specific figure."""
        response = self.client.get('/api/panels/by_figure/', {'figure': 'Figure 1'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        labels = [p['label'] for p in response.data]
        self.assertIn('A', labels)
        self.assertIn('B', labels)

    def test_by_figure_requires_parameter(self):
        """The /by_figure/ endpoint requires figure parameter."""
        response = self.client.get('/api/panels/by_figure/')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ClaimCustomActionTests(APITestCase):
    """Test Claim viewset custom actions."""

    def setUp(self):
        Claim.objects.create(title="C1", statement="S1", status=ClaimStatus.SKETCHED)
        Claim.objects.create(title="C2", statement="S2", status=ClaimStatus.SKETCHED)
        Claim.objects.create(title="C3", statement="S3", status=ClaimStatus.PUBLISHED)

    def test_by_status_returns_counts(self):
        """The /by_status/ endpoint returns status counts."""
        response = self.client.get('/api/claims/by_status/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        status_counts = {item['status']: item['count'] for item in response.data}
        self.assertEqual(status_counts.get('sketched'), 2)
        self.assertEqual(status_counts.get('published'), 1)


# =============================================================================
# Filtering Tests
# =============================================================================

class FilteringTests(APITestCase):
    """Test API filtering capabilities."""

    def test_filter_questions_by_status(self):
        """Can filter questions by status."""
        Question.objects.create(title="Draft Q", status=QuestionStatus.DRAFT)
        Question.objects.create(title="Operational Q", status=QuestionStatus.OPERATIONAL)

        response = self.client.get('/api/questions/', {'status': QuestionStatus.DRAFT})
        self.assertEqual(len(response.data['results']), 1)
        self.assertEqual(response.data['results'][0]['title'], 'Draft Q')

    def test_filter_questions_by_is_pilot(self):
        """Can filter questions by is_pilot flag."""
        Question.objects.create(title="Regular Q", is_pilot=False)
        Question.objects.create(title="Pilot Q", is_pilot=True)

        response = self.client.get('/api/questions/', {'is_pilot': 'true'})
        self.assertEqual(len(response.data['results']), 1)
        self.assertEqual(response.data['results'][0]['title'], 'Pilot Q')

    def test_filter_cohorts_by_sex(self):
        """Can filter cohorts by sex."""
        Cohort.objects.create(name="Males", sex=Sex.MALE)
        Cohort.objects.create(name="Females", sex=Sex.FEMALE)

        response = self.client.get('/api/cohorts/', {'sex': Sex.FEMALE})
        self.assertEqual(len(response.data['results']), 1)
        self.assertEqual(response.data['results'][0]['name'], 'Females')

    def test_filter_components_by_modality(self):
        """Can filter components by modality."""
        q = Question.objects.create(title="Q")
        run = Run.objects.create(name="R")
        run.questions.add(q)
        session = Session.objects.create(run=run, name="S")
        Component.objects.create(
            session=session, name="2P", role=ComponentRole.RECORDING, modality="2-photon"
        )
        Component.objects.create(
            session=session, name="Ephys", role=ComponentRole.RECORDING, modality="ephys"
        )

        response = self.client.get('/api/components/', {'modality': '2-photon'})
        self.assertEqual(len(response.data['results']), 1)
        self.assertEqual(response.data['results'][0]['name'], '2P')

    def test_search_questions(self):
        """Can search questions by title and description."""
        Question.objects.create(title="Neural coding", description="How neurons encode")
        Question.objects.create(title="Behavior analysis", description="Mouse movements")

        response = self.client.get('/api/questions/', {'search': 'neural'})
        self.assertEqual(len(response.data['results']), 1)
        self.assertEqual(response.data['results'][0]['title'], 'Neural coding')


# =============================================================================
# Integration Tests - Full Workflow
# =============================================================================

class ExperimentWorkflowTests(APITestCase):
    """Test complete experiment workflow from question to evidence."""

    def test_full_experiment_to_publication_workflow(self):
        """
        Test the complete workflow:
        Question -> Run -> Session -> Components
        -> Dataset -> Analysis -> Visualization -> Panel
        -> Claim with Evidence
        """
        # 1. Create research question
        q_response = self.client.post('/api/questions/', {
            'title': 'How do neurons respond to visual stimuli?',
            'hypothesis': 'Neurons in V1 respond selectively to oriented gratings',
            'status': QuestionStatus.OPERATIONAL
        })
        question_id = q_response.data['id']

        # 2. Create cohort
        cohort_response = self.client.post('/api/cohorts/', {
            'name': 'GCaMP6f mice',
            'genotype': 'Ai148(TIT2L-GC6f-ICL-tTA2)',
            'sex': Sex.MIXED,
            'total_count': 20,
            'available_count': 20
        })
        cohort_id = cohort_response.data['id']

        # 3. Create run
        run_response = self.client.post('/api/runs/', {
            'name': 'Visual Orientation Run 1',
            'questions': [question_id],
            'cohort': cohort_id,
            'status': RunStatus.PLANNED
        }, format='json')
        run_id = run_response.data['id']

        # 4. Create session
        session_response = self.client.post('/api/sessions/', {
            'run': run_id,
            'name': 'Imaging Session 1',
            'rig_identifier': 'scope-2p-01'
        }, format='json')
        session_id = session_response.data['id']

        # 5. Create components
        self.client.post('/api/components/', {
            'session': session_id,
            'name': 'Subject',
            'role': ComponentRole.SUBJECTS,
            'cohort': cohort_id,
            'requested_count': 1
        }, format='json')

        self.client.post('/api/components/', {
            'session': session_id,
            'name': '2-Photon Recording',
            'role': ComponentRole.RECORDING,
            'modality': '2-photon',
            'trial_count': 200
        }, format='json')

        # 6. Start and complete the run
        self.client.post(f'/api/runs/{run_id}/start/')
        self.client.post(f'/api/sessions/{session_id}/start/')
        self.client.post(f'/api/sessions/{session_id}/complete/')
        self.client.post(f'/api/runs/{run_id}/complete/')

        # 7. Create dataset from run
        dataset_response = self.client.post('/api/datasets/', {
            'name': 'V1 Orientation Dataset',
            'primary_run': run_id,
            'status': DatasetStatus.BUILDING
        }, format='json')
        dataset_id = dataset_response.data['id']

        # 8. Freeze dataset
        self.client.post(f'/api/datasets/{dataset_id}/freeze/')

        # 9. Create and run analysis
        analysis_response = self.client.post('/api/analyses/', {
            'name': 'Orientation Tuning Analysis',
            'dataset': dataset_id,
            'recipe_identifier': 'orientation-tuning-v2',
            'parameters': {'bin_size': 10}
        }, format='json')
        analysis_id = analysis_response.data['id']

        self.client.post(f'/api/analyses/{analysis_id}/start_execution/', {
            'job_identifier': 'slurm-12345'
        })
        self.client.post(f'/api/analyses/{analysis_id}/complete_execution/', {
            'output_path': '/data/analyses/orientation-1'
        })

        # 10. Create visualization
        viz_response = self.client.post('/api/visualizations/', {
            'name': 'Orientation Tuning Curves',
            'analysis': analysis_id,
            'asset_type': 'plot',
            'tags': ['orientation', 'tuning', 'v1']
        }, format='json')
        viz_id = viz_response.data['id']

        # 11. Create panel
        panel_response = self.client.post('/api/panels/', {
            'label': 'A',
            'figure_identifier': 'Figure 2',
            'visualization': viz_id,
            'caption': 'Orientation tuning curves for V1 neurons'
        }, format='json')
        panel_id = panel_response.data['id']

        # 12. Create claim with evidence
        claim_response = self.client.post('/api/claims/', {
            'title': 'V1 neurons show orientation selectivity',
            'statement': 'Neurons in primary visual cortex respond selectively to oriented gratings',
            'questions': [question_id],
            'status': ClaimStatus.EVIDENCE_GATHERING
        }, format='json')
        claim_id = claim_response.data['id']

        # 13. Link evidence to claim
        evidence_response = self.client.post('/api/claim-evidence/', {
            'claim': claim_id,
            'panel': panel_id,
            'evidence_type': 'supporting',
            'description': 'Figure 2A shows clear orientation tuning'
        }, format='json')
        self.assertEqual(evidence_response.status_code, status.HTTP_201_CREATED)

        # Verify the complete chain
        claim_evidence = self.client.get(f'/api/claims/{claim_id}/evidence/')
        self.assertEqual(len(claim_evidence.data), 1)

        # Verify run completed
        run = self.client.get(f'/api/runs/{run_id}/')
        self.assertEqual(run.data['status'], RunStatus.COMPLETED)
