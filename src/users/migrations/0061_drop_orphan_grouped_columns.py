from django.db import migrations

# Columns left behind on some databases by a reverted "group media lists"
# experiment. They are NOT NULL without a default, so any INSERT into the user
# table (e.g. creating a user) fails while they exist. No model field maps to
# them, so they are dropped directly.
ORPHAN_COLUMNS = [
    f"{media_type}_grouped"
    for media_type in (
        "tv",
        "season",
        "movie",
        "anime",
        "manga",
        "game",
        "book",
        "comic",
        "boardgame",
    )
]


def drop_orphan_columns(apps, schema_editor):
    """Drop the orphan *_grouped columns, only where they actually exist."""
    User = apps.get_model("users", "User")
    table = User._meta.db_table
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        existing = {
            column.name
            for column in connection.introspection.get_table_description(
                cursor,
                table,
            )
        }
    quote = schema_editor.quote_name
    for column in ORPHAN_COLUMNS:
        if column in existing:
            schema_editor.execute(
                f"ALTER TABLE {quote(table)} DROP COLUMN {quote(column)}",
            )


class Migration(migrations.Migration):
    """Drop orphan columns that no model field maps to (no-op where absent)."""

    dependencies = [
        ("users", "0060_merge_upstream_sync"),
    ]

    operations = [
        migrations.RunPython(drop_orphan_columns, migrations.RunPython.noop),
    ]
