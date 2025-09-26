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
from django.conf import settings
import sqlite3


def ensure_session(request: HttpRequest) -> None:
    if not request.session.session_key:
        request.session.save()


@require_GET
def library(request: HttpRequest) -> HttpResponse:
    ensure_session(request)
    standards = Standard.objects.all().order_by("title")
    return render(request, "standards/library.html", {"standards": standards})


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

    # Derive similarities/differences based on fuzzy matching of snippets
    similarity = []
    unique = {s.slug: [] for s in standards}
    if topic:
        # Compare first 20 snippets per standard
        standardized = {k: v[:20] for k, v in hits.items()}
        # Build pairwise similarities
        for a in standards:
            for b in standards:
                if a.slug >= b.slug:
                    continue
                for sa in standardized[a.slug]:
                    for sb in standardized[b.slug]:
                        score = fuzz.token_set_ratio(sa["snippet"], sb["snippet"])  # type: ignore[arg-type]
                        if score >= 80:
                            similarity.append({
                                "a": a,
                                "b": b,
                                "a_idx": sa["page_index"],
                                "b_idx": sb["page_index"],
                                "score": score,
                            })
        # Unique entries: snippets that don't reach threshold vs others
        for s in standards:
            others = [o for o in standards if o.slug != s.slug]
            for sa in standardized[s.slug]:
                max_score = 0
                for o in others:
                    for sb in standardized[o.slug]:
                        max_score = max(max_score, fuzz.token_set_ratio(sa["snippet"], sb["snippet"]))
                if max_score < 50:
                    unique[s.slug].append(sa)

    # Build template-friendly lists to avoid dict indexing in templates
    hits_list = [{"standard": s, "items": hits.get(s.slug, [])} for s in standards]
    unique_list = [{"standard": s, "items": unique.get(s.slug, [])} for s in standards]

    return render(
        request,
        "standards/compare.html",
        {
            "topic": topic,
            "standards": standards,
            "hits_list": hits_list,
            "similarity": similarity,
            "unique_list": unique_list,
        },
    )


@require_GET
def insights(request: HttpRequest) -> HttpResponse:
    ensure_session(request)
    total_pages = Page.objects.count()
    counts_by_standard = (
        Page.objects.values("standard__title").order_by("standard__title").annotate(count=models.Count("id"))
    )
    # Simple overlap estimate: pages containing common keywords of lifecycle
    lifecycle_terms = ["initiation", "planning", "execution", "monitoring", "closing", "governance", "risk", "stakeholder"]
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

    return render(
        request,
        "standards/insights.html",
        {
            "total_pages": total_pages,
            "counts_by_standard": list(counts_by_standard),
            "overlaps": overlaps,
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
