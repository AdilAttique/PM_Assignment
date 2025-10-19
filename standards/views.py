from django.db import connection, models
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from .models import Standard, Page, Bookmark
from rapidfuzz import fuzz
import re
import html as html_lib
from django.http import FileResponse
import mimetypes
import os
from django.conf import settings
import sqlite3


def ensure_session(request: HttpRequest) -> None:
    if not request.session.session_key:
        request.session.save()


@require_GET
def library(request: HttpRequest) -> HttpResponse:
    ensure_session(request)
    standards = Standard.objects.all().order_by("title")
    total_pages = Page.objects.count()
    return render(request, "standards/library.html", {"standards": standards, "total_pages": total_pages})


@require_GET
def page_view(request: HttpRequest, slug: str, page_index: int) -> HttpResponse:
    ensure_session(request)
    standard = get_object_or_404(Standard, slug=slug)
    page = get_object_or_404(Page, standard=standard, page_index=page_index)
    has_bookmark = Bookmark.objects.filter(session_key=request.session.session_key, page=page).exists()
    prev_index = page_index - 1 if page_index > 0 else None
    next_index = page_index + 1 if Page.objects.filter(standard=standard, page_index=page_index + 1).exists() else None
    # Convert raw text to readable HTML paragraphs and lists
    def _text_to_html(text: str) -> str:
        if not text:
            return ""
        t = text
        # Fix word breaks like "man-
        # agement"
        t = re.sub(r"([A-Za-z])\-\s*\n([A-Za-z])", r"\1\2", t)
        # Split paragraphs on blank lines
        paragraphs = re.split(r"\n\s*\n", t)
        html_parts: list[str] = []
        for para in paragraphs:
            lines = [ln.strip() for ln in para.split("\n") if ln.strip()]
            if not lines:
                continue
            # Detect bullet-heavy paragraph
            bullet_mask = [bool(re.match(r"^(•|-|\*)\s+|^\d+[\.)]\s+", ln)) for ln in lines]
            if sum(1 for b in bullet_mask if b) >= max(2, int(0.6 * len(lines))):
                html_parts.append('<ul class="list-disc pl-6 space-y-1">')
                for ln in lines:
                    ln_clean = re.sub(r"^(•|-|\*|\d+[\.)])\s+", "", ln)
                    html_parts.append(f"<li>{html_lib.escape(ln_clean)}</li>")
                html_parts.append("</ul>")
            else:
                # Join soft line breaks inside a paragraph
                joined = " ".join(lines)
                html_parts.append(f"<p class=\"mb-3\">{html_lib.escape(joined)}</p>")
        return "".join(html_parts)

    html = page.content_html or _text_to_html(page.content)
    return render(
        request,
        "standards/page.html",
        {
            "standard": standard,
            "page": page,
            "has_bookmark": has_bookmark,
            "prev_index": prev_index,
            "next_index": next_index,
            "html": html,
        },
    )


@require_GET
def pdf_file(request: HttpRequest, slug: str) -> HttpResponse:
    standard = get_object_or_404(Standard, slug=slug)
    if standard.source_type != "pdf":
        return HttpResponse(status=404)
    ctype, _ = mimetypes.guess_type(standard.file_path)
    resp = FileResponse(open(standard.file_path, "rb"), content_type=ctype or "application/pdf")
    resp["Content-Disposition"] = f"inline; filename=\"{os.path.basename(standard.file_path)}\""
    return resp


@require_POST
def toggle_bookmark(request: HttpRequest, page_id: int) -> HttpResponse:
    ensure_session(request)
    page = get_object_or_404(Page, pk=page_id)
    bm, created = Bookmark.objects.get_or_create(session_key=request.session.session_key, page=page)
    if not created:
        bm.delete()
    next_url = request.POST.get("next") or reverse("standards:page", args=[page.standard.slug, page.page_index])
    return redirect(next_url)


@require_GET
def bookmarks(request: HttpRequest) -> HttpResponse:
    ensure_session(request)
    items = (
        Bookmark.objects.select_related("page", "page__standard")
        .filter(session_key=request.session.session_key)
        .all()
    )
    return render(request, "standards/bookmarks.html", {"items": items})


