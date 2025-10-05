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
    phases = [
        ("Initiation", ["charter", "case", "scope", "stakeholder", "sponsor", "mandate"]),
        ("Planning", ["plan", "schedule", "cost", "resource", "risk", "quality", "communications"]),
        ("Execution", ["deliverable", "work package", "team", "leadership", "manage work"]),
        ("Monitoring & Control", ["monitor", "control", "variance", "change", "issue", "metrics"]),
        ("Closing", ["close", "handover", "benefits", "transition", "retrospective"]),
    ]
    tailored = []
    if project_type:
        keywords = {
            "it": ["agile", "iteration", "change", "software", "sprint"],
            "construction": ["contract", "site", "safety", "procurement"],
            "research": ["experiment", "hypothesis", "review", "ethics"],
        }.get(project_type.lower(), [project_type])
        query = " OR ".join(keywords)
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
        # Build a phase-oriented tailoring suggestion list with evidence
        for phase_name, phase_terms in phases:
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
    return render(
        request,
        "standards/tailor.html",
        {"project_type": project_type, "recommendations": recommendations, "tailored": tailored},
    )

# Create your views here.
