# Lab Tracker

A domain-specific web application for orchestrating neuroscience experiments from question to claim.

## Overview

Lab Tracker is a Django-based orchestration and provenance layer that treats **Questions**, **Runs**, **Datasets**, **Analyses**, **Visualizations**, **Panels**, **Claims**, **Cohorts**, **Sessions**, and **Components** as first-class objects. It provides a canonical evidence graph that other tools (ILWS, ELN helpers, analysis pipelines) can interact with via a REST API.

### Key Features

- **Question-led experiments**: Track scientific questions and their hierarchies
- **Evidence graph**: Link claims to panels, analyses, and datasets
- **Execution tracking**: Manage runs, sessions, and components with status workflows
- **ELN integration**: Store URLs and snapshot metadata for Electronic Lab Notebook links
- **Immutable artifacts**: Frozen datasets, analyses, and panels preserve evidence integrity

## Installation

### Prerequisites

- Python 3.10+
- pip or conda

### Using pip

```bash
# Clone the repository
git clone https://github.com/SamuelBrudner/lab-tracker.git
cd lab-tracker

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install in development mode
pip install -e ".[dev]"
```

### Using conda

```bash
# Clone the repository
git clone https://github.com/SamuelBrudner/lab-tracker.git
cd lab-tracker

# Create conda environment
conda env create -f environment.yml
conda activate lab-tracker

# Install package in development mode
pip install -e ".[dev]"
```

## Quick Start

```bash
# Apply database migrations
python -m lab_tracker.manage migrate

# Create a superuser for admin access
python -m lab_tracker.manage createsuperuser

# Run the development server
python -m lab_tracker.manage runserver
```

Access the application:
- **Admin interface**: http://localhost:8000/admin/
- **API root**: http://localhost:8000/api/

## Domain Model

### Core Entities

| Entity | Description |
|--------|-------------|
| **Question** | Scientific questions that drive experimental runs (hierarchical) |
| **Claim** | Scientific statements supported/refuted by evidence (hierarchical) |
| **Cohort** | Collections of specimens (pooled or enumerated) |
| **Run** | Experimental runs covering subjects and sessions |
| **Session** | Time slices of a run on specific rigs/rooms |
| **Component** | Role-specific session elements (Subjects, Intervention, Recording) |
| **Dataset** | Aggregated, QC-aware data collections |
| **Analysis** | Transformations using specified recipes |
| **Visualization** | Visual assets (plots, videos) from analyses |
| **Panel** | Figure elements wrapping visualizations |

### Evidence Graph

Claims link to evidence through the `ClaimEvidence` model:
- Claims → Panels (direct figure evidence)
- Claims → Analyses (computational evidence)
- Claims → Datasets (data evidence)

### Status Workflows

Each entity progresses through defined statuses:

- **Questions**: `draft` → `pilot` → `operational` → `completed` → `archived`
- **Claims**: `sketched` → `developing` → `evidence_gathering` → `under_review` → `assessed` → `published`
- **Runs**: `planned` → `scheduled` → `running` → `qc` → `completed` → `archived`
- **Sessions**: `planned` → `scheduled` → `in_progress` → `complete`
- **Datasets**: `building` → `qc_pending` → `frozen` → `published`
- **Analyses**: `scratch` → `running` → `completed` → `reviewed` → `frozen` → `published`
- **Visualizations/Panels**: `draft` → `reviewed` → `frozen` → `published`

## API Reference

All entities are accessible via REST API at `/api/`:

### Endpoints

| Endpoint | Description |
|----------|-------------|
| `/api/questions/` | Scientific questions CRUD |
| `/api/claims/` | Claims and evidence graph |
| `/api/cohorts/` | Specimen cohorts |
| `/api/runs/` | Experimental runs |
| `/api/sessions/` | Session scheduling |
| `/api/components/` | Session components |
| `/api/datasets/` | Data aggregations |
| `/api/analyses/` | Analysis jobs |
| `/api/visualizations/` | Visual assets |
| `/api/panels/` | Figure panels |
| `/api/claim-evidence/` | Evidence links |

### Custom Actions