@require_GET
def search(request: HttpRequest) -> HttpResponse:
    ensure_session(request)
    q = (request.GET.get("q") or "").strip()
    results = []
    if q:
        db_path = settings.DATABASES["default"]["NAME"]
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT p.id, p.standard_id, p.page_index,
                       snippet(page_fts, 0, '<mark>', '</mark>', ' … ', 12) AS highlight
                FROM page_fts
                JOIN standards_page p ON p.id = page_fts.rowid
                WHERE page_fts MATCH ?
                ORDER BY bm25(page_fts)
                LIMIT 300
                """,
                (q,),
            )
            rows = cur.fetchall()
        page_ids = [r[0] for r in rows]
        pages = {p.id: p for p in Page.objects.select_related("standard").filter(id__in=page_ids)}
        for pid, sid, pidx, highlight in rows:
            p = pages.get(pid)
            if p:
                results.append({
                    "page": p,
                    "highlight": highlight,
                })
    paginator = Paginator(results, 20)
    page_num = request.GET.get("page") or 1
    page_obj = paginator.get_page(page_num)
    return render(request, "standards/search.html", {"q": q, "results": page_obj.object_list, "page_obj": page_obj})


@require_GET
def compare(request: HttpRequest) -> HttpResponse:
    ensure_session(request)
    topic = (request.GET.get("topic") or "").strip()
    standards = list(Standard.objects.all().order_by("title"))
    hits = {s.slug: [] for s in standards}
    
    if topic:
        db_path = settings.DATABASES["default"]["NAME"]
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT p.id, s.slug, p.page_index,
                       substr(p.content, max(instr(lower(p.content), lower(?)) - 80, 1), 240)
                FROM standards_page p
                JOIN standards_standard s ON p.standard_id = s.id
                WHERE lower(p.content) LIKE '%' || lower(?) || '%'
                LIMIT 400
                """,
                (topic, topic),
            )
            for pid, sslug, pidx, snippet in cur.fetchall():
                hits[sslug].append({
                    "page_id": pid,
                    "page_index": pidx,
                    "snippet": snippet,
                })

    # Enhanced similarities and differences analysis
    similarities = []
    differences = []
    unique_points = {s.slug: [] for s in standards}
    
    if topic:
        # Sample pages for comparison (first 30 per standard)
        sample_pages = {k: v[:30] for k, v in hits.items()}
        
        # Find similarities (high overlap in content)
        for i, standard_a in enumerate(standards):
            for standard_b in standards[i+1:]:
                for page_a in sample_pages[standard_a.slug]:
                    for page_b in sample_pages[standard_b.slug]:
                        score = fuzz.token_set_ratio(page_a["snippet"], page_b["snippet"])
                        if score >= 75:  # High similarity threshold
                            similarities.append({
                                "standard_a": standard_a,
                                "standard_b": standard_b,
                                "page_a": page_a["page_index"],
                                "page_b": page_b["page_index"],
                                "score": score,
                                "snippet_a": page_a["snippet"],
                                "snippet_b": page_b["snippet"],
                                "topic": topic
                            })
        
        # Find differences (methodology-specific content)
        methodology_keywords = {
            "PMBOK": ["knowledge areas", "process groups", "deliverables", "stakeholder register", "work breakdown structure", "project charter", "scope statement"],
            "PRINCE2": ["principles", "themes", "processes", "product-based planning", "stage boundaries", "project brief", "business case"],
            "ISO 21500": ["process groups", "subject groups", "competences", "maturity", "governance", "project objectives", "stakeholder analysis"],
            "ISO 21502": ["life cycle", "processes", "competences", "governance", "maturity", "project management system", "organizational capability"]
        }
        
        for standard in standards:
            for keyword_group, keywords in methodology_keywords.items():
                if keyword_group in standard.title:
                    for keyword in keywords:
                        # Find pages containing this methodology-specific keyword
                        for page in sample_pages[standard.slug]:
                            if keyword.lower() in page["snippet"].lower():
                                differences.append({
                                    "standard": standard,
                                    "keyword": keyword,
                                    "page_index": page["page_index"],
                                    "snippet": page["snippet"],
                                    "category": f"{keyword_group} Specific",
                                    "topic": topic
                                })
        
        # Find unique points (low overlap with others)
        for standard in standards:
            others = [s for s in standards if s.slug != standard.slug]
            for page in sample_pages[standard.slug][:20]:  # Check first 20 pages
                max_score = 0
                for other_standard in others:
                    for other_page in sample_pages[other_standard.slug][:20]:
                        score = fuzz.token_set_ratio(page["snippet"], other_page["snippet"])
                        max_score = max(max_score, score)
                
                if max_score < 50:  # Low similarity = unique content
                    unique_points[standard.slug].append({
                        "page_index": page["page_index"],
                        "snippet": page["snippet"],
                        "uniqueness_score": 100 - max_score,
                        "topic": topic
                    })

    # Build template-friendly lists
    hits_list = [{"standard": s, "items": hits.get(s.slug, [])} for s in standards]
    unique_list = [{"standard": s, "items": unique_points.get(s.slug, [])} for s in standards]

    return render(
        request,
        "standards/compare.html",
        {
            "topic": topic,
            "standards": standards,
            "hits_list": hits_list,
            "similarities": similarities[:15],  # Limit to top 15
            "differences": differences[:20],   # Limit to top 20
            "unique_list": unique_list,
        },
    )


