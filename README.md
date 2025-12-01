# Lab Tracker

A Django REST API for tracking neuroscience experiments from scientific question to published claim.

## Goals

Lab Tracker provides a structured way to:

1. **Document scientific intent** - Define research questions and the claims you aim to support
2. **Track experiment execution** - Record what was planned, what was actually done, and with which subjects
3. **Manage evidence assets** - Organize datasets, analyses, visualizations, and figure panels
4. **Link evidence to claims** - Build an explicit graph connecting claims to their supporting evidence
5. **Integrate with ELNs** - Maintain deep links to Electronic Lab Notebook entries for full context

## Core Concepts

### Scientific Intent Layer

- **Question**: A scientific question or sub-question. Questions form a hierarchy and progress through statuses: Draft → Pilot → Operational → Completed.
- **Claim**: A scientific assertion you intend to support with evidence. Claims link to questions and progress through: Sketched → Developing → Evidence Gathering → Under Review → Published.

### Execution Layer

- **Cohort**: A group of subjects (animals, samples, etc.). Can be pooled (interchangeable) or enumerated (individually tracked). Tracks genotype, sex, age range, and availability.
- **Run**: A planned or executed experimental run addressing one or more questions. Tracks scheduling, execution times, and status.
- **Session**: A time slice within a run (e.g., a single recording day). Sessions can be scheduled relative to each other.
- **Component**: A specific element within a session with a defined role:
  - `subjects` - Which cohort members are involved
  - `intervention` - What manipulation is applied (optogenetics, drug, behavior task)
  - `recording` - What data is being collected (2-photon, electrophysiology, video)

### Evidence Layer

- **Dataset**: An aggregated collection of data from one or more runs, frozen for reproducible analysis.
- **Analysis**: A transformation of a dataset using a specified recipe (code + parameters), producing outputs.
- **Visualization**: A visual asset (plot, video, interactive) produced by an analysis.
- **Panel**: A figure element (e.g., "Figure 2A") wrapping a visualization for publication.
- **ClaimEvidence**: Links a claim to its supporting evidence (panels or analyses), with evidence type (supporting, refuting, contextual).

## API Endpoints

All endpoints are available at `/api/` with full CRUD operations:

| Endpoint | Description |
|----------|-------------|
| `/api/questions/` | Scientific questions hierarchy |
| `/api/claims/` | Scientific claims and their evidence |
| `/api/cohorts/` | Subject cohorts and availability |
| `/api/runs/` | Experimental runs |
| `/api/sessions/` | Sessions within runs |
| `/api/components/` | Components within sessions |
| `/api/datasets/` | Aggregated datasets |
| `/api/analyses/` | Analysis runs and outputs |
| `/api/visualizations/` | Visual assets |
| `/api/panels/` | Figure panels |
| `/api/claim-evidence/` | Claim-to-evidence links |

Each endpoint supports filtering, searching, and ordering. See the browsable API for full documentation.

## Status Workflows

Entities progress through defined status workflows:

```
Question:  Draft → Pilot → Operational → Paused/Completed → Archived
Claim:     Sketched → Developing → Evidence Gathering → Under Review → Assessed → Published
Run:       Planned → Scheduled → Running → QC → Completed/Failed → Archived
Dataset:   Building → QC Pending → Frozen → Published/Deprecated
Analysis:  Scratch → Running → Completed → Reviewed → Frozen → Published
Panel:     Draft → Reviewed → Frozen → Published
```

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run migrations
python manage.py migrate

# Start development server
python manage.py runserver
```

The API will be available at `http://localhost:8000/api/`.

## Running Tests

```bash
python manage.py test core
```

## Requirements

- Python 3.8+
- Django 4.2
- Django REST Framework 3.14
- django-filter 23.0
