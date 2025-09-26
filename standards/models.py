from django.db import models
from django.utils.text import slugify


class Standard(models.Model):
    title = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    file_path = models.CharField(max_length=1024)
    source_type = models.CharField(max_length=16, choices=[
        ("pdf", "PDF"),
        ("epub", "EPUB"),
    ])

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.title)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.title


class Page(models.Model):
    standard = models.ForeignKey(Standard, on_delete=models.CASCADE, related_name="pages")
    page_index = models.PositiveIntegerField(help_text="Zero-based index")
    content = models.TextField()
    content_html = models.TextField(blank=True, null=True)
    section_hint = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        unique_together = ("standard", "page_index")
        ordering = ["standard_id", "page_index"]

    def __str__(self) -> str:
        return f"{self.standard.slug}#{self.page_index}"


class Bookmark(models.Model):
    session_key = models.CharField(max_length=64, db_index=True)
    page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name="bookmarks")
    label = models.CharField(max_length=120, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("session_key", "page")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.session_key}:{self.page}"

# Create your models here.