@require_GET
def insights(request: HttpRequest) -> HttpResponse:
    ensure_session(request)
    total_pages = Page.objects.count()
    standards = list(Standard.objects.all().order_by("title"))
    counts_by_standard = (
        Page.objects.values("standard__title").order_by("standard__title").annotate(count=models.Count("id"))
    )
    
    # Enhanced lifecycle terms for better analysis
    lifecycle_terms = ["initiation", "planning", "execution", "monitoring", "closing", "governance", "risk", "stakeholder", "quality", "communication", "change", "procurement"]
    
    # Get overlap data
    overlaps = []
    db_path = settings.DATABASES["default"]["NAME"]
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        for term in lifecycle_terms:
            cur.execute(
                """
                SELECT s.title, COUNT(p.id)
                FROM standards_page p
                JOIN standards_standard s ON s.id = p.standard_id
                WHERE lower(p.content) LIKE '%' || lower(?) || '%'
                GROUP BY s.title
                """,
                (term,),
            )
            data = cur.fetchall()
            overlaps.append({
                "term": term,
                "data": {title: count for title, count in data},
            })
    
    # Calculate similarities, differences, and unique points
    similarities = []
    differences = []
    unique_points = []
    
    # Sample pages from each standard for comparison
    sample_pages = {}
    for standard in standards:
        pages = list(Page.objects.filter(standard=standard)[:50])  # Sample first 50 pages
        sample_pages[standard.slug] = [
            {
                "page_index": page.page_index,
                "content": page.content[:500],  # First 500 chars
                "standard": standard
            }
            for page in pages
        ]
    
    # Find similarities (high overlap in content)
    for i, standard_a in enumerate(standards):
        for standard_b in standards[i+1:]:
            for page_a in sample_pages[standard_a.slug][:10]:  # Compare first 10 pages
                for page_b in sample_pages[standard_b.slug][:10]:
                    score = fuzz.token_set_ratio(page_a["content"], page_b["content"])
                    if score >= 70:  # High similarity threshold
                        similarities.append({
                            "standard_a": standard_a,
                            "standard_b": standard_b,
                            "page_a": page_a["page_index"],
                            "page_b": page_b["page_index"],
                            "score": score,
                            "topic": "Content Overlap"
                        })
    
    # Find unique points (low overlap with others)
    for standard in standards:
        others = [s for s in standards if s.slug != standard.slug]
        for page in sample_pages[standard.slug][:20]:  # Check first 20 pages
            max_score = 0
            for other_standard in others:
                for other_page in sample_pages[other_standard.slug][:20]:
                    score = fuzz.token_set_ratio(page["content"], other_page["content"])
                    max_score = max(max_score, score)
            
            if max_score < 40:  # Low similarity = unique content
                unique_points.append({
                    "standard": standard,
                    "page_index": page["page_index"],
                    "uniqueness_score": 100 - max_score,
                    "content_preview": page["content"][:200] + "..."
                })
    
    # Find differences (methodology-specific terms)
    methodology_terms = {
        "PMBOK": ["knowledge areas", "process groups", "deliverables", "stakeholder register", "work breakdown structure"],
        "PRINCE2": ["principles", "themes", "processes", "product-based planning", "stage boundaries"],
        "ISO 21500": ["process groups", "subject groups", "competences", "maturity", "governance"],
        "ISO 21502": ["life cycle", "processes", "competences", "governance", "maturity"]
    }
    
    for standard in standards:
        for term_group, terms in methodology_terms.items():
            if term_group in standard.title:
                for term in terms:
                    differences.append({
                        "standard": standard,
                        "term": term,
                        "category": "Methodology-Specific",
                        "description": f"Unique to {term_group} methodology"
                    })

    return render(
        request,
        "standards/insights.html",
        {
            "total_pages": total_pages,
            "standards": standards,
            "counts_by_standard": list(counts_by_standard),
            "overlaps": overlaps,
            "similarities": similarities[:10],  # Limit to top 10
            "differences": differences[:15],   # Limit to top 15
            "unique_points": unique_points[:20], # Limit to top 20
        },
    )


