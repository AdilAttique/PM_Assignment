from django.contrib import admin
from .models import Standard, Page, Bookmark


@admin.register(Standard)
class StandardAdmin(admin.ModelAdmin):
    list_display = ("title", "slug", "source_type")
    search_fields = ("title", "slug")


@admin.register(Page)
class PageAdmin(admin.ModelAdmin):
    list_display = ("standard", "page_index", "short_content")
    list_filter = ("standard",)
    search_fields = ("content", "section_hint")

    def short_content(self, obj):  # type: ignore[no-untyped-def]
        return (obj.content or "")[:80]


@admin.register(Bookmark)
class BookmarkAdmin(admin.ModelAdmin):
    list_display = ("session_key", "page", "label", "created_at")
    list_filter = ("session_key", "page__standard")

# Register your models here.
