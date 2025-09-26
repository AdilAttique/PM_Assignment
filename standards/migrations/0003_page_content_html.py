from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("standards", "0002_page_fts"),
    ]

    operations = [
        migrations.AddField(
            model_name="page",
            name="content_html",
            field=models.TextField(blank=True, null=True),
        ),
    ]



