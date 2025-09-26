from django.db import migrations


SQL_CREATE_FTS = r"""
CREATE VIRTUAL TABLE IF NOT EXISTS page_fts USING fts5(
  content,
  content='standards_page',
  content_rowid='id'
);
"""

SQL_POPULATE_FTS = r"""
INSERT INTO page_fts(rowid, content)
SELECT id, content FROM standards_page;
"""

SQL_TRIGGER_AI = r"""
CREATE TRIGGER IF NOT EXISTS page_ai AFTER INSERT ON standards_page BEGIN
  INSERT INTO page_fts(rowid, content) VALUES (new.id, new.content);
END;
"""

SQL_TRIGGER_AD = r"""
CREATE TRIGGER IF NOT EXISTS page_ad AFTER DELETE ON standards_page BEGIN
  INSERT INTO page_fts(page_fts, rowid, content) VALUES('delete', old.id, old.content);
END;
"""

SQL_TRIGGER_AU = r"""
CREATE TRIGGER IF NOT EXISTS page_au AFTER UPDATE ON standards_page BEGIN
  INSERT INTO page_fts(page_fts, rowid, content) VALUES('delete', old.id, old.content);
  INSERT INTO page_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


def forwards(apps, schema_editor):  # type: ignore[no-untyped-def]
    cursor = schema_editor.connection.cursor()
    cursor.execute("PRAGMA foreign_keys=OFF;")
    cursor.execute(SQL_CREATE_FTS)
    cursor.execute(SQL_POPULATE_FTS)
    cursor.execute(SQL_TRIGGER_AI)
    cursor.execute(SQL_TRIGGER_AD)
    cursor.execute(SQL_TRIGGER_AU)


def backwards(apps, schema_editor):  # type: ignore[no-untyped-def]
    cursor = schema_editor.connection.cursor()
    cursor.execute("DROP TABLE IF EXISTS page_fts;")
    cursor.execute("DROP TRIGGER IF EXISTS page_ai;")
    cursor.execute("DROP TRIGGER IF EXISTS page_ad;")
    cursor.execute("DROP TRIGGER IF EXISTS page_au;")


class Migration(migrations.Migration):
    dependencies = [
        ("standards", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]


