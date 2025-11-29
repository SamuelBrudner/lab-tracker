"""Tests for Lab Tracker domain models."""

import pytest
from django.core.exceptions import ValidationError

from lab_tracker.core.models import (
    Question,
    Claim,
    Cohort,
    Run,
    Session,
    Component,
    Dataset,
    Analysis,
    Visualization,
    Panel,
    ClaimEvidence,
    QuestionStatus,
    CohortType,
    ComponentRole,
)


@pytest.mark.django_db
class TestQuestion:
    """Tests for the Question model."""

    def test_create_question(self):
        """Test creating a basic question."""
        question = Question.objects.create(
            title="Test Question",
            hypothesis="Test hypothesis",
            status=QuestionStatus.DRAFT,
        )
        assert question.title == "Test Question"
        assert question.status == QuestionStatus.DRAFT
        assert question.is_pilot is False

    def test_question_hierarchy(self):
        """Test question parent-child relationships."""
        parent = Question.objects.create(title="Parent Question")
        child = Question.objects.create(title="Child Question", parent=parent)

        assert child.parent == parent
        assert parent.children.count() == 1
        assert parent.children.first() == child

    def test_get_ancestors(self):
        """Test retrieving question ancestors."""
        grandparent = Question.objects.create(title="Grandparent")
        parent = Question.objects.create(title="Parent", parent=grandparent)
        child = Question.objects.create(title="Child", parent=parent)

        ancestors = child.get_ancestors()
        assert len(ancestors) == 2
        assert ancestors[0] == grandparent
        assert ancestors[1] == parent


@pytest.mark.django_db
class TestCohort:
    """Tests for the Cohort model."""

    def test_create_cohort(self):
        """Test creating a cohort."""
        cohort = Cohort.objects.create(
            name="Test Cohort",
            cohort_type=CohortType.POOLED,
            genotype="C57BL/6J",
            total_count=20,
            available_count=15,
        )
        assert cohort.name == "Test Cohort"
        assert cohort.available_count == 15

    def test_cohort_validation_available_exceeds_total(self):
        """Test that available count cannot exceed total count."""
        cohort = Cohort(
            name="Invalid Cohort",
            total_count=10,
            available_count=15,
        )
        with pytest.raises(ValidationError):
            cohort.clean()

    def test_cohort_validation_age_range(self):
        """Test that min age cannot exceed max age."""
        cohort = Cohort(
            name="Invalid Cohort",
            min_age_days=100,
            max_age_days=50,
        )
        with pytest.raises(ValidationError):
            cohort.clean()


@pytest.mark.django_db
class TestRun:
    """Tests for the Run model."""

    def test_create_run_with_question(self):
        """Test creating a run linked to a question."""
        question = Question.objects.create(title="Test Question")
        run = Run.objects.create(name="Test Run")
        run.questions.add(question)

        assert run.questions.count() == 1
        assert question.runs.first() == run


@pytest.mark.django_db
class TestClaimEvidence:
    """Tests for the ClaimEvidence model."""

    def test_evidence_requires_exactly_one_link(self):
        """Test that evidence must link to exactly one target."""
        claim = Claim.objects.create(
            title="Test Claim",
            statement="Test statement",
        )

        # No link should fail
        evidence = ClaimEvidence(claim=claim)
        with pytest.raises(ValidationError):
            evidence.clean()

    def test_evidence_with_panel(self):
        """Test creating evidence linked to a panel."""
        claim = Claim.objects.create(
            title="Test Claim",
            statement="Test statement",
        )
        panel = Panel.objects.create(label="A")

        evidence = ClaimEvidence.objects.create(
            claim=claim,
            panel=panel,
            evidence_type="supporting",
        )
        assert evidence.panel == panel