@require_GET
def tailor(request: HttpRequest) -> HttpResponse:
    ensure_session(request)
    project_type = (request.GET.get("type") or "").strip()
    recommendations = []
    
    # Define the three specific project scenarios from assignment document
    scenarios = {
        "custom_software": {
            "name": "Custom Software Development Project",
            "context": "Well-defined requirements, <6 months, <7 team members",
            "focus": "Lightweight process optimized for speed and flexibility",
            "keywords": ["agile", "iteration", "sprint", "software", "development", "scrum", "kanban", "continuous", "integration", "deployment"],
            "phases": [
                ("Project Initiation", ["initiation", "charter", "stakeholder", "requirements", "team", "risk"]),
                ("Planning & Design", ["planning", "design", "architecture", "backlog", "sprint", "quality"]),
                ("Development & Testing", ["development", "testing", "integration", "sprint", "demonstration", "monitoring"]),
                ("Deployment & Closure", ["deployment", "closure", "training", "documentation", "handover", "lessons"])
            ]
        },
        "innovative_product": {
            "name": "Innovative Product Development Project", 
            "context": "R&D-heavy, uncertain outcomes, ~1 year duration",
            "focus": "Hybrid adaptive process balancing innovation, iteration, and stakeholder management",
            "keywords": ["enterprise", "system", "implementation", "business case", "stakeholder", "governance", "compliance", "integration"],
            "phases": [
                ("Pre-Project & Initiation", ["pre-project", "initiation", "business case", "stakeholder", "governance", "compliance"]),
                ("Planning & Design", ["planning", "design", "requirements", "architecture", "migration", "quality"]),
                ("Implementation", ["implementation", "configuration", "integration", "testing", "training", "compliance"]),
                ("Deployment & Transition", ["deployment", "transition", "production", "monitoring", "benefits", "closure"])
            ]
        },
        "government_project": {
            "name": "Large Government Project",
            "context": "Civil, electrical, and IT components, 2-year duration", 
            "focus": "Comprehensive process covering governance, compliance, procurement, risk management, and reporting",
            "keywords": ["infrastructure", "upgrade", "procurement", "contract", "regulation", "audit", "reporting", "stakeholder", "risk", "management"],
            "phases": [
                ("Project Initiation", ["initiation", "assessment", "charter", "stakeholder", "requirements", "procurement"]),
                ("Detailed Planning & Design", ["planning", "design", "wbs", "schedule", "quality", "safety", "vendor"]),
                ("Procurement & Preparation", ["procurement", "preparation", "equipment", "site", "training", "change"]),
                ("Implementation & Testing", ["implementation", "testing", "installation", "configuration", "performance", "security"]),
                ("Deployment & Closure", ["deployment", "closure", "production", "monitoring", "handover", "lessons"])
            ]
        }
    }
    
    tailored = []
    process_design = None
    
    if project_type and project_type in scenarios:
        scenario = scenarios[project_type]
        keywords = scenario["keywords"]
        query = " OR ".join(keywords)
        
        # Get general recommendations
        db_path = settings.DATABASES["default"]["NAME"]
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT p.id, s.slug, s.title, p.page_index,
                       substr(p.content, 1, 220)
                FROM page_fts
                JOIN standards_page p ON p.id = page_fts.rowid
                JOIN standards_standard s ON s.id = p.standard_id
                WHERE page_fts MATCH ?
                LIMIT 100
                """,
                (query,),
            )
            for pid, sslug, stitle, pidx, snippet in cur.fetchall():
                recommendations.append({
                    "page_id": pid,
                    "standard_slug": sslug,
                    "standard_title": stitle,
                    "page_index": pidx,
                    "snippet": snippet,
                })
        
        # Build phase-oriented tailoring with evidence
        for phase_name, phase_terms in scenario["phases"]:
            evidence = []
            db_path = settings.DATABASES["default"]["NAME"]
            with sqlite3.connect(db_path) as conn:
                cur = conn.cursor()
                # Pad to exactly 3 params for the OR LIKEs
                terms3 = (phase_terms + [phase_terms[0]] * 3)[:3]
                cur.execute(
                    """
                    SELECT s.slug, s.title, p.page_index, substr(p.content, 1, 200)
                    FROM standards_page p
                    JOIN standards_standard s ON s.id = p.standard_id
                    WHERE (
                        lower(p.content) LIKE '%' || lower(?) || '%'
                        OR lower(p.content) LIKE '%' || lower(?) || '%'
                        OR lower(p.content) LIKE '%' || lower(?) || '%'
                    )
                    LIMIT 30
                    """,
                    tuple(terms3),
                )
                for sslug, stitle, pidx, snippet in cur.fetchall():
                    evidence.append({
                        "standard_slug": sslug,
                        "standard_title": stitle,
                        "page_index": pidx,
                        "snippet": snippet,
                    })
            tailored.append({"phase": phase_name, "evidence": evidence})
        
        # Generate comprehensive process design
        process_design = generate_process_design(scenario, project_type)
    
    return render(
        request,
        "standards/tailor.html",
        {
            "project_type": project_type, 
            "recommendations": recommendations, 
            "tailored": tailored,
            "scenarios": scenarios,
            "process_design": process_design
        },
    )


def generate_process_design(scenario, project_type):
    """Generate comprehensive process design for the given scenario"""
    
    # Define process components based on scenario from assignment document
    if project_type == "custom_software":
        return {
            "phases": [
                {
                    "name": "Project Initiation",
                    "duration": "1-2 weeks",
                    "activities": [
                        "Identify and engage stakeholders",
                        "Validate and document all requirements",
                        "Form the project team and define roles",
                        "Create the project charter",
                        "Conduct initial risk analysis and plan mitigations"
                    ],
                    "roles": ["Project Manager", "Product Owner", "Stakeholders", "Development Team Lead"],
                    "artifacts": ["Project Charter", "Stakeholder Register", "Requirements Document", "Risk Register", "Team Structure Document"],
                    "decision_gates": ["Gate 1 – Approval of project and confirmation of resource allocation"],
                    "standards_references": {
                        "PMBOK": "Stakeholder, Team, Development Approach domains",
                        "PRINCE2": "Initiation process with lightweight business case",
                        "ISO 21500": "Initiating process group with stakeholder analysis"
                    }
                },
                {
                    "name": "Planning & Design",
                    "duration": "2-3 weeks",
                    "activities": [
                        "Conduct sprint planning and create backlog",
                        "Develop the technical architecture design",
                        "Map user stories and define acceptance criteria",
                        "Plan for quality assurance and testing",
                        "Establish communication and reporting plan"
                    ],
                    "roles": ["Product Owner", "Scrum Master", "Technical Lead", "QA Lead"],
                    "artifacts": ["Product Backlog", "Sprint Plan", "Technical Architecture Document", "User Stories with Acceptance Criteria", "Quality Assurance Plan"],
                    "decision_gates": ["Gate 2 – Approval of design and readiness for development"],
                    "standards_references": {
                        "PMBOK": "Planning domain with iterative approach",
                        "PRINCE2": "Planning process with agile plans",
                        "ISO 21500": "Planning process group with quality management"
                    }
                },
                {
                    "name": "Development & Testing",
                    "duration": "12-16 weeks",
                    "activities": [
                        "Perform iterative development in 2-week sprints",
                        "Carry out continuous integration and testing",
                        "Conduct regular stakeholder demonstrations",
                        "Monitor risks and resolve issues",
                        "Manage changes and version control"
                    ],
                    "roles": ["Development Team", "Scrum Master", "Product Owner", "QA Team"],
                    "artifacts": ["Working Software Increments", "Test and Quality Reports", "Sprint Reviews", "Updated Risk Register", "Change Requests"],
                    "decision_gates": ["Gates 3a–3f – End-of-sprint evaluations for continuation or adjustment"],
                    "standards_references": {
                        "PMBOK": "Project Work, Delivery, Measurement domains",
                        "PRINCE2": "Delivery via sprints with continuous testing",
                        "ISO 21500": "Executing and Monitoring process groups"
                    }
                },
                {
                    "name": "Deployment & Closure",
                    "duration": "1-2 weeks",
                    "activities": [
                        "Conduct user acceptance testing (UAT)",
                        "Deploy the system to production",
                        "Provide user training and documentation",
                        "Execute project closure and lessons learned activities"
                    ],
                    "roles": ["Project Manager", "Development Team", "Users", "Support Team"],
                    "artifacts": ["Deployed Software System", "User Documentation", "Project Closure Report", "Lessons Learned Document", "Support Transition Plan"],
                    "decision_gates": ["Gate 4 – Final approval for project completion and handover"],
                    "standards_references": {
                        "PMBOK": "Delivery domain with value delivery focus",
                        "PRINCE2": "Closure process with lessons learned",
                        "ISO 21500": "Closing process group with benefits realization"
                    }
                }
            ],
            "tailoring_rationale": "Iterative approach for moderate complexity with experienced team. Incremental delivery via working software with simplified documentation and frequent checkpoints.",
            "governance_model": "Self-organizing teams with minimal overhead, regular sprint reviews for stakeholder engagement."
        }
    
    elif project_type == "innovative_product":
        return {
            "phases": [
                {
                    "name": "Pre-Project & Initiation",
                    "duration": "2-3 months",
                    "activities": [
                        "Develop and approve business case",
                        "Conduct comprehensive stakeholder analysis",
                        "Prepare project charter and mandate",
                        "Establish governance and oversight structures",
                        "Review initial risks and compliance factors",
                        "Select vendors and finalize contracts"
                    ],
                    "roles": ["Project Director", "Business Analyst", "Compliance Officer", "Stakeholder Manager", "Procurement Manager"],
                    "artifacts": ["Approved Business Case", "Project Charter and Mandate", "Governance Structure Document", "Stakeholder Register and Analysis", "Initial Risk Register", "Compliance Framework", "Vendor Contracts"],
                    "decision_gates": ["Gate 1 – Authorization and funding approval"],
                    "standards_references": {
                        "PMBOK": "Stakeholder, Planning, Uncertainty domains",
                        "PRINCE2": "Business justification, staged management principles",
                        "ISO 21500": "All five process groups, formally documented"
                    }
                },
                {
                    "name": "Planning & Design",
                    "duration": "4-6 months",
                    "activities": [
                        "Perform detailed requirements analysis",
                        "Design enterprise and integration architecture",
                        "Plan data migration and transformation",
                        "Prepare master project schedule and quality plans",
                        "Plan for training and change management"
                    ],
                    "roles": ["Project Manager", "Architecture Lead", "Data Migration Specialist", "Quality Manager", "Change Manager"],
                    "artifacts": ["Detailed Requirements Specification", "Enterprise Architecture Design", "Integration Architecture", "Data Migration Plan", "Master Project Schedule", "Quality Management Plan", "Change Management Strategy", "Training Plan"],
                    "decision_gates": ["Gate 2 – Design approval and implementation authorization"],
                    "standards_references": {
                        "PMBOK": "Predictive approach with adaptive elements",
                        "PRINCE2": "Full implementation emphasizing Business Case, Organization, Quality",
                        "ISO 21500": "All ten knowledge areas, focusing on Risk, Quality, Stakeholder"
                    }
                },
                {
                    "name": "Implementation",
                    "duration": "8-12 months",
                    "activities": [
                        "Configure and develop system components",
                        "Integrate systems and perform testing",
                        "Execute data migration and validation",
                        "Conduct user acceptance and performance tests",
                        "Perform security validation and ensure compliance",
                        "Carry out change management and user training"
                    ],
                    "roles": ["Implementation Team", "Integration Specialists", "QA Team", "Security Team", "Training Team"],
                    "artifacts": ["Configured System Components", "Integration Solutions", "Migrated Data", "Test Reports and Evidence", "Compliance Certificates", "Trained Users", "Deployment Packages"],
                    "decision_gates": ["Gates 3a–3d – Approvals for development, testing, training, and deployment readiness"],
                    "standards_references": {
                        "PMBOK": "Multi-tier governance structure with steering committee",
                        "PRINCE2": "Complete model with formal decision points",
                        "ISO 21500": "Governance aligned with organizational structure"
                    }
                },
                {
                    "name": "Deployment & Transition",
                    "duration": "2-4 months",
                    "activities": [
                        "Deploy to production and support go-live",
                        "Monitor system performance and resolve issues",
                        "Validate benefits realization",
                        "Transfer knowledge and finalize project closure"
                    ],
                    "roles": ["Deployment Team", "Support Team", "Project Manager", "Benefits Manager"],
                    "artifacts": ["Live Production System", "Support Documentation", "Performance Reports", "Issue Resolution Reports", "Benefits Realization Report", "Project Closure Report", "Lessons Learned Document"],
                    "decision_gates": ["Gate 4 – Confirmation of project completion and benefits realization"],
                    "standards_references": {
                        "PMBOK": "Formal documentation and governance",
                        "PRINCE2": "Defined roles, focus on products, tailored control",
                        "ISO 21500": "Communication and stakeholder management focus"
                    }
                }
            ],
            "tailoring_rationale": "Predictive approach with adaptive elements and formal documentation. Multi-tier governance structure with steering committee and project board.",
            "governance_model": "Complete model with formal decision points, emphasizing business case, organization, quality, risk, and change."
        }
    
    elif project_type == "government_project":
        return {
            "phases": [
                {
                    "name": "Project Initiation",
                    "duration": "1 month",
                    "activities": [
                        "Assess current infrastructure and establish baselines",
                        "Identify stakeholders and confirm requirements",
                        "Create project charter and perform initial risk review",
                        "Mobilize team and plan procurement"
                    ],
                    "roles": ["Project Manager", "Infrastructure Lead", "Stakeholders", "Procurement Manager"],
                    "artifacts": ["Infrastructure Assessment Report", "Project Charter", "Stakeholder Register", "Requirements Specification", "Risk Register", "Procurement Strategy"],
                    "decision_gates": ["Gate 1 – Authorization and team confirmation"],
                    "standards_references": {
                        "PMBOK": "Planning, Project Work, Delivery, Measurement domains",
                        "PRINCE2": "Business Case, Planning, Quality, Risk, Change themes",
                        "ISO 21500": "Sequential process groups with defined phase boundaries"
                    }
                },
                {
                    "name": "Detailed Planning & Design",
                    "duration": "2 months",
                    "activities": [
                        "Create detailed work breakdown structure (WBS)",
                        "Prepare technical design and specifications",
                        "Plan resources, schedules, and quality assurance",
                        "Address safety, compliance, and vendor selection"
                    ],
                    "roles": ["Project Manager", "Technical Lead", "Safety Officer", "Quality Manager", "Vendor Manager"],
                    "artifacts": ["Work Breakdown Structure", "Master Schedule", "Technical Design Documents", "Resource Management Plan", "Quality Assurance Plan", "Safety Plan", "Vendor Contracts"],
                    "decision_gates": ["Gate 2 – Approval of design and procurement authorization"],
                    "standards_references": {
                        "PMBOK": "Predictive development approach, suited for fixed scope",
                        "PRINCE2": "Sequential, stage-based approach with clear deliverables",
                        "ISO 21500": "Scope, Schedule, Cost, Quality, Risk, Procurement knowledge areas"
                    }
                },
                {
                    "name": "Procurement & Preparation",
                    "duration": "2 months",
                    "activities": [
                        "Procure and deliver equipment",
                        "Prepare sites and testing environments",
                        "Plan installation and train teams",
                        "Prepare for change management"
                    ],
                    "roles": ["Procurement Manager", "Site Manager", "Training Coordinator", "Change Manager"],
                    "artifacts": ["Procured Equipment and Materials", "Prepared Installation Sites", "Test Environment", "Installation Procedures", "Trained Team Members", "Change Management Plan"],
                    "decision_gates": ["Gate 3 – Readiness confirmation for installation"],
                    "standards_references": {
                        "PMBOK": "Traditional management with stage gates",
                        "PRINCE2": "Focus on technical outputs and quality documentation",
                        "ISO 21500": "Technical oversight with operational alignment"
                    }
                },
                {
                    "name": "Implementation & Testing",
                    "duration": "5 months",
                    "activities": [
                        "Install infrastructure and configure systems",
                        "Conduct integration, performance, and security testing",
                        "Complete documentation and user acceptance testing"
                    ],
                    "roles": ["Installation Team", "Configuration Specialists", "Testing Team", "Security Team", "Documentation Team"],
                    "artifacts": ["Installed Infrastructure", "Configured Systems", "Test Results and Reports", "Performance Validation", "Security Certificates", "User Acceptance Sign-off", "Technical Documentation"],
                    "decision_gates": ["Gates 4a–4c – Completion of installation, testing approval, and go-live authorization"],
                    "standards_references": {
                        "PMBOK": "Structured execution with formal documentation",
                        "PRINCE2": "Progress monitoring and quality assurance",
                        "ISO 21500": "Governance with technical oversight"
                    }
                },
                {
                    "name": "Deployment & Closure",
                    "duration": "2 months",
                    "activities": [
                        "Execute production cutover and support setup",
                        "Monitor performance and resolve issues",
                        "Transfer knowledge and close the project"
                    ],
                    "roles": ["Deployment Team", "Support Team", "Project Manager", "Knowledge Transfer Specialist"],
                    "artifacts": ["Operational Infrastructure", "Support Procedures", "Performance Reports", "Optimization Recommendations", "Knowledge Transfer Documentation", "Project Closure Report", "Lessons Learned"],
                    "decision_gates": ["Gate 5 – Final handover and operational acceptance"],
                    "standards_references": {
                        "PMBOK": "Project closure with operational handover",
                        "PRINCE2": "Final project closure and benefits realization",
                        "ISO 21500": "Closing process group with operational alignment"
                    }
                }
            ],
            "tailoring_rationale": "Predictive approach suited for fixed scope and structured execution. Traditional management with stage gates and formal documentation.",
            "governance_model": "Technical oversight with operational alignment, sequential stage-based approach with clear deliverables."
        }
    
    return None


@require_GET
def process_diagram(request: HttpRequest) -> HttpResponse:
    """Generate process diagrams and workflow visualizations"""
    ensure_session(request)
    project_type = (request.GET.get("type") or "").strip()
    
    if not project_type:
        return JsonResponse({"error": "Project type required"}, status=400)
    
    # Get the process design
    scenarios = {
        "custom_software": {
            "name": "Custom Software Development Project",
            "context": "Well-defined requirements, <6 months, <7 team members",
            "focus": "Lightweight process optimized for speed and flexibility",
        },
        "innovative_product": {
            "name": "Innovative Product Development Project", 
            "context": "R&D-heavy, uncertain outcomes, ~1 year duration",
            "focus": "Hybrid adaptive process balancing innovation, iteration, and stakeholder management",
        },
        "government_project": {
            "name": "Large Government Project",
            "context": "Civil, electrical, and IT components, 2-year duration", 
            "focus": "Comprehensive process covering governance, compliance, procurement, risk management, and reporting",
        }
    }
    
    if project_type not in scenarios:
        return JsonResponse({"error": "Invalid project type"}, status=400)
    
    scenario = scenarios[project_type]
    process_design = generate_process_design(scenario, project_type)
    
    if not process_design:
        return JsonResponse({"error": "Process design not found"}, status=404)
    
    # Generate diagram data for Chart.js or similar visualization
    diagram_data = {
        "title": scenario["name"],
        "context": scenario["context"],
        "focus": scenario["focus"],
        "phases": [],
        "workflow": {
            "nodes": [],
            "edges": []
        }
    }
    
    # Convert phases to diagram format
    for i, phase in enumerate(process_design["phases"]):
        phase_data = {
            "id": f"phase_{i+1}",
            "name": phase["name"],
            "duration": phase["duration"],
            "activities": phase["activities"],
            "roles": phase["roles"],
            "artifacts": phase["artifacts"],
            "decision_gates": phase["decision_gates"],
            "position": {"x": i * 200, "y": 100},
            "color": get_phase_color(i)
        }
        diagram_data["phases"].append(phase_data)
        
        # Add workflow nodes
        diagram_data["workflow"]["nodes"].append({
            "id": f"phase_{i+1}",
            "label": phase["name"],
            "type": "phase",
            "data": phase_data
        })
        
        # Add edges between phases
        if i > 0:
            diagram_data["workflow"]["edges"].append({
                "from": f"phase_{i}",
                "to": f"phase_{i+1}",
                "type": "transition"
            })
    
    return JsonResponse(diagram_data)


def get_phase_color(index):
    """Get color for phase based on index"""
    colors = [
        "#3B82F6",  # Blue
        "#10B981",  # Green  
        "#8B5CF6",  # Purple
        "#F59E0B",  # Orange
        "#EF4444",  # Red
        "#06B6D4",  # Cyan
    ]
    return colors[index % len(colors)]


@require_GET
def process_document(request: HttpRequest) -> HttpResponse:
    """Generate comprehensive Process Design Document"""
    ensure_session(request)
    project_type = (request.GET.get("type") or "").strip()
    
    if not project_type:
        return JsonResponse({"error": "Project type required"}, status=400)
    
    scenarios = {
        "custom_software": {
            "name": "Custom Software Development Project",
            "context": "Well-defined requirements, <6 months, <7 team members",
            "focus": "Lightweight process optimized for speed and flexibility",
        },
        "innovative_product": {
            "name": "Innovative Product Development Project", 
            "context": "R&D-heavy, uncertain outcomes, ~1 year duration",
            "focus": "Hybrid adaptive process balancing innovation, iteration, and stakeholder management",
        },
        "government_project": {
            "name": "Large Government Project",
            "context": "Civil, electrical, and IT components, 2-year duration", 
            "focus": "Comprehensive process covering governance, compliance, procurement, risk management, and reporting",
        }
    }
    
    if project_type not in scenarios:
        return JsonResponse({"error": "Invalid project type"}, status=400)
    
    scenario = scenarios[project_type]
    process_design = generate_process_design(scenario, project_type)
    
    if not process_design:
        return JsonResponse({"error": "Process design not found"}, status=404)
    
    # Generate comprehensive document
    document = {
        "title": f"Process Design Document: {scenario['name']}",
        "metadata": {
            "project_type": project_type,
            "scenario_name": scenario["name"],
            "context": scenario["context"],
            "focus": scenario["focus"],
            "generated_date": "2025-01-27",
            "standards_referenced": ["PMBOK Guide 7th Edition", "PRINCE2 2023", "ISO 21500:2021", "ISO 21502:2020"]
        },
        "executive_summary": {
            "tailoring_rationale": process_design["tailoring_rationale"],
            "governance_model": process_design["governance_model"],
            "key_characteristics": get_key_characteristics(project_type)
        },
        "process_phases": process_design["phases"],
        "standards_mapping": generate_standards_mapping(process_design["phases"]),
        "tailoring_decisions": generate_tailoring_decisions(project_type),
        "implementation_guidance": generate_implementation_guidance(project_type)
    }
    
    return JsonResponse(document)


def get_key_characteristics(project_type):
    """Get key characteristics for the project type"""
    characteristics = {
        "custom_software": [
            "Agile methodology with short sprints",
            "Continuous integration and deployment",
            "Self-organizing teams",
            "Minimal documentation overhead",
            "Rapid feedback cycles"
        ],
        "innovative_product": [
            "Hybrid waterfall-agile approach",
            "Multiple validation gates",
            "Stakeholder-centric design",
            "Risk-driven decision making",
            "Flexible stage boundaries"
        ],
        "government_project": [
            "Formal governance structure",
            "Comprehensive compliance framework",
            "Multi-tier approval processes",
            "Detailed documentation requirements",
            "Audit trail maintenance"
        ]
    }
    return characteristics.get(project_type, [])


def generate_standards_mapping(phases):
    """Generate mapping of phases to standards"""
    mapping = {
        "PMBOK": [],
        "PRINCE2": [],
        "ISO 21500": [],
        "ISO 21502": []
    }
    
    for phase in phases:
        for standard, reference in phase["standards_references"].items():
            mapping[standard].append({
                "phase": phase["name"],
                "reference": reference,
                "activities": phase["activities"],
                "artifacts": phase["artifacts"]
            })
    
    return mapping


def generate_tailoring_decisions(project_type):
    """Generate tailoring decisions and rationale"""
    decisions = {
        "custom_software": [
            {
                "decision": "Adopt Scrum framework",
                "rationale": "Well-suited for small teams with defined requirements",
                "standards_basis": "PMBOK Agile practices, PRINCE2 stage boundaries adapted for sprints"
            },
            {
                "decision": "Minimize formal documentation",
                "rationale": "Focus on working software over comprehensive documentation",
                "standards_basis": "PMBOK principle of value delivery, ISO 21500 quality management"
            },
            {
                "decision": "Continuous integration/deployment",
                "rationale": "Enable rapid feedback and risk reduction",
                "standards_basis": "PMBOK quality management, PRINCE2 managing product delivery"
            }
        ],
        "innovative_product": [
            {
                "decision": "Hybrid waterfall-agile approach",
                "rationale": "Balance structured planning with iterative development",
                "standards_basis": "PMBOK adaptive approaches, PRINCE2 stage boundaries, ISO 21500 lifecycle management"
            },
            {
                "decision": "Multiple validation gates",
                "rationale": "Manage uncertainty through frequent validation",
                "standards_basis": "PMBOK risk management, PRINCE2 stage boundaries, ISO 21500 quality assurance"
            },
            {
                "decision": "Stakeholder-centric design",
                "rationale": "Ensure innovation aligns with market needs",
                "standards_basis": "PMBOK stakeholder management, PRINCE2 business case, ISO 21500 stakeholder analysis"
            }
        ],
        "government_project": [
            {
                "decision": "Formal governance structure",
                "rationale": "Ensure compliance and accountability",
                "standards_basis": "PMBOK governance, PRINCE2 project board, ISO 21500 governance framework"
            },
            {
                "decision": "Comprehensive compliance framework",
                "rationale": "Meet regulatory and audit requirements",
                "standards_basis": "PMBOK compliance management, ISO 21500 governance and compliance"
            },
            {
                "decision": "Multi-tier approval processes",
                "rationale": "Ensure proper oversight and risk management",
                "standards_basis": "PRINCE2 stage boundaries, PMBOK change management, ISO 21500 decision gates"
            }
        ]
    }
    return decisions.get(project_type, [])


def generate_implementation_guidance(project_type):
    """Generate implementation guidance"""
    guidance = {
        "custom_software": {
            "team_structure": "Cross-functional team of 5-7 members including developers, testers, and product owner",
            "tools_recommended": ["Jira/Confluence", "Git", "CI/CD pipeline", "Slack/Teams"],
            "success_metrics": ["Sprint velocity", "Code quality metrics", "Customer satisfaction", "Time to market"],
            "risks": ["Scope creep", "Technical debt", "Team burnout", "Integration issues"],
            "mitigation_strategies": ["Regular sprint reviews", "Code reviews", "Sustainable pace", "Continuous integration"]
        },
        "innovative_product": {
            "team_structure": "Multi-disciplinary team including researchers, designers, developers, and business analysts",
            "tools_recommended": ["Design thinking tools", "Prototyping software", "Project management platform", "Analytics tools"],
            "success_metrics": ["Innovation index", "Market validation", "User adoption", "Revenue potential"],
            "risks": ["Market uncertainty", "Technical feasibility", "Stakeholder alignment", "Resource constraints"],
            "mitigation_strategies": ["Market research", "Proof of concept", "Regular stakeholder reviews", "Agile resource allocation"]
        },
        "government_project": {
            "team_structure": "Large multi-disciplinary team with clear hierarchy and specialized roles",
            "tools_recommended": ["Enterprise PM software", "Document management system", "Compliance tracking", "Reporting tools"],
            "success_metrics": ["Compliance score", "Schedule adherence", "Budget control", "Quality metrics"],
            "risks": ["Regulatory changes", "Vendor issues", "Scope changes", "Resource availability"],
            "mitigation_strategies": ["Regular compliance reviews", "Vendor management", "Change control", "Resource planning"]
        }
    }
    return guidance.get(project_type, {})

# Create your views here.
