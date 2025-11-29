"""
URL routing for the Neuroscience Experiment Orchestration App API.

Uses Django REST Framework routers to automatically generate
URL patterns for all viewsets.
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    QuestionViewSet, ClaimViewSet, CohortViewSet,
    RunViewSet, SessionViewSet, ComponentViewSet,
    DatasetViewSet, AnalysisViewSet, VisualizationViewSet,
    PanelViewSet, ClaimEvidenceViewSet
)

# Create a router and register our viewsets
router = DefaultRouter()
router.register(r'questions', QuestionViewSet)
router.register(r'claims', ClaimViewSet)
router.register(r'cohorts', CohortViewSet)
router.register(r'runs', RunViewSet)
router.register(r'sessions', SessionViewSet)
router.register(r'components', ComponentViewSet)
router.register(r'datasets', DatasetViewSet)
router.register(r'analyses', AnalysisViewSet)
router.register(r'visualizations', VisualizationViewSet)
router.register(r'panels', PanelViewSet)
router.register(r'claim-evidence', ClaimEvidenceViewSet)

# The API URLs are determined automatically by the router
urlpatterns = [
    path('', include(router.urls)),
]
