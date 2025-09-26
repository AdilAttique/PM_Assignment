# PM Hub (PMBOK + PRINCE2 + ISO 21500/21502)

A Django + SQLite prototype that ingests your local standards (PDF/EPUB), makes every page searchable, supports bookmarks and navigation, compares topics across standards with deep linking, visualizes insights, and generates tailored processes with evidence links.

## Stack
- Django 5 + SQLite (FTS5)
- Tailwind (CDN) + Chart.js
- Parsing: pypdf, EbookLib, BeautifulSoup

## Quick Start
```bash
# In project root
python -m venv .venv
.\.venv\Scripts\pip install django pypdf ebooklib beautifulsoup4 lxml
.\.venv\Scripts\python manage.py migrate
.\.venv\Scripts\python manage.py ingest_standards --base_dir . --rebuild
.\.venv\Scripts\python manage.py runserver 0.0.0.0:8000
```

Open http://localhost:8000

## Features
- Library: First page open, prev/next, bookmarks
- Search: FTS5 across all pages with highlighting and pagination
- Comparison: Topic search side-by-side, similarities, unique points, deep links
- Insights: Counts and lifecycle keyword coverage bar chart
- Tailoring: Project-type keywords and lifecycle phase evidence (deep links)

## Notes
- FTS5 virtual table defined in migration `0002_page_fts.py`
- Ingestion splits PDFs per physical page; EPUB concatenated HTML docs into chunks
- Models: `Standard`, `Page`, `Bookmark`

## Folder expectations
Place the provided files in the project root:
- `A Guide to the Project Management Body of Knowledge (PMBOK® Guide) – Seventh Edition and The Standard for Project Management (ENGLISH).epub`
- `Managing Successful Projects with PRINCE2® -- Andy Murray -- 7, 2023 -- PeopleCert International Limited.pdf`
- `ISO 21500-2021_ Project, programme and portfolio management - Context and concepts.pdf`
- `ISO 21502-2020_ Project, programme and portfolio management - Guidance on project management.pdf`

## Tailoring caveat
This is a text-based heuristic using keyword matches. For academic use, validate quotations with the linked pages.
