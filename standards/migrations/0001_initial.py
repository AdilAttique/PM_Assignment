from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Standard",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(max_length=255)),
                ("slug", models.SlugField(max_length=255, unique=True)),
                ("file_path", models.CharField(max_length=1024)),
                ("source_type", models.CharField(choices=[("pdf", "PDF"), ("epub", "EPUB")], max_length=16)),
            ],
        ),
        migrations.CreateModel(
            name="Page",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("page_index", models.PositiveIntegerField(help_text="Zero-based index")),
                ("content", models.TextField()),
                ("section_hint", models.CharField(blank=True, default="", max_length=255)),
                ("standard", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="pages", to="standards.standard")),
            ],
            options={
                "ordering": ["standard_id", "page_index"],
                "unique_together": {("standard", "page_index")},
            },
        ),
        migrations.CreateModel(
            name="Bookmark",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("session_key", models.CharField(db_index=True, max_length=64)),
                ("label", models.CharField(blank=True, default="", max_length=120)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("page", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="bookmarks", to="standards.page")),
            ],
            options={
                "ordering": ["-created_at"],
                "unique_together": {("session_key", "page")},
            },
        ),
    ]