```bash
# Start a run
POST /api/runs/{id}/start/

# Complete a run
POST /api/runs/{id}/complete/

# Freeze a dataset
POST /api/datasets/{id}/freeze/

# Start analysis execution
POST /api/analyses/{id}/start_execution/
    {"job_identifier": "slurm-12345", "job_dashboard_url": "https://..."}

# Complete analysis
POST /api/analyses/{id}/complete_execution/
    {"output_path": "/data/outputs/...", "outputs_manifest": {...}}
```

### Filtering & Search

All list endpoints support:
- **Filtering**: `?status=running&cohort=<uuid>`
- **Search**: `?search=keyword`
- **Ordering**: `?ordering=-created_at`

### Board Views

Convenience endpoints for common queries:

```bash
GET /api/runs/upcoming/      # Planned and scheduled runs
GET /api/runs/active/        # Currently running
GET /api/datasets/ready/     # Frozen or published datasets
GET /api/cohorts/available/  # Cohorts with available specimens
GET /api/claims/by_status/   # Claim counts by status
```

## Usage Examples

### Python Client

```python
import requests

BASE_URL = "http://localhost:8000/api"

# Create a question
question = requests.post(f"{BASE_URL}/questions/", json={
    "title": "Does optogenetic activation of PFC neurons affect decision-making?",
    "hypothesis": "Activation will increase exploration behavior",
    "status": "pilot"
}).json()

# Create a run linked to the question
run = requests.post(f"{BASE_URL}/runs/", json={
    "name": "PFC-opto-pilot-001",
    "question_ids": [question["id"]],
    "data_sink": "/data/experiments/pfc-opto/pilot-001"
}).json()

# Start the run
requests.post(f"{BASE_URL}/runs/{run['id']}/start/")

# Create a session
session = requests.post(f"{BASE_URL}/sessions/", json={
    "run": run["id"],
    "name": "Day 1 - Habituation",
    "rig_identifier": "behavior-rig-01"
}).json()

# Add a recording component
component = requests.post(f"{BASE_URL}/components/", json={
    "session": session["id"],
    "name": "2P Calcium Imaging",
    "role": "recording",
    "modality": "2-photon",
    "metadata": {
        "laser_power": 15,
        "wavelength": 920,
        "frame_rate": 30
    }
}).json()
```

### Django Shell

```python
from core.models import Question, Run, Claim, ClaimEvidence

# Create a question hierarchy
main_q = Question.objects.create(
    title="Neural basis of decision-making",
    status="operational"
)
sub_q = Question.objects.create(
    title="Role of PFC in exploration",
    parent=main_q,
    status="pilot"
)

# Get all descendants
descendants = main_q.children.all()

# Create a claim with evidence
claim = Claim.objects.create(
    title="PFC activation increases exploration",
    statement="Optogenetic activation of PFC neurons increases...",
    status="evidence_gathering"
)
claim.questions.add(sub_q)

# Link evidence
ClaimEvidence.objects.create(
    claim=claim,
    panel=some_panel,
    evidence_type="supporting",
    description="Figure 2A shows increased exploration rate"
)
```

## Development

### Running Tests

```bash
pytest
```

### Code Quality

```bash
# Linting
ruff check src/

# Type checking
mypy src/
```

### Database Migrations

```bash
# Create migrations after model changes
python -m lab_tracker.manage makemigrations

# Apply migrations
python -m lab_tracker.manage migrate
```

## Project Structure

```
lab-tracker/
├── src/
│   └── lab_tracker/
│       ├── __init__.py
│       ├── manage.py
│       ├── settings.py
│       ├── urls.py
│       ├── wsgi.py
│       ├── asgi.py
│       └── core/
│           ├── __init__.py
│           ├── models.py      # Domain models
│           ├── serializers.py # DRF serializers
│           ├── views.py       # API viewsets
│           ├── admin.py       # Admin configuration
│           ├── urls.py        # API routing
│           └── migrations/
├── tests/
├── pyproject.toml
├── environment.yml
└── README.md
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `DJANGO_SECRET_KEY` | Django secret key | Development key |
| `DJANGO_DEBUG` | Debug mode | `True` |
| `DJANGO_ALLOWED_HOSTS` | Allowed hosts | `[]` |
| `DATABASE_URL` | Database connection | SQLite |

## License

MIT License - see LICENSE file for details.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Run tests and linting
5. Submit a pull request

## Authors

- Lab Tracker Team
